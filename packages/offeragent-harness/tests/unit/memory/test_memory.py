from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from offeragent_harness.adapters.sqlite_stores import SqliteUnitOfWorkFactory
from offeragent_harness.agent.state import RunPhase
from offeragent_harness.memory import (
    MemoryEvidenceError,
    MemoryKind,
    MemoryRepository,
    MemoryScope,
    MemoryScopeViolation,
    MemoryStatus,
    MemoryToolExecutor,
    memory_tool_definitions,
)
from offeragent_harness.ports import UnitOfWorkFactory
from offeragent_harness.runtime.memory_preparation import StructuredMemoryRunPreparationAdapter
from offeragent_harness.runtime.run_preparation import RunPreparationRequest
from offeragent_harness.sessions import (
    AgentLineage,
    Run,
    RunKind,
    RunStatus,
    Session,
    SessionStatus,
    Turn,
    TurnStatus,
)
from offeragent_harness.skills import SkillLimits
from offeragent_harness.skills.frontmatter import parse_skill_document
from offeragent_harness.testing import (
    DeterministicIdGenerator,
    InMemoryUnitOfWorkFactory,
    ManualCancellationToken,
    ManualClock,
)
from offeragent_harness.tools import ToolCall, ToolResultStatus, canonical_json_sha256

NOW = datetime(2026, 8, 8, 8, 0, tzinfo=timezone.utc)
WORKSPACE_ID = "workspace_test"


def test_builtin_personal_memory_skill_has_only_memory_authority() -> None:
    package_root = Path(__file__).resolve().parents[3]
    document = parse_skill_document(
        (package_root / "packaging" / "runtime-skills" / "personal-memory" / "SKILL.md").read_bytes(),
        SkillLimits(),
    )
    assert document.metadata.allowed_tools == frozenset(
        {
            "memory.search",
            "memory.get",
            "memory.pending",
            "memory.remember",
            "memory.propose",
            "memory.confirm",
            "memory.forget",
            "memory.history",
        }
    )
    assert "fixed topic dictionary" in document.body
    assert "exact contiguous span" in document.body


async def _seed_call(
    uow_factory: UnitOfWorkFactory,
    *,
    sequence: int,
    text: str,
    profile_id: str = "profile_alice",
    session_id: str = "session_alice",
    parent_lineage: AgentLineage | None = None,
    tool_name: str = "memory.search",
    arguments: dict[str, Any] | None = None,
) -> ToolCall:
    turn_id = f"turn_{session_id}_{sequence}"
    run_id = f"run_{session_id}_{sequence}"
    lineage = AgentLineage.root(run_id) if parent_lineage is None else parent_lineage.child(run_id, "researcher")
    async with uow_factory.begin() as uow:
        session = await uow.entities.get("sessions", session_id)
        if session is None:
            await uow.entities.put(
                "sessions",
                session_id,
                Session(
                    session_id,
                    WORKSPACE_ID,
                    profile_id,
                    "Memory test",
                    SessionStatus.ACTIVE,
                    NOW,
                    NOW,
                    1,
                ),
                expected_revision=0,
            )
        await uow.entities.put(
            "turns",
            turn_id,
            Turn(
                turn_id,
                session_id,
                sequence,
                TurnStatus.RUNNING,
                ({"type": "text", "text": text},),
                NOW,
                NOW,
            ),
            expected_revision=0,
        )
        await uow.entities.put(
            "runs",
            run_id,
            Run(
                run_id,
                session_id,
                turn_id,
                WORKSPACE_ID,
                lineage,
                RunKind.ROOT if lineage.depth == 0 else RunKind.SUBAGENT,
                RunStatus.PLANNING,
                1,
                0,
                {},
                NOW,
                NOW,
                None,
            ),
            expected_revision=0,
        )
        await uow.commit()
    definition = next(item for item in memory_tool_definitions() if item.name == tool_name)
    call_arguments = arguments or {}
    return ToolCall(
        tool_call_id=f"call_{session_id}_{sequence}_{tool_name.replace('.', '_')}",
        run_id=run_id,
        workspace_id=WORKSPACE_ID,
        name=tool_name,
        version=definition.version,
        arguments=call_arguments,
        args_hash=canonical_json_sha256(call_arguments),
        idempotency_key=f"idem_{session_id}_{sequence}_{tool_name}",
        deadline=None,
        lineage=lineage,
        definition_fingerprint=definition.fingerprint,
        result_sensitivity=definition.result_sensitivity,
    )


