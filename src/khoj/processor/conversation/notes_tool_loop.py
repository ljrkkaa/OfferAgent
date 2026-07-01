import inspect
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

import yaml

from khoj.processor.conversation.utils import ToolCall, load_complex_json
from khoj.utils.helpers import ConversationCommand, ToolDefinition, tools_for_research_llm
from khoj.utils.local_kb import (
    LocalKBError,
    LocalKBWriteResult,
    append_local_kb_note,
    get_local_kb_root,
    kb_grep,
    kb_headings,
    kb_list,
    kb_read,
    kb_resolve_link,
    propose_local_kb_edit,
)
from khoj.utils.openkb import wiki_search_documents


@dataclass
class NotesToolLoopResult:
    references: list[dict[str, Any]] = field(default_factory=list)
    inferred_queries: list[str] = field(default_factory=list)
    searched: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class LocalSkill:
    name: str
    description: str
    path: Path


NOTES_TOOL_SYSTEM_PROMPT = """
You collect evidence from the user's personal knowledge base for the main chat answer.

For Notes requests, your first response should normally be a JSON tool call, not prose.
重要：如果用户用中文要求“写入 / 新建 / 追加 / 修改 / 更新 / 加到索引 / 不要只口头说”，必须返回 JSON 工具调用；
不要直接用自然语言回答做不到，除非工具已经返回错误。

Use tools instead of guessing. Prefer this workflow:
1. list_files or regex_search_files to find candidate notes.
2. kb_headings to locate useful sections in large Markdown files.
3. view_file to read exact lines before relying on a note.
4. kb_resolve_link when a read note points to a related note.

Only exact view_file or OpenKB evidence becomes final references. Use append_note only when the
user clearly asks to create or append note content. append_note can create a new .md/.txt file under an
existing folder. Use propose_edit for replace/delete/overwrite-style requests.
For requests like "add X to section Y in file Z", read or inspect file Z, then call append_note with
path=Z and heading=Y. Do not stop with plain text before trying an available tool.
If project instructions or a matching skill covers the task, follow them before writing.
To call tools, return only a json object like:
{"calls":[{"name":"view_file","args":{"path":"notes.md"},"id":"1"}]}
When enough evidence has been collected, return {"calls":[]}.
""".strip()


APPEND_NOTE_TOOL = ToolDefinition(
    name="append_note",
    description=(
        "Create or append user-approved content to a local knowledge base Markdown or text file. "
        "Can create a new .md/.txt file under an existing folder when vault writes are enabled."
    ),
    schema={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Target local KB file path."},
            "content": {"type": "string", "description": "Content to append."},
            "heading": {"type": "string", "description": "Optional Markdown heading to append under."},
        },
        "required": ["path", "content"],
    },
)

PROPOSE_EDIT_TOOL = ToolDefinition(
    name="propose_edit",
    description="Prepare a local KB edit diff without modifying the file.",
    schema={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Target local KB file path."},
            "find": {"type": "string", "description": "Existing text to replace."},
            "replace": {"type": "string", "description": "Replacement text."},
            "reason": {"type": "string", "description": "Optional edit reason."},
        },
        "required": ["path", "find", "replace"],
    },
)

OPENKB_TOOL = ToolDefinition(
    name="wiki_search_documents",
    description="Search the compiled OpenKB wiki for evidence references.",
    schema={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "OpenKB wiki evidence query."},
            "n": {"type": "integer", "description": "Maximum number of references to return."},
        },
        "required": ["query"],
    },
)


READ_SKILL_TOOL = ToolDefinition(
    name="read_skill",
    description="Read one local SKILL.md package by name before following its instructions.",
    schema={
        "type": "object",
        "properties": {"name": {"type": "string", "description": "Skill name from the available skills catalog."}},
        "required": ["name"],
    },
)


async def _send_status(send_status: Optional[Callable], message: str) -> None:
    if not send_status:
        return
    result = send_status(message)
    if hasattr(result, "__aiter__"):
        async for _ in result:
            pass
    elif inspect.isawaitable(result):
        await result


