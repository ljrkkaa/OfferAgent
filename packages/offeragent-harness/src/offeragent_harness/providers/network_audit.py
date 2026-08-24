"""Durable, content-free audit boundary for model Provider network attempts."""

from __future__ import annotations

import asyncio
import concurrent.futures
import re
from urllib.parse import urlsplit

from offeragent_harness.foundation import (
    NetworkAuditRecord,
    NetworkCategory,
    NetworkOperationIdentity,
    NetworkOperationPurpose,
    network_audit_event_id,
)
from offeragent_harness.models import ModelRequest
from offeragent_harness.ports.cancellation import CancellationToken
from offeragent_harness.ports.network_audit import NetworkAuditSink
from offeragent_harness.ports.system import Clock

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


class ModelNetworkAuditError(RuntimeError):
    """Audit identity/storage failed, so the network operation must not continue."""


class ModelNetworkAuditor:
    def __init__(
        self,
        *,
        workspace_id: str,
        provider_id: str,
        endpoint: str,
        sink: NetworkAuditSink,
        clock: Clock,
        blocking_timeout_seconds: float = 10.0,
    ) -> None:
        parsed = urlsplit(endpoint)
        if (
            not workspace_id
            or not provider_id
            or parsed.scheme not in {"http", "https"}
            or parsed.hostname is None
            or blocking_timeout_seconds <= 0
            or blocking_timeout_seconds > 60
        ):
            raise ValueError("model network auditor configuration is invalid")
        self._workspace_id = workspace_id
        self._provider_id = provider_id
        self._host = parsed.hostname.casefold()
        self._port = parsed.port or (443 if parsed.scheme == "https" else 80)
        self._sink = sink
        self._clock = clock
        self._blocking_timeout = blocking_timeout_seconds

    def attempt(
        self,
        request: ModelRequest,
        attempt: int,
        *,
        payload_bytes: int,
        cancellation: CancellationToken,
    ) -> ModelAuditAttempt:
        return ModelAuditAttempt(self, request, attempt, payload_bytes, cancellation)

    async def record_intent(self, request: ModelRequest, attempt: int, *, sent_bytes: int) -> None:
        await self._record(
            request,
            attempt,
            stage="intent",
            outcome="attempting",
            status_code=None,
            sent_bytes=sent_bytes,
            received_bytes=0,
        )

    async def record_result(
        self,
        request: ModelRequest,
        attempt: int,
        *,
        outcome: str,
        status_code: int | None,
        sent_bytes: int,
        received_bytes: int,
    ) -> None:
        await self._record(
            request,
            attempt,
            stage="result",
            outcome=outcome,
            status_code=status_code,
            sent_bytes=sent_bytes,
            received_bytes=received_bytes,
        )

    def record_intent_blocking(
        self,
        loop: asyncio.AbstractEventLoop,
        request: ModelRequest,
        attempt: int,
        *,
        sent_bytes: int,
    ) -> None:
        self._wait(
            loop,
            self.record_intent(request, attempt, sent_bytes=sent_bytes),
        )

    def record_result_blocking(
        self,
        loop: asyncio.AbstractEventLoop,
        request: ModelRequest,
        attempt: int,
        *,
        outcome: str,
        status_code: int | None,
        sent_bytes: int,
        received_bytes: int,
    ) -> None:
        self._wait(
            loop,
            self.record_result(
                request,
                attempt,
                outcome=outcome,
                status_code=status_code,
                sent_bytes=sent_bytes,
                received_bytes=received_bytes,
            ),
        )

    def _wait(self, loop: asyncio.AbstractEventLoop, operation: object) -> None:
        if not asyncio.iscoroutine(operation):
            raise TypeError("model audit bridge requires a coroutine")
        future = asyncio.run_coroutine_threadsafe(operation, loop)
        try:
            future.result(timeout=self._blocking_timeout)
        except (Exception, concurrent.futures.TimeoutError) as error:
            future.cancel()
            raise ModelNetworkAuditError("model network audit could not be durably recorded") from error

    async def _record(
        self,
        request: ModelRequest,
        attempt: int,
        *,
        stage: str,
        outcome: str,
        status_code: int | None,
        sent_bytes: int,
        received_bytes: int,
    ) -> None:
        try:
            operation = self._operation(request, attempt)
            record = NetworkAuditRecord(
                category=NetworkCategory.MODEL,
                workspace_id=self._workspace_id,
                run_id=operation.run_id,
                tool_call_id=None,
                tool_name=None,
                provider_id=self._provider_id,
                host=self._host,
                port=self._port,
                outcome=outcome,
                status_code=status_code,
                sent_bytes=sent_bytes,
                received_bytes=received_bytes,
                redirect_count=0,
                attempt=attempt,
                approval_source=None,
                occurred_at=self._clock.utcnow(),
                operation_id=operation.operation_id,
                client_request_id=operation.client_request_id,
                operation_purpose=operation.purpose,
                phase="http_request",
                stage=stage,
                sequence=0,
                event_id=network_audit_event_id(operation, self._provider_id, "http_request", stage, 0),
            )
            await self._sink.record(record)
        except ModelNetworkAuditError:
            raise
        except Exception as error:
            raise ModelNetworkAuditError("model network audit identity/storage is unavailable") from error

    def _operation(self, request: ModelRequest, attempt: int) -> NetworkOperationIdentity:
        workspace = request.metadata.get("workspaceId")
        run_id = request.metadata.get("runId")
        if isinstance(run_id, str):
            if workspace != self._workspace_id:
                raise ModelNetworkAuditError("model Run audit Workspace identity mismatches the Provider scope")
            return NetworkOperationIdentity(
                workspace_id=self._workspace_id,
                operation_id=_identifier(request.request_id, "model request ID"),
                purpose=NetworkOperationPurpose.MODEL_INFERENCE,
                attempt=attempt,
                run_id=_identifier(run_id, "model Run ID"),
            )
        client_request_id = request.metadata.get("clientRequestId")
        if request.metadata.get("operation") != "model_health" or not isinstance(client_request_id, str):
            raise ModelNetworkAuditError("non-Run model network request lacks an explicit admin identity")
        return NetworkOperationIdentity(
            workspace_id=self._workspace_id,
            operation_id=_identifier(request.request_id, "model request ID"),
            purpose=NetworkOperationPurpose.MODEL_HEALTH,
            attempt=attempt,
            client_request_id=_identifier(client_request_id, "model health client request ID"),
        )


