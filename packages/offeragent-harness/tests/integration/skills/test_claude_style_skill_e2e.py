from __future__ import annotations

from pathlib import Path

import pytest

from offeragent_harness.agent.context_manager import ContextFragment, ContextInputs, ContextLayer
from offeragent_harness.config import HarnessConfig
from offeragent_harness.ports import Sensitivity
from offeragent_harness.runtime.production_skills import ProductionSkillBundleFactory
from offeragent_harness.runtime.production_worker_composition import _with_skill_prompt_context
from offeragent_harness.sessions import AgentLineage
from offeragent_harness.skills import SkillAuthority
from offeragent_harness.testing import ManualCancellationToken
from offeragent_harness.tools import ToolCall, ToolResultStatus, canonical_json_sha256

WORKSPACE_ID = "ws_claude_style_skill"


def _write_skill(root: Path, body: str) -> Path:
    package = root / "daily-study-workflow"
    package.mkdir(parents=True, exist_ok=True)
    file = package / "SKILL.md"
    file.write_text(
        "---\n"
        "name: daily-study-workflow\n"
        "description: Create evidence-based daily study plans from this Vault.\n"
        'allowed-tools: ["glob","grep","read","vault.transaction"]\n'
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


def _factory(tmp_path: Path) -> tuple[ProductionSkillBundleFactory, Path]:
    workspace = (tmp_path / "workspace").resolve()
    runtime = (tmp_path / "runtime").resolve()
    home = (tmp_path / "home").resolve()
    workspace.mkdir()
    (runtime / "skills").mkdir(parents=True)
    factory = ProductionSkillBundleFactory(
        workspace_id=WORKSPACE_ID,
        workspace_root=workspace,
        runtime_root=runtime,
        user_home=home,
    )
    return factory, workspace


def _authority() -> SkillAuthority:
    tools = frozenset({"skill", "glob", "grep", "read", "vault.transaction"})
    return SkillAuthority(tools, tools, workspace_trusted=True)


@pytest.mark.asyncio
async def test_workspace_trust_discovers_metadata_and_invocation_loads_body_lazily(tmp_path: Path) -> None:
    factory, workspace = _factory(tmp_path)
    body = "Read CLAUDE.md, the daily template, progress, and exact interview questions before writing.\n" * 200
    skill_file = _write_skill(workspace / ".claude" / "skills", body)
    token = ManualCancellationToken()

    prepared = await factory.prepare(
        HarnessConfig.model_validate({"policy": {"workspace_trusted": True}}),
        token,
        authority_ceiling=_authority(),
    )

    assert prepared.active_skill_names == frozenset({"daily-study-workflow"})
    assert len(prepared.prompt_descriptors) == 1
    assert prepared.prompt_descriptors[0].description in ("Create evidence-based daily study plans from this Vault.",)
    assert prepared.prompt_descriptors[0].description not in body
    assert prepared.catalog_status.discovered_count == prepared.catalog_status.enabled_count == 1
    assert prepared.catalog_status.diagnostics == ()

    bundle = factory.build_prepared(prepared)
    assert [item.name for item in bundle.definitions] == ["skill"]
    assert bundle.executor is not None
    result = await bundle.executor.execute(
        _call(bundle.definitions[0], {"name": "daily-study-workflow", "arguments": "7.21"}, 1),
        token,
    )

    assert result.status is ToolResultStatus.SUCCEEDED
    assert result.data is not None
    assert result.data["instruction"]["text"].replace("\r\n", "\n") == body
    assert result.data["arguments"] == "7.21"
    assert result.data["effectiveAllowedTools"] == ("glob", "grep", "read", "vault.transaction")
    assert skill_file.stat().st_size > len(prepared.prompt_descriptors[0].description)


@pytest.mark.asyncio
async def test_skill_metadata_is_in_context_and_body_is_not_loaded_before_invocation(tmp_path: Path) -> None:
    factory, workspace = _factory(tmp_path)
    body = "This exact body must remain lazy until the Skill tool is invoked."
    _write_skill(workspace / ".claude" / "skills", body)
    prepared = await factory.prepare(
        HarnessConfig.model_validate({"policy": {"workspace_trusted": True}}),
        ManualCancellationToken(),
        authority_ceiling=_authority(),
    )
    inputs = ContextInputs(
        (ContextFragment("turn:user", ContextLayer.USER_INPUT, "制定学习计划", Sensitivity.WORKSPACE),)
    )

    injected = _with_skill_prompt_context(inputs, prepared)

    assert len(injected.skills) == 1
    assert "daily-study-workflow" in injected.skills[0].text
    assert "Create evidence-based daily study plans" in injected.skills[0].text
    assert "invoke the `skill` tool before using any other tool" in injected.skills[0].text
    assert body not in injected.skills[0].text


@pytest.mark.asyncio
async def test_workspace_skill_changes_are_discovered_on_the_next_run_without_per_skill_confirmation(
    tmp_path: Path,
) -> None:
    factory, workspace = _factory(tmp_path)
    skill_file = _write_skill(workspace / ".claude" / "skills", "first body")
    token = ManualCancellationToken()
    config = HarnessConfig.model_validate({"policy": {"workspace_trusted": True}})
    first = await factory.prepare(config, token, authority_ceiling=_authority())

    skill_file.write_text(
        skill_file.read_text(encoding="utf-8").replace("first body", "second body"),
        encoding="utf-8",
    )
    second = await factory.prepare(config, token, authority_ceiling=_authority())

    assert second.catalog_revision == first.catalog_revision + 1
    assert second.active_skill_names == frozenset({"daily-study-workflow"})
    bundle = factory.build_prepared(second)
    assert bundle.executor is not None
    result = await bundle.executor.execute(
        _call(bundle.definitions[0], {"name": "daily-study-workflow"}, 2),
        token,
    )
    assert result.status is ToolResultStatus.SUCCEEDED
    assert result.data is not None
    assert result.data["instruction"]["text"].replace("\r\n", "\n") == "second body"


@pytest.mark.asyncio
async def test_untrusted_workspace_does_not_scan_project_skills(tmp_path: Path) -> None:
    factory, workspace = _factory(tmp_path)
    _write_skill(workspace / ".claude" / "skills", "workspace body")

    prepared = await factory.prepare(
        HarnessConfig.model_validate({"policy": {"workspace_trusted": False}}),
        ManualCancellationToken(),
        authority_ceiling=_authority(),
    )

    assert prepared.active_skill_names == frozenset()
    assert prepared.prompt_descriptors == ()
    assert factory.build_prepared(prepared).definitions == ()
