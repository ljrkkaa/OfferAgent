"""Worker-start reconciliation for queued, expired and orphaned child Runs."""

from __future__ import annotations

from offeragent_harness.ports.subagents import SubagentOwnershipCleaner

from .service import SubagentService


class RunRecoverySupervisor:
    def __init__(self, service: SubagentService, cleaner: SubagentOwnershipCleaner) -> None:
        self._service = service
        self._cleaner = cleaner

    async def recover(self) -> tuple[str, ...]:
        return await self._service.recover_orphans(self._cleaner)


__all__ = ["RunRecoverySupervisor"]