def _repository() -> tuple[MemoryRepository, InMemoryUnitOfWorkFactory]:
    uow = InMemoryUnitOfWorkFactory()
    return (
        MemoryRepository(
            workspace_id=WORKSPACE_ID,
            unit_of_work=uow,
            clock=ManualClock(NOW),
            ids=DeterministicIdGenerator(),
        ),
        uow,
    )


async def test_confirmed_memory_is_revisioned_searchable_and_forgotten() -> None:
    repository, uow = _repository()
    cancellation = ManualCancellationToken()
    first_call = await _seed_call(
        uow,
        sequence=1,
        text="Remember: I prefer Chinese for technical presentations.",
    )
    first = await repository.remember(
        first_call,
        key="communication.presentation_language",
        kind=MemoryKind.PREFERENCE,
        scope=MemoryScope.PROFILE,
        content="I prefer Chinese for technical presentations",
        evidence_quote="I prefer Chinese for technical presentations",
        pinned=True,
        importance=4,
        expires_at=None,
        cancellation=cancellation,
    )
    initial_hits = await repository.search(
        first_call,
        query="technical presentations",
        limit=8,
        cancellation=cancellation,
    )
    assert [hit.item.memory_id for hit in initial_hits] == [first.memory_id]

    second_call = await _seed_call(
        uow,
        sequence=2,
        text="Update this: use English for technical presentations from now on.",
    )
    second = await repository.remember(
        second_call,
        key="communication.presentation_language",
        kind=MemoryKind.PREFERENCE,
        scope=MemoryScope.PROFILE,
        content="use English for technical presentations from now on",
        evidence_quote="use English for technical presentations from now on",
        pinned=True,
        importance=4,
        expires_at=None,
        cancellation=cancellation,
    )
    assert second.supersedes_id == first.memory_id
    assert (await repository.get(second_call, first.memory_id, cancellation)).status is MemoryStatus.SUPERSEDED
    hits = await repository.search(
        second_call,
        query="technical presentations",
        limit=8,
        cancellation=cancellation,
    )
    assert [(hit.item.memory_id, hit.item.content) for hit in hits] == [
        (second.memory_id, "use English for technical presentations from now on")
    ]
    history = await repository.history(second_call, first.memory_id, cancellation)
    assert [event.event_type.value for event in history] == ["confirmed", "superseded"]

    third_call = await _seed_call(uow, sequence=3, text="Forget my technical presentation language preference.")
    forgotten = await repository.forget(
        third_call,
        memory_id=second.memory_id,
        evidence_quote="Forget my technical presentation language preference",
        cancellation=cancellation,
    )
    assert forgotten.status is MemoryStatus.FORGOTTEN
    assert (
        await repository.search(
            third_call,
            query="technical presentations",
            limit=8,
            cancellation=cancellation,
        )
        == ()
    )


