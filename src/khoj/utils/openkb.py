import inspect
import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from khoj.utils.helpers import is_env_var_true
from khoj.utils.lexical import message_text, query_terms

logger = logging.getLogger(__name__)

OPENKB_ENABLE_ENV = "KHOJ_ENABLE_OPENKB"
OPENKB_ROOT_ENV = "KHOJ_OPENKB_ROOT"
KB_ENGINE_ENV = "KHOJ_KB_ENGINE"
OPENKB_ALLOWED_SUFFIXES = {".md", ".txt", ".json"}


class OpenKBError(ValueError):
    pass


def get_kb_engine() -> str:
    engine = os.getenv(KB_ENGINE_ENV, "file_first").strip().lower()
    if engine not in {"file_first", "openkb", "hybrid"}:
        logger.warning("Unsupported %s=%s; using file_first", KB_ENGINE_ENV, engine)
        return "file_first"
    return engine


def get_openkb_root() -> Path:
    return Path(os.getenv(OPENKB_ROOT_ENV, ".khoj/openkb")).expanduser().resolve()


def get_openkb_wiki_root() -> Path:
    return get_openkb_root() / "wiki"


def is_openkb_enabled() -> bool:
    return is_env_var_true(OPENKB_ENABLE_ENV)


def openkb_is_ready() -> bool:
    if not is_openkb_enabled():
        return False
    root = get_openkb_root()
    wiki = root / "wiki"
    if not wiki.is_dir():
        return False
    manifest = root / "manifest.json"
    if not manifest.exists():
        return (wiki / "index.md").is_file()
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning("OpenKB manifest is not readable: %s", manifest)
        return False
    return data.get("status") == "ready"


def resolve_openkb_wiki_path(path: str) -> Path:
    if not path:
        raise OpenKBError("OpenKB path cannot be empty.")
    raw = Path(path)
    if raw.is_absolute():
        raise OpenKBError("OpenKB paths must be relative to wiki/.")
    parts = raw.parts
    if parts and parts[0] == "wiki":
        raw = Path(*parts[1:])
    if any(part.startswith(".") or part == ".." for part in raw.parts):
        raise OpenKBError("OpenKB hidden paths and traversal are not readable.")
    wiki = get_openkb_wiki_root().resolve()
    target = (wiki / raw).resolve(strict=False)
    if not target.is_relative_to(wiki):
        raise OpenKBError("OpenKB path escapes wiki root.")
    if target.suffix.lower() not in OPENKB_ALLOWED_SUFFIXES:
        raise OpenKBError("OpenKB path suffix is not readable.")
    return target


def _is_safe_wiki_child(path: Path, wiki: Path, *, require_file: bool = True) -> bool:
    try:
        resolved = path.resolve(strict=True)
        rel_parts = path.relative_to(wiki).parts
        resolved_parts = resolved.relative_to(wiki.resolve()).parts
    except (OSError, ValueError):
        return False
    if any(part.startswith(".") or part == ".." for part in (*rel_parts, *resolved_parts)):
        return False
    if require_file and (not resolved.is_file() or resolved.suffix.lower() not in OPENKB_ALLOWED_SUFFIXES):
        return False
    return True


def openkb_relative_wiki_path(path: Path) -> str:
    return path.resolve(strict=False).relative_to(get_openkb_wiki_root().resolve()).as_posix()


