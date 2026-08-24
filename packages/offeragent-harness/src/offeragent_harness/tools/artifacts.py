"""Externalizes schema-valid oversized tool output into the Artifact Store."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import replace
from typing import Any, Protocol

from offeragent_harness.ports import (
    ArtifactMetadata,
    ArtifactState,
    ArtifactStore,
    Clock,
    IdGenerator,
    Sensitivity,
)

from .canonical import canonical_json_bytes
from .definitions import ResultSensitivity, ToolCall
from .results import ToolResult, ToolResultStatus


class ArtifactReservation(Protocol):
    async def commit(self) -> None: ...

    async def release(self) -> None: ...


class ArtifactByteBudget(Protocol):
    async def reserve_artifact_bytes(self, byte_length: int) -> ArtifactReservation: ...


class ToolArtifactManager:
    def __init__(
        self,
        store: ArtifactStore,
        clock: Clock,
        ids: IdGenerator,
        budget: ArtifactByteBudget,
    ) -> None:
        self._store = store
        self._clock = clock
        self._ids = ids
        self._budget = budget

    async def reserve_artifact_bytes(self, byte_length: int) -> ArtifactReservation:
        """Expose only the reservation port needed by budget-aware local producers."""

        return await self._budget.reserve_artifact_bytes(byte_length)

    async def store_content(
        self,
        call: ToolCall,
        content: bytes,
        *,
        mime_type: str,
        purpose: str,
        sensitivity: Sensitivity = Sensitivity.WORKSPACE,
        state: ArtifactState = ArtifactState.COMPLETE,
        attributes: Mapping[str, Any] | None = None,
    ) -> ArtifactMetadata:
        """Persist bounded tool-owned content under this manager's Run budget and ACL."""

        if not isinstance(content, bytes):
            raise TypeError("Tool Artifact content must be bytes")
        if not mime_type or len(mime_type) > 256 or "\x00" in mime_type:
            raise ValueError("Tool Artifact MIME type is invalid")
        if not purpose or len(purpose) > 512 or "\x00" in purpose:
            raise ValueError("Tool Artifact purpose is invalid")
        if sensitivity is Sensitivity.SECRET:
            raise ValueError("Secret content cannot enter the Tool Artifact Store")
        custom = dict(attributes or {})
        reserved = {
            "toolCallId",
            "toolName",
            "toolVersion",
            "argsHash",
            "purpose",
            "acl",
        }
        if reserved & custom.keys():
            raise ValueError("Tool Artifact custom attributes cannot replace authority metadata")
        digest = f"sha256:{hashlib.sha256(content).hexdigest()}"
        identity = hashlib.sha256(
            (
                f"tool-content-v1\0{call.workspace_id}\0{call.run_id}\0{call.tool_call_id}\0"
                f"{purpose}\0{mime_type}\0{digest}"
            ).encode()
        ).hexdigest()
        metadata = ArtifactMetadata(
            artifact_id=f"artifact_{identity[:48]}",
            workspace_id=call.workspace_id,
            owner_run_id=call.run_id,
            mime_type=mime_type,
            byte_length=len(content),
            sha256=digest,
            sensitivity=sensitivity,
            state=state,
            created_at=self._clock.utcnow(),
            attributes={
                "toolCallId": call.tool_call_id,
                "toolName": call.name,
                "toolVersion": call.version,
                "argsHash": call.args_hash,
                "purpose": purpose,
                "acl": {
                    "workspaceId": call.workspace_id,
                    "ownerRunId": call.run_id,
                },
                **custom,
            },
        )
        reservation = await self._budget.reserve_artifact_bytes(len(content))
        try:
            stored = await self._store.put(
                metadata,
                content,
                idempotency_key=f"tool-content:{identity}",
            )
            if stored != metadata:
                raise ValueError("Artifact Store returned metadata that does not match the stored tool content")
            await reservation.commit()
        except BaseException:
            await reservation.release()
            raise
        return stored

    async def externalize(self, call: ToolCall, result: ToolResult) -> ToolResult:
        if result.data is None:
            return result
        if call.result_sensitivity in {ResultSensitivity.UNKNOWN, ResultSensitivity.SECRET}:
            raise ValueError("Unclassified or Secret tool output cannot enter the Artifact Store")
        content = canonical_json_bytes(result.data)
        digest = f"sha256:{hashlib.sha256(content).hexdigest()}"
        artifact_id = self._ids.new_id("artifact")
        state = {
            ToolResultStatus.SUCCEEDED: ArtifactState.COMPLETE,
            ToolResultStatus.PARTIAL: ArtifactState.PARTIAL,
            ToolResultStatus.CANCELLED: ArtifactState.CANCELLED,
            ToolResultStatus.FAILED: ArtifactState.FAILED,
        }.get(result.status, ArtifactState.UNVERIFIED)
        metadata = ArtifactMetadata(
            artifact_id=artifact_id,
            workspace_id=call.workspace_id,
            owner_run_id=call.run_id,
            mime_type="application/json",
            byte_length=len(content),
            sha256=digest,
            sensitivity=Sensitivity(call.result_sensitivity.value),
            state=state,
            created_at=self._clock.utcnow(),
            attributes={
                "toolCallId": call.tool_call_id,
                "toolName": call.name,
                "toolVersion": call.version,
                "argsHash": call.args_hash,
            },
        )
        reservation = await self._budget.reserve_artifact_bytes(len(content))
        try:
            stored = await self._store.put(
                metadata,
                content,
                idempotency_key=f"tool-output:{call.idempotency_key}:{digest}",
            )
            await reservation.commit()
        except BaseException:
            await reservation.release()
            raise
        if (
            stored.workspace_id != call.workspace_id
            or stored.owner_run_id != call.run_id
            or stored.sha256 != digest
            or stored.byte_length != len(content)
        ):
            raise ValueError("Artifact Store returned metadata that does not match the stored tool output")
        return replace(
            result,
            data=None,
            artifact_ids=(*result.artifact_ids, stored.artifact_id),
            user_visible_summary=f"{result.user_visible_summary} (完整输出已保存为 Artifact)",
        )


__all__ = ["ArtifactByteBudget", "ArtifactReservation", "ToolArtifactManager"]
