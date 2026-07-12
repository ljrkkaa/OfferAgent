"""Persistent approval presentation and resolution boundary."""

from typing import Protocol, runtime_checkable

from offeragent_harness.permissions import ApprovalRequest, ApprovalResolution

from .cancellation import CancellationToken


@runtime_checkable
class ApprovalPort(Protocol):
    async def request(
        self,
        approval: ApprovalRequest,
        cancellation: CancellationToken,
    ) -> ApprovalResolution: ...

    async def cancel(self, approval_id: str, reason: str) -> None: ...

    async def pending(self, approval_id: str) -> ApprovalRequest | None: ...


__all__ = ["ApprovalPort"]
