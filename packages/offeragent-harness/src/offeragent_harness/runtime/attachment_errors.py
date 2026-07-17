"""Dependency-free attachment failures shared with transport error mapping."""

from __future__ import annotations


class AttachmentError(RuntimeError):
    """A bounded user-actionable attachment failure."""

    def __init__(self, code: str, message: str) -> None:
        if not code or len(code) > 128:
            raise ValueError("attachment error code is invalid")
        self.code = code
        super().__init__(message)


__all__ = ["AttachmentError"]
