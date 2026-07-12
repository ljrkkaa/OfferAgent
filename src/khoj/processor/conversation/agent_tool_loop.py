import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from khoj.processor.conversation.knowledge_workspace import (
    _recent_artifact_catalog,
    available_workspace_tools,
    execute_workspace_tool_calls,
    workspace_planner_context,
)
from khoj.processor.conversation.tool_protocol import parse_tool_plan, validate_tool_arguments
from khoj.processor.conversation.utils import ToolCall
from khoj.processor.tools.online_search import read_webpages, read_webpages_content, search_online
from khoj.routers.helpers import ChatEvent
from khoj.utils.helpers import AgentToolName, ToolDefinition, agent_tool_definitions

logger = logging.getLogger(__name__)
WRITE_TOOL_NAMES = {"append_note", "propose_edit"}
SUCCESSFUL_WRITE_STATUSES = {"action_prepared", "written"}
WRITE_COMPLETION_RESERVE = 3

AGENT_TOOL_SYSTEM_PROMPT = """
You are the tool planner for the main OfferAgent chat answer.
Return only a json object: {"requires_write_action":false,"calls":[{"name":"...", "args":{...}, "id":"1"}]}.
Use tools when the user asks for current web information, personal knowledge base evidence, or writeback.
Do not decide intent with keywords. Choose tools from task meaning, conversation context, and available tools.
Set requires_write_action=true only when the user explicitly asks to persist, create, append, or modify a file in the vault.
When runtime facts say persistent_write_required=true, a write tool only prepares a VaultAction for later user review.
The user's explicit write request authorizes preparing that action; do not ask for a second confirmation first.
For a missing .md/.txt target under an existing folder, append_note prepares a create-only action.
In that mode, an empty calls list is invalid until append_note or propose_edit returns status action_prepared or written.
Rejected statuses such as source_mismatch are not completion; use their reason to correct and retry the write.
When enough evidence is collected, return {"requires_write_action":false,"calls":[]}.
""".strip()

WEB_SEARCH_TOOL = ToolDefinition(
    name="web_search",
    description=agent_tool_definitions[AgentToolName.SearchWeb].description,
    schema=agent_tool_definitions[AgentToolName.SearchWeb].schema,
)


@dataclass
class AgentToolLoopResult:
    references: list[dict[str, Any]] = field(default_factory=list)
    inferred_queries: list[str] = field(default_factory=list)
    online_results: dict[str, Any] = field(default_factory=dict)
    program_context: list[str] = field(default_factory=list)
    searched: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    tool_transcript: list[dict[str, Any]] = field(default_factory=list)
    artifacts: dict[str, dict[str, Any]] = field(default_factory=dict)
    used_workspace_tools: bool = False
    planner_failed: bool = False


@dataclass
class ToolBatch:
    is_concurrency_safe: bool
    calls: list[ToolCall]


@dataclass(frozen=True)
class ExecutableTool:
    definition: ToolDefinition
    handler: Callable[..., Awaitable[None]]
    concurrency_safe: bool = False


def build_agent_tool_registry(
    *, allow_local_kb: bool, allow_openkb: bool, allow_web: bool, write_mode: str = "disabled"
) -> dict[str, ExecutableTool]:
    registry: dict[str, ExecutableTool] = {}
    if allow_web:
        registry[WEB_SEARCH_TOOL.name] = ExecutableTool(
            WEB_SEARCH_TOOL,
            run_web_search_tool,
            concurrency_safe=True,
        )
        read_webpage = agent_tool_definitions[AgentToolName.ReadWebpage]
        registry[read_webpage.name] = ExecutableTool(
            read_webpage,
            run_read_webpage_tool,
            concurrency_safe=True,
        )
    for tool in available_workspace_tools(allow_local_kb=allow_local_kb, allow_openkb=allow_openkb):
        if write_mode != "client_actions" and tool.name in {"append_note", "propose_edit"}:
            continue
        registry[tool.name] = ExecutableTool(
            tool,
            run_notes_tool_call,
            concurrency_safe=tool.name not in {"append_note", "propose_edit"},
        )
    return registry


