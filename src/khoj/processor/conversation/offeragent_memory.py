import os
import re
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Optional
from uuid import uuid4

if os.name == "nt":
    import msvcrt
else:
    import fcntl

from pydantic import BaseModel, ConfigDict, Field

from khoj.database.models import KhojUser
from khoj.utils.local_kb import get_local_kb_root

MEMORY_TYPES = {"user", "feedback", "project"}
INDEX_NAME = "MEMORY.md"
MAX_RELEVANT_MEMORIES = 3
_memory_write_lock = threading.Lock()

MEMORY_POLICY = """
OfferAgent has a persistent, local, file-based memory system.

Memory types:
- user: durable information about the user's role, goals, responsibilities, preferences, or knowledge.
- feedback: durable guidance from the user about how OfferAgent should behave, including corrections and confirmed preferences.
- project: durable project context, decisions, constraints, goals, deadlines, and rationale that are not derivable from current files.

Do not save:
- file paths, folder locations, or pointers to local resources
- daily notes, file summaries, tool results, search results, reports, or one-off evaluations
- temporary task state or current conversation progress
- anything already available from the local vault/index/grep

If current files or tool output are relevant, those are authoritative. Memory is only for durable context that should affect future conversations.
""".strip()


class MemorySelection(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    selected_memories: list[str] = Field(
        default_factory=list,
        description="Memory ids from the manifest to inject into this turn. Empty means inject no memory.",
    )


class MemoryWriteDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    action: Literal["create", "none"] = Field(description="Whether to create one memory from the latest user message.")
    memory_type: Optional[Literal["user", "feedback", "project"]] = Field(default=None)
    raw: Optional[str] = Field(default=None, description="Atomic memory content to save.")
    description: Optional[str] = Field(default=None, description="Short hook used in the memory index.")


@dataclass(frozen=True)
class OfferAgentMemory:
    id: str
    raw: str
    memory_type: str
    description: str
    created_at: datetime
    updated_at: datetime
    path: Path


def get_memory_root(user: KhojUser) -> Path:
    user_id = getattr(user, "uuid", None)
    if not user_id:
        raise ValueError("A persisted user is required for memory access")
    local_root = get_local_kb_root()
    if local_root:
        base = local_root / ".offeragent" / "memory"
    else:
        base = Path.home() / ".offeragent" / "memory"
    return base / str(user_id)


def is_supported_memory_type(memory_type: str) -> bool:
    return memory_type in MEMORY_TYPES


def list_memories(user: KhojUser) -> list[OfferAgentMemory]:
    root = get_memory_root(user)
    if not root.exists():
        return []
    memories = [_read_memory(path) for path in root.glob("*.md") if path.name != INDEX_NAME]
    return sorted(
        [memory for memory in memories if memory],
        key=lambda memory: (memory.updated_at, memory.id),
        reverse=True,
    )


def get_memory_by_id(user: KhojUser, memory_id: str) -> Optional[OfferAgentMemory]:
    path = _memory_path(user, memory_id)
    if not path.exists():
        return None
    return _read_memory(path)


def create_memory(
    user: KhojUser,
    raw: str,
    memory_type: str,
    *,
    description: Optional[str] = None,
    name: Optional[str] = None,
    source_turn_id: Optional[str] = None,
) -> OfferAgentMemory:
    if memory_type not in MEMORY_TYPES:
        raise ValueError(f"Unsupported memory type: {memory_type}")
    raw = raw.strip()
    if not raw:
        raise ValueError("Memory content cannot be empty")

    with _memory_write_lock:
        root = get_memory_root(user)
        root.mkdir(parents=True, exist_ok=True)
        with _memory_root_lock(root):
            now = _now()
            description = _one_line(description or raw)
            name = _one_line(name or description)
            while True:
                path = _unique_memory_path(root, name or raw)
                try:
                    _write_memory(
                        path,
                        raw,
                        memory_type,
                        name,
                        description,
                        now,
                        now,
                        source_turn_id,
                        exclusive=True,
                    )
                    break
                except FileExistsError:
                    continue
            _write_index(user)
            memory = _read_memory(path)
            if memory is None:
                raise RuntimeError(f"Failed to read newly created memory: {path.name}")
            return memory


def update_memory(user: KhojUser, memory_id: str, raw: str) -> Optional[OfferAgentMemory]:
    raw = raw.strip()
    if not raw:
        raise ValueError("Memory content cannot be empty")

    with _memory_write_lock:
        root = get_memory_root(user)
        root.mkdir(parents=True, exist_ok=True)
        with _memory_root_lock(root):
            memory = get_memory_by_id(user, memory_id)
            if not memory:
                return None
            _write_memory(
                memory.path,
                raw,
                memory.memory_type,
                memory.description,
                _one_line(raw),
                memory.created_at,
                _now(),
                None,
            )
            _write_index(user)
            return get_memory_by_id(user, memory_id)


def delete_memory(user: KhojUser, memory_id: str) -> bool:
    with _memory_write_lock:
        root = get_memory_root(user)
        root.mkdir(parents=True, exist_ok=True)
        with _memory_root_lock(root):
            path = _memory_path(user, memory_id)
            if not path.exists():
                return False
            path.unlink()
            _write_index(user)
            return True


def build_memory_selection_prompt(query: str, memories: list[OfferAgentMemory]) -> str:
    if not query.strip() or not memories:
        return ""

    return f"""
{MEMORY_POLICY}

You are selecting OfferAgent memories for the next response.

Rules:
- Select at most {MAX_RELEVANT_MEMORIES} memories.
- Select only memories that are clearly useful for answering the latest user message.
- If the latest user message is casual small talk, return an empty list.
- If the latest user message says to ignore, skip, disable, or not use memory, return an empty list.
- If you are unsure whether a memory helps, do not select it.

Latest user message:
{query}

Available memories:
{format_memory_manifest(memories)}
""".strip()


def select_memories_from_decision(
    decision: MemorySelection,
    memories: list[OfferAgentMemory],
    *,
    limit: int = MAX_RELEVANT_MEMORIES,
) -> list[OfferAgentMemory]:
    by_id = {memory.id: memory for memory in memories}
    selected: list[OfferAgentMemory] = []
    for memory_id in decision.selected_memories:
        memory = by_id.get(memory_id)
        if memory and memory not in selected:
            selected.append(memory)
        if len(selected) >= limit:
            break
    return selected


def build_memory_write_prompt(
    latest_user_message: str,
    memories: list[OfferAgentMemory],
    *,
    current_date: str,
    used_workspace_tools: bool,
) -> str:
    return f"""
{MEMORY_POLICY}

You are deciding whether OfferAgent should create exactly one durable memory from the latest user message.

Rules:
- Use only the latest user message, not the assistant's answer.
- Create a memory only when the user clearly gives durable information or durable behavioral guidance.
- If local notes/files/tools were used this turn, do not save conclusions from that evidence.
- If the user asks to remember a daily summary, file-derived evaluation, report, folder path, or search result, return action "none".
- For project memories, convert relative dates to absolute dates using current_date.
- Return action "none" unless the memory would be useful in future conversations.

current_date: {current_date}
local_notes_or_tools_used: {used_workspace_tools}

Existing memory manifest:
{format_memory_manifest(memories)}

Latest user message:
{latest_user_message}
""".strip()


def apply_memory_write_decision(
    user: KhojUser,
    decision: MemoryWriteDecision,
    *,
    source_turn_id: Optional[str] = None,
) -> Optional[OfferAgentMemory]:
    if decision.action != "create":
        return None
    if decision.memory_type not in MEMORY_TYPES:
        return None
    raw = (decision.raw or "").strip()
    if not raw:
        return None
    return create_memory(
        user,
        raw,
        decision.memory_type,
        description=_one_line(decision.description or raw),
        source_turn_id=source_turn_id,
    )


def format_memories_for_system(memories: list[OfferAgentMemory]) -> str:
    if not memories:
        return ""

    lines = [
        "OfferAgent retrieved these local long-term memories. Use them only when directly relevant; current files, tools, and the user's latest message override memory.",
        "<offeragent_memories>",
    ]
    for memory in memories:
        created = memory.created_at.strftime("%Y-%m-%d %H:%M:%S")
        lines.append(f"- [{created}][{memory.memory_type}] {memory.raw}")
    lines.append("</offeragent_memories>")
    return "\n".join(lines)


def format_memory_manifest(memories: list[OfferAgentMemory]) -> str:
    if not memories:
        return "(none)"
    return "\n".join(
        f"- {memory.id} [{memory.memory_type}] {memory.updated_at.isoformat()}: {memory.description or memory.raw}"
        for memory in memories
    )


def _memory_path(user: KhojUser, memory_id: str) -> Path:
    if "/" in memory_id or "\\" in memory_id or memory_id in {"", ".", "..", INDEX_NAME}:
        raise ValueError("Invalid memory id")
    path = (get_memory_root(user) / memory_id).resolve(strict=False)
    root = get_memory_root(user).resolve(strict=False)
    if not path.is_relative_to(root):
        raise ValueError("Invalid memory id")
    return path


def _read_memory(path: Path) -> Optional[OfferAgentMemory]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None

    match = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.S)
    if not match:
        return None

    frontmatter = _parse_frontmatter(match.group(1))
    memory_type = frontmatter.get("type", "")
    if memory_type not in MEMORY_TYPES:
        return None

    created_at = _parse_datetime(frontmatter.get("created_at")) or _datetime_from_mtime(path)
    updated_at = _parse_datetime(frontmatter.get("updated_at")) or _datetime_from_mtime(path)
    return OfferAgentMemory(
        id=path.name,
        raw=match.group(2).strip(),
        memory_type=memory_type,
        description=frontmatter.get("description", ""),
        created_at=created_at,
        updated_at=updated_at,
        path=path,
    )


