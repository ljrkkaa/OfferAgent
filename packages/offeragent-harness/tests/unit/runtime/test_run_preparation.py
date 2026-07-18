from __future__ import annotations

import base64
import hashlib
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from offeragent_harness.agent.context_manager import (
    ContextFragment,
    ContextLayer,
    UserImageProvenance,
)
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
from offeragent_harness.runtime.conversation_attachments import (
    AttachmentClaim,
    AttachmentUploadRequest,
    ConversationAttachmentStore,
)
from offeragent_harness.runtime.run_preparation import (
    CompositeRunContextProvider,
    ConversationHistoryLimits,
    ConversationHistoryRunPreparationAdapter,
    ConversationImagePolicy,
    PreparedRunContext,
    RunPreparationLimits,
    RunPreparationRequest,
    VaultMemoryLimits,
    VaultMemoryRunPreparationAdapter,
    WorkspaceInstructionRunPreparationAdapter,
)
from offeragent_harness.sessions import AgentLineage, Run, RunKind, RunStatus, TerminationReason, Turn, TurnStatus
from offeragent_harness.testing import (
    DeterministicIdGenerator,
    InMemoryUnitOfWorkFactory,
    ManualCancellationToken,
    ManualClock,
)

FIRST_IMAGE = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4z8AAAAMBAQDJ/pLvAAAAAElFTkSuQmCC"
)
SECOND_IMAGE = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGNgYPgPAAEDAQAIicLsAAAAAElFTkSuQmCC"
)


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
        2,
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
    oversized_turn = Turn(
        "turn_oversized_old",
        "ses_main",
        1,
        TurnStatus.COMPLETED,
        ({"type": "text", "text": "x" * 2_000},),
        now,
        now,
    )
    oversized_run = Run(
        "run_oversized_old",
        "ses_main",
        oversized_turn.turn_id,
        "ws_main",
        AgentLineage.root("run_oversized_old"),
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
    oversized_state = RunState(
        "ws_main",
        "ses_main",
        oversized_turn.turn_id,
        oversized_run.run_id,
        oversized_run.lineage,
        phase=RunPhase.COMPLETED,
        assistant_text="old oversized answer",
    )
    async with unit_of_work.begin() as work:
        await work.entities.put("turns", oversized_turn.turn_id, oversized_turn, expected_revision=0)
        await work.entities.put("runs", oversized_run.run_id, oversized_run, expected_revision=0)
        await work.entities.put("run_states", oversized_state.run_id, oversized_state, expected_revision=0)
        await work.entities.put("turns", turn.turn_id, turn, expected_revision=0)
        await work.entities.put("runs", run.run_id, run, expected_revision=0)
        await work.entities.put("run_states", state.run_id, state, expected_revision=0)
        await work.commit()

    adapter = ConversationHistoryRunPreparationAdapter(
        workspace_id="ws_main",
        unit_of_work=unit_of_work,
        limits=ConversationHistoryLimits(max_turn_bytes=512, max_total_bytes=1_024),
    )
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
async def test_completed_historical_turn_rematerializes_ordered_images_on_user_only_after_restart(
    tmp_path: Path,
) -> None:
    unit_of_work = InMemoryUnitOfWorkFactory()
    now = datetime(2026, 7, 18, tzinfo=timezone.utc)
    root = tmp_path / "attachments"
    token = ManualCancellationToken()
    store = ConversationAttachmentStore(
        root,
        workspace_id="ws_main",
        clock=ManualClock(now),
        ids=DeterministicIdGenerator(),
    )
    artifacts = []
    for index, payload in enumerate((FIRST_IMAGE, SECOND_IMAGE)):
        content_hash = f"sha256:{hashlib.sha256(payload).hexdigest()}"
        begun = await store.begin(
            AttachmentUploadRequest(
                "ses_main",
                f"req_history_{index}",
                f"page-{index}.png",
                "image/png",
                len(payload),
                content_hash,
            ),
            token,
        )
        await store.append(begun.upload_id, 0, payload, token)
        artifacts.append((await store.commit(begun.upload_id, token)).artifact)
    claims = tuple(
        AttachmentClaim(
            artifact.artifact_id,
            index,
            artifact.content_hash,
            artifact.media_type,
            artifact.size_bytes,
        )
        for index, artifact in enumerate(artifacts)
    )
    await store.claim_submission("ses_main", "turn_previous", claims, token)
    turn = Turn(
        "turn_previous",
        "ses_main",
        2,
        TurnStatus.COMPLETED,
        (
            {"type": "text", "text": "请按顺序读取两页面经"},
            *(
                {"type": "image", "artifact": artifact.to_wire(), "altText": f"page {index}"}
                for index, artifact in enumerate(artifacts, start=1)
            ),
        ),
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
        assistant_text="两页属于同一场面试。",
    )
    text_turn = Turn(
        "turn_text_before_image",
        "ses_main",
        1,
        TurnStatus.COMPLETED,
        ({"type": "text", "text": "Earlier text-only question."},),
        now,
        now,
    )
    text_run = Run(
        "run_text_before_image",
        "ses_main",
        "turn_text_before_image",
        "ws_main",
        AgentLineage.root("run_text_before_image"),
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
    text_state = RunState(
        "ws_main",
        "ses_main",
        "turn_text_before_image",
        "run_text_before_image",
        AgentLineage.root("run_text_before_image"),
        phase=RunPhase.COMPLETED,
        assistant_text="Earlier text-only answer.",
    )
    async with unit_of_work.begin() as work:
        await work.entities.put("turns", text_turn.turn_id, text_turn, expected_revision=0)
        await work.entities.put("runs", text_run.run_id, text_run, expected_revision=0)
        await work.entities.put("run_states", text_state.run_id, text_state, expected_revision=0)
        await work.entities.put("turns", turn.turn_id, turn, expected_revision=0)
        await work.entities.put("runs", run.run_id, run, expected_revision=0)
        await work.entities.put("run_states", state.run_id, state, expected_revision=0)
        await work.commit()

    class _MustNotReadAttachments:
        calls = 0

        async def materialize_claimed_submission(self, *args: object) -> object:
            del args
            self.calls += 1
            raise AssertionError("text-only model gate must run before historical attachment I/O")

    unread = _MustNotReadAttachments()
    text_only = ConversationHistoryRunPreparationAdapter(
        workspace_id="ws_main",
        unit_of_work=unit_of_work,
        attachments=unread,  # type: ignore[arg-type]
    )
    with pytest.raises(RunPreparationFailure) as unsupported:
        await text_only.load_for_run(
            session_id="ses_main",
            current_turn_id="turn_main",
            image_policy=ConversationImagePolicy(supports_images=False),
            cancellation=token,
        )
    assert unsupported.value.error_code.value == "provider.image_unsupported"
    assert unread.calls == 0

    bounded_images = await text_only.load_for_run(
        session_id="ses_main",
        current_turn_id="turn_main",
        image_policy=ConversationImagePolicy(
            supports_images=True,
            max_images=1,
            max_image_bytes=len(FIRST_IMAGE) + len(SECOND_IMAGE),
        ),
        cancellation=token,
    )
    assert bounded_images == ()
    assert unread.calls == 0

    class _InspectOnlyAttachments:
        def __init__(self) -> None:
            self.inspections = 0
            self.materializations = 0

        async def inspect_claimed_submission(
            self,
            session_id: str,
            turn_id: str,
            expected_claims: tuple[AttachmentClaim, ...],
            cancellation: ManualCancellationToken,
        ) -> object:
            assert session_id == "ses_main"
            assert turn_id == "turn_previous"
            cancellation.checkpoint()
            self.inspections += 1
            return tuple(
                SimpleNamespace(
                    attachment=SimpleNamespace(artifact_id=claim.artifact_id),
                    width=1_920,
                    height=1_080,
                )
                for claim in expected_claims
            )

        async def materialize_claimed_submission(self, *args: object) -> object:
            del args
            self.materializations += 1
            raise AssertionError("a token-omitted historical Turn must not materialize image bodies")

    inspect_only = _InspectOnlyAttachments()
    token_bounded = ConversationHistoryRunPreparationAdapter(
        workspace_id="ws_main",
        unit_of_work=unit_of_work,
        attachments=inspect_only,  # type: ignore[arg-type]
    )
    token_bounded_fragments = await token_bounded.load_for_run(
        session_id="ses_main",
        current_turn_id="turn_main",
        image_policy=ConversationImagePolicy(
            supports_images=True,
            max_estimated_tokens=1,
        ),
        cancellation=token,
    )
    assert token_bounded_fragments == ()
    assert inspect_only.inspections == 1
    assert inspect_only.materializations == 0

    reopened = ConversationAttachmentStore(
        root,
        workspace_id="ws_main",
        clock=ManualClock(now),
        ids=DeterministicIdGenerator(start=100),
    )
    adapter = ConversationHistoryRunPreparationAdapter(
        workspace_id="ws_main",
        unit_of_work=unit_of_work,
        attachments=reopened,
    )

    fragments = await adapter.load_for_run(
        session_id="ses_main",
        current_turn_id="turn_main",
        image_policy=ConversationImagePolicy(supports_images=True, detail="original"),
        cancellation=token,
    )

    assert [fragment.role for fragment in fragments] == [
        ModelRole.USER,
        ModelRole.ASSISTANT,
        ModelRole.USER,
        ModelRole.ASSISTANT,
    ]
    assert [fragment.conversation_turn_id for fragment in fragments] == [
        "turn_text_before_image",
        "turn_text_before_image",
        "turn_previous",
        "turn_previous",
    ]
    assert fragments[2].image_provenance is UserImageProvenance.RETAINED_CONVERSATION
    assert [block.binary_data for block in fragments[2].model_blocks] == [FIRST_IMAGE, SECOND_IMAGE]
    assert [block.data["detail"] for block in fragments[2].model_blocks] == ["original", "original"]
    assert fragments[1].model_blocks == ()
    assert fragments[3].model_blocks == ()

    second_path = root / "objects" / artifacts[1].artifact_id
    second_path.write_bytes(b"x" * len(SECOND_IMAGE))
    with pytest.raises(RunPreparationFailure) as corrupt_history:
        await adapter.load_for_run(
            session_id="ses_main",
            current_turn_id="turn_main",
            image_policy=ConversationImagePolicy(supports_images=True),
            cancellation=token,
        )
    assert corrupt_history.value.error_code.value == "input.image_invalid"
    assert corrupt_history.value.details["imageIndex"] == 2
    second_path.write_bytes(SECOND_IMAGE)

    await reopened.claim_submission("ses_main", "turn_orphan_claim", (claims[0],), token)
    orphan_turn = Turn(
        "turn_orphan_claim",
        "ses_main",
        3,
        TurnStatus.COMPLETED,
        ({"type": "text", "text": "This durable Turn declares no image."},),
        now,
        now,
    )
    orphan_run = Run(
        "run_orphan_claim",
        "ses_main",
        "turn_orphan_claim",
        "ws_main",
        AgentLineage.root("run_orphan_claim"),
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
    orphan_state = RunState(
        "ws_main",
        "ses_main",
        "turn_orphan_claim",
        "run_orphan_claim",
        AgentLineage.root("run_orphan_claim"),
        phase=RunPhase.COMPLETED,
        assistant_text="This result must not make the orphaned claim trustworthy.",
    )
    async with unit_of_work.begin() as work:
        await work.entities.put("turns", orphan_turn.turn_id, orphan_turn, expected_revision=0)
        await work.entities.put("runs", orphan_run.run_id, orphan_run, expected_revision=0)
        await work.entities.put("run_states", orphan_state.run_id, orphan_state, expected_revision=0)
        await work.commit()

    with pytest.raises(RunPreparationFailure) as orphaned:
        await adapter.load_for_run(
            session_id="ses_main",
            current_turn_id="turn_main",
            image_policy=ConversationImagePolicy(supports_images=True),
            cancellation=token,
        )
    assert orphaned.value.error_code.value == "input.image_invalid"
    assert orphaned.value.details["reason"] == "claim_conflict"


@pytest.mark.asyncio
async def test_generic_run_preparation_never_splits_a_conversation_turn_pair_at_its_fragment_limit() -> None:
    pair = (
        ContextFragment(
            "conversation:previous:user",
            ContextLayer.CONVERSATION,
            "question",
            Sensitivity.WORKSPACE,
            conversation_turn_id="turn_previous",
        ),
        ContextFragment(
            "conversation:previous:assistant",
            ContextLayer.CONVERSATION,
            "answer",
            Sensitivity.WORKSPACE,
            role=ModelRole.ASSISTANT,
            conversation_turn_id="turn_previous",
        ),
    )
    planner = _Planner()
    prepared = PreparedRunContext(
        request=_request(),
        provider=_Provider(pair),
        planner=planner,
        limits=RunPreparationLimits(max_fragments=1),
    )
    token = ManualCancellationToken()

    await prepared.prepare(_state(RunPhase.LOADING_CONTEXT), RunPhase.LOADING_CONTEXT, token)
    await prepared.prepare(_state(RunPhase.SELECTING_MEMORY), RunPhase.SELECTING_MEMORY, token)

    assert planner.conversation == []


@pytest.mark.asyncio
async def test_generic_run_preparation_keeps_the_newest_complete_conversation_suffix() -> None:
    pairs = tuple(
        ContextFragment(
            f"conversation:{turn_id}:{role.value}",
            ContextLayer.CONVERSATION,
            f"{role.value} {turn_id}",
            Sensitivity.WORKSPACE,
            role=role,
            conversation_turn_id=turn_id,
        )
        for turn_id in ("turn_old", "turn_new")
        for role in (ModelRole.USER, ModelRole.ASSISTANT)
    )
    planner = _Planner()
    prepared = PreparedRunContext(
        request=_request(),
        provider=_Provider(pairs),
        planner=planner,
        limits=RunPreparationLimits(max_fragments=2),
    )
    token = ManualCancellationToken()

    await prepared.prepare(_state(RunPhase.LOADING_CONTEXT), RunPhase.LOADING_CONTEXT, token)
    await prepared.prepare(_state(RunPhase.SELECTING_MEMORY), RunPhase.SELECTING_MEMORY, token)

    assert [fragment.conversation_turn_id for fragment in planner.conversation] == ["turn_new", "turn_new"]


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
async def test_completed_run_with_noncompleted_run_state_fails_closed_during_history_recovery() -> None:
    unit_of_work = InMemoryUnitOfWorkFactory()
    now = datetime(2026, 7, 14, tzinfo=timezone.utc)
    turn = Turn(
        "turn_divergent",
        "ses_main",
        1,
        TurnStatus.COMPLETED,
        ({"type": "text", "text": "prompt"},),
        now,
        now,
    )
    run = Run(
        "run_divergent",
        "ses_main",
        turn.turn_id,
        "ws_main",
        AgentLineage.root("run_divergent"),
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
        turn.turn_id,
        run.run_id,
        run.lineage,
        phase=RunPhase.FAILED,
        assistant_text="partial answer that must not become future context",
    )
    async with unit_of_work.begin() as work:
        await work.entities.put("turns", turn.turn_id, turn, expected_revision=0)
        await work.entities.put("runs", run.run_id, run, expected_revision=0)
        await work.entities.put("run_states", state.run_id, state, expected_revision=0)
        await work.commit()

    adapter = ConversationHistoryRunPreparationAdapter(workspace_id="ws_main", unit_of_work=unit_of_work)

    with pytest.raises(RunPreparationFailure, match="coherent assistant result"):
        await adapter.context_fragments(_request(), RunPhase.LOADING_CONTEXT, ManualCancellationToken())


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
