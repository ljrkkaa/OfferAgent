from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timezone

import pytest

from offeragent_harness.ports import ApplicationCommandContext, CancellationToken
from offeragent_harness.protocol.content import ArtifactRef, ArtifactSensitivity, ArtifactState
from offeragent_harness.protocol.messages import (
    AgentCancelParams,
    AgentCancelResult,
    AgentResultParams,
    AgentResultResult,
    AgentStatusParams,
    AgentStatusResult,
)
from offeragent_harness.runtime.application_handlers import (
    SubagentCommandAuthority,
    subagent_command_handlers,
)
from offeragent_harness.subagents.models import (
    AgentBudget,
    AgentCancelCommand,
    AgentUsage,
    SubagentCancelReceipt,
    SubagentResult,
    SubagentRunStatus,
    SubagentStatusSnapshot,
)
from offeragent_harness.subagents.service import SubagentService
from offeragent_harness.testing import ManualCancellationToken

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


class Authority:
    async def resolve(self, context: ApplicationCommandContext, target_run_id: str) -> SubagentCommandAuthority:
        assert context.client_id == "client_1" and target_run_id == "run_child"
        return SubagentCommandAuthority("run_parent", "ses_1", "turn_1", NOW, 12)


class Artifacts:
    async def resolve(
        self,
        *,
        requester_run_id: str,
        owner_run_id: str,
        artifact_ids: Sequence[str],
    ) -> tuple[ArtifactRef, ...]:
        assert requester_run_id == "run_parent"
        assert owner_run_id == "run_child"
        assert artifact_ids == ("art_1",)
        return (
            ArtifactRef(
                artifact_id="art_1",
                content_hash="sha256:" + "a" * 64,
                media_type="application/json",
                size_bytes=10,
                sensitivity=ArtifactSensitivity.WORKSPACE,
                state=ArtifactState.COMPLETE,
            ),
        )


class Service(SubagentService):
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def status(self, requester_run_id: str, run_id: str) -> SubagentStatusSnapshot:
        self.calls.append(("status", requester_run_id))
        return SubagentStatusSnapshot(
            run_id,
            "run_root",
            "run_parent",
            "researcher",
            SubagentRunStatus.RUNNING,
            "executing_tools",
            1,
            AgentBudget(100, 100, 5, 5, 60, 1000, 1),
            AgentUsage(input_tokens=10, tool_calls=1),
            NOW,
            ("run_grandchild",),
            NOW,
        )

    async def result(
        self,
        requester_run_id: str,
        run_id: str,
        include: frozenset[str] = frozenset({"summary", "findings", "artifacts", "usage"}),
    ) -> SubagentResult:
        self.calls.append(("result", requester_run_id))
        assert {"summary", "usage"} <= include
        return SubagentResult(
            run_id,
            "completed",
            "done",
            ({"title": "finding", "summary": "validated"},),
            ({"toolCallId": "call_1", "sourceRefs": []},),
            ("art_1",),
            ({"description": "apply", "artifactId": "art_1", "targetFiles": []},),
            ("none",),
            {
                "inputTokens": 10,
                "outputTokens": 3,
                "modelCalls": 1,
                "toolCalls": 1,
                "costMicros": 5,
                "wallTimeMs": 20,
            },
        )

    async def cancel(
        self,
        command: AgentCancelCommand,
        cancellation: CancellationToken,
    ) -> SubagentCancelReceipt:
        cancellation.checkpoint()
        self.calls.append(("cancel", command.requester_run_id))
        assert command.run_id == "run_child" and command.cascade
        return SubagentCancelReceipt("run_child", True, ("run_grandchild",))


@pytest.mark.asyncio
async def test_subagent_commands_use_only_public_service_boundary() -> None:
    service = Service()
    handlers = subagent_command_handlers(
        service=service,
        authorities=Authority(),
        artifacts=Artifacts(),
    )
    context = ApplicationCommandContext(transport="stdio", client_id="client_1")
    cancellation = ManualCancellationToken()

    status = await handlers["agent/status"](AgentStatusParams(run_id="run_child"), cancellation, context)
    result = await handlers["agent/result"](
        AgentResultParams(run_id="run_child", include=["evidence", "artifacts", "proposedActions"]),
        cancellation,
        context,
    )
    cancelled = await handlers["agent/cancel"](
        AgentCancelParams(run_id="run_child", reason="stop", cascade=True),
        cancellation,
        context,
    )

    assert isinstance(status, AgentStatusResult)
    assert isinstance(result, AgentResultResult)
    assert isinstance(cancelled, AgentCancelResult)
    assert status.run.session_id == "ses_1" and status.run.last_sequence == 12
    assert result.result.evidence[0]["toolCallId"] == "call_1"
    assert result.result.artifacts[0].artifact_id == "art_1"
    assert cancelled.descendant_run_ids == ["run_grandchild"]
    assert service.calls == [("status", "run_parent"), ("result", "run_parent"), ("cancel", "run_parent")]