async def run_web_search_tool(
    call: ToolCall,
    *,
    result: AgentToolLoopResult,
    user: Any,
    conversation_history: list,
    location: Any = None,
    query_images: list[str] | None = None,
    query_files: str | None = None,
    relevant_memories: list | None = None,
    agent: Any = None,
    tracer: dict | None = None,
    **_: Any,
) -> None:
    args = call.args
    query = str(args.get("query") or "").strip()
    if not query:
        _record_tool_error(result, "web_search", args, "web_search requires query")
        return

    response_dict: dict[str, Any] = {}
    async for response in search_online(
        query=query,
        conversation_history=conversation_history,
        location=location,
        user=user,
        custom_filters=[],
        max_online_searches=3,
        query_images=query_images,
        query_files=query_files,
        relevant_memories=relevant_memories,
        agent=agent,
        tracer=tracer or {},
    ):
        if isinstance(response, dict) and ChatEvent.STATUS in response:
            continue
        if response:
            response_dict.update(response)

    if response_dict:
        result.online_results.update(response_dict)
    result.inferred_queries.append(query)
    result.searched.append(f"web_search: {query}")
    _record_tool_result(result, "web_search", args, response_dict)


async def run_read_webpage_tool(
    call: ToolCall,
    *,
    result: AgentToolLoopResult,
    user: Any,
    conversation_history: list,
    location: Any = None,
    query_images: list[str] | None = None,
    query_files: str | None = None,
    relevant_memories: list | None = None,
    agent: Any = None,
    tracer: dict | None = None,
    **_: Any,
) -> None:
    args = call.args
    query = str(args.get("query") or "").strip()
    urls = args.get("urls") or []
    if not query:
        _record_tool_error(result, "read_webpage", args, "read_webpage requires query")
        return

    response_dict: dict[str, Any] = {}
    if isinstance(urls, list) and urls:
        iterator = read_webpages_content(
            query,
            [str(url) for url in urls if str(url).strip()],
            user,
            agent=agent,
            relevant_memories=relevant_memories,
            tracer=tracer or {},
        )
    else:
        iterator = read_webpages(
            query,
            conversation_history,
            location,
            user,
            query_images=query_images,
            query_files=query_files,
            agent=agent,
            relevant_memories=relevant_memories,
            tracer=tracer or {},
        )
    async for response in iterator:
        if isinstance(response, dict) and ChatEvent.STATUS in response:
            continue
        if response:
            response_dict.update(response)

    if response_dict:
        result.online_results.update(response_dict)
    result.inferred_queries.append(query)
    result.searched.append(f"read_webpage: {query}")
    _record_tool_result(result, "read_webpage", args, response_dict)


async def run_notes_tool_call(
    call: ToolCall,
    *,
    result: AgentToolLoopResult,
    query: str,
    chat_history: list,
    user: Any,
    agent: Any,
    send_message: Callable[..., Awaitable[Any]],
    client_app: Any = None,
    allow_local_kb: bool = True,
    allow_openkb: bool = False,
    conversation_id: str = "agent-tool-loop",
    write_mode: str = "disabled",
    **_: Any,
) -> None:
    result.used_workspace_tools = True
    notes_result = await execute_workspace_tool_calls(
        query,
        chat_history,
        user,
        agent,
        [call],
        send_message=send_message,
        client_app=client_app,
        allow_local_kb=allow_local_kb,
        allow_openkb=allow_openkb,
        conversation_id=conversation_id,
        initial_tool_transcript=result.tool_transcript,
        write_mode=write_mode,
    )
    result.references.extend(notes_result.references)
    result.inferred_queries.extend(notes_result.inferred_queries)
    result.searched.extend(notes_result.searched)
    result.errors.extend(notes_result.errors)
    result.tool_transcript.extend(notes_result.tool_transcript)
    for reference in notes_result.references:
        add_write_reference_context(result, reference)


