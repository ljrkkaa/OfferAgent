import fnmatch
import inspect
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncGenerator, Awaitable, Callable, Optional

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError

from khoj.database.adapters import FileObjectAdapters
from khoj.processor.conversation.utils import ToolCall
from khoj.processor.conversation.vault_policy import compact_policy_for_prompt
from khoj.search_type import text_search
from khoj.utils.helpers import AgentToolName, ToolDefinition, agent_tool_definitions
from khoj.utils.lexical import query_terms
from khoj.utils.local_kb import (
    LocalKBError,
    LocalKBWriteResult,
    get_local_kb_root,
    kb_grep,
    kb_headings,
    kb_list,
    kb_read,
    kb_resolve_link,
    local_kb_relative_path,
    resolve_local_kb_path,
)
from khoj.utils.openkb import get_kb_engine, openkb_is_ready, wiki_search_documents
from khoj.utils.rawconfig import SearchResponse
from khoj.utils.state import SearchType

logger = logging.getLogger(__name__)


@dataclass
class WorkspaceToolResult:
    references: list[dict[str, Any]] = field(default_factory=list)
    inferred_queries: list[str] = field(default_factory=list)
    searched: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    tool_transcript: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class WorkspaceSources:
    engine: str
    local_root: Optional[Path]
    local_enabled: bool
    openkb_enabled: bool

    @property
    def tool_source_available(self) -> bool:
        return self.local_enabled or self.openkb_enabled


def get_workspace_sources() -> WorkspaceSources:
    engine = get_kb_engine()
    local_root = get_local_kb_root()
    return WorkspaceSources(
        engine=engine,
        local_root=local_root,
        local_enabled=local_root is not None and engine in {"file_first", "hybrid"},
        openkb_enabled=engine in {"openkb", "hybrid"} and openkb_is_ready(),
    )


