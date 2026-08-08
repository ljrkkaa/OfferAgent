from __future__ import annotations

import hashlib
import os
from pathlib import Path, PurePosixPath

from offeragent_harness.foundation.canonical import canonical_json_bytes

from .models import KnowledgeCandidate, KnowledgeStatus, SourceRecord, SourceState
from .naming import stable_readable_id

_MEDIA_BY_SUFFIX = {
    ".md": "text/markdown",
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}


class KnowledgeDiscoveryError(RuntimeError):
    pass


class KnowledgeDiscovery:
    def __init__(self, *, workspace_id: str, vault_root: Path, maximum_file_bytes: int = 64 * 1024 * 1024) -> None:
        if not workspace_id or maximum_file_bytes < 1:
            raise ValueError("knowledge discovery configuration is invalid")
        self._workspace_id = workspace_id
        self._root = vault_root.resolve(strict=True)
        self._raw = self._root / "raw"
        self._maximum_file_bytes = maximum_file_bytes

    def status(self, *, catalog_revision: int, catalog: tuple[SourceRecord, ...]) -> KnowledgeStatus:
        if catalog_revision < 0:
            raise ValueError("catalog_revision cannot be negative")
        known = {record.source_id: record for record in catalog}
        discovered = self._discover()
        candidates: list[KnowledgeCandidate] = []
        for source in discovered:
            previous = known.get(source.source_id)
            state = (
                SourceState.NEW
                if previous is None
                else (SourceState.UNCHANGED if previous.content_hash == source.content_hash else SourceState.CHANGED)
            )
            candidates.append(
                KnowledgeCandidate(
                    candidate_id=_candidate_id(catalog_revision, source),
                    catalog_revision=catalog_revision,
                    state=state,
                    source=source,
                )
            )
        live_ids = {item.source_id for item in discovered}
        missing = tuple(
            sorted(
                (item for item in catalog if item.source_id not in live_ids), key=lambda x: x.relative_path.casefold()
            )
        )
        return KnowledgeStatus(catalog_revision, tuple(candidates), missing)

    def _discover(self) -> tuple[SourceRecord, ...]:
        if not self._raw.exists():
            return ()
        raw = self._raw.resolve(strict=True)
        if not raw.is_dir() or os.path.commonpath((str(self._root), str(raw))) != str(self._root):
            raise KnowledgeDiscoveryError("raw root is not a safe workspace directory")
        records: list[SourceRecord] = []
        for path in raw.rglob("*"):
            if path.is_symlink():
                raise KnowledgeDiscoveryError("raw sources cannot contain symbolic links")
            if not path.is_file():
                continue
            media_type = _MEDIA_BY_SUFFIX.get(path.suffix.casefold())
            if media_type is None:
                continue
            size = path.stat().st_size
            if size > self._maximum_file_bytes:
                raise KnowledgeDiscoveryError("raw source exceeds the configured size limit")
            relative = PurePosixPath(path.relative_to(self._root).as_posix()).as_posix()
            content_hash = _file_hash(path)
            records.append(
                SourceRecord(
                    source_id=_source_id(self._workspace_id, relative),
                    relative_path=relative,
                    content_hash=content_hash,
                    media_type=media_type,
                    byte_size=size,
                )
            )
        records.sort(key=lambda item: item.relative_path.casefold())
        return tuple(records)


def _source_id(workspace_id: str, relative_path: str) -> str:
    label = PurePosixPath(relative_path).stem
    return stable_readable_id(
        "src",
        label,
        namespace=f"{workspace_id}:{relative_path}",
        max_length=48,
    )


def _candidate_id(revision: int, source: SourceRecord) -> str:
    digest = hashlib.sha256(
        canonical_json_bytes(
            {"catalogRevision": revision, "contentHash": source.content_hash, "path": source.relative_path}
        )
    ).hexdigest()
    return f"sha256:{digest}"


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"