def _as_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def _parse_skill_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    try:
        end = lines.index("---", 1)
    except ValueError:
        return {}, text
    try:
        meta = yaml.safe_load("\n".join(lines[1:end])) or {}
    except yaml.YAMLError:
        meta = {}
    return (meta if isinstance(meta, dict) else {}), "\n".join(lines[end + 1 :]).strip()


def _scan_local_skills(root: Optional[Path]) -> list[LocalSkill]:
    if root is None:
        return []

    seen: set[str] = set()
    skills: list[LocalSkill] = []
    safe_kb_root = root.resolve(strict=False)
    for skill_root in (root / ".codex" / "skills", root / "skills"):
        if not skill_root.is_dir():
            continue
        try:
            safe_root = skill_root.resolve(strict=True)
        except OSError:
            continue
        if not safe_root.is_relative_to(safe_kb_root):
            continue
        skill_files = []
        for pattern in ("*/SKILL.md", "*/*/SKILL.md", "*/*/*/SKILL.md"):
            skill_files.extend(sorted(skill_root.glob(pattern)))
        for skill_file in skill_files[:80]:
            try:
                requested_parts = skill_file.relative_to(skill_root).parts
            except ValueError:
                continue
            if any(part.startswith(".") for part in requested_parts[:-1]):
                continue
            try:
                safe_file = skill_file.resolve(strict=True)
            except OSError:
                continue
            if not safe_file.is_relative_to(safe_root):
                continue
            try:
                resolved_parts = safe_file.relative_to(safe_root).parts
            except ValueError:
                continue
            if any(part.startswith(".") for part in resolved_parts[:-1]):
                continue
            try:
                text = safe_file.read_text(encoding="utf-8")
            except OSError:
                continue
            meta, _ = _parse_skill_frontmatter(text)
            name = str(meta.get("name") or skill_file.parent.name).strip()
            description = str(meta.get("description") or "").strip()
            if not name or not description or name in seen:
                continue
            seen.add(name)
            skills.append(LocalSkill(name=name, description=description[:1024], path=skill_file.parent))
    return skills


def _skill_catalog(skills: list[LocalSkill]) -> str:
    if not skills:
        return "No local skills installed."
    lines = [f"{len(skills)} local skill(s) available:"]
    for skill in skills:
        description = " ".join(skill.description.split())
        lines.append(f"- {skill.name}: {description}")
    lines.append("To use a skill, call read_skill(name) and follow its instructions.")
    return "\n".join(lines)


def _read_skill(skill: LocalSkill) -> dict[str, str]:
    root = get_local_kb_root()
    if root is None:
        raise OSError("Local knowledge base is not configured.")
    safe_root = root.resolve(strict=True)
    safe_skill_dir = skill.path.resolve(strict=True)
    skill_file = (skill.path / "SKILL.md").resolve(strict=True)
    if not skill_file.is_relative_to(safe_root) or skill_file.parent != safe_skill_dir:
        raise OSError("Local skill path escapes the knowledge base.")
    text = skill_file.read_text(encoding="utf-8")
    _, body = _parse_skill_frontmatter(text)
    return {"name": skill.name, "description": skill.description, "body": body[:12000]}


def _local_profile_prompt(root: Optional[Path]) -> str:
    if root is None:
        return ""
    chunks: list[str] = []
    safe_root = root.resolve(strict=False)
    for name in ("AGENTS.md", "agents.md", "agent.md", "index.md", "README.md"):
        path = root / name
        if path.is_file():
            try:
                if not path.resolve(strict=True).is_relative_to(safe_root):
                    continue
                text = path.read_text(encoding="utf-8", errors="replace").strip()
            except OSError:
                continue
            if text:
                chunks.append(f"# {name}\n\n{text}")
    if not chunks:
        return ""
    compiled = "\n\n".join(chunks)
    return f"\n\n## Local knowledge base instructions\n\n{compiled[:6000]}"


