import fnmatch
import hashlib
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Pattern
from urllib.parse import unquote

logger = logging.getLogger(__name__)

LOCAL_KB_ENV = "KHOJ_LOCAL_KB_PATH"
OBSIDIAN_VAULT_ENV = "KHOJ_OBSIDIAN_VAULT_PATH"
ALLOWED_SUFFIXES = {".md", ".txt"}
ROOT_ALIASES = {"", "/", ".", "./", "~", "~/"}


def _is_hidden_part(part: str) -> bool:
    return part.startswith(".") and part not in {".", ".."}


class LocalKBError(ValueError):
    def __init__(self, message: str, *, kind: str = "blocked"):
        super().__init__(message)
        self.kind = kind


@dataclass(frozen=True)
class LocalKBReadResult:
    path: str
    text: str
    start_line: int = 1
    end_line: int = 0
    total_lines: int = 0
    mtime: float = 0
    checksum: str = ""
    truncated: bool = False
    lines: List[Dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class LocalKBGrepResult:
    line_count: int
    document_count: int
    lines: List[str]
    matches: List[Dict[str, Any]] = field(default_factory=list)
    truncated: bool = False


@dataclass(frozen=True)
class LocalKBHeadingResult:
    path: str
    headings: List[Dict[str, Any]]
    total_lines: int


@dataclass(frozen=True)
class LocalKBListResult:
    path: str
    items: List[Dict[str, Any]]
    total: int
    truncated: bool


@dataclass(frozen=True)
class LocalKBResolveResult:
    link: str
    status: str
    resolved: Optional[str] = None
    anchor: Optional[str] = None
    candidates: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class LocalKBWriteResult:
    action: str
    path: str
    status: str
    changed: bool
    message: str


def get_local_kb_root() -> Optional[Path]:
    raw_root = os.getenv(LOCAL_KB_ENV) or os.getenv(OBSIDIAN_VAULT_ENV)
    if not raw_root:
        return None

    root = Path(raw_root).expanduser().resolve()
    if not root.is_dir():
        logger.warning("Local KB path is not a directory: %s", root)
        return None
    return root


def resolve_local_kb_path(path: Optional[str] = None, *, root: Optional[Path] = None) -> Path:
    root = root or get_local_kb_root()
    if root is None:
        raise LocalKBError("Local knowledge base is not configured.", kind="not_configured")

    value = (path or "").strip()
    if value in ROOT_ALIASES:
        candidate = root
        resolved = root
    else:
        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            candidate = root / candidate
        resolved = candidate.resolve(strict=False)

    try:
        requested_parts = candidate.relative_to(root).parts
    except ValueError:
        requested_parts = ()
    if any(_is_hidden_part(part) for part in requested_parts):
        raise LocalKBError(f"Path '{path}' is hidden and cannot be read.")

    if not resolved.is_relative_to(root):
        raise LocalKBError(f"Path '{path}' is outside the local knowledge base.")

    rel_parts = resolved.relative_to(root).parts
    if any(_is_hidden_part(part) for part in rel_parts):
        raise LocalKBError(f"Path '{path}' is hidden and cannot be read.")

    return resolved


def local_kb_relative_path(path: Path, *, root: Optional[Path] = None) -> str:
    root = root or get_local_kb_root()
    if root is None:
        raise LocalKBError("Local knowledge base is not configured.", kind="not_configured")
    return path.resolve(strict=False).relative_to(root).as_posix()


def _allowed_file(path: Path, root: Path) -> bool:
    resolved = path.resolve(strict=False)
    if resolved.suffix.lower() not in ALLOWED_SUFFIXES:
        return False
    try:
        path_parts = path.relative_to(root).parts
        resolved_parts = resolved.relative_to(root).parts
    except ValueError:
        return False
    return not any(_is_hidden_part(part) for part in (*path_parts, *resolved_parts))


def _allowed_directory(path: Path, root: Path) -> bool:
    try:
        path_parts = path.relative_to(root).parts
        resolved_parts = path.resolve(strict=False).relative_to(root).parts
    except ValueError:
        return False
    return not any(_is_hidden_part(part) for part in (*path_parts, *resolved_parts))


def _iter_allowed_files(path: Optional[str] = None, *, root: Optional[Path] = None) -> List[Path]:
    root = root or get_local_kb_root()
    if root is None:
        raise LocalKBError("Local knowledge base is not configured.", kind="not_configured")

    target = resolve_local_kb_path(path, root=root)
    if not target.exists():
        raise LocalKBError(f"Path '{path}' not found in local knowledge base.", kind="not_found")

    if target.is_file():
        if not _allowed_file(target, root):
            raise LocalKBError(f"File '{path}' is not a supported local knowledge base text file.")
        return [target]

    if not target.is_dir():
        raise LocalKBError(f"Path '{path}' is not a file or directory.", kind="not_found")

    return sorted(p for p in target.rglob("*") if p.is_file() and _allowed_file(p, root))


def _read_lines(path: Path) -> List[str]:
    return path.read_text(encoding="utf-8", errors="replace").splitlines()


def _range_checksum(lines: List[str]) -> str:
    digest = hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()[:16]
    return f"sha256:{digest}"


def kb_list(
    path: Optional[str] = None,
    pattern: Optional[str] = None,
    *,
    depth: int = 2,
    limit: int = 200,
) -> LocalKBListResult:
    root = get_local_kb_root()
    target = resolve_local_kb_path(path, root=root)
    if not target.exists():
        raise LocalKBError(f"Path '{path}' not found in local knowledge base.", kind="not_found")

    depth = max(0, min(depth, 5))
    limit = max(1, min(limit, 500))
    base_rel = "" if target == root else local_kb_relative_path(target, root=root)

    def include(relpath: str) -> bool:
        return not pattern or fnmatch.fnmatch(relpath, pattern) or fnmatch.fnmatch(Path(relpath).name, pattern)

    candidates: List[Path] = []
    if target.is_file():
        if not _allowed_file(target, root):
            raise LocalKBError(f"File '{path}' is not a supported local knowledge base text file.")
        candidates = [target]
    elif target.is_dir():
        for item in target.rglob("*"):
            try:
                rel_depth = len(item.relative_to(target).parts)
            except ValueError:
                continue
            if rel_depth > depth:
                continue
            if item.is_dir():
                if _allowed_directory(item, root):
                    candidates.append(item)
            elif item.is_file() and _allowed_file(item, root):
                candidates.append(item)
    else:
        raise LocalKBError(f"Path '{path}' is not a file or directory.", kind="not_found")

    items: List[Dict[str, Any]] = []
    for item in sorted(candidates, key=lambda p: (not p.is_dir(), local_kb_relative_path(p, root=root).lower())):
        relpath = local_kb_relative_path(item, root=root)
        if not include(relpath):
            continue
        entry: Dict[str, Any] = {"path": relpath, "type": "directory" if item.is_dir() else "file"}
        if item.is_file():
            stat = item.stat()
            entry["size"] = stat.st_size
            entry["mtime"] = stat.st_mtime
        items.append(entry)

    return LocalKBListResult(path=base_rel, items=items[:limit], total=len(items), truncated=len(items) > limit)


def kb_read(
    path: str,
    *,
    start_line: Optional[int] = None,
    end_line: Optional[int] = None,
    max_lines: int = 160,
) -> LocalKBReadResult:
    root = get_local_kb_root()
    target = resolve_local_kb_path(path, root=root)
    if not target.exists():
        raise LocalKBError(f"File '{path}' not found in local knowledge base.", kind="not_found")
    if not target.is_file() or not _allowed_file(target, root):
        raise LocalKBError(f"File '{path}' is not a supported local knowledge base text file.")

    lines = _read_lines(target)
    total_lines = len(lines)
    if total_lines == 0:
        relpath = local_kb_relative_path(target, root=root)
        return LocalKBReadResult(
            path=relpath,
            text="",
            start_line=0,
            end_line=0,
            total_lines=0,
            mtime=target.stat().st_mtime,
            checksum=_range_checksum([]),
            lines=[],
        )

    start_line = start_line or 1
    end_line = end_line or total_lines
    max_lines = max(1, min(max_lines, 200))

    if start_line < 1 or end_line < 1 or start_line > end_line:
        raise LocalKBError(f"Invalid line range: {start_line}-{end_line}")
    if start_line > total_lines:
        raise LocalKBError(f"Start line {start_line} exceeds total number of lines {total_lines}")

    start_idx = start_line - 1
    requested_end_idx = min(total_lines, end_line)
    end_idx = requested_end_idx
    truncated = False
    if end_idx - start_idx > max_lines:
        end_idx = start_idx + max_lines
        truncated = True

    selected = lines[start_idx:end_idx]
    numbered_lines = [{"line": idx + 1, "text": line} for idx, line in enumerate(selected, start_idx)]
    text = "\n".join(f"{item['line']}: {item['text']}" for item in numbered_lines)
    if truncated:
        text += f"\n\n[Truncated after {max_lines} lines! Use narrower line range to view complete section.]"

    relpath = local_kb_relative_path(target, root=root)
    return LocalKBReadResult(
        path=relpath,
        text=text,
        start_line=start_line,
        end_line=end_idx,
        total_lines=total_lines,
        mtime=target.stat().st_mtime,
        checksum=_range_checksum(selected),
        truncated=truncated,
        lines=numbered_lines,
    )


def kb_grep(
    query: str,
    *,
    path_prefix: Optional[str] = None,
    mode: str = "literal",
    before: int = 1,
    after: int = 2,
    max_results: int = 80,
) -> LocalKBGrepResult:
    if not query:
        return LocalKBGrepResult(line_count=0, document_count=0, lines=[])
    if mode not in {"literal", "regex"}:
        raise LocalKBError(f"Unsupported grep mode: {mode}")
    if mode == "regex" and len(query) > 200:
        raise LocalKBError("Regex pattern is too long.")

    pattern = re.escape(query) if mode == "literal" else query
    try:
        regex = re.compile(pattern, re.IGNORECASE | re.MULTILINE)
    except re.error as e:
        raise LocalKBError(f"Invalid regex pattern: {e}") from e

    return _grep_local_kb_regex(
        regex,
        path_prefix=path_prefix,
        lines_before=max(0, min(before, 20)),
        lines_after=max(0, min(after, 20)),
        max_results=max(1, min(max_results, 200)),
        started_at=time.monotonic(),
    )


def _grep_local_kb_regex(
    regex: Pattern[str],
    *,
    path_prefix: Optional[str] = None,
    lines_before: int = 0,
    lines_after: int = 0,
    max_results: int = 1000,
    started_at: Optional[float] = None,
) -> LocalKBGrepResult:
    root = get_local_kb_root()
    files = _iter_allowed_files(path_prefix, root=root)
    line_matches: List[str] = []
    matches: List[Dict[str, Any]] = []
    line_count = 0
    matched_documents = 0
    scanned_files = 0
    scanned_bytes = 0
    truncated = False
    started_at = started_at or time.monotonic()

    for file_path in files:
        scanned_files += 1
        if scanned_files > 2000 or scanned_bytes > 25 * 1024 * 1024 or time.monotonic() - started_at > 2:
            truncated = True
            break
        relpath = local_kb_relative_path(file_path, root=root)
        scanned_bytes += file_path.stat().st_size
        lines = _read_lines(file_path)
        matched_line_numbers = [idx for idx, line in enumerate(lines, 1) if regex.search(line)]
        if not matched_line_numbers:
            continue

        matched_documents += 1
        line_count += len(matched_line_numbers)

        for line_num in matched_line_numbers:
            start_idx = max(0, line_num - 1 - lines_before)
            end_idx = min(len(lines), line_num + lines_after)
            before_lines = [{"line": idx + 1, "text": lines[idx]} for idx in range(start_idx, line_num - 1)]
            after_lines = [{"line": idx + 1, "text": lines[idx]} for idx in range(line_num, end_idx)]
            matches.append(
                {
                    "path": relpath,
                    "line": line_num,
                    "text": lines[line_num - 1],
                    "before": before_lines,
                    "after": after_lines,
                }
            )
            for idx in range(start_idx, end_idx):
                current_line_num = idx + 1
                line_content = lines[idx]
                if current_line_num == line_num:
                    line_matches.append(f"{relpath}:{current_line_num}: {line_content}")
                else:
                    line_matches.append(f"{relpath}-{current_line_num}-  {line_content}")
            if lines_before > 0 or lines_after > 0:
                line_matches.append("--")

            if len(matches) >= max_results:
                truncated = True
                break
        if len(matches) >= max_results:
            break

    if line_matches and line_matches[-1] == "--":
        line_matches.pop()
    if truncated:
        line_matches = line_matches[:max_results] + [
            "... results truncated. Use stricter query or path to narrow down results."
        ]

    return LocalKBGrepResult(
        line_count=line_count,
        document_count=matched_documents,
        lines=line_matches,
        matches=matches[:max_results],
        truncated=truncated,
    )


def kb_headings(path: str) -> LocalKBHeadingResult:
    root = get_local_kb_root()
    target = resolve_local_kb_path(path, root=root)
    if not target.exists():
        raise LocalKBError(f"File '{path}' not found in local knowledge base.", kind="not_found")
    if not target.is_file() or not _allowed_file(target, root):
        raise LocalKBError(f"File '{path}' is not a supported local knowledge base text file.")

    lines = _read_lines(target)
    headings: List[Dict[str, Any]] = []
    in_fence = False
    fence_marker = ""
    for idx, line in enumerate(lines, 1):
        stripped = line.strip()
        if stripped.startswith(("```", "~~~")):
            marker = stripped[:3]
            if not in_fence:
                in_fence = True
                fence_marker = marker
            elif marker == fence_marker:
                in_fence = False
            continue
        if in_fence:
            continue
        match = re.match(r"^(#{1,6})\s+(.+?)\s*#*\s*$", line)
        if match:
            headings.append(
                {
                    "level": len(match.group(1)),
                    "title": match.group(2).strip(),
                    "start_line": idx,
                    "end_line": len(lines),
                }
            )

    for index, heading in enumerate(headings):
        for next_heading in headings[index + 1 :]:
            if next_heading["level"] <= heading["level"]:
                heading["end_line"] = next_heading["start_line"] - 1
                break

    return LocalKBHeadingResult(
        path=local_kb_relative_path(target, root=root), headings=headings, total_lines=len(lines)
    )


def kb_resolve_link(from_path: str, link: str) -> LocalKBResolveResult:
    root = get_local_kb_root()
    raw = link.strip()
    anchor = None
    if not raw or raw.startswith("#") or re.match(r"^[a-z]+://", raw):
        return LocalKBResolveResult(link=link, status="unsupported", anchor=anchor)

    wiki = re.fullmatch(r"\[\[([^\]]+)\]\]", raw)
    if wiki:
        raw = wiki.group(1).split("|", 1)[0]
    markdown = re.fullmatch(r"\[[^\]]+\]\(([^)]+)\)", raw)
    if markdown:
        raw = markdown.group(1)
    raw = unquote(raw).replace("\\ ", " ").strip()
    if "#" in raw:
        raw, anchor = raw.split("#", 1)
    if not raw or raw.startswith("#") or re.match(r"^[a-z]+://", raw):
        return LocalKBResolveResult(link=link, status="unsupported", anchor=anchor)

    if root is None:
        raise LocalKBError("Local knowledge base is not configured.", kind="not_configured")

    try:
        from_file = resolve_local_kb_path(from_path, root=root)
    except LocalKBError:
        from_file = root
    base = from_file.parent if from_file.is_file() else from_file

    candidates: List[Path] = []

    def add_options(base_candidate: Path) -> None:
        options = [base_candidate]
        if base_candidate.suffix == "":
            options.extend(
                [base_candidate.with_suffix(".md"), base_candidate / "index.md", base_candidate / "README.md"]
            )
        elif base_candidate.is_dir():
            options.extend([base_candidate / "index.md", base_candidate / "README.md"])
        for option in options:
            resolved = option.resolve(strict=False)
            if (
                resolved.exists()
                and resolved.is_file()
                and _allowed_file(resolved, root)
                and resolved not in candidates
            ):
                candidates.append(resolved)

    value = Path(raw).expanduser()
    try:
        add_options(value if value.is_absolute() else base / value)
        add_options(value if value.is_absolute() else root / value)
    except OSError:
        return LocalKBResolveResult(link=link, status="blocked", anchor=anchor)

    if "/" not in raw and "\\" not in raw and Path(raw).suffix == "":
        for file_path in _iter_allowed_files("", root=root):
            if file_path.stem == raw or file_path.name == raw:
                candidates.append(file_path)

    safe_candidates: List[str] = []
    try:
        for candidate in candidates:
            resolved = resolve_local_kb_path(local_kb_relative_path(candidate, root=root), root=root)
            if resolved.is_file() and _allowed_file(resolved, root):
                relpath = local_kb_relative_path(resolved, root=root)
                if relpath not in safe_candidates:
                    safe_candidates.append(relpath)
    except LocalKBError:
        return LocalKBResolveResult(link=link, status="blocked", anchor=anchor)

    if len(safe_candidates) == 1:
        return LocalKBResolveResult(link=link, status="resolved", resolved=safe_candidates[0], anchor=anchor)
    if len(safe_candidates) > 1:
        return LocalKBResolveResult(link=link, status="ambiguous", anchor=anchor, candidates=safe_candidates)
    return LocalKBResolveResult(link=link, status="not_found", anchor=anchor)