def _write_memory(
    path: Path,
    raw: str,
    memory_type: str,
    name: str,
    description: str,
    created_at: datetime,
    updated_at: datetime,
    source_turn_id: Optional[str],
    *,
    exclusive: bool = False,
) -> None:
    fields = {
        "name": _frontmatter_value(name),
        "description": _frontmatter_value(description),
        "type": memory_type,
        "created_at": created_at.isoformat(),
        "updated_at": updated_at.isoformat(),
    }
    if source_turn_id:
        fields["source_turn_id"] = _frontmatter_value(source_turn_id)
    frontmatter = "\n".join(f"{key}: {value}" for key, value in fields.items())
    _write_text_atomic(path, f"---\n{frontmatter}\n---\n{raw.strip()}\n", exclusive=exclusive)


def _write_index(user: KhojUser) -> None:
    root = get_memory_root(user)
    root.mkdir(parents=True, exist_ok=True)
    lines = [
        f"- [{memory.description or memory.id}]({memory.id}) - {memory.memory_type}" for memory in list_memories(user)
    ]
    _write_text_atomic(root / INDEX_NAME, "\n".join(lines) + ("\n" if lines else ""))


def _write_text_atomic(path: Path, text: str, *, exclusive: bool = False) -> None:
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_text(text, encoding="utf-8")
        if exclusive:
            os.link(temporary, path)
        else:
            temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def _memory_root_lock(root: Path):
    lock_path = root / ".memory.lock"
    with lock_path.open("a+b") as lock_file:
        if os.name == "nt":
            lock_file.seek(0)
            if lock_file.read(1) == b"":
                lock_file.write(b"\0")
                lock_file.flush()
            lock_file.seek(0)
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
        else:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if os.name == "nt":
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _parse_frontmatter(text: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        fields[key.strip()] = value.strip().strip('"')
    return fields


def _parse_datetime(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _datetime_from_mtime(path: Path) -> datetime:
    return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _one_line(text: str, limit: int = 160) -> str:
    return re.sub(r"\s+", " ", text.strip())[:limit]


def _frontmatter_value(text: str) -> str:
    return _one_line(text).replace('"', "'")


def _unique_memory_path(root: Path, text: str) -> Path:
    stem = re.sub(r"[^a-zA-Z0-9_-]+", "-", text.lower()).strip("-")[:48] or "memory"
    path = root / f"{stem}.md"
    if not path.exists():
        return path
    return root / f"{stem}-{uuid4().hex[:12]}.md"
