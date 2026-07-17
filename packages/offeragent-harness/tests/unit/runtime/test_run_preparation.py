from __future__ import annotations

import hashlib
from collections.abc import Sequence
from datetime import datetime, timezone

import pytest

from offeragent_harness.agent.context_manager import ContextFragment, ContextLayer
from offeragent_harness.agent.planner import PlanningStep
from offeragent_harness.agent.preparation import RunPreparationFailure
from offeragent_harness.agent.state import RunPhase, RunState
from offeragent_harness.models import ModelRole
from offeragent_harness.ports import (
    CancellationToken,
    Sensitivity,
    VaultEntry,
    VaultEntryKind,
    VaultRead,
)
from offeragent_harness.runtime.run_preparation import (
    CompositeRunContextProvider,
    ConversationHistoryRunPreparationAdapter,
    PreparedRunContext,
    RunPreparationLimits,
    RunPreparationRequest,
    VaultMemoryLimits,
    VaultMemoryRunPreparationAdapter,
    WorkspaceInstructionRunPreparationAdapter,
)
from offeragent_harness.sessions import AgentLineage, Run, RunKind, RunStatus, TerminationReason, Turn, TurnStatus
from offeragent_harness.testing import InMemoryUnitOfWorkFactory, ManualCancellationToken


def _fragment(*, scope: str = "workspace", text: str = "remember local evidence") -> ContextFragment:
    return ContextFragment(
        fragment_id="vault:memory:fixture",
        layer=ContextLayer.MEMORY,
        text=text,
        sensitivity=Sensitivity.WORKSPACE,
        source_refs=("vault:ws_main:.offeragent/memory/MEMORY.md",),
        content_hash="sha256:" + "a" * 64,
    )


def _request(*, workspace_id: str = "ws_main") -> RunPreparationRequest:
    return RunPreparationRequest(
        profile_id="profile_main",
        workspace_id=workspace_id,
        session_id="ses_main",
        turn_id="turn_main",
        run_id="run_main",
        lineage=AgentLineage.root("run_main"),
        query_text='[{"text":"local evidence","type":"text"}]',
        memory_enabled=True,
    )


def _state(phase: RunPhase = RunPhase.CREATED) -> RunState:
    return RunState(
        "ws_main",
        "ses_main",
        "turn_main",
        "run_main",
        AgentLineage.root("run_main"),
        phase=phase,
    )


class _Planner:
    def __init__(self) -> None:
        self.conversation: list[ContextFragment] = []
        self.memories: list[ContextFragment] = []
        self.skills: list[ContextFragment] = []

    def add_conversation_context(self, fragments: Sequence[ContextFragment]) -> None:
        self.conversation.extend(fragments)

    def add_memory_context(self, fragments: Sequence[ContextFragment]) -> None:
        self.memories.extend(fragments)

    def add_skill_context(self, fragments: Sequence[ContextFragment]) -> None:
        self.skills.extend(fragments)

    async def plan(self, state: RunState, cancellation: CancellationToken) -> PlanningStep:
        del state
        cancellation.checkpoint()
        return PlanningStep((), False, "done")


class _Provider:
    def __init__(self, fragments: tuple[ContextFragment, ...]) -> None:
        self.fragments = fragments
        self.calls: list[tuple[RunPhase, CancellationToken]] = []
        self.failures = 0

    async def context_fragments(
        self,
        request: RunPreparationRequest,
        phase: RunPhase,
        cancellation: CancellationToken,
    ) -> tuple[ContextFragment, ...]:
        del request
        self.calls.append((phase, cancellation))
        if self.failures:
            self.failures -= 1
            raise RuntimeError("transient")
        return () if phase is RunPhase.LOADING_CONTEXT else self.fragments


