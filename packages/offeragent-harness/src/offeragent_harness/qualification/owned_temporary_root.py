"""Fail-closed cleanup for qualification-owned temporary directory trees."""

from __future__ import annotations

import os
import shutil
import stat
from collections.abc import Callable
from pathlib import Path
from types import TracebackType

from offeragent_harness.runtime.windows_paths import windows_extended_path

_OWNERSHIP_MARKER = ".offeragent-qualification-owner.json"


class OwnedTemporaryRootError(RuntimeError):
    """A temporary root cannot be proven to belong to this qualification run."""


def remove_owned_temporary_root(root: Path, expected_marker: bytes) -> None:
    marker = windows_extended_path(root / _OWNERSHIP_MARKER)
    try:
        info = marker.lstat()
        actual = marker.read_bytes()
    except OSError as error:
        raise OwnedTemporaryRootError("qualification temp ownership marker is unavailable") from error
    if marker.is_symlink() or info.st_nlink != 1 or actual != expected_marker:
        raise OwnedTemporaryRootError("qualification temp ownership identity differs")

    def clear_read_only(
        function: Callable[[str], object],
        path: str,
        error_info: tuple[type[BaseException], BaseException, TracebackType],
    ) -> None:
        error = error_info[1]
        if not isinstance(error, PermissionError):
            raise error
        filesystem_path = windows_extended_path(Path(path))
        os.chmod(filesystem_path, stat.S_IWRITE)
        function(str(filesystem_path))

    shutil.rmtree(windows_extended_path(root), onerror=clear_read_only)


__all__ = ["OwnedTemporaryRootError", "remove_owned_temporary_root"]