class ModelAuditAttempt:
    def __init__(
        self,
        auditor: ModelNetworkAuditor,
        request: ModelRequest,
        attempt: int,
        payload_bytes: int,
        cancellation: CancellationToken,
    ) -> None:
        self._auditor = auditor
        self._request = request
        self._attempt = attempt
        self._payload_bytes = payload_bytes
        self._sent_bytes = 0
        self._cancellation = cancellation
        self.outcome = "internal_error"
        self.status_code: int | None = None
        self.received_bytes = 0

    async def __aenter__(self) -> ModelAuditAttempt:
        await self._auditor.record_intent(
            self._request,
            self._attempt,
            sent_bytes=0,
        )
        return self

    def mark_sending(self) -> None:
        if self._sent_bytes != 0:
            raise RuntimeError("model audit attempt was marked as sending more than once")
        self._sent_bytes = self._payload_bytes

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        del exc_type, traceback
        locally_cancelled = self._cancellation.cancelled or isinstance(exc, (asyncio.CancelledError, GeneratorExit))
        if locally_cancelled:
            self.outcome = "cancelled_before_send" if self._sent_bytes == 0 else "provider_cancel_unconfirmed"
        try:
            await self._auditor.record_result(
                self._request,
                self._attempt,
                outcome=self.outcome,
                status_code=self.status_code,
                sent_bytes=self._sent_bytes,
                received_bytes=self.received_bytes,
            )
        except ModelNetworkAuditError:
            if locally_cancelled and isinstance(exc, BaseException):
                return
            raise


class NullModelAuditAttempt:
    outcome = "internal_error"
    status_code: int | None = None
    received_bytes = 0

    async def __aenter__(self) -> NullModelAuditAttempt:
        return self

    def mark_sending(self) -> None:
        return None

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        del exc_type, exc, traceback


def _identifier(value: str, label: str) -> str:
    if _IDENTIFIER.fullmatch(value) is None:
        raise ModelNetworkAuditError(f"{label} is not a bounded opaque identifier")
    return value


__all__ = [
    "ModelAuditAttempt",
    "ModelNetworkAuditError",
    "ModelNetworkAuditor",
    "NullModelAuditAttempt",
]