async def wiki_search_documents(
    q: str,
    n: int,
    user: Any,
    chat_history: list[dict],
    conversation_id: str,
    agent: Any = None,
    send_status_func: Optional[Callable] = None,
) -> tuple[list[dict[str, Any]], list[str], str]:
    if not openkb_is_ready():
        return [], [], q

    await _send_status(send_status_func, "Searching OpenKB compiled wiki")
    terms = query_terms(q, cjk_sizes=(4, 3, 2)) + [term for term in _history_terms(chat_history) if term not in q]
    candidates = _rank_candidates(q, terms)
    references: list[dict[str, Any]] = []
    seen: set[tuple[str, Optional[str]]] = set()
    max_refs = max(1, min(n or 7, 12))
    remaining_chars = 20000

    for candidate in candidates:
        if len(references) >= max_refs or remaining_chars <= 0:
            break
        try:
            text = candidate.path.read_text(encoding="utf-8", errors="replace").strip()
        except OSError as e:
            logger.warning("Failed reading OpenKB candidate %s: %s", candidate.path, e)
            continue
        if not text:
            continue
        relpath = openkb_relative_wiki_path(candidate.path)
        clipped = text[: min(remaining_chars, 5000)].rstrip()
        key = (relpath, None)
        if key not in seen:
            seen.add(key)
            references.append(_wiki_reference(q, relpath, clipped, candidate.evidence_type))
            remaining_chars -= len(clipped)

        page_ref = _pageindex_reference_for_query(q, relpath, text, remaining_chars)
        if page_ref:
            page_key = (page_ref["wiki_path"], page_ref["source_pages"])
            if page_key not in seen and len(references) < max_refs:
                seen.add(page_key)
                references.append(page_ref)
                remaining_chars -= len(page_ref["compiled"])

    if references:
        await _send_status(send_status_func, f"Found {len(references)} OpenKB wiki references")
    else:
        await _send_status(send_status_func, "No OpenKB wiki evidence found")
    return references, list(dict.fromkeys(ref["query"] for ref in references)), q


def read_openkb_page_range(source_path_or_doc: str, pages: str) -> str:
    source_path = source_path_or_doc
    if not source_path.endswith(".json"):
        source_path = f"sources/{source_path}.json"
    target = resolve_openkb_wiki_path(source_path)
    if not target.is_file():
        raise OpenKBError(f"OpenKB source not found: {source_path}")
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise OpenKBError(f"OpenKB source JSON is invalid: {source_path}") from e
    if not isinstance(data, list):
        raise OpenKBError(f"OpenKB source JSON must be a list: {source_path}")

    requested = set(_parse_pages(pages))
    if not requested:
        raise OpenKBError(f"OpenKB page range is invalid: {pages}")

    blocks: list[str] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        try:
            page = int(item.get("page"))
        except (TypeError, ValueError):
            continue
        if page not in requested:
            continue
        block = f"[Page {page}]\n{str(item.get('content') or '').strip()}"
        images = item.get("images")
        if isinstance(images, list):
            image_paths = ", ".join(
                str(image.get("path")) for image in images if isinstance(image, dict) and image.get("path")
            )
            if image_paths:
                block += f"\n[Images: {image_paths}]"
        blocks.append(block.rstrip())
    if not blocks:
        raise OpenKBError(f"No OpenKB page content found for {source_path} pages {pages}")
    return "\n\n".join(blocks)


@dataclass(frozen=True)
class _Candidate:
    path: Path
    score: int
    evidence_type: str


def _rank_candidates(query: str, terms: list[str]) -> list[_Candidate]:
    wiki = get_openkb_wiki_root()
    candidates: list[Path] = []
    for rel in ("AGENTS.md", "index.md"):
        path = wiki / rel
        if _is_safe_wiki_child(path, wiki):
            candidates.append(path)
    for dirname in ("summaries", "concepts", "entities", "explorations"):
        directory = wiki / dirname
        if directory.is_dir():
            candidates.extend(path for path in sorted(directory.glob("*.md"))[:200] if _is_safe_wiki_child(path, wiki))

    ranked: list[_Candidate] = []
    for path in candidates:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")[:12000]
        except OSError:
            continue
        relpath = path.relative_to(wiki).as_posix()
        haystack = f"{relpath}\n{text}".lower()
        score = 0
        for term in terms:
            lowered = term.lower()
            if lowered in relpath.lower():
                score += 8
            if lowered in haystack:
                score += 2
        if path.name == "index.md":
            score += 1
        if score:
            ranked.append(_Candidate(path=path, score=score, evidence_type=_evidence_type(relpath)))

    return sorted(ranked, key=lambda item: (-item.score, _candidate_order(item.path), item.path.as_posix()))


