# System Packages
from __future__ import annotations  # to avoid quoting type hints

from enum import Enum


class SearchType(str, Enum):
    All = "all"
    Markdown = "markdown"
    Pdf = "pdf"
    Plaintext = "plaintext"
