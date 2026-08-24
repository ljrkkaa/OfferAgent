"""Structured child-result Artifact persistence without transcript injection."""

from __future__ import annotations

import hashlib

from offeragent_harness.models.json_types import thaw_json
from offeragent_harness.ports import (
    ArtifactMetadata,
    ArtifactState,
    ArtifactStore,
    CancellationToken,
    Clock,
    IdGenerator,
    Sensitivity,
)
from offeragent_harness.ports.subagents import StoredSubagentResultArtifact
from offeragent_harness.tools.canonical import canonical_json_bytes

from .models import SubagentResult, SubagentRunRecord


class SubagentResultArtifactManager:
    def __init__(self, store: ArtifactStore, clock: Clock, ids: IdGenerator) -> None:
        self._store = store
        self._clock = clock
        self._ids = ids

    async def store(
        self,
        record: SubagentRunRecord,
        result: SubagentResult,
        cancellation: CancellationToken,
    ) -> StoredSubagentResultArtifact:
        cancellation.checkpoint()
        content = canonical_json_bytes(
            {
                "schemaVersion": 1,
                "runId": result.run_id,
                "status": result.status,
                "summary": result.summary,
                "findings": thaw_json(result.findings),
                "evidence": thaw_json(result.evidence),
                "artifactIds": list(result.artifact_ids),
                "proposedActions": thaw_json(result.proposed_actions),
                "unresolvedQuestions": list(result.unresolved_questions),
                "usage": thaw_json(result.usage),
                "error": None if result.error is None else thaw_json(result.error),
            }
        )
        if len(content) > record.budget_limit.artifact_bytes:
            raise ValueError("Subagent result Artifact exceeds the child Artifact budget")
        digest = f"sha256:{hashlib.sha256(content).hexdigest()}"
        state = {
            "completed": ArtifactState.COMPLETE,
            "cancelled": ArtifactState.CANCELLED,
            "failed": ArtifactState.FAILED,
            "interrupted": ArtifactState.PARTIAL,
        }.get(result.status, ArtifactState.UNVERIFIED)
        metadata = ArtifactMetadata(
            artifact_id=self._ids.new_id("art"),
            workspace_id=record.workspace_id,
            owner_run_id=record.run_id,
            mime_type="application/vnd.offeragent.subagent-result+json",
            byte_length=len(content),
            sha256=digest,
            sensitivity=Sensitivity.WORKSPACE,
            state=state,
            created_at=self._clock.utcnow(),
            attributes={
                "rootRunId": record.root_run_id,
                "parentRunId": record.parent_run_id,
                "agentName": record.agent_name,
                "resultSchema": record.result_schema,
            },
        )
        stored = await self._store.put(
            metadata,
            content,
            idempotency_key=f"subagent-result:{record.run_id}:{digest}",
        )
        if (
            stored.workspace_id != record.workspace_id
            or stored.owner_run_id != record.run_id
            or stored.sha256 != digest
            or stored.byte_length != len(content)
        ):
            raise ValueError("Artifact Store returned mismatched Subagent result metadata")
        cancellation.checkpoint()
        return StoredSubagentResultArtifact(stored)


__all__ = ["SubagentResultArtifactManager"]
