import inspect
import json
import re
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
    tool_transcript: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class LocalSkill:
    name: str
    description: str
    path: Path


NOTES_TOOL_SYSTEM_PROMPT = """
You collect evidence from the user's personal knowledge base for the main chat answer.

For Notes requests, your first response should normally be a JSON tool call, not prose.
If the user asks for a knowledge-base file operation, return a JSON tool call instead of plain prose.
Do not claim an operation cannot be done until the relevant tool has returned an error.

Use tools instead of guessing. Prefer this workflow:
1. list_files or regex_search_files to find candidate notes.
2. kb_headings to locate useful sections in large Markdown files.
3. view_file to read exact lines before relying on a note.
4. kb_resolve_link when a read note points to a related note.

Only exact view_file or OpenKB evidence becomes final references. Use append_note only when the
user clearly asks to create or append note content. append_note can create a new .md/.txt file under an
existing folder. Use propose_edit for replace/delete/overwrite-style requests.
For append_note, include source_refs and write_intent whenever content is derived from prior chat,
files, tool results, or any source other than text explicitly provided in the current user request.
When the needed content exists in Recent conversation artifacts, prefer artifact_id or
source_refs=[{"type":"artifact","id":"..."}] over re-copying from raw chat history.
If append_note says source_refs_required or source_mismatch, retry with concrete source_refs and
content grounded in those sources instead of summarizing unrelated conversation history.
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
            "artifact_id": {
                "type": "string",
                "description": "Conversation artifact id to append or adapt, e.g. assistant:<turnId>.",
            },
            "content": {"type": "string", "description": "Content to append. Optional when artifact_id is provided."},
            "heading": {"type": "string", "description": "Optional Markdown heading to append under."},
            "write_intent": {
                "type": "string",
                "description": "How content transforms source_refs, e.g. preserve, summarize, adapt_to_template, merge.",
            },
            "source_refs": {
                "type": "array",
                "description": (
                    "Concrete sources used to write content when content is not explicitly provided by the current user request. "
                    "Examples: {type:'artifact', id:'assistant:<turnId>'}, {type:'assistant_message', turn:-1}, {type:'user_message', turn:-1}, "
                    "{type:'file', path:'templates/daily-template.md'}."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "type": {"type": "string"},
                        "id": {"type": "string"},
                        "artifact_id": {"type": "string"},
                        "turn": {"type": "integer"},
                        "path": {"type": "string"},
                        "start_line": {"type": "integer"},
                        "end_line": {"type": "integer"},
                    },
                },
            },
        },
        "required": ["path"],
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
            "source_refs": APPEND_NOTE_TOOL.schema["properties"]["source_refs"],
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
    local_root = get_local_kb_root()
    return {
        "query": reason,
        "file": item.path,
        "uri": f"local-kb://{item.path}#L{item.start_line}-L{item.end_line}",
        "kb_root": str(local_root) if local_root else None,
        "compiled": f"# {item.path} L{item.start_line}-L{item.end_line}\n{text}",
        "start_line": item.start_line,
        "end_line": item.end_line,
        "total_lines": item.total_lines,
        "mtime": item.mtime,
        "checksum": item.checksum,
    }


def _write_reference(result: LocalKBWriteResult) -> dict[str, Any]:
    compiled = result.message
    local_root = get_local_kb_root()
    if result.diff:
        compiled += f"\n\n```diff\n{result.diff.rstrip()}\n```"
    if result.start_line and result.end_line:
        compiled += f"\n\nLines: {result.start_line}-{result.end_line}"
    return {
        "query": result.action,
        "file": result.path,
        "uri": f"local-kb://{result.path}",
        "kb_root": str(local_root) if local_root else None,
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


_FILE_REF_RE = re.compile(r"[\w./-]+\.(?:md|txt|pdf|png|jpe?g|webp|json|ya?ml)", re.I)


def _plain_message_text(message: Any) -> str:
    value = (
        (message.get("message") or message.get("content"))
        if isinstance(message, dict)
        else (getattr(message, "message", None) or getattr(message, "content", ""))
    )
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        chunks = []
        for item in value:
            if isinstance(item, dict):
                chunks.append(str(item.get("text") or item.get("content") or ""))
            else:
                chunks.append(str(item))
        return "\n".join(chunk for chunk in chunks if chunk)
    return str(value or "")


def _message_by(message: Any) -> str:
    value = (message.get("by") or message.get("role")) if isinstance(message, dict) else (getattr(message, "by", None) or getattr(message, "role", ""))
    return str(value).lower()


def _message_artifacts(message: Any) -> list[dict[str, Any]]:
    artifacts = message.get("artifacts") if isinstance(message, dict) else getattr(message, "artifacts", None)
    if not isinstance(artifacts, list):
        return []
    return [item for item in artifacts if isinstance(item, dict) and item.get("id")]


def _find_artifact(chat_history: list[Any], artifact_id: str) -> dict[str, Any] | None:
    if not artifact_id:
        return None
    for message in reversed(chat_history):
        for artifact in _message_artifacts(message):
            if str(artifact.get("id") or "") == artifact_id:
                return artifact
    return None


def _recent_artifact_catalog(chat_history: list[Any], limit: int = 6, preview_chars: int = 600) -> list[dict[str, Any]]:
    artifacts = [artifact for message in chat_history for artifact in _message_artifacts(message)]
    catalog = []
    for artifact in artifacts[-limit:]:
        source_files = []
        for ref in artifact.get("source_refs") or []:
            if isinstance(ref, dict):
                source = ref.get("file") or ref.get("uri")
                if source:
                    source_files.append(source)
        catalog.append(
            {
                "id": artifact.get("id"),
                "type": artifact.get("type"),
                "preview": str(artifact.get("content") or "")[:preview_chars],
                "source_files": source_files[:6],
            }
        )
    return catalog


def _select_turn(messages: list[Any], turn: Any) -> Any | None:
    if not messages:
        return None
    idx = _as_int(turn, -1, -len(messages), len(messages))
    if idx < 0:
        idx = len(messages) + idx
    elif idx > 0:
        idx -= 1
    if 0 <= idx < len(messages):
        return messages[idx]
    return None


def _source_ref_text(ref: dict[str, Any], chat_history: list, tool_transcript: list[dict[str, Any]]) -> str:
    ref_type = str(ref.get("type") or ref.get("kind") or "").lower()
    if ref_type == "artifact":
        artifact = _find_artifact(chat_history, str(ref.get("id") or ref.get("artifact_id") or ""))
        return str(artifact.get("content") or "") if artifact else ""
    if ref_type in {"assistant_message", "assistant"}:
        messages = [item for item in chat_history if _message_by(item) not in {"you", "user"}]
        selected = _select_turn(messages, ref.get("turn", -1))
        return _plain_message_text(selected) if selected is not None else ""
    if ref_type in {"user_message", "user"}:
        messages = [item for item in chat_history if _message_by(item) in {"you", "user"}]
        selected = _select_turn(messages, ref.get("turn", -1))
        return _plain_message_text(selected) if selected is not None else ""
    if ref_type in {"file", "view_file"} and ref.get("path"):
        item = kb_read(ref.get("path") or "", start_line=ref.get("start_line"), end_line=ref.get("end_line"), max_lines=200)
        return item.text
    if ref_type in {"tool_result", "tool"}:
        tool_name = str(ref.get("tool") or ref.get("name") or "")
        for item in reversed(tool_transcript):
            if tool_name and item.get("tool") != tool_name:
                continue
            return str(item.get("result") or "")
    return ""


def _compact_text(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


def _tool_result_value(item: dict[str, Any]) -> Any:
    value = item.get("result")
    if isinstance(value, str):
        try:
            return load_complex_json(value)
        except Exception:
            return value
    return value


def _edit_source_error(args: dict[str, Any], chat_history: list, tool_transcript: list[dict[str, Any]]) -> str:
    find = str(args.get("find") or "").strip()
    path = str(args.get("path") or "").strip()
    if not find:
        return ""

    refs = args.get("source_refs")
    if isinstance(refs, list) and refs:
        source_text = "\n\n".join(
            _source_ref_text(ref, chat_history, tool_transcript) for ref in refs if isinstance(ref, dict)
        )
        if find in source_text:
            return ""
        return "edit_source_required: Could not find the proposed edit text in source_refs. Read the target file and retry."

    for item in reversed(tool_transcript):
        if item.get("tool") != ConversationCommand.ViewFile.value:
            continue
        item_args = item.get("args") if isinstance(item.get("args"), dict) else {}
        if path and str(item_args.get("path") or "").strip() != path:
            continue
        result = _tool_result_value(item)
        text = result.get("text") if isinstance(result, dict) else str(result or "")
        if find in str(text or ""):
            return ""
    return "edit_source_required: Read the target file with view_file before calling propose_edit."


def _source_refs_required_error(query: str, args: dict[str, Any]) -> str:
    if args.get("source_refs"):
        return ""
    content = str(args.get("content") or "").strip()
    if content and _compact_text(content) in _compact_text(query):
        return ""
    return (
        "source_refs_required: append content was not explicitly provided in the current user request. "
        "Retry append_note with concrete source_refs such as assistant_message, user_message, file, or tool_result."
    )


def _json_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"true", "yes", "ok", "grounded"}
    return False


async def _source_bound_append_error(
    query: str,
    args: dict[str, Any],
    chat_history: list,
    tool_transcript: list[dict[str, Any]],
    send_message: Callable[..., Awaitable[Any]],
) -> str:
    refs = args.get("source_refs")
    if not refs:
        return ""
    if not isinstance(refs, list):
        return "source_refs must be an array of concrete source objects."
    source_texts = [_source_ref_text(ref, chat_history, tool_transcript) for ref in refs if isinstance(ref, dict)]
    source_text = "\n\n".join(text for text in source_texts if text.strip())
    if not source_text.strip():
        return "Could not resolve source_refs. Retry with concrete assistant_message, user_message, file, or tool_result refs."

    content = str(args.get("content") or "")
    if content.strip() and any(content.strip() == text.strip() for text in source_texts):
        return ""

    # Keep execution deterministic: code checks protocol/safety; the verifier owns semantic grounding.
    content_file_refs = {ref.lower() for ref in _FILE_REF_RE.findall(content)}
    source_file_refs = {ref.lower() for ref in _FILE_REF_RE.findall(source_text)}
    new_file_refs = content_file_refs - source_file_refs
    if new_file_refs:
        return f"source_mismatch: append content mentions file refs not present in source_refs: {', '.join(sorted(new_file_refs))}."

    if not content.strip():
        return ""

    prompt = (
        "Check whether this append_note content is grounded in the declared sources.\n"
        "Return only JSON: {\"grounded\": true|false, \"reason\": \"short reason\"}.\n"
        "Accept summaries, rewording, and template formatting. Reject unrelated conversation topics, "
        "unsupported facts, or content that appears to come from another source.\n\n"
        f"User request:\n{query[:4000]}\n\n"
        f"Write intent:\n{str(args.get('write_intent') or '')[:200]}\n\n"
        f"Declared source text:\n{source_text[:12000]}\n\n"
        f"Candidate append content:\n{content[:8000]}"
    )
    response = await send_message(
        query=prompt,
        system_message="You are a strict write-grounding verifier for a local notes agent.",
        chat_history=[],
        tools=[],
        response_type="json_object",
        deepthought=False,
        fast_model=False,
    )
    try:
        verdict = load_complex_json(getattr(response, "text", "") or "")
    except Exception:
        verdict = {}
    if not isinstance(verdict, dict) or not _json_bool(verdict.get("grounded")):
        reason = verdict.get("reason") if isinstance(verdict, dict) else ""
        return f"source_mismatch: grounding verifier rejected append content. {str(reason or '').strip()}"
    return ""


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
    initial_tool_transcript: Optional[list[dict[str, Any]]] = None,
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

    tool_transcript: list[dict[str, Any]] = list(initial_tool_transcript or [])
    exact_evidence_retry_sent = False
    artifact_catalog = _recent_artifact_catalog(chat_history)
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
                args = dict(args)
                artifact_id = str(args.get("artifact_id") or "").strip()
                if artifact_id:
                    artifact = _find_artifact(chat_history, artifact_id)
                    if artifact is None:
                        blocked = LocalKBWriteResult(
                            action="append_note",
                            path=str(args.get("path") or "").strip(),
                            status="artifact_not_found",
                            changed=False,
                            message=f"Conversation artifact not found: {artifact_id}",
                        )
                        references.append(_write_reference(blocked))
                        return blocked.__dict__
                    if not str(args.get("content") or "").strip():
                        args["content"] = str(artifact.get("content") or "")
                    source_refs = args.get("source_refs") if isinstance(args.get("source_refs"), list) else []
                    artifact_ref = {"type": "artifact", "id": artifact_id}
                    if not any(
                        isinstance(ref, dict)
                        and str(ref.get("type") or "") == "artifact"
                        and str(ref.get("id") or ref.get("artifact_id") or "") == artifact_id
                        for ref in source_refs
                    ):
                        args["source_refs"] = [*source_refs, artifact_ref]
                call.args = args
                if not str(args.get("content") or "").strip():
                    blocked = LocalKBWriteResult(
                        action="append_note",
                        path=str(args.get("path") or "").strip(),
                        status="missing_content",
                        changed=False,
                        message="append_note requires content or a resolvable artifact_id.",
                    )
                    references.append(_write_reference(blocked))
                    return blocked.__dict__
                source_refs_error = _source_refs_required_error(query, args)
                if source_refs_error:
                    blocked = LocalKBWriteResult(
                        action="append_note",
                        path=str(args.get("path") or "").strip(),
                        status="source_refs_required",
                        changed=False,
                        message=source_refs_error,
                    )
                    references.append(_write_reference(blocked))
                    return blocked.__dict__
                source_error = await _source_bound_append_error(query, args, chat_history, tool_transcript, send_message)
                if source_error:
                    blocked = LocalKBWriteResult(
                        action="append_note",
                        path=str(args.get("path") or "").strip(),
                        status="source_mismatch",
                        changed=False,
                        message=source_error,
                    )
                    references.append(_write_reference(blocked))
                    return blocked.__dict__
                write = append_local_kb_note(args.get("path") or "", args.get("content") or "", args.get("heading"))
                references.append(_write_reference(write))
                return write.__dict__
            if call.name == "propose_edit":
                edit_source_error = _edit_source_error(args, chat_history, tool_transcript)
                if edit_source_error:
                    blocked = LocalKBWriteResult(
                        action="propose_edit",
                        path=str(args.get("path") or "").strip(),
                        status="edit_source_required",
                        changed=False,
                        message=edit_source_error,
                    )
                    references.append(_write_reference(blocked))
                    return blocked.__dict__
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
            f"Recent conversation artifacts:\n{json.dumps(artifact_catalog, ensure_ascii=False, default=str)[:6000]}\n\n"
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
    result.tool_transcript = tool_transcript
    result.inferred_queries = list(
        dict.fromkeys(result.inferred_queries + [ref.get("query", "") for ref in references] + result.searched)
    )
    if result.references:
        await _send_status(send_status, f"Found {len(result.references)} Notes references")
    else:
        await _send_status(send_status, "No Notes evidence found")
    return result