class _InstructionVault:
    def __init__(self, files: dict[str, str]) -> None:
        self.files = files

    async def stat(self, relative_path: str, cancellation: CancellationToken) -> VaultEntry | None:
        cancellation.checkpoint()
        if relative_path in self.files:
            return self._entry(relative_path, VaultEntryKind.FILE)
        prefix = f"{relative_path}/" if relative_path else ""
        if any(path.startswith(prefix) for path in self.files):
            return self._entry(relative_path, VaultEntryKind.DIRECTORY)
        return None

    async def read_bounded(self, relative_path: str, maximum: int, cancellation: CancellationToken) -> VaultRead:
        cancellation.checkpoint()
        content = self.files[relative_path].encode("utf-8")
        return VaultRead(self._entry(relative_path, VaultEntryKind.FILE), content[:maximum], len(content) > maximum)

    async def list(self, relative_path: str, cancellation: CancellationToken) -> tuple[VaultEntry, ...]:
        cancellation.checkpoint()
        prefix = f"{relative_path}/" if relative_path else ""
        children: dict[str, VaultEntryKind] = {}
        for path in self.files:
            if not path.startswith(prefix):
                continue
            remainder = path[len(prefix) :]
            name, _, nested = remainder.partition("/")
            child = f"{prefix}{name}" if prefix else name
            children[child] = VaultEntryKind.DIRECTORY if nested else VaultEntryKind.FILE
        return tuple(self._entry(path, kind) for path, kind in sorted(children.items()))

    def _entry(self, relative_path: str, kind: VaultEntryKind) -> VaultEntry:
        content = self.files.get(relative_path, "").encode("utf-8")
        return VaultEntry(
            resource_id=f"vault:ws_main:{relative_path}",
            relative_path=relative_path,
            kind=kind,
            size=len(content),
            modified_at=datetime(2026, 7, 14, tzinfo=timezone.utc),
            content_hash=None if kind is VaultEntryKind.DIRECTORY else f"sha256:{hashlib.sha256(content).hexdigest()}",
            workspace_revision=7,
        )


@pytest.mark.asyncio
async def test_vault_memory_adapter_reads_only_the_fixed_entry_point() -> None:
    vault = _InstructionVault({".offeragent/memory/MEMORY.md": "# Memory\nUse verified details only.\n"})
    adapter = VaultMemoryRunPreparationAdapter(workspace_id="ws_main", vault=vault)
    token = ManualCancellationToken()

    assert await adapter.context_fragments(_request(), RunPhase.LOADING_CONTEXT, token) == ()
    fragments = await adapter.context_fragments(_request(), RunPhase.SELECTING_MEMORY, token)

    assert len(fragments) == 1
    assert fragments[0].source_refs == ("vault:ws_main:.offeragent/memory/MEMORY.md",)
    assert "Use verified details only" in fragments[0].text


@pytest.mark.asyncio
async def test_vault_memory_adapter_rejects_other_workspace_and_fixed_limits() -> None:
    vault = _InstructionVault({".offeragent/memory/MEMORY.md": "one\ntwo\n"})
    adapter = VaultMemoryRunPreparationAdapter(
        workspace_id="ws_main",
        vault=vault,
        limits=VaultMemoryLimits(max_lines=1, max_bytes=64),
    )
    token = ManualCancellationToken()

    with pytest.raises(RunPreparationFailure, match="another Workspace"):
        await adapter.context_fragments(
            _request(workspace_id="ws_other"),
            RunPhase.SELECTING_MEMORY,
            token,
        )
    with pytest.raises(RunPreparationFailure, match="line limit"):
        await adapter.context_fragments(_request(), RunPhase.SELECTING_MEMORY, token)


@pytest.mark.asyncio
async def test_prepared_context_is_bounded_retried_once_and_idempotently_enriches_agent() -> None:
    fragment = _fragment()
    provider = _Provider((fragment, fragment))
    provider.failures = 1
    planner = _Planner()
    prepared = PreparedRunContext(
        request=_request(),
        provider=provider,
        planner=planner,
        limits=RunPreparationLimits(max_attempts=2),
    )
    token = ManualCancellationToken()

    await prepared.prepare(_state(RunPhase.LOADING_CONTEXT), RunPhase.LOADING_CONTEXT, token)
    await prepared.prepare(_state(RunPhase.SELECTING_MEMORY), RunPhase.SELECTING_MEMORY, token)
    await prepared.prepare(_state(RunPhase.SELECTING_MEMORY), RunPhase.SELECTING_MEMORY, token)

    assert [phase for phase, _ in provider.calls] == [
        RunPhase.LOADING_CONTEXT,
        RunPhase.LOADING_CONTEXT,
        RunPhase.SELECTING_MEMORY,
    ]
    assert planner.memories == [fragment]


@pytest.mark.asyncio
async def test_workspace_instructions_are_injected_before_planning() -> None:
    vault = _InstructionVault(
        {
            "CLAUDE.md": "@AGENTS.md\n",
            "AGENTS.md": "每日计划必须基于面经与 interview 问题。",
        }
    )
    provider = CompositeRunContextProvider(
        WorkspaceInstructionRunPreparationAdapter(
            workspace_id="ws_main",
            vault=vault,
        ),
        _Provider(()),
    )
    planner = _Planner()
    request = RunPreparationRequest(
        profile_id="profile_main",
        workspace_id="ws_main",
        session_id="ses_main",
        turn_id="turn_main",
        run_id="run_main",
        lineage=AgentLineage.root("run_main"),
        query_text="请安排今天的学习内容",
        memory_enabled=True,
    )
    prepared = PreparedRunContext(request=request, provider=provider, planner=planner)
    token = ManualCancellationToken()

    await prepared.prepare(_state(RunPhase.LOADING_CONTEXT), RunPhase.LOADING_CONTEXT, token)
    await prepared.prepare(_state(RunPhase.SELECTING_MEMORY), RunPhase.SELECTING_MEMORY, token)

    assert [item.source_refs for item in planner.skills] == [
        ("vault:ws_main:AGENTS.md",),
    ]