def _skill_prompt(skills: list[LocalSkill]) -> str:
    if not skills:
        return ""
    return (
        "\n\n## Available local skills\n\n"
        "These SKILL.md packages are installed inside the bound knowledge base. "
        "When a user request matches a skill description, call read_skill(name) before writing or answering. "
        "Skills are instructions, not extra tool names; execute them only through the available Notes tools. "
        "For Obsidian note, daily note, wikilink, frontmatter, canvas, or bases tasks, prefer the matching obsidian skill.\n\n"
        f"{_skill_catalog(skills)}"
    )


def _local_read_reference(item, reason: str, remaining_chars: int) -> dict[str, Any] | None:
    text = item.text[:remaining_chars].rstrip()
    if not item.lines or not text:
        return None
    return {
        "query": reason,
        "file": item.path,
        "uri": f"local-kb://{item.path}#L{item.start_line}-L{item.end_line}",
        "compiled": f"# {item.path} L{item.start_line}-L{item.end_line}\n{text}",
        "start_line": item.start_line,
        "end_line": item.end_line,
        "total_lines": item.total_lines,
        "mtime": item.mtime,
        "checksum": item.checksum,
    }


def _write_reference(result: LocalKBWriteResult) -> dict[str, Any]:
    compiled = result.message
    if result.diff:
        compiled += f"\n\n```diff\n{result.diff.rstrip()}\n```"
    if result.start_line and result.end_line:
        compiled += f"\n\nLines: {result.start_line}-{result.end_line}"
    return {
        "query": result.action,
        "file": result.path,
        "uri": f"local-kb://{result.path}",
        "compiled": compiled,
        "action": result.action,
        "status": result.status,
        "changed": result.changed,
        "start_line": result.start_line,
        "end_line": result.end_line,
        "checksum": result.checksum,
    }


def _tool_result_text(value: Any, limit: int = 8000) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return text[:limit]


def _parse_tool_calls(text: str) -> list[ToolCall]:
    try:
        payload = load_complex_json(text)
    except Exception:
        return []
    if isinstance(payload, dict):
        for key in ("calls", "tool_calls", "tools"):
            if isinstance(payload.get(key), list):
                payload = payload[key]
                break
        else:
            payload = [payload]
    calls = []
    for item in payload or []:
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
        calls.append(ToolCall(name=name, args=args, id=item.get("id")))
    return calls


def _tool_specs(tools: list[ToolDefinition]) -> list[dict[str, Any]]:
    return [{"name": tool.name, "description": tool.description, "schema": tool.schema} for tool in tools]


def _notes_tools(*, allow_local_kb: bool, allow_openkb: bool, allow_skills: bool) -> list[ToolDefinition]:
    tools: list[ToolDefinition] = []
    if allow_local_kb:
        tools.extend(
            [
                tools_for_research_llm[ConversationCommand.ListFiles],
                tools_for_research_llm[ConversationCommand.RegexSearchFiles],
                tools_for_research_llm[ConversationCommand.KbHeadings],
                tools_for_research_llm[ConversationCommand.ViewFile],
                tools_for_research_llm[ConversationCommand.KbResolveLink],
                APPEND_NOTE_TOOL,
                PROPOSE_EDIT_TOOL,
            ]
        )
    if allow_skills:
        tools.append(READ_SKILL_TOOL)
    if allow_openkb:
        tools.append(OPENKB_TOOL)
    return tools


