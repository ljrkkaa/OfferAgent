"""Durable, narrowly-bound approval grants.

Grants are authorization evidence, not policy bypasses.  The policy evaluator
therefore consults them only after all hard safety gates and explicit DENY
rules have passed.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING, Any, Protocol

from .policy import ApprovalBinding, ApprovalRequest, ApprovalResolution, ApprovalScope, PolicyContext

if TYPE_CHECKING:
    from offeragent_harness.tools.definitions import ToolCall

APPROVAL_GRANT_COLLECTION = "approval_grants"


def _require_aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


class ApprovalGrantState(str, Enum):
    ACTIVE = "active"
    REVOKED = "revoked"
    EXPIRED = "expired"


@dataclass(frozen=True, slots=True)
class ApprovalGrant:
    grant_id: str
    approval_id: str
    scope: ApprovalScope
    binding: ApprovalBinding
    include_descendants: bool
    created_at: datetime
    expires_at: datetime | None
    state: ApprovalGrantState = ApprovalGrantState.ACTIVE
    revision: int = 1
    revoked_at: datetime | None = None
    revoked_by: str | None = None
    revocation_reason: str | None = None

    def __post_init__(self) -> None:
        if not self.grant_id or not self.approval_id:
            raise ValueError("approval grant identity fields must not be empty")
        if self.scope is ApprovalScope.ONCE:
            raise ValueError("one-time approvals must never create grants")
        if not self.binding.has_grant_identity:
            raise ValueError("reusable approval requires session and agent lineage identity")
        _require_aware(self.created_at, "created_at")
        if self.expires_at is not None:
            _require_aware(self.expires_at, "expires_at")
            if self.expires_at <= self.created_at:
                raise ValueError("approval grant expiry must follow creation")
        if self.revision < 1:
            raise ValueError("approval grant revision starts at 1")
        revoked_fields = (self.revoked_at, self.revoked_by, self.revocation_reason)
        if self.state is ApprovalGrantState.ACTIVE:
            if any(value is not None for value in revoked_fields):
                raise ValueError("active grants cannot carry revocation metadata")
        else:
            if any(value is None for value in revoked_fields):
                raise ValueError("inactive grants require complete revocation metadata")
            assert self.revoked_at is not None
            _require_aware(self.revoked_at, "revoked_at")

    def is_active_at(self, now: datetime) -> bool:
        _require_aware(now, "now")
        return (
            self.state is ApprovalGrantState.ACTIVE
            and now >= self.created_at
            and (self.expires_at is None or now < self.expires_at)
        )

    def matches(self, call: ToolCall, context: PolicyContext) -> bool:
        """Match exact resource/argument identity and the selected lifetime scope."""

        if not self.is_active_at(context.now):
            return False
        binding = self.binding
        expected_hash = call.arguments.get("expectedHash")
        if not isinstance(expected_hash, str):
            expected_hash = None
        if (
            binding.tool_name != call.name
            or binding.tool_version != call.version
            or binding.definition_fingerprint != call.definition_fingerprint
            or binding.args_hash != call.args_hash
            or binding.workspace_id != call.workspace_id
            or binding.workspace_id != context.workspace_id
            or binding.expected_state_hash != expected_hash
            or binding.principal_id != context.principal_id
            or call.run_id != context.run_id
        ):
            return False
        if self.scope in {ApprovalScope.RUN, ApprovalScope.SESSION} and binding.session_id != context.session_id:
            return False

        lineage = call.lineage
        exact_origin = (
            lineage.root_run_id == binding.root_run_id
            and lineage.run_id == binding.run_id
            and lineage.agent_name == binding.agent_name
            and lineage.ancestor_run_ids == binding.ancestor_run_ids
        )
        if exact_origin:
            return True

        # A session/persistent grant made by a root Agent is reusable by the
        # same root principal in a later root Run. It never silently widens to
        # a subagent.
        if (
            self.scope in {ApprovalScope.SESSION, ApprovalScope.PERSISTENT}
            and not binding.ancestor_run_ids
            and not lineage.ancestor_run_ids
            and lineage.agent_name == binding.agent_name
        ):
            return True

        # Descendant propagation is deliberately narrower than scope lifetime:
        # the child must remain under the exact originating root/ancestor path.
        if not self.include_descendants or lineage.root_run_id != binding.root_run_id:
            return False
        origin_path = (*binding.ancestor_run_ids, binding.run_id)
        return lineage.ancestor_run_ids[: len(origin_path)] == origin_path


class ApprovalGrantReader(Protocol):
    async def find_matching(self, call: ToolCall, context: PolicyContext) -> ApprovalGrant | None: ...


class _UnitOfWorkFactory(Protocol):
    def begin(self) -> Any: ...


GrantExpiryPolicy = Callable[[ApprovalRequest, ApprovalResolution], datetime | None]


def grant_id_for_approval(approval_id: str) -> str:
    digest = hashlib.sha256(approval_id.encode("utf-8")).hexdigest()
    return f"agr_{digest}"


def grant_from_resolution(
    request: ApprovalRequest,
    resolution: ApprovalResolution,
    *,
    expires_at: datetime | None,
) -> ApprovalGrant:
    if resolution.state.value != "approved" or resolution.scope is ApprovalScope.ONCE:
        raise ValueError("only approved reusable resolutions create grants")
    if expires_at is not None:
        _require_aware(expires_at, "expires_at")
    return ApprovalGrant(
        grant_id=grant_id_for_approval(request.approval_id),
        approval_id=request.approval_id,
        scope=resolution.scope,
        binding=request.binding,
        include_descendants=resolution.include_descendants,
        created_at=resolution.resolved_at,
        expires_at=expires_at,
    )


class ApprovalGrantRepository(ApprovalGrantReader):
    """Unit-of-Work-backed grant reader and lifecycle administration API."""

    def __init__(self, unit_of_work: _UnitOfWorkFactory) -> None:
        self._unit_of_work = unit_of_work

    async def get(self, grant_id: str) -> ApprovalGrant | None:
        async with self._unit_of_work.begin() as uow:
            value = await uow.entities.get(APPROVAL_GRANT_COLLECTION, grant_id)
        if value is None:
            return None
        if not isinstance(value, ApprovalGrant):
            raise TypeError("approval grant record is corrupt")
        return value

    async def find_matching(self, call: ToolCall, context: PolicyContext) -> ApprovalGrant | None:
        matches: list[ApprovalGrant] = []
        async with self._unit_of_work.begin() as uow:
            after_id: str | None = None
            while True:
                page = await uow.entities.list(APPROVAL_GRANT_COLLECTION, after_id=after_id, limit=100)
                if not page:
                    break
                for row in page:
                    grant = row.value
                    if not isinstance(grant, ApprovalGrant):
                        raise TypeError("approval grant record is corrupt")
                    if grant.matches(call, context):
                        matches.append(grant)
                after_id = page[-1].entity_id
                if len(page) < 100:
                    break
        if not matches:
            return None
        # A deterministic result keeps audit/replay stable when multiple valid
        # grants represent the same authorization.
        return max(matches, key=lambda grant: (grant.created_at, grant.grant_id))

    async def revoke(
        self,
        grant_id: str,
        *,
        revoked_at: datetime,
        revoked_by: str,
        reason: str,
    ) -> ApprovalGrant:
        if not revoked_by or not reason:
            raise ValueError("grant revocation requires actor and reason")
        _require_aware(revoked_at, "revoked_at")
        async with self._unit_of_work.begin() as uow:
            value = await uow.entities.get(APPROVAL_GRANT_COLLECTION, grant_id)
            if value is None:
                raise KeyError(grant_id)
            if not isinstance(value, ApprovalGrant):
                raise TypeError("approval grant record is corrupt")
            if value.state is not ApprovalGrantState.ACTIVE:
                return value
            updated = replace(
                value,
                state=ApprovalGrantState.REVOKED,
                revision=value.revision + 1,
                revoked_at=revoked_at,
                revoked_by=revoked_by,
                revocation_reason=reason,
            )
            await uow.entities.put(
                APPROVAL_GRANT_COLLECTION,
                grant_id,
                updated,
                expected_revision=value.revision,
            )
            await uow.commit()
        return updated

    async def expire_due(self, now: datetime) -> tuple[ApprovalGrant, ...]:
        _require_aware(now, "now")
        return await self._deactivate_matching(
            lambda grant: grant.expires_at is not None and now >= grant.expires_at,
            state=ApprovalGrantState.EXPIRED,
            at=now,
            actor="runtime:expiry",
            reason="approval grant expired",
        )

    async def revoke_run(
        self,
        root_run_id: str,
        *,
        revoked_at: datetime,
        reason: str,
    ) -> tuple[ApprovalGrant, ...]:
        if not root_run_id:
            raise ValueError("root_run_id must not be empty")
        return await self._deactivate_matching(
            lambda grant: grant.scope is ApprovalScope.RUN and grant.binding.root_run_id == root_run_id,
            state=ApprovalGrantState.REVOKED,
            at=revoked_at,
            actor="runtime:run-lifecycle",
            reason=reason,
        )

    async def revoke_session(
        self,
        session_id: str,
        *,
        revoked_at: datetime,
        reason: str,
    ) -> tuple[ApprovalGrant, ...]:
        if not session_id:
            raise ValueError("session_id must not be empty")
        return await self._deactivate_matching(
            lambda grant: grant.scope is ApprovalScope.SESSION and grant.binding.session_id == session_id,
            state=ApprovalGrantState.REVOKED,
            at=revoked_at,
            actor="runtime:session-lifecycle",
            reason=reason,
        )

    async def _deactivate_matching(
        self,
        predicate: Callable[[ApprovalGrant], bool],
        *,
        state: ApprovalGrantState,
        at: datetime,
        actor: str,
        reason: str,
    ) -> tuple[ApprovalGrant, ...]:
        _require_aware(at, "at")
        changed: list[ApprovalGrant] = []
        async with self._unit_of_work.begin() as uow:
            after_id: str | None = None
            while True:
                page = await uow.entities.list(APPROVAL_GRANT_COLLECTION, after_id=after_id, limit=100)
                if not page:
                    break
                for row in page:
                    current = row.value
                    if not isinstance(current, ApprovalGrant):
                        raise TypeError("approval grant record is corrupt")
                    if current.state is not ApprovalGrantState.ACTIVE or not predicate(current):
                        continue
                    updated = replace(
                        current,
                        state=state,
                        revision=current.revision + 1,
                        revoked_at=at,
                        revoked_by=actor,
                        revocation_reason=reason,
                    )
                    await uow.entities.put(
                        APPROVAL_GRANT_COLLECTION,
                        current.grant_id,
                        updated,
                        expected_revision=current.revision,
                    )
                    changed.append(updated)
                after_id = page[-1].entity_id
                if len(page) < 100:
                    break
            if changed:
                await uow.commit()
        return tuple(changed)


__all__ = [
    "APPROVAL_GRANT_COLLECTION",
    "ApprovalGrant",
    "ApprovalGrantReader",
    "ApprovalGrantRepository",
    "ApprovalGrantState",
    "GrantExpiryPolicy",
    "grant_from_resolution",
    "grant_id_for_approval",
]
