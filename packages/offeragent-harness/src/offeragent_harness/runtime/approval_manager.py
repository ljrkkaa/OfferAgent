from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime

from offeragent_harness.error_codes import ResourceConflictCause, ResourceNotFoundCause
from offeragent_harness.permissions import (
    APPROVAL_GRANT_COLLECTION,
    ApprovalBinding,
    ApprovalDecisionReceipt,
    ApprovalGrantRepository,
    ApprovalRequest,
    ApprovalResolution,
    ApprovalScope,
    ApprovalState,
    GrantExpiryPolicy,
    grant_from_resolution,
)
from offeragent_harness.ports import (
    ApprovalObserver,
    CancellationToken,
    Clock,
    EntityRevisionConflict,
    EntityStore,
    UnitOfWorkFactory,
)


class ApprovalError(RuntimeError):
    pass


class ApprovalConflict(ApprovalError, ResourceConflictCause):
    pass


class ApprovalNotFound(ApprovalError, ResourceNotFoundCause):
    pass


@dataclass(frozen=True, slots=True)
class ApprovalRecord:
    request: ApprovalRequest
    state: ApprovalState
    revision: int
    resolution: ApprovalResolution | None = None

    def __post_init__(self) -> None:
        if self.revision < 1:
            raise ValueError("approval revision starts at 1")
        if (self.state is ApprovalState.PENDING) != (self.resolution is None):
            raise ValueError("only pending approvals omit a resolution")
        if self.resolution is not None:
            if self.resolution.approval_id != self.request.approval_id:
                raise ValueError("approval resolution identity must match its request")
            if self.resolution.state is not self.state:
                raise ValueError("approval record state must match its resolution")


