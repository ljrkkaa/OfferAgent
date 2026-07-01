import json
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from khoj.processor.conversation.notes_tool_loop import (
    APPEND_NOTE_TOOL,
    OPENKB_TOOL,
    PROPOSE_EDIT_TOOL,
    READ_SKILL_TOOL,
    _recent_artifact_catalog,
    collect_notes_evidence_with_tools,
)
from khoj.processor.conversation.utils import ResponseWithThought, ToolCall, load_complex_json
from khoj.processor.tools.online_search import read_webpages, read_webpages_content, search_online
from khoj.routers.helpers import ChatEvent
from khoj.utils.helpers import ConversationCommand, ToolDefinition, tools_for_research_llm

logger = logging.getLogger(__name__)

AGENT_TOOL_SYSTEM_PROMPT = """
You are the tool planner for the main Khoj chat answer.
Return only a json object: {"calls":[{"name":"...", "args":{...}, "id":"1"}]}.
Use tools when the user asks for current web information, personal knowledge base evidence, or writeback.
Do not decide intent with keywords. Choose tools from task meaning, conversation context, and available tools.
When enough evidence is collected, return {"calls":[]}.
""".strip()

WEB_SEARCH_TOOL = ToolDefinition(
    name="web_search",
    description=tools_for_research_llm[ConversationCommand.SearchWeb].description,
    schema=tools_for_research_llm[ConversationCommand.SearchWeb].schema,
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


def parse_agent_tool_calls(raw: str) -> list[ToolCall]:
    try:
        payload = load_complex_json(raw)
    except Exception:
        return []
    if isinstance(payload, dict):
        for key in ("calls", "tool_calls", "tools"):
            if isinstance(payload.get(key), list):
                payload = payload[key]
                break
        else:
            payload = [payload]
    if not isinstance(payload, list):
        return []

    calls: list[ToolCall] = []
    for index, item in enumerate(payload):
        if not isinstance(item, dict):
            continue
        name = item.get("name") or item.get("tool")
        if not name:
            continue
        args = item.get("args") or item.get("arguments") or {}
        if isinstance(args, str):
            try:
                args = load_complex_json(args)
            except Exception:
                args = {}
        if not isinstance(args, dict):
            args = {}
        calls.append(ToolCall(name=_normalize_tool_name(str(name)), args=args, id=item.get("id") or str(index + 1)))
    return calls


def build_agent_tool_registry(*, allow_local_kb: bool, allow_openkb: bool, allow_web: bool) -> dict[str, ToolDefinition]:
    registry: dict[str, ToolDefinition] = {}
    if allow_web:
        registry[WEB_SEARCH_TOOL.name] = WEB_SEARCH_TOOL
        registry[tools_for_research_llm[ConversationCommand.ReadWebpage].name] = tools_for_research_llm[
            ConversationCommand.ReadWebpage
        ]
    if allow_local_kb:
        for command in (
            ConversationCommand.ViewFile,
            ConversationCommand.ListFiles,
            ConversationCommand.KbHeadings,
            ConversationCommand.KbResolveLink,
            ConversationCommand.RegexSearchFiles,
        ):
            tool = tools_for_research_llm[command]
            registry[tool.name] = tool
        registry[APPEND_NOTE_TOOL.name] = APPEND_NOTE_TOOL
        registry[PROPOSE_EDIT_TOOL.name] = PROPOSE_EDIT_TOOL
        registry[READ_SKILL_TOOL.name] = READ_SKILL_TOOL
    if allow_openkb:
        registry[OPENKB_TOOL.name] = OPENKB_TOOL
    return registry


async def run_web_search_tool(
    args: dict[str, Any],
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
    result.tool_transcript.append({"tool": "web_search", "args": args, "result": _tool_result_text(response_dict)})


async def run_read_webpage_tool(
    args: dict[str, Any],
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
    result.tool_transcript.append({"tool": "read_webpage", "args": args, "result": _tool_result_text(response_dict)})


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
    **_: Any,
) -> None:
    used = False

    async def single_tool_send_message(**kwargs):
        nonlocal used
        if "write-grounding verifier" in str(kwargs.get("system_message") or ""):
            return await send_message(**kwargs)
        if used:
            return ResponseWithThought(text=json.dumps({"calls": []}))
        used = True
        return ResponseWithThought(
            text=json.dumps(
                {"calls": [{"name": call.name, "args": call.args or {}, "id": call.id or "1"}]},
                ensure_ascii=False,
            )
        )

    notes_result = await collect_notes_evidence_with_tools(
        query,
        chat_history,
        user,
        agent,
        send_message=single_tool_send_message,
        client_app=client_app,
        allow_local_kb=allow_local_kb,
        allow_openkb=allow_openkb,
        conversation_id=conversation_id,
        max_iterations=3,
        initial_tool_transcript=result.tool_transcript,
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
    context = (
        "Notes write tool result: "
        f"{json.dumps(payload, ensure_ascii=False, default=str)}. "
        f"{instruction}"
    )
    if context not in result.program_context:
        result.program_context.append(context)


async def collect_agent_context_and_actions(
    query: str,
    chat_history: list,
    *,
    user: Any,
    agent: Any,
    send_message: Callable[..., Awaitable[Any]],
    send_status: Optional[Callable[[str], Any]] = None,
    before_tool_call: Optional[Callable[[ConversationCommand], Awaitable[None]]] = None,
    client_app: Any = None,
    allow_local_kb: bool,
    allow_openkb: bool,
    allow_web: bool,
    conversation_id: str = "agent-tool-loop",
    max_iterations: int = 4,
    location: Any = None,
    query_images: list[str] | None = None,
    query_files: str | None = None,
    relevant_memories: list | None = None,
    tracer: dict | None = None,
) -> AgentToolLoopResult:
    result = AgentToolLoopResult()
    registry = build_agent_tool_registry(allow_local_kb=allow_local_kb, allow_openkb=allow_openkb, allow_web=allow_web)
    if not registry:
        result.errors.append("No agent runtime tools are available.")
        return result

    await _send_status(send_status, "Planning with unified agent runtime")
    for _ in range(max(1, max_iterations)):
        response = await send_message(
            query=_build_planner_query(query, chat_history, registry, result.tool_transcript),
            system_message=AGENT_TOOL_SYSTEM_PROMPT,
            chat_history=chat_history,
            tools=[],
            response_type="json_object",
            deepthought=True,
            fast_model=False,
        )
        if response and getattr(response, "thought", None):
            await _send_status(send_status, response.thought)
        calls = parse_agent_tool_calls(getattr(response, "text", response) or "")
        if not calls:
            break
        for call in calls:
            call.name = _normalize_tool_name(call.name)
            if call.name not in registry:
                _record_tool_error(result, call.name, call.args, f"Agent runtime tool is not available: {call.name}")
                continue
            command = _command_for_tool(call.name)
            if before_tool_call and command:
                await before_tool_call(command)
            await _send_status(send_status, f"Using agent tool: {call.name}")
            reference_start = len(result.references)
            try:
                if call.name == "web_search":
                    await run_web_search_tool(
                        call.args,
                        result=result,
                        user=user,
                        conversation_history=chat_history,
                        location=location,
                        query_images=query_images,
                        query_files=query_files,
                        relevant_memories=relevant_memories,
                        agent=agent,
                        tracer=tracer,
                    )
                elif call.name == ConversationCommand.ReadWebpage.value:
                    await run_read_webpage_tool(
                        call.args,
                        result=result,
                        user=user,
                        conversation_history=chat_history,
                        location=location,
                        query_images=query_images,
                        query_files=query_files,
                        relevant_memories=relevant_memories,
                        agent=agent,
                        tracer=tracer,
                    )
                else:
                    await run_notes_tool_call(
                        call,
                        result=result,
                        query=query,
                        chat_history=chat_history,
                        user=user,
                        agent=agent,
                        send_message=send_message,
                        client_app=client_app,
                        allow_local_kb=allow_local_kb,
                        allow_openkb=allow_openkb,
                        conversation_id=conversation_id,
                    )
            except Exception as exc:
                logger.warning("Agent runtime tool failed: %s", call.name, exc_info=True)
                _record_tool_error(result, call.name, call.args, str(exc))
            for reference in result.references[reference_start:]:
                add_write_reference_context(result, reference)

    result.inferred_queries = list(dict.fromkeys(item for item in result.inferred_queries if item))
    return result


def _build_planner_query(
    query: str,
    chat_history: list,
    registry: dict[str, ToolDefinition],
    tool_transcript: list[dict[str, Any]],
) -> str:
    return (
        f"User question:\n{query}\n\n"
        f"Recent conversation artifacts:\n{json.dumps(_recent_artifact_catalog(chat_history), ensure_ascii=False, default=str)[:6000]}\n\n"
        "Return a json object with a calls array. Use an empty calls array when no more tools are needed.\n\n"
        f"Available tools:\n{json.dumps(_tool_specs(list(registry.values())), ensure_ascii=False, default=str)[:10000]}\n\n"
        f"Tool results so far:\n{json.dumps(tool_transcript, ensure_ascii=False, default=str)[:16000]}"
    )


def _normalize_tool_name(name: str) -> str:
    if name == "search_web":
        return "web_search"
    return name


def _command_for_tool(name: str) -> ConversationCommand | None:
    if name == "web_search":
        return ConversationCommand.Online
    if name == ConversationCommand.ReadWebpage.value:
        return ConversationCommand.Webpage
    if name in {
        ConversationCommand.ViewFile.value,
        ConversationCommand.ListFiles.value,
        ConversationCommand.KbHeadings.value,
        ConversationCommand.KbResolveLink.value,
        ConversationCommand.RegexSearchFiles.value,
        APPEND_NOTE_TOOL.name,
        PROPOSE_EDIT_TOOL.name,
        READ_SKILL_TOOL.name,
        OPENKB_TOOL.name,
    }:
        return ConversationCommand.Notes
    return None


def _tool_specs(tools: list[ToolDefinition]) -> list[dict[str, Any]]:
    return [{"name": tool.name, "description": tool.description, "schema": tool.schema} for tool in tools]


def _tool_result_text(value: Any, limit: int = 8000) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return text[:limit]


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