async def test_inference_is_not_recalled_until_the_user_confirms_it() -> None:
    repository, uow = _repository()
    cancellation = ManualCancellationToken()
    proposal_call = await _seed_call(
        uow,
        sequence=1,
        text="For this one interview retrospective, make it very concise.",
    )
    proposal = await repository.propose(
        proposal_call,
        key="communication.conciseness",
        kind=MemoryKind.PREFERENCE,
        scope=MemoryScope.PROFILE,
        content="The user may prefer concise interview retrospectives long term",
        evidence_quote="For this one interview retrospective, make it very concise",
        pinned=False,
        importance=2,
        expires_at=None,
        cancellation=cancellation,
    )
    assert proposal.status is MemoryStatus.PROPOSED
    assert [item.memory_id for item in await repository.pending(proposal_call, limit=8, cancellation=cancellation)] == [
        proposal.memory_id
    ]
    assert (
        await repository.search(
            proposal_call,
            query="interview retrospective preference",
            limit=8,
            cancellation=cancellation,
        )
        == ()
    )

    confirm_call = await _seed_call(
        uow,
        sequence=2,
        text="I confirm this preference; keep future interview retrospectives concise.",
    )
    confirmed = await repository.confirm(
        confirm_call,
        memory_id=proposal.memory_id,
        evidence_quote="I confirm this preference",
        cancellation=cancellation,
    )
    assert confirmed.status is MemoryStatus.CONFIRMED
    assert await repository.pending(confirm_call, limit=8, cancellation=cancellation) == ()
    hits = await repository.search(
        confirm_call,
        query="concise interview retrospective",
        limit=8,
        cancellation=cancellation,
    )
    assert [hit.item.memory_id for hit in hits] == [proposal.memory_id]


async def test_run_preparation_injects_only_confirmed_pinned_memory() -> None:
    repository, uow = _repository()
    cancellation = ManualCancellationToken()
    call = await _seed_call(
        uow,
        sequence=1,
        text="Remember: always include source citations in my research notes.",
    )
    confirmed = await repository.remember(
        call,
        key="research.citations",
        kind=MemoryKind.PREFERENCE,
        scope=MemoryScope.PROFILE,
        content="always include source citations in my research notes",
        evidence_quote="always include source citations in my research notes",
        pinned=True,
        importance=5,
        expires_at=None,
        cancellation=cancellation,
    )
    await repository.propose(
        call,
        key="research.tables",
        kind=MemoryKind.PREFERENCE,
        scope=MemoryScope.PROFILE,
        content="The user may prefer comparison tables",
        evidence_quote="always include source citations in my research notes",
        pinned=True,
        importance=5,
        expires_at=None,
        cancellation=cancellation,
    )
    request = RunPreparationRequest(
        profile_id="profile_alice",
        workspace_id=WORKSPACE_ID,
        session_id="session_alice",
        turn_id="turn_session_alice_1",
        run_id=call.run_id,
        lineage=call.lineage,
        query_text="Draft the notes",
        memory_enabled=True,
    )
    adapter = StructuredMemoryRunPreparationAdapter(
        workspace_id=WORKSPACE_ID,
        repository=repository,
    )
    fragments = await adapter.context_fragments(request, RunPhase.SELECTING_MEMORY, cancellation)
    assert len(fragments) == 1
    assert fragments[0].content_hash == confirmed.content_hash
    assert fragments[0].source_refs == (
        f"memory:{confirmed.memory_id}",
        "session:session_alice:turn:turn_session_alice_1:run:run_session_alice_1",
    )
    assert "source citations" in fragments[0].text
    disabled = await adapter.context_fragments(
        RunPreparationRequest(
            profile_id=request.profile_id,
            workspace_id=request.workspace_id,
            session_id=request.session_id,
            turn_id=request.turn_id,
            run_id=request.run_id,
            lineage=request.lineage,
            query_text=request.query_text,
            memory_enabled=False,
        ),
        RunPhase.SELECTING_MEMORY,
        cancellation,
    )
    assert disabled == ()