def add_write_reference_context(result: AgentToolLoopResult, reference: dict[str, Any]) -> None:
    if reference.get("action") not in {"append_note", "propose_edit"}:
        return
    payload = {
        "action": reference.get("action"),
        "status": reference.get("status"),
        "file": reference.get("file"),
        "changed": reference.get("changed"),
        "result": reference.get("compiled", ""),
    }
    instruction = "Final answer must report this exact write tool result."
    if reference.get("status") == "written":
        instruction += " Do not say writing is unavailable."
    elif reference.get("status") == "action_prepared":
        instruction = "Final answer must say a local vault action is waiting for the client to apply."
    context = f"Notes write tool result: {json.dumps(payload, ensure_ascii=False, default=str)}. {instruction}"
    if context not in result.program_context:
        result.program_context.append(context)


def _has_write_tool_result(result: AgentToolLoopResult) -> bool:
    return any(
        reference.get("action") in WRITE_TOOL_NAMES and reference.get("status") in SUCCESSFUL_WRITE_STATUSES
        for reference in result.references
    )


async def collect_agent_context_and_actions(
    query: str,
    chat_history: list,
    *,
    user: Any,
    agent: Any,
    send_message: Callable[..., Awaitable[Any]],
    send_status: Optional[Callable[[str], Any]] = None,
    client_app: Any = None,
    allow_local_kb: bool,
    allow_openkb: bool,
    allow_web: bool,
    conversation_id: str = "agent-tool-loop",
    max_iterations: int = 8,
    write_mode: str = "disabled",
    vault_policy: Optional[dict[str, Any]] = None,
    location: Any = None,
    query_images: list[str] | None = None,
    query_files: str | None = None,
    relevant_memories: list | None = None,
    tracer: dict | None = None,
) -> AgentToolLoopResult:
    result = AgentToolLoopResult()
    write_action_required = False
    registry = build_agent_tool_registry(
        allow_local_kb=allow_local_kb,
        allow_openkb=allow_openkb,
        allow_web=allow_web,
        write_mode=write_mode,
    )
    await _send_status(send_status, "Planning with unified agent runtime")
    for iteration in range(max(1, max_iterations)):
        iterations_remaining = max(1, max_iterations) - iteration
        write_completion_phase = (
            write_action_required
            and not _has_write_tool_result(result)
            and iterations_remaining <= WRITE_COMPLETION_RESERVE
        )
        active_registry = (
            {name: tool for name, tool in registry.items() if name in WRITE_TOOL_NAMES}
            if write_completion_phase
            else registry
        )
        planner_query = _build_planner_query(
            query,
            chat_history,
            active_registry,
            result.tool_transcript,
            runtime_facts=_runtime_facts(
                allow_local_kb=allow_local_kb,
                allow_openkb=allow_openkb,
                client_app=client_app,
                write_mode=write_mode,
                require_write_action=write_action_required,
                successful_write_result_present=_has_write_tool_result(result),
                tool_iterations_remaining=iterations_remaining,
                write_completion_phase=write_completion_phase,
            ),
            workspace_instructions=workspace_planner_context(
                allow_local_kb=allow_local_kb,
                vault_policy=vault_policy,
            ),
        )
        try:
            response = await _send_planner_message(send_message, planner_query, chat_history)
        except Exception as error:
            _record_tool_error(result, "planner", {}, f"Planner unavailable after retry: {error}")
            result.planner_failed = True
            result.program_context.append(
                "The agent planner failed before confirming any file change. No file change is pending or applied."
            )
            break
        if response and getattr(response, "thought", None):
            await _send_status(send_status, response.thought)
        try:
            plan_requires_write_action, calls = parse_tool_plan(getattr(response, "text", response) or "")
            write_action_required = write_action_required or plan_requires_write_action
        except ValueError as error:
            _record_tool_error(result, "planner", {}, str(error))
            continue
        if not calls:
            if write_action_required and not _has_write_tool_result(result):
                if not any(
                    item.get("tool") == "system"
                    and "explicitly requested a persistent file change" in str(item.get("result") or "")
                    for item in result.tool_transcript
                ):
                    result.tool_transcript.append(
                        {
                            "tool": "system",
                            "args": {},
                            "result": (
                                "The user explicitly requested a persistent file change, but no write tool result "
                                "exists yet. Continue planning and call append_note or propose_edit before stopping."
                            ),
                        }
                    )
                continue
            break
        for batch in _partition_tool_calls(calls, active_registry):
            await _run_tool_batch(
                batch,
                result=result,
                registry=active_registry,
                query=query,
                chat_history=chat_history,
                user=user,
                agent=agent,
                send_message=send_message,
                send_status=send_status,
                client_app=client_app,
                allow_local_kb=allow_local_kb,
                allow_openkb=allow_openkb,
                conversation_id=conversation_id,
                write_mode=write_mode,
                location=location,
                query_images=query_images,
                query_files=query_files,
                relevant_memories=relevant_memories,
                tracer=tracer,
            )

    if write_action_required and not _has_write_tool_result(result):
        error = "Planner exhausted its tool budget without preparing the explicitly requested file change."
        result.errors.append(error)
        result.program_context.append(
            f"{error} Final answer must say no file change is pending or applied and must not suggest it was written."
        )
    result.inferred_queries = list(dict.fromkeys(item for item in result.inferred_queries if item))
    return result


