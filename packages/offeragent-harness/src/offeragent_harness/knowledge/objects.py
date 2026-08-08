from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from offeragent_harness.foundation.canonical import canonical_json_bytes

from .models import PageEvidence, PageIndexNode, PageIndexTree, SourceRecord
from .naming import SOURCE_ID_PATTERN

_OBJECT_SCHEMA_VERSION = 1
_SOURCE_ID = re.compile(SOURCE_ID_PATTERN)
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_OBJECT_REVISION_DIGEST_LENGTH = 20


class KnowledgeObjectError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class PreparedKnowledgeSource:
    source: SourceRecord
    pages: tuple[PageEvidence, ...]
    page_index: PageIndexTree
    parser_fingerprint: str

    def __post_init__(self) -> None:
        if not self.pages or [item.page_number for item in self.pages] != list(range(1, len(self.pages) + 1)):
            raise ValueError("prepared source pages must be contiguous and one-based")
        if (
            self.page_index.source_id != self.source.source_id
            or self.page_index.source_hash != self.source.content_hash
            or self.page_index.page_count != len(self.pages)
        ):
            raise ValueError("prepared source PageIndex identity does not match its source")
        if _SHA256.fullmatch(self.parser_fingerprint) is None:
            raise ValueError("parser fingerprint is not canonical")