async def test_memory_is_profile_and_session_scoped_and_subagents_cannot_write() -> None:
    repository, uow = _repository()
    cancellation = ManualCancellationToken()
    role = "\u5e73\u53f0\u5de5\u7a0b\u5e08"
    alice_text = f"Remember: the target role in this Session is {role}."
    alice_call = await _seed_call(uow, sequence=1, text=alice_text)
    item = await repository.remember(
        alice_call,
        key="search.target_role",
        kind=MemoryKind.TASK,
        scope=MemoryScope.SESSION,
        content=f"the target role in this Session is {role}",
        evidence_quote=f"the target role in this Session is {role}",
        pinned=True,
        importance=5,
        expires_at=None,
        cancellation=cancellation,
    )
    other_session = await _seed_call(
        uow,
        sequence=1,
        text="What is the target role?",
        session_id="session_alice_other",
    )
    assert (
        await repository.search(
            other_session,
            query="target role",
            limit=8,
            cancellation=cancellation,
        )
        == ()
    )
    with pytest.raises(MemoryScopeViolation):
        await repository.get(other_session, item.memory_id, cancellation)

    bob_call = await _seed_call(
        uow,
        sequence=1,
        text="What is the target role?",
        profile_id="profile_bob",
        session_id="session_bob",
    )
    assert (
        await repository.search(
            bob_call,
            query="target role",
            limit=8,
            cancellation=cancellation,
        )
        == ()
    )
    root_lineage = AgentLineage.root("parent_run")
    subagent_call = await _seed_call(
        uow,
        sequence=2,
        text="Remember: subagents must not write long-term memory.",
        parent_lineage=root_lineage,
    )
    with pytest.raises(MemoryScopeViolation, match="Subagents cannot"):
        await repository.remember(
            subagent_call,
            key="safety.subagent_write",
            kind=MemoryKind.DECISION,
            scope=MemoryScope.PROFILE,
            content="subagents must not write long-term memory",
            evidence_quote="subagents must not write long-term memory",
            pinned=False,
            importance=3,
            expires_at=None,
            cancellation=cancellation,
        )


async def test_tool_executor_rejects_paraphrased_evidence_and_credentials() -> None:
    repository, uow = _repository()
    executor = MemoryToolExecutor(repository)
    cancellation = ManualCancellationToken()
    paraphrase_args = {
        "memoryKey": "profile.role",
        "kind": "profile",
        "scope": "profile",
        "content": "I am a senior platform engineer",
        "evidenceQuote": "The user is a senior platform engineer",
    }
    paraphrase_call = await _seed_call(
        uow,
        sequence=1,
        text="I am a senior platform engineer.",
        tool_name="memory.remember",
        arguments=paraphrase_args,
    )
    paraphrase = await executor.execute(paraphrase_call, cancellation)
    assert paraphrase.status is ToolResultStatus.FAILED
    assert paraphrase.error is not None and paraphrase.error.code == "memory_validation_failed"

    secret_args = {
        "memoryKey": "account.deepseek",
        "kind": "profile",
        "scope": "profile",
        "content": "sk-private-value",
        "evidenceQuote": "api_key=sk-private-value",
    }
    secret_call = await _seed_call(
        uow,
        sequence=2,
        text="Remember api_key=sk-private-value",
        tool_name="memory.remember",
        arguments=secret_args,
    )
    secret = await executor.execute(secret_call, cancellation)
    assert secret.status is ToolResultStatus.FAILED
    assert secret.error is not None and secret.error.code == "memory_validation_failed"
    password_call = await _seed_call(
        uow,
        sequence=3,
        text="Remember that my password is correct-horse-battery-staple",
    )
    with pytest.raises(MemoryEvidenceError, match="credentials and secrets"):
        await repository.remember(
            password_call,
            key="account.password",
            kind=MemoryKind.PROFILE,
            scope=MemoryScope.PROFILE,
            content="my password is correct-horse-battery-staple",
            evidence_quote="my password is correct-horse-battery-staple",
            pinned=False,
            importance=3,
            expires_at=None,
            cancellation=cancellation,
        )
    with pytest.raises(MemoryEvidenceError):
        await repository.remember(
            paraphrase_call,
            key="profile.role",
            kind=MemoryKind.PROFILE,
            scope=MemoryScope.PROFILE,
            content="I am a senior platform engineer",
            evidence_quote="The user is a senior platform engineer",
            pinned=False,
            importance=3,
            expires_at=None,
            cancellation=cancellation,
        )