class ApprovalManager:
    """Durable approval state with reconnect/restart-safe wait semantics."""

    def __init__(
        self,
        *,
        unit_of_work: UnitOfWorkFactory,
        clock: Clock,
        grant_expiry_policy: GrantExpiryPolicy | None = None,
    ) -> None:
        self._unit_of_work = unit_of_work
        self._clock = clock
        self._grants = ApprovalGrantRepository(unit_of_work)
        self._grant_expiry_policy = grant_expiry_policy or (lambda _request, _resolution: None)
        self._waiters: dict[str, asyncio.Future[ApprovalResolution]] = {}
        self._lock = asyncio.Lock()

    async def request(
        self,
        approval: ApprovalRequest,
        cancellation: CancellationToken,
        observer: ApprovalObserver | None = None,
    ) -> ApprovalDecisionReceipt:
        record = await self._ensure_pending(approval)
        effective_request = record.request
        if record.resolution is not None:
            if observer is not None:
                await observer.resolved(record.request, record.resolution)
            return ApprovalDecisionReceipt(record.request, record.resolution)
        if observer is not None:
            await observer.required(effective_request)

        async with self._lock:
            future = self._waiters.get(effective_request.approval_id)
            if future is None or future.done():
                future = asyncio.get_running_loop().create_future()
                self._waiters[effective_request.approval_id] = future

        # Close the durable-state -> in-memory-waiter lost-wakeup window. If a
        # resolver committed between _ensure_pending() and waiter registration,
        # this read observes it; if it commits after the read, resolve() sees
        # the registered future and completes it.
        refreshed = await self._get_record(effective_request.approval_id)
        if refreshed is None:
            raise ApprovalNotFound(effective_request.approval_id)
        if refreshed.resolution is not None and not future.done():
            future.set_result(refreshed.resolution)

        cancellation_task = asyncio.ensure_future(cancellation.wait())
        expiry_task = asyncio.create_task(self._clock.sleep_until(effective_request.binding.expires_at))
        try:
            done, pending = await asyncio.wait(
                (future, cancellation_task, expiry_task),
                return_when=asyncio.FIRST_COMPLETED,
            )
        except BaseException:
            # A transport/client task may disappear while the durable approval
            # remains valid. Clean up only this request's helper tasks; never
            # cancel the shared approval future or mutate persistent state.
            for helper_task in (cancellation_task, expiry_task):
                helper_task.cancel()
            await asyncio.gather(cancellation_task, expiry_task, return_exceptions=True)
            raise
        cleanup_tasks = [task for task in pending if task is not future]
        for pending_task in cleanup_tasks:
            pending_task.cancel()
        if cleanup_tasks:
            await asyncio.gather(*cleanup_tasks, return_exceptions=True)
        if future in done:
            result = future.result()
        elif cancellation_task in done:
            reason = cancellation_task.result()
            result = await self._resolve_system(
                effective_request.approval_id,
                state=ApprovalState.CANCELLED,
                resolver_id="runtime:cancellation",
                reason=reason.message,
            )
        else:
            result = await self._resolve_system(
                effective_request.approval_id,
                state=ApprovalState.EXPIRED,
                resolver_id="runtime:expiry",
                reason="approval expired",
            )
        async with self._lock:
            if self._waiters.get(effective_request.approval_id) is future:
                self._waiters.pop(effective_request.approval_id, None)
        if observer is not None:
            await observer.resolved(record.request, result)
        return ApprovalDecisionReceipt(record.request, result)

    async def resolve(self, resolution: ApprovalResolution) -> ApprovalResolution:
        async with self._lock:
            record = await self._get_record(resolution.approval_id)
            if record is None:
                raise ApprovalNotFound(resolution.approval_id)
            now = self._clock.utcnow()
            if resolution.resolved_at > now:
                raise ApprovalConflict("approval resolution time cannot be in the future")
            effective = _effective_resolution(record, resolution, now)
            if record.resolution is not None:
                if not _same_resolution_intent(record.resolution, effective):
                    raise ApprovalConflict("approval already resolved with a different decision")
                waiter = self._waiters.get(resolution.approval_id)
                if waiter is not None and not waiter.done():
                    waiter.set_result(record.resolution)
                return record.resolution

            updated = ApprovalRecord(
                request=record.request,
                state=effective.state,
                revision=record.revision + 1,
                resolution=effective,
            )
            grant = None
            if effective.state is ApprovalState.APPROVED and effective.scope is not ApprovalScope.ONCE:
                if not record.request.binding.has_grant_identity:
                    raise ApprovalConflict("reusable approval is missing session or Agent lineage identity")
                grant = grant_from_resolution(
                    record.request,
                    effective,
                    expires_at=self._grant_expiry_policy(record.request, effective),
                )
            try:
                async with self._unit_of_work.begin() as uow:
                    await uow.entities.put(
                        "approvals",
                        resolution.approval_id,
                        updated,
                        expected_revision=record.revision,
                    )
                    if grant is not None:
                        await uow.entities.put(
                            APPROVAL_GRANT_COLLECTION,
                            grant.grant_id,
                            grant,
                            expected_revision=0,
                        )
                    await uow.commit()
            except EntityRevisionConflict as error:
                current = await self._get_record(resolution.approval_id)
                if (
                    current is not None
                    and current.resolution is not None
                    and _same_resolution_intent(current.resolution, effective)
                ):
                    waiter = self._waiters.get(resolution.approval_id)
                    if waiter is not None and not waiter.done():
                        waiter.set_result(current.resolution)
                    return current.resolution
                raise ApprovalConflict("approval resolution lost a compare-and-swap race") from error
            waiter = self._waiters.get(resolution.approval_id)
            if waiter is not None and not waiter.done():
                waiter.set_result(effective)
            return effective

    async def _resolve_system(
        self,
        approval_id: str,
        *,
        state: ApprovalState,
        resolver_id: str,
        reason: str,
    ) -> ApprovalResolution:
        resolution = ApprovalResolution(
            approval_id=approval_id,
            state=state,
            scope=ApprovalScope.ONCE,
            resolved_at=self._clock.utcnow(),
            resolver_id=resolver_id,
            include_descendants=False,
            reason=reason,
        )
        return await self.resolve(resolution)

    async def cancel(self, approval_id: str, reason: str) -> None:
        await self._resolve_system(
            approval_id,
            state=ApprovalState.CANCELLED,
            resolver_id="runtime:cancel",
            reason=reason,
        )

    async def pending(self, approval_id: str) -> ApprovalRequest | None:
        async with self._unit_of_work.begin() as uow:
            record = await uow.entities.get("approvals", approval_id)
            if record is None:
                return None
            checked = _checked_record(record, approval_id)
            checked, changed = _expire_record_if_due(checked, self._clock.utcnow())
            if changed:
                await uow.entities.put("approvals", approval_id, checked, expected_revision=checked.revision - 1)
                await uow.commit()
            return checked.request if checked.state is ApprovalState.PENDING else None

    async def get(self, approval_id: str) -> ApprovalRecord | None:
        """Return the durable approval snapshot for command-bound identity checks."""

        return await self._get_record(approval_id)

    async def find_pending(self, tool_call_id: str, binding: ApprovalBinding) -> ApprovalRequest | None:
        """Find an exact durable pending request without widening or extending it."""

        if not tool_call_id or not isinstance(binding, ApprovalBinding):
            raise TypeError("find_pending requires a tool_call_id and ApprovalBinding")
        async with self._unit_of_work.begin() as uow:
            records = await _scan_approval_records(uow.entities)
            matching_call = tuple(record for _, record in records if record.request.tool_call_id == tool_call_id)
            if not matching_call:
                return None
            if len(matching_call) != 1:
                raise ApprovalConflict("multiple durable approvals exist for one ToolCall")
            record = matching_call[0]
            if not record.request.binding.same_recovery_identity(binding):
                raise ApprovalConflict("pending approval binding drifted")
            record, changed = _expire_record_if_due(record, self._clock.utcnow())
            if changed:
                await uow.entities.put(
                    "approvals",
                    record.request.approval_id,
                    record,
                    expected_revision=record.revision - 1,
                )
                await uow.commit()
        return record.request if record.state is ApprovalState.PENDING else None

    async def list_pending(
        self,
        *,
        workspace_id: str,
        session_id: str,
        principal_id: str,
    ) -> tuple[ApprovalRequest, ...]:
        """Return reconnect-safe pending approvals visible to exactly one client principal."""

        if not workspace_id or not session_id or not principal_id:
            raise ValueError("pending approval listing requires workspace, session and principal identity")
        now = self._clock.utcnow()
        visible: list[ApprovalRequest] = []
        async with self._unit_of_work.begin() as uow:
            changed = False
            records = await _scan_approval_records(uow.entities)
            for entity_id, original in records:
                record, expired = _expire_record_if_due(original, now)
                if expired:
                    await uow.entities.put(
                        "approvals",
                        entity_id,
                        record,
                        expected_revision=original.revision,
                    )
                    changed = True
                    continue
                binding = record.request.binding
                if (
                    record.state is ApprovalState.PENDING
                    and binding.workspace_id == workspace_id
                    and binding.session_id == session_id
                    and binding.principal_id == principal_id
                ):
                    visible.append(record.request)
            if changed:
                await uow.commit()
        return tuple(sorted(visible, key=lambda request: request.approval_id))

    @property
    def grants(self) -> ApprovalGrantRepository:
        """Grant reader/admin API to inject into the policy evaluator."""

        return self._grants

    async def revoke_grant(self, grant_id: str, *, revoked_by: str, reason: str) -> None:
        await self._grants.revoke(
            grant_id,
            revoked_at=self._clock.utcnow(),
            revoked_by=revoked_by,
            reason=reason,
        )

    async def revoke_run_grants(self, root_run_id: str, *, reason: str) -> None:
        await self._grants.revoke_run(
            root_run_id,
            revoked_at=self._clock.utcnow(),
            reason=reason,
        )

    async def revoke_session_grants(self, session_id: str, *, reason: str) -> None:
        await self._grants.revoke_session(
            session_id,
            revoked_at=self._clock.utcnow(),
            reason=reason,
        )

    async def expire_grants(self) -> None:
        await self._grants.expire_due(self._clock.utcnow())

    async def _ensure_pending(self, approval: ApprovalRequest) -> ApprovalRecord:
        drifted = False
        drift_resolution: ApprovalResolution | None = None
        async with self._unit_of_work.begin() as uow:
            records = await _scan_approval_records(uow.entities)
            by_call = tuple(record for _, record in records if record.request.tool_call_id == approval.tool_call_id)
            if len(by_call) > 1:
                raise ApprovalConflict("multiple durable approvals exist for one ToolCall")
            candidate_collision = next(
                (record for entity_id, record in records if entity_id == approval.approval_id),
                None,
            )
            if by_call:
                existing = by_call[0]
                if not existing.request.binding.same_recovery_identity(approval.binding):
                    if existing.state is ApprovalState.PENDING:
                        cancelled = _cancelled_record(
                            existing,
                            now=self._clock.utcnow(),
                            resolver_id="runtime:binding-drift",
                            reason="approval binding changed before resolution",
                        )
                        await uow.entities.put(
                            "approvals",
                            existing.request.approval_id,
                            cancelled,
                            expected_revision=existing.revision,
                        )
                        await uow.commit()
                        drift_resolution = cancelled.resolution
                    drifted = True
                else:
                    if candidate_collision is not None and candidate_collision.request != existing.request:
                        raise ApprovalConflict("candidate approval ID is already bound to another request")
                    checked, changed = _expire_record_if_due(existing, self._clock.utcnow())
                    if changed:
                        await uow.entities.put(
                            "approvals",
                            existing.request.approval_id,
                            checked,
                            expected_revision=existing.revision,
                        )
                        await uow.commit()
                    return checked
            elif candidate_collision is not None:
                raise ApprovalConflict("approval ID is bound to a different ToolCall")
            else:
                now = self._clock.utcnow()
                if now >= approval.binding.expires_at:
                    record = _new_expired_record(approval, now)
                else:
                    record = ApprovalRecord(approval, ApprovalState.PENDING, 1)
                await uow.entities.put("approvals", approval.approval_id, record, expected_revision=0)
                await uow.commit()
                return record
        if drifted:
            if drift_resolution is not None:
                waiter = self._waiters.get(drift_resolution.approval_id)
                if waiter is not None and not waiter.done():
                    waiter.set_result(drift_resolution)
            raise ApprovalConflict("approval binding drifted and the old pending request was cancelled")
        raise AssertionError("approval persistence reached an impossible state")

    async def _get_record(self, approval_id: str) -> ApprovalRecord | None:
        async with self._unit_of_work.begin() as uow:
            record = await uow.entities.get("approvals", approval_id)
        if record is None:
            return None
        return _checked_record(record, approval_id)


