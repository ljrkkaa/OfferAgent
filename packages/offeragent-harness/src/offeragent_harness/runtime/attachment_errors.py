"""Dependency-free attachment failures shared with transport error mapping."""

from __future__ import annotations


class AttachmentError(RuntimeError):
    """A bounded user-actionable attachment failure."""

    def __init__(self, code: str, message: str, *, item_order: int | None = None) -> None:
        if not code or len(code) > 128:
            raise ValueError("attachment error code is invalid")
        if item_order is not None and item_order < 0:
            raise ValueError("attachment error item order cannot be negative")
        self.code = code
        self.item_order = item_order
        super().__init__(message)


__all__ = ["AttachmentError"]