def _partition_tool_calls(calls: list[ToolCall], registry: dict[str, ExecutableTool]) -> list[ToolBatch]:
    batches: list[ToolBatch] = []
    for call in calls:
        is_safe = bool(registry.get(call.name) and registry[call.name].concurrency_safe)
        if is_safe and batches and batches[-1].is_concurrency_safe:
            batches[-1].calls.append(call)
        else:
            batches.append(ToolBatch(is_concurrency_safe=is_safe, calls=[call]))
    return batches


async def _run_tool_batch(
    batch: ToolBatch,
    *,
    result: AgentToolLoopResult,
    registry: dict[str, ExecutableTool],
    query: str,
    chat_history: list,
    user: Any,
    agent: Any,
    send_message: Callable[..., Awaitable[Any]],
    send_status: Optional[Callable[[str], Any]],
    client_app: Any,
    allow_local_kb: bool,
    allow_openkb: bool,
    conversation_id: str,
    write_mode: str = "disabled",
    location: Any = None,
    query_images: list[str] | None = None,
    query_files: str | None = None,
    relevant_memories: list | None = None,
    tracer: dict | None = None,
) -> None:
    if batch.is_concurrency_safe and len(batch.calls) > 1:
        await _send_status(send_status, f"Using agent tools: {', '.join(call.name for call in batch.calls)}")
        semaphore = asyncio.Semaphore(_max_tool_concurrency())

        async def run_one(call: ToolCall) -> AgentToolLoopResult:
            local_result = AgentToolLoopResult()
            async with semaphore:
                await _run_tool_call(
                    call,
                    result=local_result,
                    registry=registry,
                    query=query,
                    chat_history=chat_history,
                    user=user,
                    agent=agent,
                    send_message=send_message,
                    send_status=None,
                    client_app=client_app,
                    allow_local_kb=allow_local_kb,
                    allow_openkb=allow_openkb,
                    conversation_id=conversation_id,
                    write_mode=write_mode,
                    location=location,
                    query_images=query_images,
                    query_files=query_files,
                    relevant_memories=relevant_memories,
                    tracer=tracer,
                )
            return local_result

        local_results = await asyncio.gather(*(run_one(call) for call in batch.calls))
        for local_result in local_results:
            _merge_tool_result(result, local_result)
        return

    for call in batch.calls:
        await _run_tool_call(
            call,
            result=result,
            registry=registry,
            query=query,
            chat_history=chat_history,
            user=user,
            agent=agent,
            send_message=send_message,
            send_status=send_status,
            client_app=client_app,
            allow_local_kb=allow_local_kb,
            allow_openkb=allow_openkb,
            conversation_id=conversation_id,
            write_mode=write_mode,
            location=location,
            query_images=query_images,
            query_files=query_files,
            relevant_memories=relevant_memories,
            tracer=tracer,
        )