async def collect_notes_evidence_with_tools(
    query: str,
    chat_history: list,
    user: Any,
    agent: Any,
    *,
    send_message: Callable[..., Awaitable[Any]],
    send_status: Optional[Callable[[str], Any]] = None,
    client_app: Any = None,
    allow_local_kb: bool = True,
    allow_openkb: bool = False,
    conversation_id: str = "notes-tool-loop",
    max_iterations: int = 4,
    max_evidence_chars: int = 16000,
) -> NotesToolLoopResult:
    result = NotesToolLoopResult()
    references: list[dict[str, Any]] = []
    evidence_chars = 0
    read_keys: set[tuple[str, int, int, str]] = set()
    local_root = get_local_kb_root()
    local_kb_allowed = allow_local_kb and local_root is not None
    local_skills = _scan_local_skills(local_root) if local_kb_allowed else []
    tools = _notes_tools(
        allow_local_kb=local_kb_allowed,
        allow_openkb=allow_openkb,
        allow_skills=bool(local_skills),
    )
    if not tools:
        result.errors.append("No Notes evidence tools are available.")
        return result
    allowed_tool_names = {tool.name for tool in tools}
    skill_index = {skill.name: skill for skill in local_skills}
    system_message = NOTES_TOOL_SYSTEM_PROMPT + _local_profile_prompt(local_root if local_kb_allowed else None)
    system_message += _skill_prompt(local_skills)

    tool_transcript: list[dict[str, Any]] = []
    exact_evidence_retry_sent = False
    await _send_status(send_status, "Planning Notes evidence with the main agent")

    async def execute_tool(call: ToolCall) -> Any:
        nonlocal evidence_chars
        args = call.args or {}
        result.searched.append(f"{call.name} {json.dumps(args, ensure_ascii=False, default=str)}")
        if call.name not in allowed_tool_names:
            message = f"Notes tool is not available: {call.name}"
            result.errors.append(message)
            return {"error": message}

        try:
            if call.name == ConversationCommand.ListFiles.value:
                listing = kb_list(args.get("path"), args.get("pattern"), limit=80)
                return {
                    "path": listing.path,
                    "items": listing.items,
                    "total": listing.total,
                    "truncated": listing.truncated,
                }
            if call.name == ConversationCommand.RegexSearchFiles.value:
                grep = kb_grep(
                    args.get("regex_pattern") or "",
                    path_prefix=args.get("path_prefix"),
                    mode="regex",
                    before=_as_int(args.get("lines_before"), 0, 0, 5),
                    after=_as_int(args.get("lines_after"), 0, 0, 5),
                    max_results=80,
                )
                return {
                    "line_count": grep.line_count,
                    "document_count": grep.document_count,
                    "lines": grep.lines,
                    "matches": grep.matches,
                    "truncated": grep.truncated,
                }
            if call.name == ConversationCommand.KbHeadings.value:
                headings = kb_headings(args.get("path") or "")
                return {
                    "path": headings.path,
                    "headings": headings.headings,
                    "total_lines": headings.total_lines,
                }
            if call.name == ConversationCommand.KbResolveLink.value:
                resolved = kb_resolve_link(args.get("from_path") or "", args.get("link") or "")
                return {
                    "link": resolved.link,
                    "status": resolved.status,
                    "resolved": resolved.resolved,
                    "anchor": resolved.anchor,
                    "candidates": resolved.candidates,
                }
            if call.name == ConversationCommand.ViewFile.value:
                item = kb_read(
                    args.get("path") or "",
                    start_line=args.get("start_line"),
                    end_line=args.get("end_line"),
                    max_lines=80,
                )
                key = (item.path, item.start_line, item.end_line, item.checksum)
                if key not in read_keys and evidence_chars < max_evidence_chars:
                    ref = _local_read_reference(item, f"view_file:{item.path}", max_evidence_chars - evidence_chars)
                    if ref:
                        read_keys.add(key)
                        evidence_chars += len(ref["compiled"])
                        references.append(ref)
                return {
                    "path": item.path,
                    "start_line": item.start_line,
                    "end_line": item.end_line,
                    "total_lines": item.total_lines,
                    "text": item.text,
                    "truncated": item.truncated,
                }
            if call.name == "append_note":
                if str(client_app or "").lower() == "qqbot":
                    blocked = LocalKBWriteResult(
                        action="append_note",
                        path=str(args.get("path") or "").strip(),
                        status="blocked",
                        changed=False,
                        message="QQBot client writes are disabled by default.",
                    )
                    references.append(_write_reference(blocked))
                    return blocked.__dict__
                write = append_local_kb_note(args.get("path") or "", args.get("content") or "", args.get("heading"))
                references.append(_write_reference(write))
                return write.__dict__
            if call.name == "propose_edit":
                edit = propose_local_kb_edit(
                    args.get("path") or "",
                    args.get("find") or "",
                    args.get("replace") or "",
                    reason=args.get("reason"),
                )
                references.append(_write_reference(edit))
                return edit.__dict__
            if call.name == "read_skill":
                skill_name = str(args.get("name") or "").strip()
                skill = skill_index.get(skill_name)
                if skill is None:
                    return {"error": f"Unknown local skill: {skill_name}"}
                try:
                    return _read_skill(skill)
                except OSError as e:
                    message = f"Local skill is not readable: {skill_name}"
                    result.errors.append(message)
                    return {"error": message, "detail": str(e)}
            if call.name == "wiki_search_documents" and allow_openkb:
                refs, queries, _ = await wiki_search_documents(
                    args.get("query") or query,
                    _as_int(args.get("n"), 5, 1, 10),
                    user,
                    chat_history,
                    conversation_id,
                    agent=agent,
                    send_status_func=send_status,
                )
                references.extend(refs)
                result.inferred_queries.extend(queries)
                return {"references": refs, "queries": queries}
            return {"error": f"Unknown Notes tool: {call.name}"}
        except LocalKBError as e:
            result.errors.append(str(e))
            if call.name in {"append_note", "propose_edit"}:
                failed = LocalKBWriteResult(
                    action=call.name,
                    path=str(args.get("path") or "").strip(),
                    status=e.kind,
                    changed=False,
                    message=str(e),
                )
                references.append(_write_reference(failed))
                return failed.__dict__
            return {"error": str(e)}

    for _ in range(max(1, max_iterations)):
        prompt = (
            f"User question:\n{query}\n\n"
            "Return a json object with a calls array. Use an empty calls array when no more tools are needed.\n\n"
            f"Available tools:\n{json.dumps(_tool_specs(tools), ensure_ascii=False, default=str)[:8000]}\n\n"
            f"Tool results so far:\n{json.dumps(tool_transcript, ensure_ascii=False, default=str)[:12000]}"
        )
        message_kwargs = {
            "query": prompt,
            "system_message": system_message,
            "chat_history": chat_history,
            "tools": [],
            "response_type": "json_object",
            "deepthought": True,
            "fast_model": False,
        }
        for attempt in range(2):
            try:
                response = await send_message(
                    **message_kwargs,
                )
                break
            except Exception:
                if attempt:
                    raise
                await _send_status(send_status, "Notes planner failed once; retrying")
        if response and getattr(response, "thought", None):
            await _send_status(send_status, response.thought)
        calls = _parse_tool_calls(getattr(response, "text", "") or "")
        if not calls:
            if not tool_transcript:
                tool_transcript.append(
                    {
                        "tool": "system",
                        "args": {},
                        "result": (
                            "No tool call was returned. For a Notes request, use at least one relevant available "
                            "tool before stopping. For write requests, call append_note or propose_edit."
                        ),
                    }
                )
                continue
            if not references and not exact_evidence_retry_sent:
                exact_evidence_retry_sent = True
                tool_transcript.append(
                    {
                        "tool": "system",
                        "args": {},
                        "result": (
                            "No exact Notes evidence has been collected yet. Discovery tools like list_files, "
                            "regex_search_files, kb_headings, kb_resolve_link, and read_skill are not final "
                            "references. If their results contain a candidate file or line, call view_file next."
                        ),
                    }
                )
                continue
            break
        for call in calls:
            await _send_status(send_status, f"Using Notes tool: {call.name}")
            tool_output = await execute_tool(call)
            tool_transcript.append({"tool": call.name, "args": call.args, "result": _tool_result_text(tool_output)})

    result.references = references
    result.inferred_queries = list(
        dict.fromkeys(result.inferred_queries + [ref.get("query", "") for ref in references] + result.searched)
    )
    if result.references:
        await _send_status(send_status, f"Found {len(result.references)} Notes references")
    else:
        await _send_status(send_status, "No Notes evidence found")
    return result
