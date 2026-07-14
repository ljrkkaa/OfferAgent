"""Persistent approval presentation and resolution boundary."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from offeragent_harness.permissions import ApprovalDecisionReceipt, ApprovalRequest, ApprovalResolution

from .cancellation import CancellationToken


@runtime_checkable
class ApprovalPort(Protocol):
    async def request(
        self,
        approval: ApprovalRequest,
        cancellation: CancellationToken,
        observer: ApprovalObserver | None = None,
    ) -> ApprovalDecisionReceipt: ...

    async def cancel(self, approval_id: str, reason: str) -> None: ...

    async def pending(self, approval_id: str) -> ApprovalRequest | None: ...


@runtime_checkable
class ApprovalObserver(Protocol):
    async def required(self, approval: ApprovalRequest) -> None: ...

    async def resolved(self, approval: ApprovalRequest, resolution: ApprovalResolution) -> None: ...


__all__ = ["ApprovalObserver", "ApprovalPort"]