async def _run_tool_call(
    call: ToolCall,
    *,
    result: AgentToolLoopResult,
    registry: dict[str, ExecutableTool],
    query: str,
    chat_history: list,
    user: Any,
    agent: Any,
    send_message: Callable[..., Awaitable[Any]],
    send_status: Optional[Callable[[str], Any]],
    client_app: Any,
    allow_local_kb: bool,
    allow_openkb: bool,
    conversation_id: str,
    write_mode: str = "disabled",
    location: Any = None,
    query_images: list[str] | None = None,
    query_files: str | None = None,
    relevant_memories: list | None = None,
    tracer: dict | None = None,
) -> None:
    tool = registry.get(call.name)
    if tool is None:
        _record_tool_error(result, call.name, call.args, f"Agent runtime tool is not available: {call.name}")
        return
    try:
        validate_tool_arguments(tool.definition, call.args)
    except ValueError as error:
        _record_tool_error(result, call.name, call.args, str(error))
        return
    await _send_status(send_status, f"Using agent tool: {call.name}")
    reference_start = len(result.references)
    try:
        await tool.handler(
            call,
            result=result,
            query=query,
            chat_history=chat_history,
            user=user,
            conversation_history=chat_history,
            agent=agent,
            send_message=send_message,
            client_app=client_app,
            allow_local_kb=allow_local_kb,
            allow_openkb=allow_openkb,
            conversation_id=conversation_id,
            write_mode=write_mode,
            location=location,
            query_images=query_images,
            query_files=query_files,
            relevant_memories=relevant_memories,
            tracer=tracer,
        )
    except Exception as exc:
        logger.warning("Agent runtime tool failed: %s", call.name, exc_info=True)
        _record_tool_error(result, call.name, call.args, str(exc))
    for reference in result.references[reference_start:]:
        add_write_reference_context(result, reference)


def _merge_tool_result(parent: AgentToolLoopResult, child: AgentToolLoopResult) -> None:
    artifact_ids = _merge_artifacts(parent, child)
    parent.references.extend(_remap_artifact_ids(ref, artifact_ids) for ref in child.references)
    parent.inferred_queries.extend(child.inferred_queries)
    parent.online_results.update(child.online_results)
    for context in child.program_context:
        if context not in parent.program_context:
            parent.program_context.append(context)
    parent.searched.extend(child.searched)
    parent.errors.extend(child.errors)
    parent.tool_transcript.extend(_remap_artifact_ids(item, artifact_ids) for item in child.tool_transcript)
    parent.used_workspace_tools = parent.used_workspace_tools or child.used_workspace_tools
    parent.planner_failed = parent.planner_failed or child.planner_failed


def _merge_artifacts(parent: AgentToolLoopResult, child: AgentToolLoopResult) -> dict[str, str]:
    artifact_ids: dict[str, str] = {}
    for old_id, artifact in child.artifacts.items():
        new_id = f"tool-result:{len(parent.artifacts) + 1}"
        artifact_ids[old_id] = new_id
        new_artifact = dict(artifact)
        new_artifact["id"] = new_id
        parent.artifacts[new_id] = new_artifact
    return artifact_ids


