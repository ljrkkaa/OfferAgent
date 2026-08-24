from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from datetime import datetime, timezone
from typing import Any

import pytest

from offeragent_harness.adapters.local_artifacts import LocalArtifactStore
from offeragent_harness.agent.state import RunPhase, RunState
from offeragent_harness.models import (
    ModelEvent,
    ModelEventKind,
    ModelFinishReason,
    ModelRequest,
    ModelUsage,
)
from offeragent_harness.runtime.context_summary import (
    ContextCompactionService,
    ContextSummaryError,
    ContextSummaryRepository,
)
from offeragent_harness.runtime.run_preparation import (
    ConversationHistoryRunPreparationAdapter,
    RunPreparationRequest,
)
from offeragent_harness.sessions import (
    AgentLineage,
    Run,
    RunKind,
    RunStatus,
    TerminationReason,
    Turn,
    TurnStatus,
)
from offeragent_harness.testing import (
    DeterministicIdGenerator,
    InMemoryUnitOfWorkFactory,
    ManualCancellationToken,
    ManualClock,
)

NOW = datetime(2026, 8, 8, tzinfo=timezone.utc)


def _summary(turn_ids: tuple[str, ...]) -> dict[str, object]:
    empty: list[object] = []
    return {
        "completedWork": [{"text": "完成了已提交工作", "sourceTurnIds": list(turn_ids)}],
        "constraints": [{"text": "不得硬编码评测答案", "sourceTurnIds": [turn_ids[0]]}],
        "currentState": [{"text": "继续验证实现", "sourceTurnIds": [turn_ids[-1]]}],
        "decisions": empty,
        "failuresAndResolutions": empty,
        "openQuestions": empty,
        "pendingWork": [{"text": "运行真实链路评测", "sourceTurnIds": [turn_ids[-1]]}],
        "userGoals": [{"text": "完成 OfferAgent", "sourceTurnIds": [turn_ids[0]]}],
        "userPreferences": empty,
    }


class _Gateway:
    def __init__(self, outputs: list[Mapping[str, Any]]) -> None:
        self.outputs = outputs
        self.requests: list[ModelRequest] = []

    async def stream(self, request: ModelRequest, cancellation: Any) -> AsyncIterator[ModelEvent]:
        cancellation.checkpoint()
        self.requests.append(request)
        output = self.outputs.pop(0)
        usage = ModelUsage(120, 40, 20, 0)
        yield ModelEvent(request.request_id, 1, ModelEventKind.STARTED)
        yield ModelEvent(request.request_id, 2, ModelEventKind.STRUCTURED_OUTPUT, data=output)
        yield ModelEvent(request.request_id, 3, ModelEventKind.USAGE, usage=usage)
        yield ModelEvent(request.request_id, 4, ModelEventKind.COMPLETED, finish_reason=ModelFinishReason.STOP)


async def _seed_turns(unit_of_work: InMemoryUnitOfWorkFactory, start: int, end: int) -> None:
    async with unit_of_work.begin() as work:
        for ordinal in range(start, end + 1):
            turn_id = f"turn_{ordinal}"
            run_id = f"run_{ordinal}"
            turn = Turn(
                turn_id,
                "ses_main",
                ordinal,
                TurnStatus.COMPLETED,
                ({"type": "text", "text": f"目标 {ordinal}; api_key=should-not-leak"},),
                NOW,
                NOW,
            )
            run = Run(
                run_id,
                "ses_main",
                turn_id,
                "ws_main",
                AgentLineage.root(run_id),
                RunKind.ROOT,
                RunStatus.COMPLETED,
                1,
                3,
                {"model": "deepseek-v4-flash"},
                NOW,
                NOW,
                None,
                TerminationReason.COMPLETED,
            )
            state = RunState(
                "ws_main",
                "ses_main",
                turn_id,
                run_id,
                AgentLineage.root(run_id),
                phase=RunPhase.COMPLETED,
                assistant_text=f"已完成第 {ordinal} 步",
            )
            await work.entities.put("turns", turn_id, turn, expected_revision=0)
            await work.entities.put("runs", run_id, run, expected_revision=0)
            await work.entities.put("run_states", run_id, state, expected_revision=0)
        await work.commit()


def _service(tmp_path: Any, unit_of_work: InMemoryUnitOfWorkFactory, gateway: _Gateway) -> ContextCompactionService:
    artifacts = LocalArtifactStore(tmp_path / "artifacts", workspace_id="ws_main")
    return ContextCompactionService(
        workspace_id="ws_main",
        unit_of_work=unit_of_work,
        artifacts=artifacts,
        gateway_factory=lambda _: gateway,
        default_model="deepseek-v4-flash",
        clock=ManualClock(NOW),
        ids=DeterministicIdGenerator(),
    )