@pytest.mark.asyncio
async def test_claude_instruction_scope_follows_only_active_file_ancestors() -> None:
    provider = WorkspaceInstructionRunPreparationAdapter(
        workspace_id="ws_main",
        vault=_InstructionVault(
            {
                "CLAUDE.md": "root instruction\n",
                "src/CLAUDE.md": "src instruction\n",
                "src/backend/CLAUDE.md": "backend instruction\n",
                "docs/CLAUDE.md": "docs instruction\n",
                "AGENTS.md": "must never auto load\n",
            }
        ),
    )
    request = RunPreparationRequest(
        profile_id="profile_main",
        workspace_id="ws_main",
        session_id="ses_main",
        turn_id="turn_main",
        run_id="run_main",
        lineage=AgentLineage.root("run_main"),
        query_text="review active code",
        memory_enabled=False,
        active_file="src/backend/service.py",
    )
    fragments = await provider.context_fragments(request, RunPhase.LOADING_CONTEXT, ManualCancellationToken())
    assert [item.source_refs for item in fragments] == [
        ("vault:ws_main:CLAUDE.md",),
        ("vault:ws_main:src/CLAUDE.md",),
        ("vault:ws_main:src/backend/CLAUDE.md",),
    ]
    assert all("docs instruction" not in item.text and "must never" not in item.text for item in fragments)


@pytest.mark.asyncio
async def test_claude_rules_load_globally_or_for_the_explicit_active_file() -> None:
    vault = _InstructionVault(
        {
            ".claude/rules/global.md": "Always keep public interfaces typed.\n",
            ".claude/rules/python.md": '---\npaths:\n  - "src/**/*.py"\n---\nUse explicit domain types.\n',
            ".claude/rules/typescript.md": '---\npaths:\n  - "src/**/*.ts"\n---\nUse strict TypeScript.\n',
        }
    )
    provider = WorkspaceInstructionRunPreparationAdapter(
        workspace_id="ws_main",
        vault=vault,
    )
    request = RunPreparationRequest(
        profile_id="profile_main",
        workspace_id="ws_main",
        session_id="ses_main",
        turn_id="turn_main",
        run_id="run_main",
        lineage=AgentLineage.root("run_main"),
        query_text="检查当前文件",
        memory_enabled=False,
        active_file="src/runtime/worker.py",
    )

    fragments = await provider.context_fragments(request, RunPhase.LOADING_CONTEXT, ManualCancellationToken())

    assert [item.source_refs for item in fragments] == [
        ("vault:ws_main:.claude/rules/global.md",),
        ("vault:ws_main:.claude/rules/python.md",),
    ]
    assert all("TypeScript" not in item.text for item in fragments)


@pytest.mark.asyncio
async def test_completed_session_turns_are_injected_as_exact_native_role_history() -> None:
    unit_of_work = InMemoryUnitOfWorkFactory()
    now = datetime(2026, 7, 14, tzinfo=timezone.utc)
    turn = Turn(
        "turn_previous",
        "ses_main",
        1,
        TurnStatus.COMPLETED,
        ({"type": "text", "text": "请记住我有两年 Python 经验"},),
        now,
        now,
    )
    run = Run(
        "run_previous",
        "ses_main",
        "turn_previous",
        "ws_main",
        AgentLineage.root("run_previous"),
        RunKind.ROOT,
        RunStatus.COMPLETED,
        1,
        4,
        {},
        now,
        now,
        None,
        TerminationReason.COMPLETED,
    )
    state = RunState(
        "ws_main",
        "ses_main",
        "turn_previous",
        "run_previous",
        AgentLineage.root("run_previous"),
        phase=RunPhase.COMPLETED,
        assistant_text="已记住: 你有两年 Python 经验。",
    )
    async with unit_of_work.begin() as work:
        await work.entities.put("turns", turn.turn_id, turn, expected_revision=0)
        await work.entities.put("runs", run.run_id, run, expected_revision=0)
        await work.entities.put("run_states", state.run_id, state, expected_revision=0)
        await work.commit()

    adapter = ConversationHistoryRunPreparationAdapter(workspace_id="ws_main", unit_of_work=unit_of_work)
    planner = _Planner()
    prepared = PreparedRunContext(request=_request(), provider=adapter, planner=planner)
    token = ManualCancellationToken()

    await prepared.prepare(_state(RunPhase.LOADING_CONTEXT), RunPhase.LOADING_CONTEXT, token)
    await prepared.prepare(_state(RunPhase.SELECTING_MEMORY), RunPhase.SELECTING_MEMORY, token)

    assert [fragment.role for fragment in planner.conversation] == [ModelRole.USER, ModelRole.ASSISTANT]
    assert [fragment.source_refs for fragment in planner.conversation] == [
        ("session:ses_main:turn:turn_previous:input",),
        ("session:ses_main:run:run_previous:assistant",),
    ]
    assert "Python" in planner.conversation[0].text
    assert planner.conversation[1].text == "已记住: 你有两年 Python 经验。"