async def test_memory_round_trips_through_the_production_sqlite_codec(tmp_path: Path) -> None:
    uow = SqliteUnitOfWorkFactory(tmp_path / "state.sqlite")
    await uow.initialize()
    repository = MemoryRepository(
        workspace_id=WORKSPACE_ID,
        unit_of_work=uow,
        clock=ManualClock(NOW),
        ids=DeterministicIdGenerator(),
    )
    cancellation = ManualCancellationToken()
    call = await _seed_call(
        uow,
        sequence=1,
        text="Remember: my preferred deployment target is a single RTX 4090.",
    )
    item = await repository.remember(
        call,
        key="deployment.target",
        kind=MemoryKind.PREFERENCE,
        scope=MemoryScope.PROFILE,
        content="my preferred deployment target is a single RTX 4090",
        evidence_quote="my preferred deployment target is a single RTX 4090",
        pinned=True,
        importance=5,
        expires_at=None,
        cancellation=cancellation,
    )

    reloaded = MemoryRepository(
        workspace_id=WORKSPACE_ID,
        unit_of_work=SqliteUnitOfWorkFactory(tmp_path / "state.sqlite"),
        clock=ManualClock(NOW),
        ids=DeterministicIdGenerator(start=100),
    )
    hits = await reloaded.search(
        call,
        query="deployment RTX 4090",
        limit=8,
        cancellation=cancellation,
    )
    assert [hit.item.memory_id for hit in hits] == [item.memory_id]
    events = await reloaded.history(call, item.memory_id, cancellation)
    assert [(event.event_type.value, event.revision) for event in events] == [("confirmed", 1)]


async def test_expired_memory_is_excluded_without_destroying_its_audit_record() -> None:
    uow = InMemoryUnitOfWorkFactory()
    clock = ManualClock(NOW)
    repository = MemoryRepository(
        workspace_id=WORKSPACE_ID,
        unit_of_work=uow,
        clock=clock,
        ids=DeterministicIdGenerator(),
    )
    cancellation = ManualCancellationToken()
    call = await _seed_call(
        uow,
        sequence=1,
        text="Remember temporarily: the maintenance window is at 03:30 UTC.",
    )
    item = await repository.remember(
        call,
        key="maintenance.window",
        kind=MemoryKind.TASK,
        scope=MemoryScope.PROFILE,
        content="the maintenance window is at 03:30 UTC",
        evidence_quote="the maintenance window is at 03:30 UTC",
        pinned=True,
        importance=3,
        expires_at=NOW + timedelta(hours=1),
        cancellation=cancellation,
    )
    assert await repository.search(
        call,
        query="maintenance window",
        limit=8,
        cancellation=cancellation,
    )
    clock.advance(timedelta(hours=2))
    assert (
        await repository.search(
            call,
            query="maintenance window",
            limit=8,
            cancellation=cancellation,
        )
        == ()
    )
    assert (await repository.get(call, item.memory_id, cancellation)).memory_id == item.memory_id


async def test_search_indexes_semantic_key_segments_without_a_domain_vocabulary() -> None:
    repository, uow = _repository()
    cancellation = ManualCancellationToken()
    call = await _seed_call(
        uow,
        sequence=1,
        text="Remember exactly: the approved architecture label is RIVER-STONE-44.",
    )
    item = await repository.remember(
        call,
        key="architecture.label",
        kind=MemoryKind.DECISION,
        scope=MemoryScope.PROFILE,
        content="RIVER-STONE-44",
        evidence_quote="RIVER-STONE-44",
        pinned=False,
        importance=4,
        expires_at=None,
        cancellation=cancellation,
    )

    hits = await repository.search(
        call,
        query="approved architecture label",
        limit=8,
        cancellation=cancellation,
    )
    assert [hit.item.memory_id for hit in hits] == [item.memory_id]