def dedupe_workspace_evidence(references: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for reference in references:
        key = (
            str(reference.get("uri") or ""),
            str(reference.get("file") or ""),
            str(reference.get("source_pages") or ""),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(reference)
    return deduped


async def search_indexed_evidence(user: Any, query: str, agent: Any = None, limit: int = 8) -> list[dict[str, Any]]:
    if not getattr(user, "uuid", None):
        return []
    hits = await text_search.query(query, user, SearchType.All)
    results = list(text_search.collate_results(hits))[: limit * 5]

    unique_results = []
    seen_files = set()
    for result in results:
        file_name = (result.additional or {}).get("file") or result.corpus_id
        if file_name in seen_files:
            continue
        seen_files.add(file_name)
        unique_results.append(result)
        if len(unique_results) >= limit:
            break

    file_names = [(result.additional or {}).get("file") for result in unique_results]
    file_objects = await FileObjectAdapters.aget_file_objects_by_names(user, [name for name in file_names if name])
    raw_text_by_file = {file_object.file_name: file_object.raw_text for file_object in file_objects}

    references: list[dict[str, Any]] = []
    for result in unique_results:
        additional = result.additional or {}
        file_name = additional.get("file")
        raw_text = raw_text_by_file.get(file_name)
        references.append(
            {
                "query": additional.get("query") or query,
                "file": file_name,
                "uri": additional.get("uri") or file_name,
                "compiled": f"# {file_name}\n{raw_text}" if raw_text else result.entry,
                "score": result.score,
                "source": additional.get("source") or "indexed",
                "heading": additional.get("heading"),
            }
        )
    return references


def _search_local_workspace(query: str, limit: int) -> list[SearchResponse]:
    results: list[SearchResponse] = []
    seen: set[tuple[str, int]] = set()
    terms = query_terms(query, max_terms=6, cjk_sizes=(4, 3, 2), ignore_prefixes=("file:", "dt:"))
    for term in terms:
        try:
            matches = kb_grep(term, mode="literal", before=1, after=2, max_results=max(limit * 2, 1)).matches
        except LocalKBError:
            continue
        for match in matches:
            key = (str(match.get("path") or ""), int(match.get("line") or 0))
            if key in seen:
                continue
            seen.add(key)
            try:
                item = kb_read(key[0], start_line=max(1, key[1] - 2), end_line=key[1] + 4, max_lines=80)
            except LocalKBError:
                continue
            uri = f"local-kb://{item.path}#L{item.start_line}-L{item.end_line}"
            results.append(
                SearchResponse(
                    entry=item.text,
                    score=float(len(results)),
                    additional={"file": item.path, "uri": uri, "query": term, "source": "local_kb"},
                    corpus_id=uri,
                )
            )
            if len(results) >= limit:
                return results
    return results


async def search_workspace(
    query: str,
    user: Any,
    *,
    limit: int = 5,
    search_type: SearchType = SearchType.All,
) -> list[SearchResponse]:
    searchable_types = {
        SearchType.All.value,
        SearchType.Markdown.value,
        SearchType.Plaintext.value,
        SearchType.Pdf.value,
    }
    if not query.strip() or getattr(search_type, "value", search_type) not in searchable_types:
        return []

    sources = get_workspace_sources()
    results: list[SearchResponse] = []
    if sources.local_enabled:
        results.extend(_search_local_workspace(query, limit))
    if len(results) < limit and sources.openkb_enabled:
        refs, _, _ = await wiki_search_documents(query, limit - len(results), user, [], "api-search")
        for index, reference in enumerate(dedupe_workspace_evidence(refs)[: limit - len(results)]):
            results.append(
                SearchResponse(
                    entry=str(reference.get("compiled") or ""),
                    score=float(index),
                    additional={
                        "file": reference.get("file"),
                        "uri": reference.get("uri"),
                        "query": reference.get("query"),
                        "source": "openkb",
                    },
                    corpus_id=str(reference.get("uri") or reference.get("file") or index),
                )
            )
    if not sources.tool_source_available:
        hits = await text_search.query(query, user, search_type)
        results.extend(list(text_search.collate_results(hits))[:limit])
    return results[:limit]


async def view_workspace_file(
    path: str,
    start_line: Optional[int] = None,
    end_line: Optional[int] = None,
    user: Any = None,
) -> AsyncGenerator[list[dict[str, str]], None]:
    query = f"View file: {path}"
    if start_line and end_line:
        query += f" (lines {start_line}-{end_line})"
    if get_workspace_sources().local_enabled:
        try:
            item = kb_read(path, start_line=start_line, end_line=end_line, max_lines=80)
            yield [{"query": query, "file": item.path, "uri": item.path, "compiled": item.text}]
        except LocalKBError as error:
            yield [{"query": query, "file": path, "uri": path, "compiled": str(error)}]
        return

    file_objects = await FileObjectAdapters.aget_file_objects_by_name(user, path)
    if not file_objects:
        message = f"File '{path}' not found in user documents"
        yield [{"query": query, "file": path, "uri": path, "compiled": message}]
        return
    lines = file_objects[0].raw_text.split("\n")
    first = start_line or 1
    last = end_line or len(lines)
    if first < 1 or last < 1 or first > last:
        message = f"Invalid line range: {first}-{last}"
        yield [{"query": query, "file": path, "uri": path, "compiled": message}]
        return
    if first > len(lines):
        message = f"Start line {first} exceeds total number of lines {len(lines)}"
        yield [{"query": query, "file": path, "uri": path, "compiled": message}]
        return
    start_index = first - 1
    end_index = min(len(lines), last)
    suffix = ""
    if end_index - start_index > 50:
        end_index = start_index + 50
        suffix = "\n\n[Truncated after 50 lines! Use narrower line range to view complete section.]"
    yield [{"query": query, "file": path, "uri": path, "compiled": "\n".join(lines[start_index:end_index]) + suffix}]


async def read_workspace_document(path: str, user: Any, *, max_lines: int = 200) -> Optional[tuple[str, str]]:
    if get_workspace_sources().local_enabled:
        try:
            item = kb_read(path, max_lines=max_lines)
        except LocalKBError:
            return None
        return item.path, item.text
    file_objects = await FileObjectAdapters.aget_file_objects_by_name(user, path)
    if not file_objects:
        return None
    item = file_objects[0]
    return item.file_name, item.raw_text


def _grep_query(
    line_count: int,
    document_count: int,
    path: str,
    pattern: str,
    lines_before: int,
    lines_after: int,
    max_results: int = 1000,
) -> str:
    query = f"**Found {line_count} matches for '{pattern}' in {document_count} documents**"
    if path:
        query += f" in {path}"
    if lines_before or lines_after or line_count > max_results:
        query += " Showing"
    context = []
    if lines_before:
        context.append(f"{lines_before} lines before")
    if lines_after:
        context.append(f"{lines_after} lines after")
    if context:
        query += f" {' and '.join(context)}"
    if line_count > max_results:
        query += f"{' for' if context else ''} first {max_results} results"
    return query


async def grep_workspace_files(
    regex_pattern: str,
    path_prefix: Optional[str] = None,
    lines_before: Optional[int] = None,
    lines_after: Optional[int] = None,
    user: Any = None,
):
    path_prefix = path_prefix or ""
    before = lines_before or 0
    after = lines_after or 0
    try:
        regex = re.compile(regex_pattern, re.IGNORECASE | re.MULTILINE)
    except re.error as error:
        yield {
            "query": _grep_query(0, 0, path_prefix, regex_pattern, before, after),
            "file": path_prefix,
            "compiled": f"Invalid regex pattern: {error}",
        }
        return

    if get_workspace_sources().local_enabled:
        try:
            result = kb_grep(
                regex_pattern,
                path_prefix=path_prefix,
                mode="regex",
                before=before,
                after=after,
                max_results=1000,
            )
            yield {
                "query": _grep_query(
                    result.line_count,
                    result.document_count,
                    path_prefix,
                    regex_pattern,
                    before,
                    after,
                ),
                "file": path_prefix,
                "uri": path_prefix,
                "compiled": "\n".join(result.lines) if result.lines else "No matches found.",
            }
        except LocalKBError as error:
            yield {
                "query": _grep_query(0, 0, path_prefix, regex_pattern, before, after),
                "file": path_prefix,
                "uri": path_prefix,
                "compiled": str(error),
            }
        return

    db_pattern = re.sub(r"\(\?\w*\)", "", regex_pattern)
    db_pattern = re.sub(r"^\^", "", db_pattern)
    db_pattern = re.sub(r"\$$", "", db_pattern)
    file_objects = await FileObjectAdapters.aget_file_objects_by_regex(user, db_pattern, path_prefix)
    output: list[str] = []
    match_count = 0
    for file_object in file_objects:
        lines = file_object.raw_text.split("\n")
        matches = [index for index, line in enumerate(lines, 1) if regex.search(line)]
        match_count += len(matches)
        for line_number in matches:
            start_index = max(0, line_number - 1 - before)
            end_index = min(len(lines), line_number + after)
            for index in range(start_index, end_index):
                current = index + 1
                marker = ":" if current == line_number else "-"
                separator = " " if current == line_number else "  "
                output.append(f"{file_object.file_name}{marker}{current}{marker}{separator}{lines[index]}")
            if before or after:
                output.append("--")
    if output and output[-1] == "--":
        output.pop()
    query = _grep_query(match_count, len(file_objects), path_prefix, regex_pattern, before, after)
    if not output:
        yield {"query": query, "file": path_prefix, "uri": path_prefix, "compiled": "No matches found."}
        return
    if len(output) > 1000:
        output = output[:1000] + [f"... {len(output) - 1000} more results found. Use a stricter regex."]
    yield {"query": query, "file": path_prefix, "uri": path_prefix, "compiled": "\n".join(output)}


async def list_workspace_files(
    path: Optional[str] = None,
    pattern: Optional[str] = None,
    user: Any = None,
):
    def query(count: int) -> str:
        text = f"**Found {count} files**"
        if path:
            text += f" in {path}"
        if pattern:
            text += f" filtered by {pattern}"
        return text

    if get_workspace_sources().local_enabled:
        try:
            result = kb_list(path, pattern, limit=100)
            items = [f"{item['path']}/" if item["type"] == "directory" else item["path"] for item in result.items]
            if result.truncated:
                items.append(f"... {result.total - len(result.items)} more files found. Use a narrower pattern.")
            yield {
                "query": query(result.total),
                "file": path,
                "uri": path,
                "compiled": "\n- ".join(items) if items else "No files found.",
            }
        except LocalKBError as error:
            yield {"query": query(0), "file": path, "uri": path, "compiled": str(error)}
        return

    normalized_path = path or ""
    if normalized_path in {"", "/", ".", "./", "~", "~/"}:
        file_objects = await FileObjectAdapters.aget_all_file_objects(user, limit=10000)
    else:
        file_objects = await FileObjectAdapters.aget_file_objects_by_path_prefix(user, normalized_path)
    files = [item.file_name for item in file_objects]
    if normalized_path:
        files = [name[len(normalized_path) :] for name in files]
    if pattern:
        files = [name for name in files if fnmatch.fnmatch(name, pattern)]
    count = len(files)
    if len(files) > 100:
        files = files[:100] + [f"... {len(files) - 100} more files found. Use a narrower pattern."]
    yield {
        "query": query(count),
        "file": normalized_path,
        "uri": normalized_path,
        "compiled": "\n- ".join(files) if files else "No files found.",
    }


async def view_workspace_headings(path: str, user: Any = None):
    query = f"View headings: {path}"
    if not get_workspace_sources().local_enabled:
        yield {"query": query, "file": path, "uri": path, "compiled": "Local knowledge base is not configured."}
        return
    try:
        result = kb_headings(path)
        compiled = "\n".join(
            f"{'#' * heading['level']} {heading['title']} (L{heading['start_line']}-L{heading['end_line']})"
            for heading in result.headings
        )
        yield {
            "query": query,
            "file": result.path,
            "uri": result.path,
            "compiled": compiled or "No Markdown headings found.",
        }
    except LocalKBError as error:
        yield {"query": query, "file": path, "uri": path, "compiled": str(error)}


async def resolve_workspace_link(from_path: str, link: str, user: Any = None):
    query = f"Resolve link: {link} from {from_path}"
    if not get_workspace_sources().local_enabled:
        yield {
            "query": query,
            "file": from_path,
            "uri": from_path,
            "compiled": "Local knowledge base is not configured.",
        }
        return
    result = kb_resolve_link(from_path, link)
    if result.status == "resolved":
        compiled = f"Resolved to {result.resolved}{f'#{result.anchor}' if result.anchor else ''}"
    elif result.status == "ambiguous":
        compiled = "Ambiguous link candidates:\n- " + "\n- ".join(result.candidates)
    else:
        compiled = f"Link {result.status}."
    yield {
        "query": query,
        "file": result.resolved or from_path,
        "uri": result.resolved or from_path,
        "compiled": compiled,
    }


class GroundingDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    grounded: bool
    reason: str


@dataclass(frozen=True)
class LocalSkill:
    name: str
    description: str
    path: Path


SOURCE_REF_SCHEMA = {
    "type": "array",
    "minItems": 1,
    "description": "Exact sources used to produce note content.",
    "items": {
        "oneOf": [
            {
                "type": "object",
                "properties": {"type": {"const": "current_user_request"}},
                "required": ["type"],
                "additionalProperties": False,
            },
            {
                "type": "object",
                "properties": {
                    "type": {"const": "artifact"},
                    "id": {"type": "string", "minLength": 1},
                },
                "required": ["type", "id"],
                "additionalProperties": False,
            },
            {
                "type": "object",
                "properties": {
                    "type": {"enum": ["assistant_message", "user_message"]},
                    "turn": {"type": "integer"},
                },
                "required": ["type", "turn"],
                "additionalProperties": False,
            },
            {
                "type": "object",
                "properties": {
                    "type": {"const": "file"},
                    "path": {"type": "string", "minLength": 1},
                    "start_line": {"type": "integer", "minimum": 1, "maximum": 1_000_000},
                    "end_line": {"type": "integer", "minimum": 1, "maximum": 1_000_000},
                },
                "required": ["type", "path"],
                "additionalProperties": False,
            },
            {
                "type": "object",
                "properties": {
                    "type": {"const": "tool_result"},
                    "tool": {"type": "string", "minLength": 1},
                },
                "required": ["type", "tool"],
                "additionalProperties": False,
            },
        ]
    },
}


WORKSPACE_PLANNER_INSTRUCTIONS = """
You collect evidence from the user's personal knowledge base for the main chat answer.

Use tools instead of guessing. Prefer this workflow:
1. list_files or regex_search_files to find candidate notes.
2. kb_headings to locate useful sections in large Markdown files.
3. view_file to read exact lines before relying on a note.
4. kb_resolve_link when a read note points to a related note.

Only exact view_file or OpenKB evidence becomes final references. Use append_note only when the
user clearly asks to create or append note content. Use propose_edit for replace/delete/overwrite requests.
When VaultActions are enabled, write tools only prepare reviewed actions; they do not apply them.
An explicit write request is sufficient to prepare an action. Do not ask for a second confirmation.
For append_note, always include write_intent and source_refs. Use
source_refs=[{"type":"current_user_request"}] when content comes directly from the current request.
When content exists in Recent conversation artifacts, prefer artifact_id or
source_refs=[{"type":"artifact","id":"..."}] over copying raw chat history.
If append_note reports source_refs_required or source_mismatch, retry with concrete, grounded sources.
For "add X to section Y in file Z", inspect file Z, then call append_note with path=Z and heading=Y.
Follow applicable project instructions, vault policy, and skills before preparing a write.
""".strip()


APPEND_NOTE_TOOL = ToolDefinition(
    name="append_note",
    description=(
        "Prepare a reviewed VaultAction to create or append grounded content in a local knowledge base file. "
        "This does not immediately write. A missing .md/.txt file under an existing folder becomes a create-only "
        "action, and the client review UI provides the later confirmation."
    ),
    schema={
        "type": "object",
        "properties": {
            "path": {"type": "string", "minLength": 1, "description": "Target local KB file path."},
            "artifact_id": {
                "type": "string",
                "description": "Conversation artifact id to append or adapt, e.g. assistant:<turnId>.",
            },
            "content": {"type": "string", "description": "Content to append. Optional when artifact_id is provided."},
            "heading": {"type": "string", "description": "Optional Markdown heading to append under."},
            "write_intent": {
                "type": "string",
                "enum": ["preserve", "summarize", "adapt_to_template", "merge"],
                "description": "How content transforms source_refs, e.g. preserve, summarize, adapt_to_template, merge.",
            },
            "source_refs": SOURCE_REF_SCHEMA,
        },
        "required": ["path", "write_intent", "source_refs"],
    },
)

PROPOSE_EDIT_TOOL = ToolDefinition(
    name="propose_edit",
    description="Prepare a local KB edit diff without modifying the file.",
    schema={
        "type": "object",
        "properties": {
            "path": {"type": "string", "minLength": 1, "description": "Target local KB file path."},
            "find": {"type": "string", "minLength": 1, "description": "Existing text to replace."},
            "replace": {"type": "string", "description": "Replacement text."},
            "reason": {"type": "string", "description": "Optional edit reason."},
            "source_refs": SOURCE_REF_SCHEMA,
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
            "query": {"type": "string", "minLength": 1, "description": "OpenKB wiki evidence query."},
            "n": {
                "type": "integer",
                "minimum": 1,
                "maximum": 10,
                "description": "Maximum number of references to return.",
            },
        },
        "required": ["query"],
    },
)


READ_SKILL_TOOL = ToolDefinition(
    name="read_skill",
    description="Read one local SKILL.md package by name before following its instructions.",
    schema={
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "minLength": 1,
                "description": "Skill name from the available skills catalog.",
            }
        },
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


def _vault_policy_prompt(vault_policy: Optional[dict[str, Any]]) -> str:
    if not vault_policy:
        return ""
    return f"\n\n## Vault policy\n\n{compact_policy_for_prompt(vault_policy)}"


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
    local_root = get_local_kb_root()
    return {
        "query": result.action,
        "file": result.path,
        "uri": f"local-kb://{result.path}",
        "kb_root": str(local_root) if local_root else None,
        "compiled": result.message,
        "action": result.action,
        "status": result.status,
        "changed": result.changed,
    }


def _validate_client_action_path(path: str) -> tuple[Path, str]:
    root = get_local_kb_root()
    if root is None:
        raise LocalKBError("Local knowledge base is not configured.", kind="not_configured")
    target = resolve_local_kb_path(path, root=root)
    if target.suffix.lower() not in {".md", ".txt"}:
        raise LocalKBError(f"File '{path}' is not a supported local knowledge base text file.")
    relpath = local_kb_relative_path(target, root=root)
    return target, relpath


def _client_vault_action_reference(tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
    target, relpath = _validate_client_action_path(str(args.get("path") or ""))
    if tool_name == "append_note":
        content = str(args.get("content") or "")
        op = "append_file" if target.exists() else "create_file"
        vault_action = {
            "op": op,
            "path": relpath,
            "content": content,
            "mode": "append" if op == "append_file" else "create_only",
        }
        if op == "append_file" and (heading := str(args.get("heading") or "").strip()):
            vault_action["heading"] = heading
        message = f"Prepared client vault action {op} for {relpath}."
    elif tool_name == "propose_edit":
        vault_action = {
            "op": "replace_text",
            "path": relpath,
            "find": str(args.get("find") or ""),
            "replace": str(args.get("replace") or ""),
            "mode": "replace",
        }
        if reason := str(args.get("reason") or "").strip():
            vault_action["reason"] = reason
        message = f"Prepared client vault action replace_text for {relpath}."
    else:
        raise LocalKBError(f"Unsupported client vault action: {tool_name}")

    return {
        "query": "vault_action",
        "file": relpath,
        "uri": f"local-kb://{relpath}",
        "kb_root": str(get_local_kb_root()) if get_local_kb_root() else None,
        "compiled": message,
        "action": tool_name,
        "status": "action_prepared",
        "changed": False,
        "vault_action": vault_action,
    }


def _tool_result_text(value: Any, limit: int = 8000) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    if len(text) <= limit:
        return text
    preview_chars = max(1, (limit - 80) // 2)
    return f"{text[:preview_chars]}\n...[tool result truncated]...\n{text[-preview_chars:]}"


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
    value = (
        (message.get("by") or message.get("role"))
        if isinstance(message, dict)
        else (getattr(message, "by", None) or getattr(message, "role", ""))
    )
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


def _source_ref_text(ref: dict[str, Any], query: str, chat_history: list, tool_transcript: list[dict[str, Any]]) -> str:
    ref_type = str(ref.get("type") or "")
    if ref_type == "current_user_request":
        return query
    if ref_type == "artifact":
        artifact = _find_artifact(chat_history, str(ref.get("id") or ""))
        return str(artifact.get("content") or "") if artifact else ""
    if ref_type == "assistant_message":
        messages = [item for item in chat_history if _message_by(item) not in {"you", "user"}]
        selected = _select_turn(messages, ref.get("turn", -1))
        return _plain_message_text(selected) if selected is not None else ""
    if ref_type == "user_message":
        messages = [item for item in chat_history if _message_by(item) in {"you", "user"}]
        selected = _select_turn(messages, ref.get("turn", -1))
        return _plain_message_text(selected) if selected is not None else ""
    if ref_type == "file":
        item = kb_read(
            ref.get("path") or "", start_line=ref.get("start_line"), end_line=ref.get("end_line"), max_lines=200
        )
        return item.text
    if ref_type == "tool_result":
        tool_name = str(ref.get("tool") or "")
        for item in reversed(tool_transcript):
            if item.get("tool") != tool_name:
                continue
            return str(item.get("result") or "")
    return ""


def _tool_result_value(item: dict[str, Any]) -> Any:
    value = item.get("result")
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def _edit_source_error(
    query: str, args: dict[str, Any], chat_history: list, tool_transcript: list[dict[str, Any]]
) -> str:
    find = str(args.get("find") or "").strip()
    path = str(args.get("path") or "").strip()
    if not find:
        return ""

    refs = args.get("source_refs")
    if isinstance(refs, list) and refs:
        source_text = "\n\n".join(
            _source_ref_text(ref, query, chat_history, tool_transcript) for ref in refs if isinstance(ref, dict)
        )
        if find in source_text:
            return ""
        return "edit_source_required: Could not find the proposed edit text in source_refs. Read the target file and retry."

    for item in reversed(tool_transcript):
        if item.get("tool") != AgentToolName.ViewFile.value:
            continue
        item_args = item.get("args") if isinstance(item.get("args"), dict) else {}
        if path and str(item_args.get("path") or "").strip() != path:
            continue
        result = _tool_result_value(item)
        text = result.get("text") if isinstance(result, dict) else str(result or "")
        if find in str(text or ""):
            return ""
    return "edit_source_required: Read the target file with view_file before calling propose_edit."


def _source_refs_required_error(args: dict[str, Any]) -> str:
    refs = args.get("source_refs")
    if isinstance(refs, list) and refs:
        return ""
    return "source_refs_required: append_note requires at least one concrete source_ref."


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
    source_texts = [
        _source_ref_text(ref, query, chat_history, tool_transcript) for ref in refs if isinstance(ref, dict)
    ]
    source_text = "\n\n".join(text for text in source_texts if text.strip())
    if not source_text.strip():
        return "Could not resolve source_refs. Retry with concrete assistant_message, user_message, file, or tool_result refs."

    content = str(args.get("content") or "")
    if content.strip() and any(content.strip() == text.strip() for text in source_texts):
        return ""

    if not content.strip():
        return ""

    prompt = (
        "Check whether this append_note content is grounded in the declared sources.\n"
        'Return only JSON: {"grounded": true|false, "reason": "short reason"}.\n'
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
        response_schema=GroundingDecision,
        deepthought=False,
        fast_model=False,
    )
    try:
        verdict = GroundingDecision.model_validate(json.loads(getattr(response, "text", "") or ""))
    except (json.JSONDecodeError, TypeError, ValueError, ValidationError):
        return "source_mismatch: grounding verifier returned an invalid structured verdict."
    if not verdict.grounded:
        return f"source_mismatch: grounding verifier rejected append content. {verdict.reason.strip()}"
    return ""


def _notes_tools(*, allow_local_kb: bool, allow_openkb: bool, allow_skills: bool) -> list[ToolDefinition]:
    tools: list[ToolDefinition] = []
    if allow_local_kb:
        tools.extend(
            [
                agent_tool_definitions[AgentToolName.ListFiles],
                agent_tool_definitions[AgentToolName.RegexSearchFiles],
                agent_tool_definitions[AgentToolName.KbHeadings],
                agent_tool_definitions[AgentToolName.ViewFile],
                agent_tool_definitions[AgentToolName.KbResolveLink],
                APPEND_NOTE_TOOL,
                PROPOSE_EDIT_TOOL,
            ]
        )
    if allow_skills:
        tools.append(READ_SKILL_TOOL)
    if allow_openkb:
        tools.append(OPENKB_TOOL)
    return tools


def available_workspace_tools(*, allow_local_kb: bool, allow_openkb: bool) -> list[ToolDefinition]:
    local_root = get_local_kb_root() if allow_local_kb else None
    return _notes_tools(
        allow_local_kb=local_root is not None,
        allow_openkb=allow_openkb,
        allow_skills=bool(_scan_local_skills(local_root)) if local_root else False,
    )


def workspace_planner_context(*, allow_local_kb: bool, vault_policy: Optional[dict[str, Any]] = None) -> str:
    local_root = get_local_kb_root() if allow_local_kb else None
    skills = _scan_local_skills(local_root) if local_root else []
    return (
        WORKSPACE_PLANNER_INSTRUCTIONS
        + _vault_policy_prompt(vault_policy)
        + _local_profile_prompt(local_root)
        + _skill_prompt(skills)
    )


async def execute_workspace_tool_calls(
    query: str,
    chat_history: list,
    user: Any,
    agent: Any,
    calls: list[ToolCall],
    *,
    send_message: Callable[..., Awaitable[Any]],
    send_status: Optional[Callable[[str], Any]] = None,
    client_app: Any = None,
    allow_local_kb: bool = True,
    allow_openkb: bool = False,
    conversation_id: str = "notes-tool-loop",
    max_evidence_chars: int = 16000,
    initial_tool_transcript: Optional[list[dict[str, Any]]] = None,
    write_mode: str = "disabled",
) -> WorkspaceToolResult:
    result = WorkspaceToolResult()
    references: list[dict[str, Any]] = []
    evidence_chars = 0
    read_keys: set[tuple[str, int, int, str]] = set()
    local_root = get_local_kb_root()
    local_kb_allowed = allow_local_kb and local_root is not None
    local_skills = _scan_local_skills(local_root) if local_kb_allowed else []
    tools = available_workspace_tools(allow_local_kb=local_kb_allowed, allow_openkb=allow_openkb)
    if write_mode != "client_actions":
        tools = [tool for tool in tools if tool.name not in {"append_note", "propose_edit"}]
    if not tools:
        result.errors.append("No Notes evidence tools are available.")
        return result
    tool_by_name = {tool.name: tool for tool in tools}
    skill_index = {skill.name: skill for skill in local_skills}
    initial_transcript_size = len(initial_tool_transcript or [])
    tool_transcript: list[dict[str, Any]] = list(initial_tool_transcript or [])
    await _send_status(send_status, "Executing Notes tools")

    async def finish_result() -> WorkspaceToolResult:
        result.references = references
        result.tool_transcript = tool_transcript[initial_transcript_size:]
        result.inferred_queries = list(
            dict.fromkeys(result.inferred_queries + [ref.get("query", "") for ref in references] + result.searched)
        )
        if result.references:
            await _send_status(send_status, f"Found {len(result.references)} Notes references")
        else:
            await _send_status(send_status, "No Notes evidence found")
        return result

    async def execute_tool(call: ToolCall) -> Any:
        nonlocal evidence_chars
        args = call.args or {}
        result.searched.append(f"{call.name} {json.dumps(args, ensure_ascii=False, default=str)}")
        tool = tool_by_name.get(call.name)
        if tool is None:
            message = f"Notes tool is not available: {call.name}"
            result.errors.append(message)
            return {"error": message}

        try:
            if call.name == AgentToolName.ListFiles.value:
                listing = kb_list(args.get("path"), args.get("pattern"), limit=80)
                return {
                    "path": listing.path,
                    "items": listing.items,
                    "total": listing.total,
                    "truncated": listing.truncated,
                }
            if call.name == AgentToolName.RegexSearchFiles.value:
                grep = kb_grep(
                    args.get("regex_pattern") or "",
                    path_prefix=args.get("path_prefix"),
                    mode="regex",
                    before=_as_int(args.get("lines_before"), 0, 0, 20),
                    after=_as_int(args.get("lines_after"), 0, 0, 20),
                    max_results=80,
                )
                return {
                    "line_count": grep.line_count,
                    "document_count": grep.document_count,
                    "lines": grep.lines,
                    "matches": grep.matches,
                    "truncated": grep.truncated,
                }
            if call.name == AgentToolName.KbHeadings.value:
                headings = kb_headings(args.get("path") or "")
                return {
                    "path": headings.path,
                    "headings": headings.headings,
                    "total_lines": headings.total_lines,
                }
            if call.name == AgentToolName.KbResolveLink.value:
                resolved = kb_resolve_link(args.get("from_path") or "", args.get("link") or "")
                return {
                    "link": resolved.link,
                    "status": resolved.status,
                    "resolved": resolved.resolved,
                    "anchor": resolved.anchor,
                    "candidates": resolved.candidates,
                }
            if call.name == AgentToolName.ViewFile.value:
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
                        isinstance(ref, dict) and ref.get("type") == "artifact" and ref.get("id") == artifact_id
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
                source_refs_error = _source_refs_required_error(args)
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
                source_error = await _source_bound_append_error(
                    query, args, chat_history, tool_transcript, send_message
                )
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
                action_ref = _client_vault_action_reference("append_note", args)
                references.append(action_ref)
                return {"status": "action_prepared", "vault_action": action_ref["vault_action"]}
            if call.name == "propose_edit":
                edit_source_error = _edit_source_error(query, args, chat_history, tool_transcript)
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
                action_ref = _client_vault_action_reference("propose_edit", args)
                references.append(action_ref)
                return {"status": "action_prepared", "vault_action": action_ref["vault_action"]}
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

    for call in calls:
        await _send_status(send_status, f"Using Notes tool: {call.name}")
        tool_output = await execute_tool(call)
        tool_transcript.append({"tool": call.name, "args": call.args, "result": _tool_result_text(tool_output)})
    return await finish_result()