def _wiki_reference(query: str, wiki_path: str, text: str, evidence_type: str) -> dict[str, Any]:
    file_path = f"wiki/{wiki_path}"
    return {
        "query": f"openkb:{query}",
        "file": file_path,
        "uri": f"openkb://local/{file_path}",
        "compiled": f"# {file_path}\n{text}",
        "wiki_path": wiki_path,
        "evidence_type": evidence_type,
        "source_pages": None,
    }


def _pageindex_reference_for_query(
    query: str, summary_path: str, summary_text: str, budget: int
) -> Optional[dict[str, Any]]:
    pages = _pages_from_query(query)
    if not pages:
        return None
    source_path = _summary_source_path(summary_path, summary_text)
    if not source_path:
        return None
    try:
        text = read_openkb_page_range(source_path, pages)
    except OpenKBError as e:
        logger.info("OpenKB PageIndex read skipped: %s", e)
        return None
    clipped = text[: min(max(0, budget), 5000)].rstrip()
    if not clipped:
        return None
    file_path = f"wiki/{source_path}"
    return {
        "query": f"openkb:{query}",
        "file": file_path,
        "uri": f"openkb://local/{file_path}#page={pages}",
        "compiled": f"# {file_path} pages {pages}\n{clipped}",
        "wiki_path": source_path,
        "evidence_type": "pageindex",
        "source_pages": pages,
        "summary_path": summary_path,
    }


def _summary_source_path(summary_path: str, text: str) -> Optional[str]:
    for line in text.splitlines()[:40]:
        match = re.match(r"\s*(?:full_text|source|source_path)\s*:\s*[\"']?([^\"'\n]+?)[\"']?\s*$", line)
        if match:
            value = match.group(1).strip()
            if value.startswith("wiki/"):
                value = value[5:]
            return value
    if summary_path.startswith("summaries/"):
        return f"sources/{Path(summary_path).stem}.json"
    return None


def _evidence_type(wiki_path: str) -> str:
    if wiki_path == "index.md":
        return "index"
    if wiki_path == "AGENTS.md":
        return "agent"
    first = wiki_path.split("/", 1)[0]
    return {
        "summaries": "summary",
        "concepts": "concept",
        "entities": "entity",
        "explorations": "exploration",
        "sources": "pageindex",
    }.get(first, "wiki")


def _history_terms(chat_history: list[dict], max_terms: int = 8) -> list[str]:
    texts = [message_text(message) for message in (chat_history or [])[-4:]]
    return query_terms(" ".join(texts), max_terms=max_terms, cjk_sizes=(4, 3, 2))


def _candidate_order(path: Path) -> int:
    rel = path.relative_to(get_openkb_wiki_root()).as_posix()
    if rel == "index.md":
        return 0
    if rel.startswith("concepts/"):
        return 1
    if rel.startswith("entities/"):
        return 2
    if rel.startswith("summaries/"):
        return 3
    if rel.startswith("explorations/"):
        return 4
    return 10


def _parse_pages(pages: str) -> list[int]:
    selected: set[int] = set()
    for part in re.split(r"[,，\s]+", pages.strip()):
        if not part:
            continue
        match = re.fullmatch(r"(\d+)\s*[-~—]\s*(\d+)", part)
        if match:
            start, end = int(match.group(1)), int(match.group(2))
            if start <= end and end - start <= 20:
                selected.update(range(start, end + 1))
            continue
        if part.isdigit():
            selected.add(int(part))
    return sorted(page for page in selected if page > 0)


def _pages_from_query(query: str) -> str:
    ranges = re.findall(r"(?:page|pages|第)\s*(\d+\s*(?:[-~—]\s*\d+)?)\s*(?:页)?", query, flags=re.I)
    if ranges:
        return ",".join(range_text.replace(" ", "") for range_text in ranges)
    return ""


async def _send_status(send_status: Optional[Callable], message: str) -> None:
    if not send_status:
        return
    result = send_status(message)
    if hasattr(result, "__aiter__"):
        async for _ in result:
            pass
    elif inspect.isawaitable(result):
        await result