def _checked_record(value: object, entity_id: str) -> ApprovalRecord:
    if not isinstance(value, ApprovalRecord):
        raise ApprovalConflict("approval record is corrupt")
    if value.request.approval_id != entity_id:
        raise ApprovalConflict("approval entity key does not match its durable request")
    if not value.request.binding.has_recovery_identity:
        raise ApprovalConflict("approval record has an incomplete recovery binding")
    return value


async def _scan_approval_records(entities: EntityStore) -> tuple[tuple[str, ApprovalRecord], ...]:
    records: list[tuple[str, ApprovalRecord]] = []
    after_id: str | None = None
    seen: set[str] = set()
    while True:
        page = await entities.list("approvals", after_id=after_id, limit=100)
        ids = tuple(item.entity_id for item in page)
        if ids != tuple(sorted(ids)) or len(ids) != len(set(ids)):
            raise ApprovalConflict("approval repository listing is not stable and unique")
        if after_id is not None and ids and ids[0] <= after_id:
            raise ApprovalConflict("approval repository pagination did not advance")
        if any(entity_id in seen for entity_id in ids):
            raise ApprovalConflict("approval repository repeated an entity")
        for item in page:
            record = _checked_record(item.value, item.entity_id)
            if item.revision != record.revision:
                raise ApprovalConflict("approval entity and domain revisions differ")
            records.append((item.entity_id, record))
        seen.update(ids)
        if len(page) < 100:
            break
        after_id = page[-1].entity_id
    return tuple(records)


