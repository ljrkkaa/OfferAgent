from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pytest

from offeragent_harness.adapters.sqlite_stores import SqliteUnitOfWorkFactory
from offeragent_harness.agent.context_manager import ContextFragment, ContextInputs, ContextLayer
from offeragent_harness.config import HarnessConfig
from offeragent_harness.permissions import CapabilityScope, PermissionMode, PolicyContext, RiskClass
from offeragent_harness.permissions.audit import NullPolicyAuditSink
from offeragent_harness.permissions.evaluator import RuleBasedPolicyEvaluator
from offeragent_harness.ports import Sensitivity
from offeragent_harness.ports.skills import SkillTrustVerificationRequest, SkillTrustVerificationResult
from offeragent_harness.runtime.production_skills import ProductionSkillBundleFactory
from offeragent_harness.runtime.production_worker_composition import _with_skill_prompt_context
from offeragent_harness.sessions import AgentLineage
from offeragent_harness.skills import (
    InMemorySkillStateStore,
    SkillAuthority,
    SkillCatalog,
    SkillLayer,
    SkillRoot,
    SkillToolExecutor,
    SkillTrustState,
)
from offeragent_harness.testing import (
    DeterministicIdGenerator,
    InMemoryUnitOfWorkFactory,
    ManualCancellationToken,
    ManualClock,
)
from offeragent_harness.tools import ToolCall, ToolResultStatus, ToolValidator, canonical_json_sha256
from offeragent_harness.tools.dispatcher import ToolDispatcher
from offeragent_harness.tools.kernel import UnifiedToolKernel
from offeragent_harness.tools.registry import ToolRegistry
from offeragent_harness.tools.scheduler import ToolScheduler

WORKSPACE_ID = "ws_claude_style_skill"
NOW = datetime(2026, 7, 14, tzinfo=timezone.utc)


@dataclass(frozen=True, slots=True)
class _Verifier:
    async def verify(self, request: SkillTrustVerificationRequest) -> SkillTrustVerificationResult:
        return SkillTrustVerificationResult(True, "test-release", f"verified:{request.metadata_hash}", None)


@dataclass(frozen=True, slots=True)
class _Authority:
    async def authority_for(self, call: ToolCall) -> SkillAuthority:
        del call
        return SkillAuthority(
            available_tools=frozenset({"glob", "grep", "read", "skill.list", "skill.read"}),
            policy_allowed_tools=frozenset({"glob", "grep", "read", "skill.list", "skill.read"}),
            enabled_skills=frozenset({"interview"}),
            workspace_trusted=True,
        )


def _write_skill(root: Path, body: str) -> Path:
    package = root / "interview"
    package.mkdir(parents=True)
    file = package / "SKILL.md"
    file.write_text(
        "---\n"
        "name: interview\n"
        "description: Prepare evidence-based interview material.\n"
        'allowed-tools: ["glob","grep","read"]\n'
        "---\n" + body,
        encoding="utf-8",
    )
    return file


def _call(definition: object, arguments: dict[str, object], index: int) -> ToolCall:
    from offeragent_harness.tools import ToolDefinition

    assert isinstance(definition, ToolDefinition)
    return ToolCall(
        f"call_skill_{index}",
        "run_skill_e2e",
        WORKSPACE_ID,
        definition.name,
        definition.version,
        arguments,
        canonical_json_sha256(arguments),
        f"skill-e2e-{index}",
        None,
        AgentLineage.root("run_skill_e2e"),
        definition.fingerprint,
        definition.result_sensitivity,
    )