class KnowledgeObjectStore:
    """Immutable source/build objects with one atomic active-build pointer."""

    def __init__(self, state_root: Path) -> None:
        if not state_root.is_absolute():
            raise ValueError("knowledge object state root must be absolute")
        self._root = state_root / "objects"

    def put(self, prepared: PreparedKnowledgeSource) -> Path:
        target = self.object_path(
            prepared.source.source_id,
            prepared.source.content_hash,
            prepared.parser_fingerprint,
        )
        if target.exists():
            existing_manifest = self._verified_manifest(target)
            expected_tree_hash = _sha256(canonical_json_bytes(_tree_json(prepared.page_index)))
            if (
                existing_manifest["source"] != _source_json(prepared.source)
                or existing_manifest["pageCount"] != len(prepared.pages)
                or existing_manifest["parserFingerprint"] != prepared.parser_fingerprint
                or existing_manifest["treeHash"] != expected_tree_hash
            ):
                raise KnowledgeObjectError("knowledge object identity collides with different prepared content")
            self._activate(prepared, target)
            return target
        self._root.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix="object-", dir=self._root))
        try:
            files = self._write_object(temporary, prepared)
            manifest = canonical_json_bytes(
                {
                    "files": files,
                    "pageCount": len(prepared.pages),
                    "parserFingerprint": prepared.parser_fingerprint,
                    "schemaVersion": _OBJECT_SCHEMA_VERSION,
                    "source": _source_json(prepared.source),
                    "treeHash": _sha256(_read_bytes(temporary / "pageindex" / "tree.json")),
                }
            )
            _write_bytes(temporary / "manifest.json", manifest)
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(_windows_extended_path(temporary), _windows_extended_path(target))
            self.verify(target)
            self._activate(prepared, target)
            return target
        except BaseException:
            shutil.rmtree(temporary, ignore_errors=True)
            raise

    def object_path(
        self,
        source_id: str,
        source_hash: str,
        parser_fingerprint: str | None = None,
    ) -> Path:
        if _SOURCE_ID.fullmatch(source_id) is None or _SHA256.fullmatch(source_hash) is None:
            raise ValueError("knowledge object identity is invalid")
        revision = source_hash.removeprefix("sha256:")[:_OBJECT_REVISION_DIGEST_LENGTH]
        source_root = self._root / source_id
        if parser_fingerprint is not None:
            if _SHA256.fullmatch(parser_fingerprint) is None:
                raise ValueError("knowledge object parser fingerprint is invalid")
            build = parser_fingerprint.removeprefix("sha256:")[:12]
            return source_root / f"rev-{revision}-idx-{build}"
        pointer = source_root / f"active-{revision}.json"
        if pointer.exists():
            try:
                raw = pointer.read_bytes()
                value = json.loads(raw.decode("utf-8", errors="strict"))
            except (OSError, UnicodeError, json.JSONDecodeError) as error:
                raise KnowledgeObjectError("knowledge active object pointer is unreadable") from error
            if (
                canonical_json_bytes(value) != raw
                or not isinstance(value, dict)
                or set(value)
                != {
                    "objectDirectory",
                    "parserFingerprint",
                    "sourceHash",
                    "sourceId",
                }
            ):
                raise KnowledgeObjectError("knowledge active object pointer is invalid")
            expected = self.object_path(source_id, source_hash, _string(value["parserFingerprint"]))
            if (
                value["sourceId"] != source_id
                or value["sourceHash"] != source_hash
                or value["objectDirectory"] != expected.name
            ):
                raise KnowledgeObjectError("knowledge active object pointer identity is invalid")
            return expected
        return source_root / f"rev-{revision}"

    def object_relative_path(
        self,
        source_id: str,
        source_hash: str,
        parser_fingerprint: str | None = None,
    ) -> str:
        """Return the stable Vault-relative path of a prepared object."""

        return self.object_path(source_id, source_hash, parser_fingerprint).relative_to(self._root.parent).as_posix()

    def verify(self, object_root: Path) -> None:
        self._verified_manifest(object_root)

    def load(
        self,
        source_id: str,
        source_hash: str,
        parser_fingerprint: str | None = None,
    ) -> PreparedKnowledgeSource:
        object_root = self.object_path(source_id, source_hash, parser_fingerprint)
        manifest = self._verified_manifest(object_root)
        try:
            source = _source_from_json(manifest["source"])
            page_count = _integer(manifest["pageCount"], minimum=1)
            pages = tuple(_read_page(object_root, source, page_number) for page_number in range(1, page_count + 1))
            tree_value = json.loads(
                _read_bytes(object_root / "pageindex" / "tree.json").decode("utf-8", errors="strict")
            )
            tree = _tree_from_json(tree_value)
            parser_fingerprint = _string(manifest["parserFingerprint"])
            loaded = PreparedKnowledgeSource(source, pages, tree, parser_fingerprint)
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as error:
            raise KnowledgeObjectError("knowledge object projections are invalid") from error
        if source.source_id != source_id or source.content_hash != source_hash:
            raise KnowledgeObjectError("knowledge object path identity does not match its manifest")
        return loaded

    def _verified_manifest(self, object_root: Path) -> dict[str, object]:
        try:
            raw = _read_bytes(object_root / "manifest.json")
            decoded = json.loads(raw.decode("utf-8", errors="strict"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise KnowledgeObjectError("knowledge object manifest is unreadable") from error
        if canonical_json_bytes(decoded) != raw or not isinstance(decoded, dict):
            raise KnowledgeObjectError("knowledge object manifest is not canonical")
        if set(decoded) != {"files", "pageCount", "parserFingerprint", "schemaVersion", "source", "treeHash"}:
            raise KnowledgeObjectError("knowledge object manifest shape is invalid")
        if (
            isinstance(decoded["schemaVersion"], bool)
            or decoded["schemaVersion"] != _OBJECT_SCHEMA_VERSION
            or not isinstance(decoded["files"], list)
        ):
            raise KnowledgeObjectError("knowledge object manifest schema is invalid")
        try:
            manifest_source = _source_from_json(decoded["source"])
            _integer(decoded["pageCount"], minimum=1)
            parser_fingerprint = _string(decoded["parserFingerprint"])
            tree_hash = _string(decoded["treeHash"])
        except (TypeError, ValueError) as error:
            raise KnowledgeObjectError("knowledge object manifest metadata is invalid") from error
        if _SHA256.fullmatch(parser_fingerprint) is None or _SHA256.fullmatch(tree_hash) is None:
            raise KnowledgeObjectError("knowledge object manifest hashes are invalid")
        revision = manifest_source.content_hash.removeprefix("sha256:")[:_OBJECT_REVISION_DIGEST_LENGTH]
        source_root = self._root / manifest_source.source_id
        build = parser_fingerprint.removeprefix("sha256:")[:12]
        valid_directories = {
            source_root / f"rev-{revision}",
            source_root / f"rev-{revision}-idx-{build}",
        }
        if object_root not in valid_directories:
            raise KnowledgeObjectError("knowledge object directory identity is invalid")
        io_root = _windows_extended_path(object_root)
        entries = tuple(io_root.rglob("*"))
        if any(path.is_symlink() for path in entries):
            raise KnowledgeObjectError("knowledge objects cannot contain symbolic links")
        actual_files = {
            path.relative_to(io_root).as_posix() for path in entries if path.is_file() and path.name != "manifest.json"
        }
        seen_paths: set[str] = set()
        for item in decoded["files"]:
            if not isinstance(item, dict) or set(item) != {"byteSize", "path", "sha256"}:
                raise KnowledgeObjectError("knowledge object file record is invalid")
            relative = item["path"]
            byte_size = item["byteSize"]
            content_hash = item["sha256"]
            portable = PurePosixPath(relative) if isinstance(relative, str) else None
            if (
                portable is None
                or portable.is_absolute()
                or "\\" in relative
                or any(part in {"", ".", ".."} for part in portable.parts)
                or relative in seen_paths
                or isinstance(byte_size, bool)
                or not isinstance(byte_size, int)
                or byte_size < 0
                or not isinstance(content_hash, str)
                or _SHA256.fullmatch(content_hash) is None
            ):
                raise KnowledgeObjectError("knowledge object file path is invalid")
            seen_paths.add(relative)
            try:
                content = _read_bytes(object_root / relative)
            except OSError as error:
                raise KnowledgeObjectError("knowledge object file is missing") from error
            if byte_size != len(content) or content_hash != _sha256(content):
                raise KnowledgeObjectError("knowledge object file integrity mismatch")
        if seen_paths != actual_files:
            raise KnowledgeObjectError("knowledge object manifest does not match its files")
        try:
            tree = _read_bytes(object_root / "pageindex" / "tree.json")
        except OSError as error:
            raise KnowledgeObjectError("knowledge object PageIndex is missing") from error
        if decoded["treeHash"] != _sha256(tree):
            raise KnowledgeObjectError("knowledge object tree identity mismatch")
        return decoded

    def _activate(self, prepared: PreparedKnowledgeSource, target: Path) -> None:
        revision = prepared.source.content_hash.removeprefix("sha256:")[:_OBJECT_REVISION_DIGEST_LENGTH]
        pointer = target.parent / f"active-{revision}.json"
        payload = canonical_json_bytes(
            {
                "objectDirectory": target.name,
                "parserFingerprint": prepared.parser_fingerprint,
                "sourceHash": prepared.source.content_hash,
                "sourceId": prepared.source.source_id,
            }
        )
        if pointer.exists() and pointer.read_bytes() == payload:
            return
        pointer.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(prefix="active-", suffix=".tmp", dir=pointer.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(_windows_extended_path(temporary), _windows_extended_path(pointer))
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise

    @staticmethod
    def _write_object(root: Path, prepared: PreparedKnowledgeSource) -> list[dict[str, object]]:
        payloads: list[tuple[str, bytes]] = []
        for page in prepared.pages:
            header = (
                "---\n"
                f"source_id: {prepared.source.source_id}\n"
                f"source_hash: {prepared.source.content_hash}\n"
                f"page: {page.page_number}\n"
                f"page_hash: {page.content_hash}\n"
                "---\n\n"
            )
            payloads.append((f"evidence/pages/{page.page_number:04d}.md", (header + page.text).encode("utf-8")))
        tree_payload = canonical_json_bytes(_tree_json(prepared.page_index))
        payloads.append(("pageindex/tree.json", tree_payload))
        nodes = b"".join(canonical_json_bytes(_node_json(node)) + b"\n" for node in prepared.page_index.nodes)
        payloads.append(("pageindex/nodes.jsonl", nodes))
        document = canonical_json_bytes(
            {
                "pageCount": len(prepared.pages),
                "parserFingerprint": prepared.parser_fingerprint,
                "schemaVersion": _OBJECT_SCHEMA_VERSION,
                "source": _source_json(prepared.source),
            }
        )
        payloads.append(("evidence/document.json", document))
        records: list[dict[str, object]] = []
        for relative, content in sorted(payloads):
            _write_bytes(root / relative, content)
            records.append({"byteSize": len(content), "path": relative, "sha256": _sha256(content)})
        return records


def _source_json(source: SourceRecord) -> dict[str, object]:
    return {
        "byteSize": source.byte_size,
        "contentHash": source.content_hash,
        "mediaType": source.media_type,
        "path": source.relative_path,
        "sourceId": source.source_id,
    }


def _tree_json(tree: PageIndexTree) -> dict[str, object]:
    return {
        "nodes": [_node_json(node) for node in tree.nodes],
        "pageCount": tree.page_count,
        "rootId": tree.root_id,
        "schemaVersion": _OBJECT_SCHEMA_VERSION,
        "sourceHash": tree.source_hash,
        "sourceId": tree.source_id,
    }


def _node_json(node: object) -> dict[str, object]:
    if not isinstance(node, PageIndexNode):
        raise TypeError("PageIndex projection requires PageIndexNode values")
    return {
        "children": list(node.children),
        "depth": node.depth,
        "endPage": node.end_page,
        "nodeId": node.node_id,
        "parentId": node.parent_id,
        "startPage": node.start_page,
        "summary": node.summary,
        "title": node.title,
    }


def _source_from_json(value: object) -> SourceRecord:
    item = _object(value, {"byteSize", "contentHash", "mediaType", "path", "sourceId"})
    return SourceRecord(
        _string(item["sourceId"]),
        _string(item["path"]),
        _string(item["contentHash"]),
        _string(item["mediaType"]),
        _integer(item["byteSize"], minimum=0),
    )


def _tree_from_json(value: object) -> PageIndexTree:
    item = _object(
        value,
        {"nodes", "pageCount", "rootId", "schemaVersion", "sourceHash", "sourceId"},
    )
    if item["schemaVersion"] != _OBJECT_SCHEMA_VERSION or not isinstance(item["nodes"], list):
        raise ValueError("PageIndex projection schema is invalid")
    nodes = tuple(_node_from_json(node) for node in item["nodes"])
    return PageIndexTree(
        _string(item["sourceId"]),
        _string(item["sourceHash"]),
        _integer(item["pageCount"], minimum=1),
        _string(item["rootId"]),
        nodes,
    )


def _node_from_json(value: object) -> PageIndexNode:
    item = _object(
        value,
        {"children", "depth", "endPage", "nodeId", "parentId", "startPage", "summary", "title"},
    )
    parent = item["parentId"]
    if parent is not None and not isinstance(parent, str):
        raise ValueError("PageIndex parent identity is invalid")
    children = item["children"]
    if not isinstance(children, list):
        raise ValueError("PageIndex children are invalid")
    return PageIndexNode(
        _string(item["nodeId"]),
        parent,
        _integer(item["depth"], minimum=0),
        _string(item["title"]),
        _string(item["summary"], allow_empty=True),
        _integer(item["startPage"], minimum=1),
        _integer(item["endPage"], minimum=1),
        tuple(_string(child) for child in children),
    )


def _read_page(root: Path, source: SourceRecord, page_number: int) -> PageEvidence:
    path = root / "evidence" / "pages" / f"{page_number:04d}.md"
    payload = _read_bytes(path)
    separator = b"---\n\n"
    if not payload.startswith(b"---\n") or separator not in payload:
        raise ValueError("evidence page envelope is invalid")
    _, text_bytes = payload.split(separator, maxsplit=1)
    text = text_bytes.decode("utf-8", errors="strict")
    page = PageEvidence(page_number, text, _sha256(text_bytes))
    expected_header = (
        "---\n"
        f"source_id: {source.source_id}\n"
        f"source_hash: {source.content_hash}\n"
        f"page: {page.page_number}\n"
        f"page_hash: {page.content_hash}\n"
        "---\n\n"
    ).encode()
    if payload != expected_header + text_bytes:
        raise ValueError("evidence page envelope identity is invalid")
    return page


def _object(value: object, keys: set[str]) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != keys or any(not isinstance(key, str) for key in value):
        raise ValueError("knowledge object projection shape is invalid")
    return value


def _string(value: object, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value):
        raise ValueError("knowledge object projection string is invalid")
    return value


def _integer(value: object, *, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError("knowledge object projection integer is invalid")
    return value


def _write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with _windows_extended_path(path).open("xb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def _read_bytes(path: Path) -> bytes:
    with _windows_extended_path(path).open("rb") as stream:
        return stream.read()


def _windows_extended_path(path: Path) -> Path:
    if os.name != "nt":
        return path
    absolute = os.path.abspath(path)
    if absolute.startswith("\\\\?\\"):
        return Path(absolute)
    if absolute.startswith("\\\\"):
        return Path(f"\\\\?\\UNC\\{absolute[2:]}")
    return Path(f"\\\\?\\{absolute}")


def _sha256(content: bytes) -> str:
    return f"sha256:{hashlib.sha256(content).hexdigest()}"