def _same_resolution_intent(left: ApprovalResolution, right: ApprovalResolution) -> bool:
    return (
        left.approval_id,
        left.state,
        left.scope,
        left.resolver_id,
        left.include_descendants,
        left.reason,
    ) == (
        right.approval_id,
        right.state,
        right.scope,
        right.resolver_id,
        right.include_descendants,
        right.reason,
    )


def _effective_resolution(
    record: ApprovalRecord,
    resolution: ApprovalResolution,
    now: datetime,
) -> ApprovalResolution:
    if resolution.state is ApprovalState.APPROVED and (
        now >= record.request.binding.expires_at or resolution.resolved_at >= record.request.binding.expires_at
    ):
        return ApprovalResolution(
            approval_id=record.request.approval_id,
            state=ApprovalState.EXPIRED,
            scope=ApprovalScope.ONCE,
            resolved_at=now,
            resolver_id="runtime:expiry",
            include_descendants=False,
            reason="approval expired before resolution",
        )
    return resolution


def _expire_record_if_due(record: ApprovalRecord, now: datetime) -> tuple[ApprovalRecord, bool]:
    if record.state is not ApprovalState.PENDING or now < record.request.binding.expires_at:
        return record, False
    resolution = ApprovalResolution(
        approval_id=record.request.approval_id,
        state=ApprovalState.EXPIRED,
        scope=ApprovalScope.ONCE,
        resolved_at=now,
        resolver_id="runtime:expiry",
        include_descendants=False,
        reason="approval expired",
    )
    return (
        ApprovalRecord(record.request, ApprovalState.EXPIRED, record.revision + 1, resolution),
        True,
    )


def _new_expired_record(request: ApprovalRequest, now: datetime) -> ApprovalRecord:
    resolution = ApprovalResolution(
        approval_id=request.approval_id,
        state=ApprovalState.EXPIRED,
        scope=ApprovalScope.ONCE,
        resolved_at=now,
        resolver_id="runtime:expiry",
        include_descendants=False,
        reason="approval was already expired when requested",
    )
    return ApprovalRecord(request, ApprovalState.EXPIRED, 1, resolution)


def _cancelled_record(
    record: ApprovalRecord,
    *,
    now: datetime,
    resolver_id: str,
    reason: str,
) -> ApprovalRecord:
    resolution = ApprovalResolution(
        approval_id=record.request.approval_id,
        state=ApprovalState.CANCELLED,
        scope=ApprovalScope.ONCE,
        resolved_at=now,
        resolver_id=resolver_id,
        include_descendants=False,
        reason=reason,
    )
    return ApprovalRecord(record.request, ApprovalState.CANCELLED, record.revision + 1, resolution)


__all__ = [
    "ApprovalConflict",
    "ApprovalError",
    "ApprovalManager",
    "ApprovalNotFound",
    "ApprovalRecord",
]
