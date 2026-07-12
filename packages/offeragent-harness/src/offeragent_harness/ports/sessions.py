"""Persistent conversation aggregate boundary."""

from typing import Protocol, runtime_checkable

from offeragent_harness.sessions import Run, Session, Turn


@runtime_checkable
class ConversationStore(Protocol):
    async def get_session(self, session_id: str) -> Session | None: ...

    async def save_session(self, session: Session, *, expected_revision: int) -> None: ...

    async def get_turn(self, turn_id: str) -> Turn | None: ...

    async def save_turn(self, turn: Turn) -> None: ...

    async def get_run(self, run_id: str) -> Run | None: ...

    async def save_run(self, run: Run, *, expected_event_sequence: int) -> None: ...


__all__ = ["ConversationStore"]
