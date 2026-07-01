import hashlib
import inspect
import json
import logging
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from khoj.utils.helpers import is_env_var_true
from khoj.utils.lexical import message_text, query_terms
from khoj.utils.local_kb import LocalKBError, get_local_kb_root, is_local_kb_write_allowed, resolve_local_kb_path

logger = logging.getLogger(__name__)

OPENKB_ENABLE_ENV = "KHOJ_ENABLE_OPENKB"
OPENKB_ROOT_ENV = "KHOJ_OPENKB_ROOT"
KB_ENGINE_ENV = "KHOJ_KB_ENGINE"
OPENKB_ALLOWED_SUFFIXES = {".md", ".txt", ".json"}
WIKILINK_RE = re.compile(r"\[\[([^\]|]+)(?:\|([^\]]+))?\]\]")


class OpenKBError(ValueError):
    pass


@dataclass(frozen=True)
class OpenKBSaveResult:
    path: str
    status: str
    changed: bool
    message: str
    checksum: str = ""

    def to_reference(self, query: str) -> dict[str, Any]:
        return {
            "query": "save_exploration",
            "file": self.path,
            "uri": f"openkb://local/{self.path}" if self.path.startswith("wiki/") else f"local-kb://{self.path}",
            "compiled": self.message,
            "action": "save_exploration",
            "status": self.status,
            "changed": self.changed,
            "checksum": self.checksum,
            "source_query": query,
        }


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


def wants_openkb_exploration_save(query: str) -> bool:
    text = query.lower()
    return any(
        marker in text
        for marker in (
            "保存为 exploration",
            "save exploration",
            "保存成一篇复盘",
            "保存这次",
            "沉淀到 wiki",
            "沉淀到wiki",
            "保存到 wiki",
            "保存到wiki",
        )
    )


def save_exploration(
    query: str,
    answer: str,
    references: list[dict[str, Any]],
    *,
    conversation_id: str,
    client_app: Any = None,
) -> OpenKBSaveResult:
    if str(client_app or "").lower() == "qqbot":
        return OpenKBSaveResult(
            path="",
            status="disabled",
            changed=False,
            message="QQBot client exploration saves are disabled by default; no file was modified.",
        )

    if openkb_is_ready():
        wiki = get_openkb_wiki_root()
        explore_dir = wiki / "explorations"
        if explore_dir.exists() and not _is_safe_wiki_child(explore_dir, wiki, require_file=False):
            raise OpenKBError("OpenKB exploration directory escapes wiki root.")
        rel_prefix = "wiki/explorations"
        link_root = wiki
    else:
        local_root = get_local_kb_root()
        if local_root is None or not is_local_kb_write_allowed():
            return OpenKBSaveResult(
                path="",
                status="disabled",
                changed=False,
                message="Exploration save is disabled; enable OpenKB or local KB writes first.",
            )
        try:
            explore_dir = resolve_local_kb_path("review/explorations", root=local_root)
        except LocalKBError as e:
            raise OpenKBError(str(e)) from e
        rel_prefix = "review/explorations"
        link_root = local_root

    explore_dir.mkdir(parents=True, exist_ok=True)
    title = _title_from_query(query)
    body = strip_ghost_wikilinks(answer.strip(), _existing_wiki_targets(link_root))
    content = _exploration_markdown(query, body, references, conversation_id)
    target = _unique_path(explore_dir / f"{title}.md")
    _write_text_atomic(target, content)
    relpath = f"{rel_prefix}/{target.name}"
    return OpenKBSaveResult(
        path=relpath,
        status="written",
        changed=True,
        message=f"Saved exploration to {relpath}.",
        checksum=_checksum(content),
    )


def dedupe_references(references: list[dict[str, Any]]) -> list[dict[str, Any]]:
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


def strip_ghost_wikilinks(text: str, known_targets: set[str]) -> str:
    known = {target.strip().strip("/") for target in known_targets}
    known.update(Path(target).name for target in known.copy())

    def replace(match: re.Match[str]) -> str:
        target = match.group(1).strip().strip("/")
        alias = match.group(2)
        if target in known or Path(target).name in known:
            return match.group(0)
        return (alias or target).strip()

    return WIKILINK_RE.sub(replace, text)


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
    broad = _is_broad_query(query)
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
        if path.name == "index.md" and (broad or score):
            score += 4
        if path.name == "AGENTS.md" and broad:
            score += 2
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


def _is_broad_query(query: str) -> bool:
    return any(marker in query for marker in ("目录", "总览", "有哪些", "哪几块", "结构", "index", "overview"))


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


def _existing_wiki_targets(wiki: Path) -> set[str]:
    if not wiki.is_dir():
        return set()
    targets: set[str] = set()
    for page in wiki.rglob("*.md"):
        if "sources" in page.relative_to(wiki).parts:
            continue
        rel = page.relative_to(wiki).with_suffix("").as_posix()
        targets.add(rel)
        targets.add(page.stem)
    return targets


def _title_from_query(query: str) -> str:
    ascii_slug = re.sub(r"[^a-z0-9]+", "-", query.lower()).strip("-")
    if ascii_slug:
        return ascii_slug[:60].strip("-")
    digest = hashlib.sha1(query.encode("utf-8")).hexdigest()[:10]
    return f"exploration-{digest}"


def _exploration_markdown(
    query: str,
    answer: str,
    references: list[dict[str, Any]],
    conversation_id: str,
) -> str:
    created_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    refs = [str(ref.get("file") or ref.get("uri") or "") for ref in references if ref.get("file") or ref.get("uri")]
    ref_lines = "\n".join(f"- {ref}" for ref in dict.fromkeys(refs))
    return (
        "---\n"
        f"query: {json.dumps(query, ensure_ascii=False)}\n"
        f"conversation_id: {json.dumps(str(conversation_id), ensure_ascii=False)}\n"
        "source: offeragent-chat\n"
        f"created_at: {json.dumps(created_at)}\n"
        "---\n\n"
        f"# {query.strip()[:80] or 'Exploration'}\n\n"
        f"{answer.strip()}\n\n"
        "## References\n\n"
        f"{ref_lines or '- No references captured.'}\n"
    )


def _unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    for index in range(2, 1000):
        candidate = path.with_name(f"{path.stem}-{index}{path.suffix}")
        if not candidate.exists():
            return candidate
    raise OpenKBError(f"Could not allocate unique exploration path for {path.name}")


def _checksum(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _write_text_atomic(path: Path, text: str) -> None:
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as tmp:
            tmp.write(text)
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


async def _send_status(send_status: Optional[Callable], message: str) -> None:
    if not send_status:
        return
    result = send_status(message)
    if hasattr(result, "__aiter__"):
        async for _ in result:
            pass
    elif inspect.isawaitable(result):
        await result
