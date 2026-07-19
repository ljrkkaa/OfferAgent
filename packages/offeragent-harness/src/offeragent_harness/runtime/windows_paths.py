"""Windows path projection kept separate from caller-visible path identity."""

from __future__ import annotations

import os
from pathlib import Path


def windows_extended_path(path: Path) -> Path:
    """Return an absolute Win32 extended-length path at an OS operation boundary."""

    absolute = os.path.abspath(path)
    if os.name != "nt" or absolute.startswith("\\\\?\\"):
        return Path(absolute)
    if absolute.startswith("\\\\"):
        return Path(f"\\\\?\\UNC\\{absolute[2:]}")
    return Path(f"\\\\?\\{absolute}")


__all__ = ["windows_extended_path"]