@pytest.mark.asyncio
async def test_discovery_trust_runtime_injection_and_lazy_read_are_real(tmp_path: Path) -> None:
    user_root = (tmp_path / "home" / ".claude" / "skills").resolve()
    skill_file = _write_skill(user_root, "Read the candidate files before writing a concise answer.\n" * 8_000)
    catalog = SkillCatalog(
        workspace_id=WORKSPACE_ID,
        roots=(SkillRoot(WORKSPACE_ID, "user", SkillLayer.USER, user_root, workspace_trusted=True),),
        trust_verifier=_Verifier(),
        state_store=InMemorySkillStateStore(),
    )
    token = ManualCancellationToken()
    initial = await catalog.initialize(token)
    descriptor = initial.snapshot.descriptors[0]
    assert descriptor.trust_state is SkillTrustState.CONFIRMATION_REQUIRED
    assert descriptor.metadata_bytes_read < skill_file.stat().st_size
    assert not initial.snapshot.effective_descriptors

    confirmed = await catalog.confirm_trust(
        root_id="user",
        package_path="interview",
        expected_metadata_hash=descriptor.content_hash,
        expected_revision=initial.snapshot.revision,
        confirmed=True,
        cancellation=token,
    )
    assert confirmed.snapshot.effective_descriptors[0].name == "interview"

    executor = SkillToolExecutor(workspace_id=WORKSPACE_ID, catalog=catalog, authority_provider=_Authority())
    registry = ToolRegistry("skill-e2e", executor.definitions)
    context = PolicyContext(
        workspace_id=WORKSPACE_ID,
        session_id="ses_skill_e2e",
        principal_id="principal_skill_e2e",
        run_id="run_skill_e2e",
        permission_mode=PermissionMode.BYPASS,
        effective_scope=CapabilityScope(
            allowed_tools=frozenset({"skill.list", "skill.read"}),
            denied_tools=frozenset(),
            allowed_risks=frozenset({RiskClass.READ}),
            root_capabilities=frozenset({"skills.read"}),
            allow_network=False,
            allow_secret_handles=False,
        ),
        workspace_trusted=True,
        now=NOW,
    )
    clock = ManualClock(NOW)
    kernel = UnifiedToolKernel(
        registry=registry,
        validator=ToolValidator(),
        policy=RuleBasedPolicyEvaluator((), audit_sink=NullPolicyAuditSink()),
        policy_context=lambda _: context,
        scheduler=ToolScheduler(clock=clock, max_parallel_reads=2),
        dispatcher=ToolDispatcher(clock=clock, local=executor),
        journal=SqliteUnitOfWorkFactory(tmp_path / "state.sqlite").invocation_journal,
        clock=clock,
        ids=DeterministicIdGenerator(),
    )
    listed = _call(registry.get("skill.list", "1"), {}, 1)
    read = _call(
        registry.get("skill.read", "1"),
        {"name": "interview", "expectedRevision": catalog.snapshot.revision},
        2,
    )
    results = await kernel.execute_batch((listed, read), token)
    assert [item.result.status for item in results] == [ToolResultStatus.SUCCEEDED, ToolResultStatus.SUCCEEDED]
    assert results[1].result.data is not None
    assert results[1].result.data["instruction"]["text"].startswith("Read the candidate files")
    assert tuple(results[1].result.data["effectiveAllowedTools"]) == (
        "glob",
        "grep",
        "read",
    )

    skill_file.write_text(skill_file.read_text(encoding="utf-8") + "Changed after trust.\n", encoding="utf-8")
    drift = await executor.execute(
        _call(registry.get("skill.read", "1"), {"name": "interview", "expectedRevision": catalog.snapshot.revision}, 3),
        token,
    )
    assert drift.status is ToolResultStatus.FAILED
    assert drift.error is not None and drift.error.code == "skill_hash_drift"


def test_private_skill_metadata_is_rejected(tmp_path: Path) -> None:
    from offeragent_harness.skills.frontmatter import parse_skill_document
    from offeragent_harness.skills.models import SkillError, SkillErrorCode, SkillLimits

    with pytest.raises(SkillError) as error:
        parse_skill_document(
            b"---\nname: interview\ndescription: valid\nversion: 1.0.0\n---\nbody",
            SkillLimits(),
        )
    assert error.value.code is SkillErrorCode.UNKNOWN_FIELD


@pytest.mark.asyncio
async def test_production_factory_injects_only_trusted_metadata_then_reads_through_the_registered_tool(
    tmp_path: Path,
) -> None:
    workspace = (tmp_path / "workspace").resolve()
    runtime = (tmp_path / "runtime").resolve()
    home = (tmp_path / "home").resolve()
    workspace.mkdir()
    (runtime / "skills").mkdir(parents=True)
    body = "Open the candidate files with Glob, Grep, and Read before answering."
    _write_skill(home / ".claude" / "skills", body)
    token = ManualCancellationToken()
    factory = ProductionSkillBundleFactory(
        workspace_id=WORKSPACE_ID,
        workspace_root=workspace,
        runtime_root=runtime,
        user_home=home,
        unit_of_work=InMemoryUnitOfWorkFactory(),
        trust_verifier=_Verifier(),
        clock=ManualClock(NOW),
        ids=DeterministicIdGenerator(),
    )
    catalog = await factory.catalog_for_management(workspace_trusted=True, cancellation=token)
    descriptor = catalog.snapshot.descriptors[0]
    await catalog.confirm_trust(
        root_id=descriptor.root_id,
        package_path=descriptor.package_path,
        expected_metadata_hash=descriptor.content_hash,
        expected_revision=catalog.snapshot.revision,
        confirmed=True,
        cancellation=token,
    )
    config = HarnessConfig.model_validate(
        {"policy": {"workspace_trusted": True}, "extensibility": {"skills_enabled": True}}
    )
    authority = SkillAuthority(
        available_tools=frozenset({"glob", "grep", "read", "skill.list", "skill.read"}),
        policy_allowed_tools=frozenset({"glob", "grep", "read", "skill.list", "skill.read"}),
        enabled_skills=frozenset({"interview"}),
        workspace_trusted=True,
    )
    prepared = await factory.prepare(config, ("interview",), token, authority_ceiling=authority)
    bundle = factory.build_prepared(prepared)
    assert bundle.executor is not None
    assert {item.name for item in bundle.definitions} == {"skill.list", "skill.read"}

    inputs = ContextInputs(
        (ContextFragment("turn:user", ContextLayer.USER_INPUT, "prepare interview", Sensitivity.WORKSPACE),)
    )
    injected = _with_skill_prompt_context(inputs, prepared)
    assert len(injected.skills) == 1
    assert "Prepare evidence-based interview material." in injected.skills[0].text
    assert body not in injected.skills[0].text

    read = await bundle.executor.execute(
        _call(bundle.definitions[1], {"name": "interview", "expectedRevision": prepared.catalog_revision}, 4),
        token,
    )
    assert read.status is ToolResultStatus.SUCCEEDED
    assert read.data is not None and read.data["instruction"]["text"] == body
