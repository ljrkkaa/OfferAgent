import fnmatch
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Pattern

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


@dataclass(frozen=True)
class LocalKBGrepResult:
    line_count: int
    document_count: int
    lines: List[str]


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


def list_local_kb_files(
    path: Optional[str] = None,
    pattern: Optional[str] = None,
    *,
    limit: int = 100,
) -> tuple[List[str], int]:
    root = get_local_kb_root()
    files = _iter_allowed_files(path, root=root)
    rel_files = [local_kb_relative_path(path, root=root) for path in files]
    if pattern:
        rel_files = [
            path for path in rel_files if fnmatch.fnmatch(path, pattern) or fnmatch.fnmatch(Path(path).name, pattern)
        ]
    return rel_files[:limit], len(rel_files)


def read_local_kb_file(
    path: str,
    *,
    start_line: Optional[int] = None,
    end_line: Optional[int] = None,
    max_lines: int = 80,
) -> LocalKBReadResult:
    root = get_local_kb_root()
    target = resolve_local_kb_path(path, root=root)
    if not target.exists():
        raise LocalKBError(f"File '{path}' not found in local knowledge base.", kind="not_found")
    if not target.is_file() or not _allowed_file(target, root):
        raise LocalKBError(f"File '{path}' is not a supported local knowledge base text file.")

    lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
    start_line = start_line or 1
    end_line = end_line or len(lines)

    if start_line < 1 or end_line < 1 or start_line > end_line:
        raise LocalKBError(f"Invalid line range: {start_line}-{end_line}")
    if start_line > len(lines):
        raise LocalKBError(f"Start line {start_line} exceeds total number of lines {len(lines)}")

    start_idx = start_line - 1
    end_idx = min(len(lines), end_line)
    truncation = ""
    if end_idx - start_idx > max_lines:
        end_idx = start_idx + max_lines
        truncation = f"\n\n[Truncated after {max_lines} lines! Use narrower line range to view complete section.]"

    relpath = local_kb_relative_path(target, root=root)
    return LocalKBReadResult(path=relpath, text="\n".join(lines[start_idx:end_idx]) + truncation)


def grep_local_kb_files(
    regex: Pattern[str],
    *,
    path_prefix: Optional[str] = None,
    lines_before: int = 0,
    lines_after: int = 0,
    max_results: int = 1000,
) -> LocalKBGrepResult:
    root = get_local_kb_root()
    files = _iter_allowed_files(path_prefix, root=root)
    line_matches: List[str] = []
    line_count = 0
    matched_documents = 0

    for file_path in files:
        relpath = local_kb_relative_path(file_path, root=root)
        lines = file_path.read_text(encoding="utf-8", errors="replace").splitlines()
        matched_line_numbers = [idx for idx, line in enumerate(lines, 1) if regex.search(line)]
        if not matched_line_numbers:
            continue

        matched_documents += 1
        line_count += len(matched_line_numbers)

        for line_num in matched_line_numbers:
            start_idx = max(0, line_num - 1 - lines_before)
            end_idx = min(len(lines), line_num + lines_after)
            for idx in range(start_idx, end_idx):
                current_line_num = idx + 1
                line_content = lines[idx]
                if current_line_num == line_num:
                    line_matches.append(f"{relpath}:{current_line_num}: {line_content}")
                else:
                    line_matches.append(f"{relpath}-{current_line_num}-  {line_content}")
            if lines_before > 0 or lines_after > 0:
                line_matches.append("--")

            if len(line_matches) >= max_results:
                break
        if len(line_matches) >= max_results:
            break

    if line_matches and line_matches[-1] == "--":
        line_matches.pop()
    if len(line_matches) >= max_results and line_count > max_results:
        line_matches = line_matches[:max_results] + [
            f"... {line_count - max_results} more results found. Use stricter regex or path to narrow down results."
        ]

    return LocalKBGrepResult(line_count=line_count, document_count=matched_documents, lines=line_matches)


def load_local_kb_profile_references(max_files: int = 24, max_chars: int = 12000) -> List[Dict[str, str]]:
    root = get_local_kb_root()
    if root is None:
        return []

    max_files = int(os.getenv("KHOJ_LOCAL_KB_PROFILE_MAX_FILES", os.getenv("KHOJ_LOCAL_VAULT_MAX_FILES", max_files)))
    max_chars = int(os.getenv("KHOJ_LOCAL_KB_PROFILE_MAX_CHARS", os.getenv("KHOJ_LOCAL_VAULT_MAX_CHARS", max_chars)))

    candidates = _profile_candidates(root)
    references: List[Dict[str, str]] = []
    remaining_chars = max_chars

    for path in candidates[:max_files]:
        text = path.read_text(encoding="utf-8", errors="replace").strip()
        if not text:
            continue
        relpath = local_kb_relative_path(path, root=root)
        compiled = f"# {relpath}\n\n{text[:remaining_chars].rstrip()}"
        references.append({"query": "local_kb_profile", "compiled": compiled, "file": relpath, "uri": relpath})
        remaining_chars -= len(compiled)
        if remaining_chars <= 0:
            break

    return references


def _profile_candidates(root: Path) -> List[Path]:
    candidates: List[Path] = []

    def add(path: Path) -> None:
        if path.exists() and path.is_file() and _allowed_file(path, root) and path not in candidates:
            candidates.append(path)

    for name in ("AGENTS.md", "agents.md", "agent.md", "index.md", "README.md"):
        add(root / name)

    for child in sorted(
        (p for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")), key=lambda p: p.name.lower()
    ):
        add(child / "index.md")
        add(child / "README.md")

    for path in sorted((p for p in root.iterdir() if p.is_file()), key=lambda p: p.name.lower()):
        add(path)

    for path in list(candidates):
        for linked in _linked_markdown_files(path, root):
            add(linked)

    return candidates


def _linked_markdown_files(path: Path, root: Path) -> List[Path]:
    text = path.read_text(encoding="utf-8", errors="replace")
    links: List[Path] = []

    for raw in re.findall(r"\[\[([^\]]+)\]\]", text):
        links.extend(_resolve_profile_link(raw.split("|", 1)[0].split("#", 1)[0], path.parent, root))

    for raw in re.findall(r"\[[^\]]+\]\(([^)]+)\)", text):
        if re.match(r"^[a-z]+://", raw) or raw.startswith("#"):
            continue
        links.extend(_resolve_profile_link(raw.split("#", 1)[0], path.parent, root))

    return links


def _resolve_profile_link(raw: str, base: Path, root: Path) -> List[Path]:
    value = raw.strip().replace("\\ ", " ")
    if not value:
        return []

    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = base / candidate
    candidate = candidate.resolve(strict=False)
    if not candidate.is_relative_to(root):
        return []

    options = [candidate]
    if candidate.suffix == "":
        options.extend([candidate.with_suffix(".md"), candidate / "index.md", candidate / "README.md"])
    elif candidate.is_dir():
        options.extend([candidate / "index.md", candidate / "README.md"])

    return [path for path in options if path.exists() and path.is_file() and _allowed_file(path, root)]
