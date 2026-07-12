"""Atomic domain-state, event and invocation-journal boundary."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from .events import EventStore
from .storage import EntityStore, InvocationJournal


@runtime_checkable
class UnitOfWork(Protocol):
    @property
    def entities(self) -> EntityStore: ...

    @property
    def events(self) -> EventStore: ...

    @property
    def journal(self) -> InvocationJournal: ...

    async def __aenter__(self) -> UnitOfWork: ...

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None: ...

    async def commit(self) -> None: ...

    async def rollback(self) -> None: ...


@runtime_checkable
class UnitOfWorkFactory(Protocol):
    def begin(self) -> UnitOfWork: ...


__all__ = ["UnitOfWork", "UnitOfWorkFactory"]