@pytest.mark.asyncio
async def test_cancelled_and_interrupted_partial_answers_remain_replayable_but_never_enter_future_context() -> None:
    unit_of_work = InMemoryUnitOfWorkFactory()
    now = datetime(2026, 7, 14, tzinfo=timezone.utc)
    terminal_cases = (
        (
            "cancelled",
            TurnStatus.CANCELLED,
            RunStatus.CANCELLED,
            RunPhase.CANCELLED,
            TerminationReason.CANCELLED_BY_USER,
        ),
        (
            "interrupted",
            TurnStatus.INTERRUPTED,
            RunStatus.INTERRUPTED,
            RunPhase.INTERRUPTED,
            TerminationReason.RUNTIME_INTERRUPTED,
        ),
    )
    async with unit_of_work.begin() as work:
        for ordinal, (suffix, turn_status, run_status, phase, termination) in enumerate(terminal_cases, start=1):
            turn_id = f"turn_{suffix}"
            run_id = f"run_{suffix}"
            turn = Turn(
                turn_id,
                "ses_main",
                ordinal,
                turn_status,
                ({"type": "text", "text": f"prompt {suffix}"},),
                now,
                now,
            )
            run = Run(
                run_id,
                "ses_main",
                turn_id,
                "ws_main",
                AgentLineage.root(run_id),
                RunKind.ROOT,
                run_status,
                1,
                4,
                {},
                now,
                now,
                None,
                termination,
            )
            state = RunState(
                "ws_main",
                "ses_main",
                turn_id,
                run_id,
                AgentLineage.root(run_id),
                phase=phase,
                assistant_text=f"visible partial answer {suffix}",
            )
            await work.entities.put("turns", turn_id, turn, expected_revision=0)
            await work.entities.put("runs", run_id, run, expected_revision=0)
            await work.entities.put("run_states", run_id, state, expected_revision=0)
        await work.commit()

    adapter = ConversationHistoryRunPreparationAdapter(workspace_id="ws_main", unit_of_work=unit_of_work)

    fragments = await adapter.context_fragments(_request(), RunPhase.LOADING_CONTEXT, ManualCancellationToken())

    assert fragments == ()


@pytest.mark.asyncio
async def test_prepared_context_fails_closed_before_enrichment_on_oversized_or_foreign_state() -> None:
    provider = _Provider((_fragment(text="x" * 100),))
    planner = _Planner()
    prepared = PreparedRunContext(
        request=_request(),
        provider=provider,
        planner=planner,
        limits=RunPreparationLimits(max_fragment_bytes=64, max_total_bytes=128),
    )
    token = ManualCancellationToken()

    with pytest.raises(RunPreparationFailure, match="byte limit"):
        await prepared.prepare(_state(RunPhase.SELECTING_MEMORY), RunPhase.SELECTING_MEMORY, token)
    assert planner.memories == []

    foreign = RunState("ws_other", "ses_main", "turn_main", "run_main", AgentLineage.root("run_main"))
    with pytest.raises(RunPreparationFailure, match="identity"):
        await prepared.prepare(foreign, RunPhase.SELECTING_MEMORY, token)


def test_run_preparation_request_rejects_non_boolean_memory_enablement() -> None:
    with pytest.raises(TypeError, match="boolean"):
        RunPreparationRequest(
            profile_id="profile_main",
            workspace_id="ws_main",
            session_id="ses_main",
            turn_id="turn_main",
            run_id="run_main",
            lineage=AgentLineage.root("run_main"),
            query_text="query",
            memory_enabled="false",  # type: ignore[arg-type]
        )
