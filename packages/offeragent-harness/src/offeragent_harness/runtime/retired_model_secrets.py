"""One-way, value-free retirement of obsolete model-provider Secret envelopes."""

from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path

from offeragent_harness.ports.secrets import SecretKind

from .windows_secrets import parse_secret_envelope_metadata

_MAX_ENVELOPE_BYTES = 2 * 1024 * 1024
_SCOPE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:@-]{0,255}")


class RetiredModelSecretMigrationError(RuntimeError):
    """A model Secret could not be retired without guessing its identity."""


@dataclass(frozen=True, slots=True)
class RetiredModelSecretMigrationReport:
    deleted_count: int
    provider_ids: tuple[str, ...]
    unclassified_count: int


def purge_retired_model_secrets(root: Path, *, scope_id: str) -> RetiredModelSecretMigrationReport:
    """Delete only exact legacy model-provider envelopes for one Workspace.

    The encrypted value is never decoded or copied into the report. Unknown,
    corrupt, non-regular and non-model entries remain untouched.
    """

    if not root.is_absolute():
        raise ValueError("Secret migration root must be absolute")
    if _SCOPE_ID.fullmatch(scope_id) is None:
        raise ValueError("Secret migration scope is invalid")
    if not root.exists():
        return RetiredModelSecretMigrationReport(0, (), 0)
    if not root.is_dir() or root.is_symlink():
        raise RetiredModelSecretMigrationError("Secret migration root is not a regular directory")

    providers: set[str] = set()
    deleted = 0
    unclassified = 0
    for path in sorted(root.glob("*.secret"), key=lambda item: item.name):
        try:
            before = path.lstat()
            if path.is_symlink() or not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                unclassified += 1
                continue
            if before.st_size < 2 or before.st_size > _MAX_ENVELOPE_BYTES:
                unclassified += 1
                continue
            raw = path.read_bytes()
            after_read = path.lstat()
            if not _same_file(before, after_read) or len(raw) != before.st_size:
                unclassified += 1
                continue
            metadata = parse_secret_envelope_metadata(raw, filename=path.name)
        except (OSError, UnicodeError, TypeError, ValueError):
            unclassified += 1
            continue
        if metadata.kind is not SecretKind.MODEL_PROVIDER:
            continue
        if metadata.scope_id != scope_id:
            continue

        try:
            before_delete = path.lstat()
            if not _same_file(before, before_delete):
                unclassified += 1
                continue
            path.unlink()
        except OSError as error:
            raise RetiredModelSecretMigrationError("A classified model-provider Secret could not be retired") from error
        providers.add(metadata.provider_id)
        deleted += 1
    return RetiredModelSecretMigrationReport(deleted, tuple(sorted(providers)), unclassified)


def _same_file(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        left.st_dev,
        left.st_ino,
        left.st_size,
        left.st_mtime_ns,
        left.st_ctime_ns,
    ) == (
        right.st_dev,
        right.st_ino,
        right.st_size,
        right.st_mtime_ns,
        right.st_ctime_ns,
    )


__all__ = [
    "RetiredModelSecretMigrationError",
    "RetiredModelSecretMigrationReport",
    "purge_retired_model_secrets",
]