@pytest.mark.asyncio
async def test_context_compaction_persists_validated_artifact_and_redacts_secrets(tmp_path: Any) -> None:
    unit_of_work = InMemoryUnitOfWorkFactory()
    await _seed_turns(unit_of_work, 1, 2)
    gateway = _Gateway([_summary(("turn_1", "turn_2"))])
    service = _service(tmp_path, unit_of_work, gateway)

    result = await service.compact_session(
        session_id="ses_main",
        through_turn_id="turn_2",
        trigger="manual",
        cancellation=ManualCancellationToken(),
    )
    loaded = await service.repository.latest("ses_main")

    assert loaded is not None and loaded.record == result.record
    assert loaded.record.to_turn_ordinal == 2
    assert loaded.payload["summary"] == _summary(("turn_1", "turn_2"))
    request_text = str(gateway.requests[0].messages[1].content[0].data["text"])
    assert "should-not-leak" not in request_text
    assert "<secret:redacted>" in request_text
    assert gateway.requests[0].purpose.value == "compaction"


@pytest.mark.asyncio
async def test_context_compaction_is_incremental_and_parent_chain_is_verified(tmp_path: Any) -> None:
    unit_of_work = InMemoryUnitOfWorkFactory()
    await _seed_turns(unit_of_work, 1, 2)
    gateway = _Gateway([_summary(("turn_1", "turn_2")), _summary(("turn_3", "turn_4"))])
    service = _service(tmp_path, unit_of_work, gateway)
    first = await service.compact_session(
        session_id="ses_main",
        through_turn_id="turn_2",
        trigger="manual",
        cancellation=ManualCancellationToken(),
    )
    await _seed_turns(unit_of_work, 3, 4)
    second = await service.compact_session(
        session_id="ses_main",
        through_turn_id="turn_4",
        trigger="auto",
        cancellation=ManualCancellationToken(),
    )
    replay = await service.compact_session(
        session_id="ses_main",
        through_turn_id="turn_4",
        trigger="auto",
        cancellation=ManualCancellationToken(),
    )

    assert second.record.parent_summary_id == first.record.summary_id
    assert second.record.parent_artifact_sha256 == first.record.artifact_sha256
    assert replay.record == second.record
    assert len(gateway.requests) == 2


@pytest.mark.asyncio
async def test_run_preparation_injects_summary_and_only_boundary_following_turns(tmp_path: Any) -> None:
    unit_of_work = InMemoryUnitOfWorkFactory()
    await _seed_turns(unit_of_work, 1, 4)
    gateway = _Gateway([_summary(("turn_1", "turn_2"))])
    service = _service(tmp_path, unit_of_work, gateway)
    await service.compact_session(
        session_id="ses_main",
        through_turn_id="turn_2",
        trigger="manual",
        cancellation=ManualCancellationToken(),
    )
    adapter = ConversationHistoryRunPreparationAdapter(
        workspace_id="ws_main",
        unit_of_work=unit_of_work,
        summaries=service.repository,
        compactor=service,
    )
    request = RunPreparationRequest(
        profile_id="profile_main",
        workspace_id="ws_main",
        session_id="ses_main",
        turn_id="turn_current",
        run_id="run_current",
        lineage=AgentLineage.root("run_current"),
        query_text="继续",
        memory_enabled=False,
    )
    fragments = await adapter.context_fragments(request, RunPhase.LOADING_CONTEXT, ManualCancellationToken())
    loaded = await service.repository.latest("ses_main")

    assert fragments[0].fragment_id.startswith("conversation-summary:")
    assert loaded is not None
    assert fragments[0].artifact_ids == (loaded.record.artifact_id,)
    assert [item.fragment_id for item in fragments[1:]] == [
        "conversation:turn_3:user",
        "conversation:turn_3:assistant",
        "conversation:turn_4:user",
        "conversation:turn_4:assistant",
    ]


@pytest.mark.asyncio
async def test_invalid_claim_source_fails_without_advancing_summary_head(tmp_path: Any) -> None:
    unit_of_work = InMemoryUnitOfWorkFactory()
    await _seed_turns(unit_of_work, 1, 2)
    gateway = _Gateway([_summary(("turn_unknown",))])
    service = _service(tmp_path, unit_of_work, gateway)

    with pytest.raises(ContextSummaryError, match="source Turn IDs"):
        await service.compact_session(
            session_id="ses_main",
            through_turn_id="turn_2",
            trigger="manual",
            cancellation=ManualCancellationToken(),
        )
    assert await service.repository.latest("ses_main") is None


def test_repository_rejects_cross_workspace_configuration(tmp_path: Any) -> None:
    with pytest.raises(ValueError, match="configuration"):
        ContextSummaryRepository(
            workspace_id="",
            unit_of_work=InMemoryUnitOfWorkFactory(),
            artifacts=LocalArtifactStore(tmp_path / "artifacts", workspace_id="ws_main"),
        )