def _remap_artifact_ids(value: Any, artifact_ids: dict[str, str]) -> Any:
    if not artifact_ids:
        return value
    if isinstance(value, dict):
        remapped = {key: _remap_artifact_ids(item, artifact_ids) for key, item in value.items()}
        artifact_id = remapped.get("artifact_id")
        if isinstance(artifact_id, str) and artifact_id in artifact_ids:
            remapped["artifact_id"] = artifact_ids[artifact_id]
        return remapped
    if isinstance(value, list):
        return [_remap_artifact_ids(item, artifact_ids) for item in value]
    return value


def _max_tool_concurrency() -> int:
    try:
        value = int(os.getenv("KHOJ_AGENT_TOOL_CONCURRENCY", "4"))
    except ValueError:
        value = 4
    return max(1, min(value, 8))


def _compact_transcript_value(value: Any, limit: int) -> Any:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    if len(text) <= limit:
        return value
    preview_chars = max(1, (limit - 100) // 2)
    return {
        "truncated": True,
        "chars": len(text),
        "preview": f"{text[:preview_chars]}\n...\n{text[-preview_chars:]}",
    }


def _compact_transcript_item(item: dict[str, Any]) -> dict[str, Any]:
    tool = str(item.get("tool") or "")
    compact = {
        "tool": tool,
        "args": _compact_transcript_value(item.get("args") or {}, 1200),
    }
    if "result" in item:
        result_limit = 5000 if tool == "read_skill" else 2200
        compact["result"] = _compact_transcript_value(item.get("result"), result_limit)
    if "error" in item:
        compact["error"] = _compact_transcript_value(item.get("error"), 1200)
    return compact


def _planner_transcript_json(tool_transcript: list[dict[str, Any]], limit: int = 16000) -> str:
    compact_items = [_compact_transcript_item(item) for item in tool_transcript]
    system_indices = [index for index, item in enumerate(compact_items) if item.get("tool") == "system"]
    write_indices = [index for index, item in enumerate(compact_items) if item.get("tool") in WRITE_TOOL_NAMES]
    skill_indices = [index for index, item in enumerate(compact_items) if item.get("tool") == "read_skill"]
    candidate_indices = [
        *reversed(write_indices[-2:]),
        *reversed(system_indices[-4:]),
        *(skill_indices[-1:] if skill_indices else []),
        *reversed(range(len(compact_items))),
    ]
    selected: set[int] = set()
    encoded = "[]"
    for index in candidate_indices:
        if index in selected:
            continue
        candidate = sorted({*selected, index})
        candidate_encoded = json.dumps(
            [compact_items[item_index] for item_index in candidate],
            ensure_ascii=False,
            default=str,
        )
        if len(candidate_encoded) <= limit:
            selected.add(index)
            encoded = candidate_encoded
    return encoded


def _build_planner_query(
    query: str,
    chat_history: list,
    registry: dict[str, ExecutableTool],
    tool_transcript: list[dict[str, Any]],
    runtime_facts: dict[str, Any] | None = None,
    workspace_instructions: str = "",
) -> str:
    facts = runtime_facts or {}
    completion_requirements = (
        (
            "The evidence phase is closed and only write tools are available. Prepare the requested VaultAction now; "
            "do not request more reads or ask for another confirmation. A rejected write must be corrected and "
            "retried; only action_prepared or written completes the request. The client review UI is the confirmation."
            if facts.get("write_completion_phase")
            else "A persistent write is required. Do not return an empty calls array and do not ask for another "
            "confirmation until append_note or propose_edit returns action_prepared or written. Rejected statuses "
            "must be corrected and retried. The client review UI is the confirmation."
        )
        if facts.get("persistent_write_required") and not facts.get("successful_write_result_present")
        else "No additional protocol requirement."
    )
    return (
        f"User question:\n{query}\n\n"
        f"Recent conversation artifacts:\n{json.dumps(_recent_artifact_catalog(chat_history), ensure_ascii=False, default=str)[:6000]}\n\n"
        f"Runtime facts:\n{json.dumps(facts, ensure_ascii=False, default=str)}\n\n"
        f"Workspace instructions:\n{workspace_instructions[:12000]}\n\n"
        "Return a json object with a calls array. Use an empty calls array when no more tools are needed.\n\n"
        f"Available tools:\n{json.dumps(_tool_specs([tool.definition for tool in registry.values()]), ensure_ascii=False, default=str)[:10000]}\n\n"
        f"Tool results so far:\n{_planner_transcript_json(tool_transcript)}\n\n"
        f"Completion requirements:\n{completion_requirements}"
    )


async def _send_planner_message(send_message: Callable[..., Awaitable[Any]], query: str, chat_history: list) -> Any:
    kwargs = {
        "query": query,
        "system_message": AGENT_TOOL_SYSTEM_PROMPT,
        "chat_history": chat_history,
        "tools": [],
        "response_type": "json_object",
        "deepthought": True,
        "fast_model": False,
    }
    try:
        return await send_message(**kwargs)
    except Exception:
        logger.warning("Unified agent planner failed once; retrying", exc_info=True)
        return await send_message(**kwargs)


def _runtime_facts(
    *,
    allow_local_kb: bool,
    allow_openkb: bool,
    client_app: Any = None,
    write_mode: str = "disabled",
    require_write_action: bool = False,
    successful_write_result_present: bool = False,
    tool_iterations_remaining: int | None = None,
    write_completion_phase: bool = False,
) -> dict[str, Any]:
    return {
        "local_kb_available": bool(allow_local_kb),
        "openkb_available": bool(allow_openkb),
        "vault_actions_enabled": write_mode == "client_actions",
        "persistent_write_required": require_write_action,
        "successful_write_result_present": successful_write_result_present,
        "tool_iterations_remaining": tool_iterations_remaining,
        "write_completion_phase": write_completion_phase,
        "client_app": _client_app_name(client_app),
        "write_boundary": (
            "writes require an explicit client VaultAction review and apply"
            if write_mode == "client_actions"
            else "writes are unavailable for this client"
        ),
    }


def _client_app_name(client_app: Any) -> str:
    if client_app is None:
        return ""
    return str(getattr(client_app, "value", None) or getattr(client_app, "name", None) or client_app).lower()


def _tool_specs(tools: list[ToolDefinition]) -> list[dict[str, Any]]:
    return [{"name": tool.name, "description": tool.description, "schema": tool.schema} for tool in tools]


def _tool_result_text(value: Any, limit: int = 8000) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return text[:limit]


def _record_tool_result(
    result: AgentToolLoopResult, tool: str, args: dict[str, Any], value: Any, limit: int = 8000
) -> None:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    if len(text) <= limit:
        result.tool_transcript.append({"tool": tool, "args": args, "result": text})
        return
    artifact_id = f"tool-result:{len(result.artifacts) + 1}"
    result.artifacts[artifact_id] = {
        "id": artifact_id,
        "tool": tool,
        "args": args,
        "content": text,
    }
    preview_chars = min(1000, max(1, (limit - 100) // 2))
    result.tool_transcript.append(
        {
            "tool": tool,
            "args": args,
            "result": {
                "artifact_id": artifact_id,
                "tool": tool,
                "chars": len(text),
                "preview": f"{text[:preview_chars]}\n...\n{text[-preview_chars:]}",
                "truncated": True,
            },
        }
    )


def _record_tool_error(result: AgentToolLoopResult, tool: str, args: dict[str, Any], message: str) -> None:
    result.errors.append(message)
    result.tool_transcript.append({"tool": tool, "args": args or {}, "error": message})


async def _send_status(send_status: Optional[Callable], message: str) -> None:
    if not send_status:
        return
    status_result = send_status(message)
    if hasattr(status_result, "__aiter__"):
        async for _ in status_result:
            pass
    elif hasattr(status_result, "__await__"):
        await status_result
