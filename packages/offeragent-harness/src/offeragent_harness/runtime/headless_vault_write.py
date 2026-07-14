"""Durable, explicit authorization for Loopback headless Vault writes.

This module never applies a Vault mutation from an application command.  It
only issues a short-lived authorization that one subsequent ``turn/start`` can
bind into the normal Unified Tool Kernel.  The actual write remains the
``vault.transaction`` tool with its schema, Policy, diff approval, expected
hashes, invocation journal and transaction coordinator.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import Enum
from typing import Literal, cast

from offeragent_harness.permissions import ApprovalState
from offeragent_harness.ports import (
    ApplicationCommandContext,
    CancellationToken,
    Clock,
    EntityRevisionConflict,
    ToolExecutor,
    UnitOfWorkFactory,
)
from offeragent_harness.protocol._base import WireModel
from offeragent_harness.protocol.common import ApprovalDecision, ApprovalScope
from offeragent_harness.protocol.messages import (
    ApprovalResolveParams,
    ApprovalResolveResult,
    HeadlessVaultWriteActivateParams,
    HeadlessVaultWriteRequestParams,
    HeadlessVaultWriteRevokeParams,
    HeadlessVaultWriteState,
    HeadlessVaultWriteStatusResult,
)
from offeragent_harness.tools import (
    PreflightConflict,
    PreflightEvidence,
    ToolCall,
    ToolDefinition,
    ToolResult,
    canonical_json_sha256,
)
from offeragent_harness.vault import VaultTransactionCoordinator

from .application_dispatcher import ApplicationCommandHandler

_COLLECTION = "headless_vault_authorizations"
_SCHEMA_VERSION = 1
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_OPERATION = re.compile(r"^op_headless_[0-9a-f]{32}$")
_APPROVAL = re.compile(r"^apr_[0-9a-f]{64}$")
_GRANT = re.compile(r"^hgrant_[0-9a-f]{64}$")
_CLIENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class HeadlessVaultWriteError(RuntimeError):
    """Fail-closed authorization or state error safe to surface to a local UI."""


class _AuthorizationState(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    ACTIVE = "active"
    CLAIMED = "claimed"
    DENIED = "denied"
    EXPIRED = "expired"
    REVOKED = "revoked"
    CONSUMED = "consumed"

    @property
    def terminal(self) -> bool:
        return self in {
            _AuthorizationState.DENIED,
            _AuthorizationState.EXPIRED,
            _AuthorizationState.REVOKED,
            _AuthorizationState.CONSUMED,
        }


@dataclass(frozen=True, slots=True)
class HeadlessBaseline:
    root_identity: str
    database_identity: str

    def __post_init__(self) -> None:
        if not _DIGEST.fullmatch(self.root_identity) or not _DIGEST.fullmatch(self.database_identity):
            raise ValueError("headless baseline identities must be canonical digests")

    @property
    def fingerprint(self) -> str:
        return canonical_json_sha256(self.to_value())

    def to_value(self) -> dict[str, object]:
        return {
            "rootIdentity": self.root_identity,
            "databaseIdentity": self.database_identity,
        }


@dataclass(frozen=True, slots=True)
class HeadlessAuthorizationRecord:
    operation_id: str
    approval_id: str
    grant_id: str
    workspace_id: str
    workspace_instance_id: str
    client_id: str
    client_request_id: str
    request_fingerprint: str
    args_hash: str
    baseline: HeadlessBaseline
    state: _AuthorizationState
    revision: int
    requested_at: datetime
    expires_at: datetime
    resolution_state: ApprovalState | None = None
    resolved_at: datetime | None = None
    activation_request_id: str | None = None
    activated_at: datetime | None = None
    claim_hash: str | None = None
    claimed_at: datetime | None = None
    terminal_at: datetime | None = None
    terminal_reason: str | None = None
    revocation_request_id: str | None = None

    def __post_init__(self) -> None:
        if not _OPERATION.fullmatch(self.operation_id):
            raise ValueError("headless operation identity is invalid")
        if not _APPROVAL.fullmatch(self.approval_id) or not _GRANT.fullmatch(self.grant_id):
            raise ValueError("headless approval/grant identity is invalid")
        if not self.workspace_id or not self.workspace_instance_id or not _CLIENT.fullmatch(self.client_id):
            raise ValueError("headless Workspace/browser identity is invalid")
        if not _DIGEST.fullmatch(self.request_fingerprint) or not _DIGEST.fullmatch(self.args_hash):
            raise ValueError("headless request fingerprints are invalid")
        if self.revision < 1:
            raise ValueError("headless authorization revision starts at one")
        for value in (
            self.requested_at,
            self.expires_at,
            self.resolved_at,
            self.activated_at,
            self.claimed_at,
            self.terminal_at,
        ):
            if value is not None and (value.tzinfo is None or value.utcoffset() is None):
                raise ValueError("headless authorization timestamps must be timezone-aware")
        if self.requested_at >= self.expires_at:
            raise ValueError("headless authorization must expire after it is requested")
        if (self.resolution_state is None) != (self.resolved_at is None):
            raise ValueError("headless approval resolution fields must be present together")
        if (self.activation_request_id is None) != (self.activated_at is None):
            raise ValueError("headless activation fields must be present together")
        if (self.claim_hash is None) != (self.claimed_at is None):
            raise ValueError("headless claim fields must be present together")
        if self.claim_hash is not None and not _DIGEST.fullmatch(self.claim_hash):
            raise ValueError("headless turn claim hash is invalid")
        if (self.terminal_at is None) != (self.terminal_reason is None):
            raise ValueError("headless terminal fields must be present together")

    def to_value(self) -> dict[str, object]:
        return {
            "schemaVersion": _SCHEMA_VERSION,
            "operationId": self.operation_id,
            "approvalId": self.approval_id,
            "grantId": self.grant_id,
            "workspaceId": self.workspace_id,
            "workspaceInstanceId": self.workspace_instance_id,
            "clientId": self.client_id,
            "clientRequestId": self.client_request_id,
            "requestFingerprint": self.request_fingerprint,
            "argsHash": self.args_hash,
            "baseline": self.baseline.to_value(),
            "state": self.state.value,
            "revision": self.revision,
            "requestedAt": self.requested_at.isoformat(),
            "expiresAt": self.expires_at.isoformat(),
            "resolutionState": None if self.resolution_state is None else self.resolution_state.value,
            "resolvedAt": None if self.resolved_at is None else self.resolved_at.isoformat(),
            "activationRequestId": self.activation_request_id,
            "activatedAt": None if self.activated_at is None else self.activated_at.isoformat(),
            "claimHash": self.claim_hash,
            "claimedAt": None if self.claimed_at is None else self.claimed_at.isoformat(),
            "terminalAt": None if self.terminal_at is None else self.terminal_at.isoformat(),
            "terminalReason": self.terminal_reason,
            "revocationRequestId": self.revocation_request_id,
        }


class HeadlessVaultWriteAuthority:
    """One-Worker authority shared by transport routing and Vault execution."""

    def __init__(
        self,
        *,
        workspace_id: str,
        workspace_instance_id: str,
        root_identity: str,
        database_identity: str,
        unit_of_work: UnitOfWorkFactory,
        clock: Clock,
        identity_probe: Callable[[], tuple[str, str]],
        pipe_connection_count: Callable[[], int],
    ) -> None:
        if not workspace_id or not workspace_instance_id:
            raise ValueError("headless authority requires Workspace identities")
        if not _DIGEST.fullmatch(root_identity) or not _DIGEST.fullmatch(database_identity):
            raise ValueError("headless authority requires canonical root/database identities")
        self.workspace_id = workspace_id
        self.workspace_instance_id = workspace_instance_id
        self._root_identity = root_identity
        self._database_identity = database_identity
        self._unit_of_work = unit_of_work
        self._clock = clock
        self._identity_probe = identity_probe
        self._pipe_connection_count = pipe_connection_count
        self._transition_lock = asyncio.Lock()
        self._recovered = False
        self._candidate: HeadlessAuthorizationRecord | None = None

    async def recover_after_restart(self) -> None:
        """Invalidate browser-bound state because Loopback cookies are process-local."""

        async with self._transition_lock:
            if self._recovered:
                return
            records = await self._records()
            now = self._clock.utcnow()
            for record in records:
                if record.state.terminal:
                    continue
                await self._replace(
                    record,
                    state=_AuthorizationState.REVOKED,
                    terminal_at=now,
                    terminal_reason="runtime_restarted_browser_identity_invalid",
                )
            self._candidate = None
            self._recovered = True

    @asynccontextmanager
    async def pipe_registration(self) -> AsyncIterator[None]:
        """Serialize Pipe activation against a complete local file transaction."""

        await self._require_recovered()
        async with self._transition_lock:
            await self._revoke_nonterminal("authenticated_pipe_connected")
            yield

    async def status(
        self,
        context: ApplicationCommandContext,
        cancellation: CancellationToken,
    ) -> HeadlessVaultWriteStatusResult:
        cancellation.checkpoint()
        await self._require_recovered()
        async with self._transition_lock:
            baseline = await self._baseline()
            candidate = await self._candidate_for_status(context.client_id)
            return self._status_result(context, baseline, candidate)

    async def request(
        self,
        params: HeadlessVaultWriteRequestParams,
        context: ApplicationCommandContext,
        cancellation: CancellationToken,
    ) -> HeadlessVaultWriteStatusResult:
        self._require_loopback(context)
        cancellation.checkpoint()
        await self._require_recovered()
        async with self._transition_lock:
            if self._pipe_connection_count() != 0:
                raise HeadlessVaultWriteError("Obsidian Pipe 已连接; 写入必须由唯一 Client Tool 执行")
            baseline = await self._baseline()
            self._require_reliable_baseline(baseline)
            self._require_requested_baseline(params, baseline)
            request_fingerprint = canonical_json_sha256(
                {
                    "schemaVersion": 1,
                    "workspaceId": self.workspace_id,
                    "workspaceInstanceId": self.workspace_instance_id,
                    "clientId": context.client_id,
                    "clientRequestId": params.client_request_id,
                    "confirmation": params.confirmation,
                    "ttlSeconds": params.ttl_seconds,
                    "baseline": baseline.to_value(),
                }
            )
            operation_suffix = hashlib.sha256(
                f"{self.workspace_instance_id}\0{context.client_id}\0{params.client_request_id}".encode()
            ).hexdigest()[:32]
            operation_id = f"op_headless_{operation_suffix}"
            existing = await self._record(operation_id)
            if existing is not None:
                if existing.request_fingerprint != request_fingerprint:
                    raise HeadlessVaultWriteError("clientRequestId 已绑定到不同的 headless 授权请求")
                self._candidate = None if existing.state.terminal else existing
                return self._status_result(context, baseline, existing)
            current = await self._current_nonterminal()
            if current is not None:
                if current.client_id != context.client_id:
                    raise HeadlessVaultWriteError("另一浏览器会话已有未结束的 headless 授权")
                raise HeadlessVaultWriteError("已有未结束的 headless 授权; 请先完成或撤销")
            now = self._clock.utcnow()
            expires_at = now + timedelta(seconds=params.ttl_seconds)
            args_hash = canonical_json_sha256(
                {
                    "operation": "vault.headless.authorize",
                    "workspaceId": self.workspace_id,
                    "workspaceInstanceId": self.workspace_instance_id,
                    "clientId": context.client_id,
                    "confirmation": params.confirmation,
                    "baselineFingerprint": baseline.fingerprint,
                    "rootIdentity": self._root_identity,
                    "databaseIdentity": self._database_identity,
                    "expiresAt": expires_at.isoformat(),
                }
            )
            approval_id = f"apr_{hashlib.sha256((operation_id + chr(0) + args_hash).encode()).hexdigest()}"
            grant_id = f"hgrant_{hashlib.sha256((approval_id + chr(0) + args_hash).encode()).hexdigest()}"
            record = HeadlessAuthorizationRecord(
                operation_id=operation_id,
                approval_id=approval_id,
                grant_id=grant_id,
                workspace_id=self.workspace_id,
                workspace_instance_id=self.workspace_instance_id,
                client_id=context.client_id,
                client_request_id=params.client_request_id,
                request_fingerprint=request_fingerprint,
                args_hash=args_hash,
                baseline=baseline,
                state=_AuthorizationState.PENDING,
                revision=1,
                requested_at=now,
                expires_at=expires_at,
            )
            async with self._unit_of_work.begin() as transaction:
                await transaction.entities.put(
                    _COLLECTION,
                    operation_id,
                    record.to_value(),
                    expected_revision=0,
                )
                await transaction.commit()
            self._candidate = record
            return self._status_result(context, baseline, record)

    async def try_resolve(
        self,
        params: ApprovalResolveParams,
        context: ApplicationCommandContext,
    ) -> ApprovalResolveResult | None:
        """Resolve an explicit administrative approval without inventing a Run."""

        await self._require_recovered()
        async with self._transition_lock:
            record = await self._record_by_approval(params.approval_id)
            if record is None:
                return None
            self._require_loopback(context)
            if record.client_id != context.client_id:
                raise PermissionError("headless approval belongs to another authenticated browser session")
            if record.args_hash != params.expected_args_hash:
                raise HeadlessVaultWriteError("headless approval argsHash 已变化")
            if params.scope is not ApprovalScope.ONCE or params.include_descendants:
                raise PermissionError("headless administrative approval only supports one-time decisions")
            now = self._clock.utcnow()
            if record.state is _AuthorizationState.PENDING and now >= record.expires_at:
                record = await self._replace(
                    record,
                    state=_AuthorizationState.EXPIRED,
                    resolution_state=ApprovalState.EXPIRED,
                    resolved_at=now,
                    terminal_at=now,
                    terminal_reason="approval_expired",
                )
            requested_state = (
                ApprovalState.APPROVED
                if params.decision is ApprovalDecision.ALLOW_ONCE
                else ApprovalState.DENIED
                if params.decision is ApprovalDecision.DENY
                else None
            )
            if requested_state is None:
                raise PermissionError("headless administrative approval cannot create reusable grants")
            already = record.resolution_state is not None
            if already:
                if record.resolution_state is not requested_state:
                    raise HeadlessVaultWriteError("headless approval 已以不同决定完成")
            else:
                if record.state is not _AuthorizationState.PENDING:
                    raise HeadlessVaultWriteError("headless approval 不再处于 pending 状态")
                if requested_state is ApprovalState.APPROVED:
                    record = await self._replace(
                        record,
                        state=_AuthorizationState.APPROVED,
                        resolution_state=requested_state,
                        resolved_at=now,
                    )
                else:
                    record = await self._replace(
                        record,
                        state=_AuthorizationState.DENIED,
                        resolution_state=requested_state,
                        resolved_at=now,
                        terminal_at=now,
                        terminal_reason=params.comment or "user_denied",
                    )
            self._candidate = None if record.state.terminal else record
            resolved_state = record.resolution_state
            if resolved_state is None:
                raise HeadlessVaultWriteError("headless approval resolution was not persisted")
            if resolved_state is ApprovalState.APPROVED:
                resolved_status: Literal["approved", "denied", "expired", "cancelled"] = "approved"
            elif resolved_state is ApprovalState.DENIED:
                resolved_status = "denied"
            elif resolved_state is ApprovalState.EXPIRED:
                resolved_status = "expired"
            elif resolved_state is ApprovalState.CANCELLED:
                resolved_status = "cancelled"
            else:
                raise HeadlessVaultWriteError("headless approval cannot resolve to pending")
            status: Literal["approved", "denied", "expired", "cancelled", "already_resolved"] = (
                "already_resolved" if already else resolved_status
            )
            return ApprovalResolveResult(
                approval_id=record.approval_id,
                status=status,
                run_id=None,
                operation_id=record.operation_id,
                resumed=False,
            )

    async def activate(
        self,
        params: HeadlessVaultWriteActivateParams,
        context: ApplicationCommandContext,
        cancellation: CancellationToken,
    ) -> HeadlessVaultWriteStatusResult:
        self._require_loopback(context)
        cancellation.checkpoint()
        await self._require_recovered()
        async with self._transition_lock:
            if self._pipe_connection_count() != 0:
                raise HeadlessVaultWriteError("Pipe 已连接, headless 授权不能激活")
            record = await self._required_approval(params.approval_id)
            if record.client_id != context.client_id or record.args_hash != params.expected_args_hash:
                raise PermissionError("headless activation identity does not match the approved browser request")
            if record.activation_request_id == params.client_request_id and record.state in {
                _AuthorizationState.ACTIVE,
                _AuthorizationState.CLAIMED,
                _AuthorizationState.CONSUMED,
            }:
                return self._status_result(context, await self._baseline(), record)
            if record.revision != params.expected_revision:
                raise HeadlessVaultWriteError("headless authorization revision changed before activation")
            now = self._clock.utcnow()
            if now >= record.expires_at:
                record = await self._replace(
                    record,
                    state=_AuthorizationState.EXPIRED,
                    terminal_at=now,
                    terminal_reason="authorization_expired_before_activation",
                )
                raise HeadlessVaultWriteError("headless authorization 已过期")
            if (
                record.state is not _AuthorizationState.APPROVED
                or record.resolution_state is not ApprovalState.APPROVED
            ):
                raise HeadlessVaultWriteError("headless authorization 尚未获得一次性批准")
            baseline = await self._baseline()
            self._require_exact_baseline(record.baseline, baseline)
            record = await self._replace(
                record,
                state=_AuthorizationState.ACTIVE,
                activation_request_id=params.client_request_id,
                activated_at=now,
            )
            self._candidate = record
            return self._status_result(context, baseline, record)

    async def revoke(
        self,
        params: HeadlessVaultWriteRevokeParams,
        context: ApplicationCommandContext,
        cancellation: CancellationToken,
    ) -> HeadlessVaultWriteStatusResult:
        self._require_local_ui(context)
        cancellation.checkpoint()
        await self._require_recovered()
        async with self._transition_lock:
            record = await self._required_approval(params.approval_id)
            if record.client_id != context.client_id and context.transport != "windows-named-pipe":
                raise PermissionError("headless authorization belongs to another browser session")
            if record.revocation_request_id == params.client_request_id and record.state is _AuthorizationState.REVOKED:
                return self._status_result(context, await self._baseline(), record)
            if record.revision != params.expected_revision:
                raise HeadlessVaultWriteError("headless authorization revision changed before revocation")
            if record.state.terminal:
                raise HeadlessVaultWriteError("terminal headless authorization cannot be revoked")
            now = self._clock.utcnow()
            record = await self._replace(
                record,
                state=_AuthorizationState.REVOKED,
                terminal_at=now,
                terminal_reason=params.reason,
                revocation_request_id=params.client_request_id,
            )
            self._candidate = None
            return self._status_result(context, await self._baseline(), record)

    async def claim_for_turn(self, client_id: str, turn_request_hash: str) -> str | None:
        """CAS-bind one active grant to one exact ``turn/start`` request hash."""

        if not _CLIENT.fullmatch(client_id) or not _DIGEST.fullmatch(turn_request_hash):
            raise ValueError("headless turn claim identity is invalid")
        await self._require_recovered()
        async with self._transition_lock:
            if self._pipe_connection_count() != 0:
                return None
            record = await self._current_nonterminal()
            if record is None or record.client_id != client_id:
                replay = await self._record_by_claim(client_id, turn_request_hash)
                return None if replay is None else replay.grant_id
            now = self._clock.utcnow()
            if now >= record.expires_at:
                await self._replace(
                    record,
                    state=_AuthorizationState.EXPIRED,
                    terminal_at=now,
                    terminal_reason="authorization_expired_before_turn_start",
                )
                self._candidate = None
                return None
            if record.state is _AuthorizationState.CLAIMED:
                if record.claim_hash != turn_request_hash:
                    return None
                return record.grant_id
            if record.state is not _AuthorizationState.ACTIVE:
                return None
            self._require_exact_baseline(record.baseline, await self._baseline())
            record = await self._replace(
                record,
                state=_AuthorizationState.CLAIMED,
                claim_hash=turn_request_hash,
                claimed_at=now,
            )
            self._candidate = record
            return record.grant_id

    async def validate_grant(self, grant_id: str) -> None:
        await self._require_recovered()
        async with self._transition_lock:
            await self._validate_claimed_locked(grant_id)

    @asynccontextmanager
    async def hold_execution(self, grant_id: str) -> AsyncIterator[None]:
        """Block Pipe registration across final validation and the real apply."""

        await self._require_recovered()
        async with self._transition_lock:
            await self._validate_claimed_locked(grant_id)
            yield

    async def release_claim(self, grant_id: str, *, reason: str) -> None:
        if not _GRANT.fullmatch(grant_id):
            raise ValueError("headless grant identity is invalid")
        await self._require_recovered()
        async with self._transition_lock:
            record = await self._record_by_grant(grant_id)
            if record is None or record.state is _AuthorizationState.CONSUMED:
                return
            if record.state is not _AuthorizationState.CLAIMED:
                return
            now = self._clock.utcnow()
            record = await self._replace(
                record,
                state=_AuthorizationState.CONSUMED,
                terminal_at=now,
                terminal_reason=reason,
            )
            if self._candidate is not None and self._candidate.grant_id == grant_id:
                self._candidate = None

    async def _validate_claimed_locked(self, grant_id: str) -> HeadlessAuthorizationRecord:
        if not _GRANT.fullmatch(grant_id):
            raise HeadlessVaultWriteError("headless grant identity is invalid")
        if self._pipe_connection_count() != 0:
            raise HeadlessVaultWriteError("Obsidian Pipe 已连接; LOCAL Vault executor 已动态失权")
        record = await self._record_by_grant(grant_id)
        if record is None or record.state is not _AuthorizationState.CLAIMED:
            raise HeadlessVaultWriteError("headless grant 未绑定到当前 turn/start")
        now = self._clock.utcnow()
        if now >= record.expires_at:
            record = await self._replace(
                record,
                state=_AuthorizationState.EXPIRED,
                terminal_at=now,
                terminal_reason="authorization_expired_before_vault_transaction",
            )
            self._candidate = None
            raise HeadlessVaultWriteError("headless grant 已过期")
        self._require_exact_baseline(record.baseline, await self._baseline())
        return record

    async def _candidate_for_status(self, client_id: str) -> HeadlessAuthorizationRecord | None:
        current = await self._current_nonterminal()
        if current is not None:
            now = self._clock.utcnow()
            if now >= current.expires_at:
                current = await self._replace(
                    current,
                    state=_AuthorizationState.EXPIRED,
                    terminal_at=now,
                    terminal_reason="authorization_expired",
                )
                self._candidate = None
            else:
                self._candidate = current
                return current
        own = [record for record in await self._records() if record.client_id == client_id]
        return max(own, key=lambda item: (item.requested_at, item.operation_id), default=None)

    async def _revoke_nonterminal(self, reason: str) -> None:
        now = self._clock.utcnow()
        for record in await self._records():
            if record.state.terminal:
                continue
            await self._replace(
                record,
                state=_AuthorizationState.REVOKED,
                terminal_at=now,
                terminal_reason=reason,
            )
        self._candidate = None

    async def _baseline(self) -> HeadlessBaseline:
        root_identity, database_identity = self._identity_probe()
        return HeadlessBaseline(
            root_identity=root_identity,
            database_identity=database_identity,
        )

    def _require_reliable_baseline(self, baseline: HeadlessBaseline) -> None:
        if baseline.root_identity != self._root_identity or baseline.database_identity != self._database_identity:
            raise HeadlessVaultWriteError("Workspace root/database identity changed")

    def _require_exact_baseline(self, expected: HeadlessBaseline, actual: HeadlessBaseline) -> None:
        self._require_reliable_baseline(actual)
        if actual != expected:
            raise HeadlessVaultWriteError("磁盘基线自用户确认后已变化; 必须重新授权")

    @staticmethod
    def _require_requested_baseline(params: HeadlessVaultWriteRequestParams, baseline: HeadlessBaseline) -> None:
        if params.expected_baseline_fingerprint != baseline.fingerprint:
            raise HeadlessVaultWriteError("用户看到的工作区基线已变化")

    def _status_result(
        self,
        context: ApplicationCommandContext,
        baseline: HeadlessBaseline,
        record: HeadlessAuthorizationRecord | None,
    ) -> HeadlessVaultWriteStatusResult:
        pipe_count = self._pipe_connection_count()
        visible = record is not None and record.client_id == context.client_id
        loopback_browser = context.transport in {"loopback-http", "loopback-websocket"} and bool(
            _CLIENT.fullmatch(context.client_id)
        )
        baseline_reliable = (
            baseline.root_identity == self._root_identity and baseline.database_identity == self._database_identity
        )
        if pipe_count > 1:
            state = HeadlessVaultWriteState.AMBIGUOUS_PIPE
            reason = "multiple_authenticated_pipes"
            message = "检测到多条认证 Pipe; 所有歧义写入均已关闭。"
        elif pipe_count == 1:
            state = HeadlessVaultWriteState.PIPE_CLIENT_TOOL
            reason = "pipe_client_tool_authoritative"
            message = "Obsidian 已连接; Vault 写入只会经过唯一 Named Pipe Client Tool。"
        elif not baseline_reliable:
            state = HeadlessVaultWriteState.BASELINE_UNRELIABLE
            if baseline.root_identity != self._root_identity or baseline.database_identity != self._database_identity:
                reason = "workspace_identity_changed"
                message = "Workspace root 或 SQLite 身份已变化, 当前保持只读。"
            else:
                reason = "baseline_unreliable"
                message = "工作区尚未形成可确认磁盘基线, 当前保持只读。"
        elif record is None:
            state = HeadlessVaultWriteState.READ_ONLY
            reason = "explicit_confirmation_required"
            message = "未检测到 Pipe; 确认 Obsidian 已关闭且磁盘为权威基线后才可授权一个 Turn。"
        elif not visible:
            state = HeadlessVaultWriteState.READ_ONLY
            reason = "authorization_bound_other_browser"
            message = "headless 授权属于另一浏览器会话, 当前保持只读。"
        else:
            state = {
                _AuthorizationState.PENDING: HeadlessVaultWriteState.PENDING_APPROVAL,
                _AuthorizationState.APPROVED: HeadlessVaultWriteState.APPROVED,
                _AuthorizationState.ACTIVE: HeadlessVaultWriteState.ACTIVE,
                _AuthorizationState.CLAIMED: HeadlessVaultWriteState.CLAIMED,
                _AuthorizationState.DENIED: HeadlessVaultWriteState.DENIED,
                _AuthorizationState.EXPIRED: HeadlessVaultWriteState.EXPIRED,
                _AuthorizationState.REVOKED: HeadlessVaultWriteState.REVOKED,
                _AuthorizationState.CONSUMED: HeadlessVaultWriteState.CONSUMED,
            }[record.state]
            # ``terminal_reason`` may contain user-entered local text.  Keep the
            # machine-readable wire value closed and stable instead of copying
            # arbitrary text into ``reasonCode``.
            reason = f"authorization_{record.state.value}"
            message = {
                _AuthorizationState.PENDING: "授权请求已持久化; 请核对 argsHash 并做一次性批准或拒绝。",
                _AuthorizationState.APPROVED: "一次性审批已通过; 再次确认当前基线以激活。",
                _AuthorizationState.ACTIVE: "授权已激活, 将由下一次完全一致的 turn/start 原子领取。",
                _AuthorizationState.CLAIMED: "授权已绑定到一个 Turn; LOCAL 写仍经过 Tool Kernel 与事务审批。",
                _AuthorizationState.DENIED: "headless 授权已被拒绝。",
                _AuthorizationState.EXPIRED: "headless 授权已过期。",
                _AuthorizationState.REVOKED: "headless 授权已撤销。",
                _AuthorizationState.CONSUMED: "headless 授权已被一个 Turn 消费。",
            }[record.state]
        visible_record = record if visible else None
        can_request = (
            loopback_browser and pipe_count == 0 and baseline_reliable and (record is None or record.state.terminal)
        )
        can_activate = (
            loopback_browser and visible_record is not None and visible_record.state is _AuthorizationState.APPROVED
        )
        can_revoke = visible_record is not None and not visible_record.state.terminal
        return HeadlessVaultWriteStatusResult(
            state=state,
            pipe_connection_count=pipe_count,
            baseline_reliable=baseline_reliable,
            baseline_fingerprint=baseline.fingerprint,
            approval_id=None if visible_record is None else visible_record.approval_id,
            operation_id=None if visible_record is None else visible_record.operation_id,
            args_hash=None if visible_record is None else visible_record.args_hash,
            revision=0 if visible_record is None else visible_record.revision,
            expires_at=None if visible_record is None else visible_record.expires_at.isoformat(),
            reason_code=reason,
            user_message=message,
            can_request=can_request,
            can_activate=can_activate,
            can_revoke=can_revoke,
        )

    async def _replace(self, record: HeadlessAuthorizationRecord, **changes: object) -> HeadlessAuthorizationRecord:
        updated = replace(record, revision=record.revision + 1, **changes)  # type: ignore[arg-type]
        try:
            async with self._unit_of_work.begin() as transaction:
                await transaction.entities.put(
                    _COLLECTION,
                    record.operation_id,
                    updated.to_value(),
                    expected_revision=record.revision,
                )
                await transaction.commit()
        except EntityRevisionConflict as error:
            raise HeadlessVaultWriteError("headless authorization lost a compare-and-swap race") from error
        return updated

    async def _record(self, operation_id: str) -> HeadlessAuthorizationRecord | None:
        async with self._unit_of_work.begin() as transaction:
            value = await transaction.entities.get(_COLLECTION, operation_id)
        return None if value is None else _record_from_value(value, operation_id)

    async def _required_approval(self, approval_id: str) -> HeadlessAuthorizationRecord:
        record = await self._record_by_approval(approval_id)
        if record is None:
            raise HeadlessVaultWriteError("headless approval does not exist")
        return record

    async def _records(self) -> tuple[HeadlessAuthorizationRecord, ...]:
        values: list[HeadlessAuthorizationRecord] = []
        after_id: str | None = None
        while True:
            async with self._unit_of_work.begin() as transaction:
                page = await transaction.entities.list(_COLLECTION, after_id=after_id, limit=100)
            if not page:
                break
            for item in page:
                record = _record_from_value(item.value, item.entity_id)
                if record.revision != item.revision:
                    raise HeadlessVaultWriteError("headless entity/payload revisions disagree")
                if record.workspace_id == self.workspace_id:
                    values.append(record)
            after_id = page[-1].entity_id
        return tuple(values)

    async def _record_by_approval(self, approval_id: str) -> HeadlessAuthorizationRecord | None:
        matches = [record for record in await self._records() if record.approval_id == approval_id]
        if len(matches) > 1:
            raise HeadlessVaultWriteError("duplicate headless approval identity")
        return None if not matches else matches[0]

    async def _record_by_grant(self, grant_id: str) -> HeadlessAuthorizationRecord | None:
        matches = [record for record in await self._records() if record.grant_id == grant_id]
        if len(matches) > 1:
            raise HeadlessVaultWriteError("duplicate headless grant identity")
        return None if not matches else matches[0]

    async def _record_by_claim(self, client_id: str, claim_hash: str) -> HeadlessAuthorizationRecord | None:
        matches = [
            record
            for record in await self._records()
            if record.client_id == client_id and record.claim_hash == claim_hash
        ]
        if len(matches) > 1:
            raise HeadlessVaultWriteError("duplicate headless turn claim")
        return None if not matches else matches[0]

    async def _current_nonterminal(self) -> HeadlessAuthorizationRecord | None:
        matches = [record for record in await self._records() if not record.state.terminal]
        if len(matches) > 1:
            raise HeadlessVaultWriteError("multiple non-terminal headless authorizations")
        return None if not matches else matches[0]

    async def _require_recovered(self) -> None:
        if not self._recovered:
            raise HeadlessVaultWriteError("headless authority has not completed startup recovery")

    @staticmethod
    def _require_loopback(context: ApplicationCommandContext) -> None:
        if context.transport not in {"loopback-http", "loopback-websocket"} or not _CLIENT.fullmatch(context.client_id):
            raise PermissionError("headless Vault authorization requires an authenticated Loopback browser")

    @staticmethod
    def _require_local_ui(context: ApplicationCommandContext) -> None:
        if context.transport not in {"loopback-http", "loopback-websocket", "windows-named-pipe"}:
            raise PermissionError("headless Vault authorization can be revoked only by a local authenticated UI")


class HeadlessAuthorizedVaultTransaction(ToolExecutor):
    """Run-bound gate around the one real local transaction coordinator."""

    def __init__(
        self,
        *,
        authority: HeadlessVaultWriteAuthority,
        grant_id: str,
        transaction: VaultTransactionCoordinator,
    ) -> None:
        if not _GRANT.fullmatch(grant_id):
            raise ValueError("headless transaction requires a canonical grant identity")
        self._authority = authority
        self._grant_id = grant_id
        self._transaction = transaction

    @property
    def provider_id(self) -> str:
        return self._transaction.provider_id

    async def prepare(
        self,
        definition: ToolDefinition,
        call: ToolCall,
        cancellation: CancellationToken,
    ) -> PreflightEvidence:
        try:
            await self._authority.validate_grant(self._grant_id)
        except HeadlessVaultWriteError as error:
            raise PreflightConflict(str(error), details={"reason": "headless_authority_invalid"}) from error
        return await self._transaction.prepare(definition, call, cancellation)

    async def revalidate(
        self,
        definition: ToolDefinition,
        call: ToolCall,
        evidence: PreflightEvidence,
        cancellation: CancellationToken,
    ) -> None:
        try:
            await self._authority.validate_grant(self._grant_id)
        except HeadlessVaultWriteError as error:
            raise PreflightConflict(str(error), details={"reason": "headless_authority_invalid"}) from error
        await self._transaction.revalidate(definition, call, evidence, cancellation)

    async def complete(
        self,
        definition: ToolDefinition,
        call: ToolCall,
        evidence: PreflightEvidence,
        result: ToolResult,
    ) -> None:
        await self._transaction.complete(definition, call, evidence, result)

    async def execute(self, call: ToolCall, cancellation: CancellationToken) -> ToolResult:
        async with self._authority.hold_execution(self._grant_id):
            return await self._transaction.execute(call, cancellation)


def headless_vault_write_handlers(
    authority: HeadlessVaultWriteAuthority,
) -> Mapping[str, ApplicationCommandHandler]:
    async def status(
        raw: WireModel,
        cancellation: CancellationToken,
        context: ApplicationCommandContext,
    ) -> WireModel:
        del raw
        return await authority.status(context, cancellation)

    async def request(
        raw: WireModel,
        cancellation: CancellationToken,
        context: ApplicationCommandContext,
    ) -> WireModel:
        return await authority.request(cast(HeadlessVaultWriteRequestParams, raw), context, cancellation)

    async def activate(
        raw: WireModel,
        cancellation: CancellationToken,
        context: ApplicationCommandContext,
    ) -> WireModel:
        return await authority.activate(cast(HeadlessVaultWriteActivateParams, raw), context, cancellation)

    async def revoke(
        raw: WireModel,
        cancellation: CancellationToken,
        context: ApplicationCommandContext,
    ) -> WireModel:
        return await authority.revoke(cast(HeadlessVaultWriteRevokeParams, raw), context, cancellation)

    return {
        "vault/headless/status": status,
        "vault/headless/request": request,
        "vault/headless/activate": activate,
        "vault/headless/revoke": revoke,
    }


def _record_from_value(value: object, entity_id: str) -> HeadlessAuthorizationRecord:
    if not isinstance(value, Mapping):
        raise HeadlessVaultWriteError("headless authorization payload is not an object")
    expected = {
        "schemaVersion",
        "operationId",
        "approvalId",
        "grantId",
        "workspaceId",
        "workspaceInstanceId",
        "clientId",
        "clientRequestId",
        "requestFingerprint",
        "argsHash",
        "baseline",
        "state",
        "revision",
        "requestedAt",
        "expiresAt",
        "resolutionState",
        "resolvedAt",
        "activationRequestId",
        "activatedAt",
        "claimHash",
        "claimedAt",
        "terminalAt",
        "terminalReason",
        "revocationRequestId",
    }
    if set(value) != expected or value.get("schemaVersion") != _SCHEMA_VERSION or value.get("operationId") != entity_id:
        raise HeadlessVaultWriteError("headless authorization payload schema/identity is invalid")
    baseline = value["baseline"]
    if not isinstance(baseline, Mapping) or set(baseline) != {
        "rootIdentity",
        "databaseIdentity",
    }:
        raise HeadlessVaultWriteError("headless baseline payload is invalid")
    try:
        resolution = value["resolutionState"]
        return HeadlessAuthorizationRecord(
            operation_id=_text(value["operationId"]),
            approval_id=_text(value["approvalId"]),
            grant_id=_text(value["grantId"]),
            workspace_id=_text(value["workspaceId"]),
            workspace_instance_id=_text(value["workspaceInstanceId"]),
            client_id=_text(value["clientId"]),
            client_request_id=_text(value["clientRequestId"]),
            request_fingerprint=_text(value["requestFingerprint"]),
            args_hash=_text(value["argsHash"]),
            baseline=HeadlessBaseline(
                root_identity=_text(baseline["rootIdentity"]),
                database_identity=_text(baseline["databaseIdentity"]),
            ),
            state=_AuthorizationState(_text(value["state"])),
            revision=_integer(value["revision"]),
            requested_at=_datetime(value["requestedAt"]),
            expires_at=_datetime(value["expiresAt"]),
            resolution_state=None if resolution is None else ApprovalState(_text(resolution)),
            resolved_at=_optional_datetime(value["resolvedAt"]),
            activation_request_id=_optional_text(value["activationRequestId"]),
            activated_at=_optional_datetime(value["activatedAt"]),
            claim_hash=_optional_text(value["claimHash"]),
            claimed_at=_optional_datetime(value["claimedAt"]),
            terminal_at=_optional_datetime(value["terminalAt"]),
            terminal_reason=_optional_text(value["terminalReason"]),
            revocation_request_id=_optional_text(value["revocationRequestId"]),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise HeadlessVaultWriteError("headless authorization payload is corrupt") from error


def _text(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise TypeError("expected non-empty text")
    return value


def _optional_text(value: object) -> str | None:
    return None if value is None else _text(value)


def _integer(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise TypeError("expected non-negative integer")
    return value


def _boolean(value: object) -> bool:
    if not isinstance(value, bool):
        raise TypeError("expected boolean")
    return value


def _datetime(value: object) -> datetime:
    text = _text(value)
    result = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return result


def _optional_datetime(value: object) -> datetime | None:
    return None if value is None else _datetime(value)


__all__ = [
    "HeadlessAuthorizationRecord",
    "HeadlessAuthorizedVaultTransaction",
    "HeadlessBaseline",
    "HeadlessVaultWriteAuthority",
    "HeadlessVaultWriteError",
    "headless_vault_write_handlers",
]
