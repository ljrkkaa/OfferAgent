from __future__ import annotations

import hashlib
import json
import os
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path, PurePosixPath

from offeragent_harness.foundation.canonical import canonical_json_bytes

from .catalog import KnowledgeCatalogStore
from .models import (
    CatalogSnapshot,
    PageIndexNode,
    PageIndexTree,
    SourceRecord,
    WikiCitation,
    WikiPagePatch,
    WikiPageType,
)
from .objects import KnowledgeObjectStore
from .wiki import ValidatedWikiPlan


class PublicationStage(str, Enum):
    PREPARED = "prepared"
    OLD_MOVED = "old_moved"
    NEW_MOVED = "new_moved"
    COMMITTED = "committed"


class KnowledgePublicationError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class _PublishedWikiPage:
    wiki_id: str
    page_type: WikiPageType
    path: str
    citations: tuple[WikiCitation, ...]
    related_wiki_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        # Reuse the public page model's path and identity invariants without
        # duplicating a weaker second validation policy for persisted records.
        WikiPagePatch(
            self.wiki_id,
            self.page_type,
            self.path,
            self.wiki_id,
            (),
            "persisted",
            self.citations,
            self.related_wiki_ids,
        )


class KnowledgePublisher:
    """Journaled whole-Wiki publication under the Worker's exclusive write gate."""

    def __init__(
        self,
        *,
        vault_root: Path,
        catalog: KnowledgeCatalogStore,
        objects: KnowledgeObjectStore,
        fault_injector: Callable[[PublicationStage], None] | None = None,
    ) -> None:
        if not vault_root.is_absolute():
            raise ValueError("knowledge publisher Vault root must be absolute")
        self._vault = vault_root
        self._state = vault_root / ".offeragent" / "knowledge"
        self._wiki = vault_root / "knowledge"
        self._catalog = catalog
        self._objects = objects
        self._fault_injector = fault_injector

    def publish(
        self,
        *,
        validated: ValidatedWikiPlan,
        target_sources: tuple[SourceRecord, ...],
    ) -> CatalogSnapshot:
        self.recover()
        current = self._catalog.load()
        if current.revision != validated.plan.base_revision:
            raise KnowledgePublicationError("knowledge publication base revision is stale")
        try:
            target_catalog = CatalogSnapshot(current.revision, target_sources)
        except ValueError as error:
            raise KnowledgePublicationError("knowledge publication target catalog is invalid") from error
        target_versions = {(source.source_id, source.content_hash) for source in target_catalog.sources}
        plan_versions = {
            (citation.source_id, citation.source_hash) for page in validated.plan.pages for citation in page.citations
        } | {(tree.source_id, tree.source_hash) for tree in validated.enriched_page_indexes}
        if not plan_versions <= target_versions:
            raise KnowledgePublicationError("knowledge publication plan is not bound to its target catalog")
        if self._wiki.exists():
            self._verify_snapshot(self._wiki, expected_revision=current.revision)
        elif current.revision != 0:
            raise KnowledgePublicationError("knowledge publication catalog has no corresponding Wiki snapshot")
        ingestion_id = validated.plan.ingestion_id
        transaction_root = self._state / "transactions" / ingestion_id
        staged_wiki = transaction_root / "wiki"
        backup_wiki = transaction_root / "backup"
        if transaction_root.exists():
            raise KnowledgePublicationError("knowledge publication transaction already exists")
        target_revision = current.revision + 1
        try:
            transaction_root.mkdir(parents=True)
            if self._wiki.exists():
                shutil.copytree(self._wiki, staged_wiki)
                (staged_wiki / ".manifest.json").unlink()
            else:
                staged_wiki.mkdir()
            self._merge_wiki_registry(
                staged_wiki,
                validated.plan.pages,
                validated.plan.deleted_wiki_ids,
                target_versions,
            )
            for relative, content in validated.rendered_pages:
                _safe_write(staged_wiki, relative, content)
            self._replace_page_indexes(
                staged_wiki,
                validated.enriched_page_indexes,
                target_catalog.sources,
                target_revision,
            )
            self._replace_source_pages(staged_wiki, target_catalog.sources, target_revision)
            _safe_write(staged_wiki, "index.md", _render_index(staged_wiki, target_revision))
            self._write_snapshot_manifest(staged_wiki, target_revision)
            journal = {
                "baseRevision": current.revision,
                "hadCurrent": self._wiki.exists(),
                "ingestionId": ingestion_id,
                "schemaVersion": 1,
                "stage": PublicationStage.PREPARED.value,
                "targetRevision": target_revision,
            }
            self._write_journal(journal)
            self._inject(PublicationStage.PREPARED)
            if self._wiki.exists():
                os.replace(self._wiki, backup_wiki)
            journal["stage"] = PublicationStage.OLD_MOVED.value
            self._write_journal(journal)
            self._inject(PublicationStage.OLD_MOVED)
            os.replace(staged_wiki, self._wiki)
            journal["stage"] = PublicationStage.NEW_MOVED.value
            self._write_journal(journal)
            self._inject(PublicationStage.NEW_MOVED)
            committed = self._catalog.commit(expected_revision=current.revision, sources=target_catalog.sources)
            journal["stage"] = PublicationStage.COMMITTED.value
            self._write_journal(journal)
            self._inject(PublicationStage.COMMITTED)
            self._verify_snapshot(self._wiki, expected_revision=committed.revision)
            self._finish_transaction(transaction_root)
            return committed
        except BaseException:
            if not (self._state / "active-publication.json").exists() and transaction_root.exists():
                shutil.rmtree(transaction_root)
            self.recover()
            raise

    def existing_wiki_ids(self) -> frozenset[str]:
        current = self._catalog.load()
        if not self._wiki.exists():
            if current.revision != 0:
                raise KnowledgePublicationError("knowledge catalog has no corresponding Wiki snapshot")
            return frozenset()
        self._verify_snapshot(self._wiki, expected_revision=current.revision)
        return frozenset(self._read_wiki_registry(self._wiki))

    def current_page_indexes(self) -> tuple[PageIndexTree, ...]:
        current = self._catalog.load()
        if not self._wiki.exists():
            if current.revision != 0:
                raise KnowledgePublicationError("knowledge catalog has no corresponding Wiki snapshot")
            return ()
        self._verify_snapshot(self._wiki, expected_revision=current.revision)
        root = self._wiki / "pageindexes"
        try:
            trees = tuple(_pageindex_from_bytes(path.read_bytes()) for path in sorted(root.glob("*.json")))
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as error:
            raise KnowledgePublicationError("knowledge PageIndex snapshot is invalid") from error
        expected = {(source.source_id, source.content_hash) for source in current.sources}
        actual = {(tree.source_id, tree.source_hash) for tree in trees}
        if actual != expected or len(actual) != len(trees):
            raise KnowledgePublicationError("knowledge PageIndex snapshot diverges from catalog")
        return trees

    def recover(self) -> None:
        journal = self._read_journal()
        if journal is None:
            return
        ingestion_id = _journal_identifier(journal, "ingestionId")
        transaction_root = self._state / "transactions" / ingestion_id
        backup_wiki = transaction_root / "backup"
        try:
            stage = PublicationStage(_journal_string(journal, "stage"))
        except ValueError as error:
            raise KnowledgePublicationError("knowledge publication journal stage is invalid") from error
        had_current = journal.get("hadCurrent")
        if not isinstance(had_current, bool):
            raise KnowledgePublicationError("knowledge publication journal current-state flag is invalid")
        target_revision = _journal_integer(journal, "targetRevision")
        base_revision = _journal_integer(journal, "baseRevision")
        if target_revision != base_revision + 1:
            raise KnowledgePublicationError("knowledge publication journal revisions are invalid")
        catalog_revision = self._catalog.load().revision
        if catalog_revision == target_revision:
            self._verify_snapshot(self._wiki, expected_revision=target_revision)
            self._finish_transaction(transaction_root)
            return
        if catalog_revision != base_revision or stage is PublicationStage.COMMITTED:
            raise KnowledgePublicationError("knowledge publication journal diverges from catalog")
        self._rollback_uncommitted_swap(
            backup_wiki=backup_wiki,
            had_current=had_current,
            base_revision=base_revision,
            target_revision=target_revision,
        )
        self._finish_transaction(transaction_root)

    def _rollback_uncommitted_swap(
        self,
        *,
        backup_wiki: Path,
        had_current: bool,
        base_revision: int,
        target_revision: int,
    ) -> None:
        if had_current:
            if backup_wiki.exists():
                if self._wiki.exists():
                    self._verify_snapshot(self._wiki, expected_revision=target_revision)
                    shutil.rmtree(self._wiki)
                self._verify_snapshot(backup_wiki, expected_revision=base_revision)
                os.replace(backup_wiki, self._wiki)
            elif self._wiki.exists():
                self._verify_snapshot(self._wiki, expected_revision=base_revision)
            else:
                raise KnowledgePublicationError("knowledge publication rollback backup is missing")
            return
        if backup_wiki.exists():
            raise KnowledgePublicationError("knowledge publication found an unexpected rollback backup")
        if self._wiki.exists():
            self._verify_snapshot(self._wiki, expected_revision=target_revision)
            shutil.rmtree(self._wiki)

    def _replace_source_pages(self, staged_wiki: Path, sources: tuple[SourceRecord, ...], target_revision: int) -> None:
        source_root = staged_wiki / "sources"
        if source_root.exists():
            shutil.rmtree(source_root)
        source_root.mkdir()
        for source in sorted(sources, key=lambda item: item.relative_path.casefold()):
            object_root = self._objects.object_path(source.source_id, source.content_hash)
            self._objects.verify(object_root)
            evidence_root = self._objects.object_relative_path(source.source_id, source.content_hash)
            payload = (
                "---\n"
                f"source_id: {source.source_id}\n"
                f"source_hash: {source.content_hash}\n"
                f"knowledge_revision: {target_revision}\n"
                "---\n\n"
                f"# {source.relative_path}\n\n"
                f"- Citation source: `{source.relative_path}`\n"
                f"- Media type: `{source.media_type}`\n"
                f"- Bytes: {source.byte_size}\n"
                f"- Evidence: `.offeragent/knowledge/{evidence_root}/evidence/pages/`\n"
            ).encode()
            _safe_write(staged_wiki, f"sources/{source.source_id}.md", payload)

    def _replace_page_indexes(
        self,
        staged_wiki: Path,
        enriched: tuple[PageIndexTree, ...],
        sources: tuple[SourceRecord, ...],
        target_revision: int,
    ) -> None:
        pageindex_root = staged_wiki / "pageindexes"
        existing: dict[tuple[str, str], PageIndexTree] = {}
        if pageindex_root.exists():
            for path in pageindex_root.glob("*.json"):
                existing_tree = _pageindex_from_bytes(path.read_bytes())
                existing[(existing_tree.source_id, existing_tree.source_hash)] = existing_tree
            shutil.rmtree(pageindex_root)
        pageindex_root.mkdir()
        enriched_by_source = {(tree.source_id, tree.source_hash): tree for tree in enriched}
        for source in sources:
            key = (source.source_id, source.content_hash)
            selected_tree = enriched_by_source.get(key) or existing.get(key)
            if selected_tree is None:
                raise KnowledgePublicationError("knowledge publication is missing a summarized PageIndex")
            if any(not node.summary.strip() for node in selected_tree.nodes):
                raise KnowledgePublicationError("knowledge publication PageIndex contains an empty summary")
            _safe_write(
                staged_wiki,
                f"pageindexes/{source.source_id}.json",
                canonical_json_bytes(_tree_json(selected_tree)),
            )
            _safe_write(
                staged_wiki,
                f"pageindexes/{source.source_id}.md",
                _render_pageindex(selected_tree, target_revision),
            )

    def _merge_wiki_registry(
        self,
        staged_wiki: Path,
        patches: tuple[WikiPagePatch, ...],
        deleted_wiki_ids: tuple[str, ...],
        target_versions: set[tuple[str, str]],
    ) -> None:
        records = self._read_wiki_registry(staged_wiki)
        for wiki_id in deleted_wiki_ids:
            removed = records.pop(wiki_id, None)
            if removed is None:
                raise KnowledgePublicationError("knowledge Wiki deletion target does not exist")
            _safe_remove(staged_wiki, removed.path)
        for patch in patches:
            previous = records.get(patch.wiki_id)
            if previous is not None and previous.path != patch.path:
                _safe_remove(staged_wiki, previous.path)
            records[patch.wiki_id] = _record_from_patch(patch)
        paths = [record.path.casefold() for record in records.values()]
        if len(paths) != len(set(paths)):
            raise KnowledgePublicationError("knowledge Wiki pages have conflicting paths")
        ids = set(records)
        for record in records.values():
            if any((item.source_id, item.source_hash) not in target_versions for item in record.citations):
                raise KnowledgePublicationError("knowledge Wiki retains a stale source citation")
            if any(item not in ids for item in record.related_wiki_ids):
                raise KnowledgePublicationError("knowledge Wiki retains an unresolved related page")
        payload = {
            "pages": [_record_json(records[key]) for key in sorted(records)],
            "schemaVersion": 1,
        }
        _safe_write(staged_wiki, ".pages.json", canonical_json_bytes(payload))

    @staticmethod
    def _read_wiki_registry(root: Path) -> dict[str, _PublishedWikiPage]:
        path = root / ".pages.json"
        if not path.exists():
            generated_roots = (root / "summaries", root / "concepts", root / "entities")
            if any(item.is_file() for generated_root in generated_roots for item in generated_root.glob("*.md")):
                raise KnowledgePublicationError("knowledge Wiki registry is missing")
            return {}
        try:
            raw = path.read_bytes()
            value = json.loads(raw.decode("utf-8", errors="strict"))
            if canonical_json_bytes(value) != raw:
                raise ValueError("registry is not canonical")
            item = _object(value, {"pages", "schemaVersion"})
            if item["schemaVersion"] != 1 or not isinstance(item["pages"], list):
                raise ValueError("registry schema is invalid")
            records = tuple(_record_from_json(record) for record in item["pages"])
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as error:
            raise KnowledgePublicationError("knowledge Wiki registry is invalid") from error
        by_id = {record.wiki_id: record for record in records}
        if len(by_id) != len(records) or [record.wiki_id for record in records] != sorted(by_id):
            raise KnowledgePublicationError("knowledge Wiki registry identities are invalid")
        return by_id

    def _write_snapshot_manifest(self, root: Path, revision: int) -> None:
        files = []
        entries = tuple(root.rglob("*"))
        if any(path.is_symlink() for path in entries):
            raise KnowledgePublicationError("knowledge Wiki snapshots cannot contain symbolic links")
        for path in sorted((item for item in entries if item.is_file()), key=lambda item: item.as_posix()):
            if path.name == ".manifest.json":
                continue
            content = path.read_bytes()
            files.append(
                {
                    "byteSize": len(content),
                    "path": path.relative_to(root).as_posix(),
                    "sha256": _sha256(content),
                }
            )
        manifest = {"files": files, "revision": revision, "schemaVersion": 1}
        _atomic_write(root / ".manifest.json", canonical_json_bytes(manifest))

    @staticmethod
    def _verify_snapshot(root: Path, *, expected_revision: int) -> None:
        try:
            raw = (root / ".manifest.json").read_bytes()
            value = json.loads(raw.decode("utf-8", errors="strict"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise KnowledgePublicationError("knowledge Wiki manifest is unreadable") from error
        if canonical_json_bytes(value) != raw or not isinstance(value, dict):
            raise KnowledgePublicationError("knowledge Wiki manifest is not canonical")
        if set(value) != {"files", "revision", "schemaVersion"} or value["revision"] != expected_revision:
            raise KnowledgePublicationError("knowledge Wiki manifest identity is invalid")
        files = value["files"]
        if value["schemaVersion"] != 1 or not isinstance(files, list):
            raise KnowledgePublicationError("knowledge Wiki manifest schema is invalid")
        entries = tuple(root.rglob("*"))
        if any(path.is_symlink() for path in entries):
            raise KnowledgePublicationError("knowledge Wiki snapshots cannot contain symbolic links")
        actual_files = {
            path.relative_to(root).as_posix() for path in entries if path.is_file() and path.name != ".manifest.json"
        }
        seen: set[str] = set()
        for item in files:
            if not isinstance(item, dict) or set(item) != {"byteSize", "path", "sha256"}:
                raise KnowledgePublicationError("knowledge Wiki file record is invalid")
            relative = item["path"]
            portable = PurePosixPath(relative) if isinstance(relative, str) else None
            if portable is None or portable.is_absolute() or any(part in {"", ".", ".."} for part in portable.parts):
                raise KnowledgePublicationError("knowledge Wiki file path is invalid")
            if relative in seen:
                raise KnowledgePublicationError("knowledge Wiki file path is duplicated")
            seen.add(relative)
            try:
                content = (root / relative).read_bytes()
            except OSError as error:
                raise KnowledgePublicationError("knowledge Wiki manifest references a missing file") from error
            if item["byteSize"] != len(content) or item["sha256"] != _sha256(content):
                raise KnowledgePublicationError("knowledge Wiki file integrity mismatch")
        if seen != actual_files:
            raise KnowledgePublicationError("knowledge Wiki manifest does not match snapshot contents")

    def _write_journal(self, value: dict[str, object]) -> None:
        self._state.mkdir(parents=True, exist_ok=True)
        _atomic_write(self._state / "active-publication.json", canonical_json_bytes(value))

    def _read_journal(self) -> dict[str, object] | None:
        path = self._state / "active-publication.json"
        if not path.exists():
            return None
        try:
            raw = path.read_bytes()
            value = json.loads(raw.decode("utf-8", errors="strict"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise KnowledgePublicationError("knowledge publication journal is unreadable") from error
        if canonical_json_bytes(value) != raw or not isinstance(value, dict):
            raise KnowledgePublicationError("knowledge publication journal is not canonical")
        if (
            set(value)
            != {
                "baseRevision",
                "hadCurrent",
                "ingestionId",
                "schemaVersion",
                "stage",
                "targetRevision",
            }
            or value["schemaVersion"] != 1
        ):
            raise KnowledgePublicationError("knowledge publication journal shape is invalid")
        return value

    def _finish_transaction(self, transaction_root: Path) -> None:
        if transaction_root.exists():
            shutil.rmtree(transaction_root)
        journal = self._state / "active-publication.json"
        journal.unlink(missing_ok=True)

    def _inject(self, stage: PublicationStage) -> None:
        if self._fault_injector is not None:
            self._fault_injector(stage)


def _render_index(root: Path, revision: int) -> bytes:
    pages = sorted(
        path.relative_to(root).as_posix()
        for path in root.rglob("*.md")
        if path.relative_to(root).as_posix() != "index.md"
    )
    lines = ["# OfferAgent Knowledge Base", "", f"Revision: {revision}", "", "## Pages", ""]
    lines.extend(f"- [[{path.removesuffix('.md')}]]" for path in pages)
    return ("\n".join(lines) + "\n").encode("utf-8")


def _record_from_patch(page: WikiPagePatch) -> _PublishedWikiPage:
    return _PublishedWikiPage(page.wiki_id, page.page_type, page.path, page.citations, page.related_wiki_ids)


def _record_json(page: _PublishedWikiPage) -> dict[str, object]:
    return {
        "citations": [
            {
                "endPage": item.end_page,
                "nodeId": item.node_id,
                "sourceHash": item.source_hash,
                "sourceId": item.source_id,
                "startPage": item.start_page,
            }
            for item in page.citations
        ],
        "pageType": page.page_type.value,
        "path": page.path,
        "relatedWikiIds": list(page.related_wiki_ids),
        "wikiId": page.wiki_id,
    }


def _record_from_json(value: object) -> _PublishedWikiPage:
    item = _object(value, {"citations", "pageType", "path", "relatedWikiIds", "wikiId"})
    citations = item["citations"]
    related = item["relatedWikiIds"]
    if not isinstance(citations, list) or not isinstance(related, list):
        raise ValueError("Wiki registry page lists are invalid")
    return _PublishedWikiPage(
        _string(item["wikiId"]),
        WikiPageType(_string(item["pageType"])),
        _string(item["path"]),
        tuple(_citation_from_json(citation) for citation in citations),
        tuple(_string(wiki_id) for wiki_id in related),
    )


def _citation_from_json(value: object) -> WikiCitation:
    item = _object(value, {"endPage", "nodeId", "sourceHash", "sourceId", "startPage"})
    return WikiCitation(
        _string(item["sourceId"]),
        _string(item["sourceHash"]),
        _string(item["nodeId"]),
        _integer(item["startPage"], minimum=1),
        _integer(item["endPage"], minimum=1),
    )


def _tree_json(tree: PageIndexTree) -> dict[str, object]:
    return {
        "nodes": [
            {
                "children": list(node.children),
                "depth": node.depth,
                "endPage": node.end_page,
                "nodeId": node.node_id,
                "parentId": node.parent_id,
                "startPage": node.start_page,
                "summary": node.summary,
                "title": node.title,
            }
            for node in tree.nodes
        ],
        "pageCount": tree.page_count,
        "rootId": tree.root_id,
        "schemaVersion": 1,
        "sourceHash": tree.source_hash,
        "sourceId": tree.source_id,
    }


def _pageindex_from_bytes(payload: bytes) -> PageIndexTree:
    value = json.loads(payload.decode("utf-8", errors="strict"))
    if canonical_json_bytes(value) != payload:
        raise KnowledgePublicationError("knowledge PageIndex projection is not canonical")
    item = _object(
        value,
        {"nodes", "pageCount", "rootId", "schemaVersion", "sourceHash", "sourceId"},
    )
    nodes = item["nodes"]
    if item["schemaVersion"] != 1 or not isinstance(nodes, list):
        raise KnowledgePublicationError("knowledge PageIndex projection schema is invalid")
    return PageIndexTree(
        _string(item["sourceId"]),
        _string(item["sourceHash"]),
        _integer(item["pageCount"], minimum=1),
        _string(item["rootId"]),
        tuple(_node_from_json(node) for node in nodes),
    )


def _node_from_json(value: object) -> PageIndexNode:
    item = _object(
        value,
        {"children", "depth", "endPage", "nodeId", "parentId", "startPage", "summary", "title"},
    )
    parent_id = item["parentId"]
    children = item["children"]
    if parent_id is not None and not isinstance(parent_id, str):
        raise ValueError("PageIndex parent identity is invalid")
    if not isinstance(children, list):
        raise ValueError("PageIndex children are invalid")
    return PageIndexNode(
        _string(item["nodeId"]),
        parent_id,
        _integer(item["depth"], minimum=0),
        _string(item["title"]),
        _string(item["summary"], allow_empty=True),
        _integer(item["startPage"], minimum=1),
        _integer(item["endPage"], minimum=1),
        tuple(_string(child) for child in children),
    )


def _render_pageindex(tree: PageIndexTree, revision: int) -> bytes:
    lines = [
        "---",
        f"source_id: {tree.source_id}",
        f"source_hash: {tree.source_hash}",
        f"knowledge_revision: {revision}",
        "---",
        "",
        f"# PageIndex: {tree.nodes[0].title}",
        "",
    ]
    for node in tree.nodes:
        indent = "  " * node.depth
        lines.append(
            f"{indent}- **{node.title}** (`{node.node_id}`, pages {node.start_page}-{node.end_page}): "
            f"{node.summary.strip()}"
        )
    return ("\n".join(lines) + "\n").encode()


def _object(value: object, keys: set[str]) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != keys or any(not isinstance(key, str) for key in value):
        raise ValueError("knowledge projection shape is invalid")
    return value


def _string(value: object, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value):
        raise ValueError("knowledge projection string is invalid")
    return value


def _integer(value: object, *, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError("knowledge projection integer is invalid")
    return value


def _safe_write(root: Path, relative: str, content: bytes) -> None:
    portable = PurePosixPath(relative)
    if portable.is_absolute() or any(part in {"", ".", ".."} for part in portable.parts):
        raise KnowledgePublicationError("knowledge publication path is invalid")
    target = root.joinpath(*portable.parts)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    if temporary.exists():
        raise KnowledgePublicationError("knowledge publication temporary file already exists")
    with temporary.open("xb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, target)


def _safe_remove(root: Path, relative: str) -> None:
    portable = PurePosixPath(relative)
    if portable.is_absolute() or any(part in {"", ".", ".."} for part in portable.parts):
        raise KnowledgePublicationError("knowledge publication removal path is invalid")
    target = root.joinpath(*portable.parts)
    if not target.is_file() or target.is_symlink():
        raise KnowledgePublicationError("knowledge publication removal target is invalid")
    target.unlink()


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.unlink(missing_ok=True)
    with temporary.open("xb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _journal_string(value: dict[str, object], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item:
        raise KnowledgePublicationError(f"knowledge publication journal {key} is invalid")
    return item


def _journal_identifier(value: dict[str, object], key: str) -> str:
    item = _journal_string(value, key)
    portable = PurePosixPath(item)
    if len(portable.parts) != 1 or portable.parts[0] in {"", ".", ".."}:
        raise KnowledgePublicationError(f"knowledge publication journal {key} is invalid")
    if not all(character.isascii() and (character.isalnum() or character in "_-") for character in item):
        raise KnowledgePublicationError(f"knowledge publication journal {key} is invalid")
    return item


def _journal_integer(value: dict[str, object], key: str) -> int:
    item = value.get(key)
    if isinstance(item, bool) or not isinstance(item, int) or item < 0:
        raise KnowledgePublicationError(f"knowledge publication journal {key} is invalid")
    return item


def _sha256(content: bytes) -> str:
    return f"sha256:{hashlib.sha256(content).hexdigest()}"
