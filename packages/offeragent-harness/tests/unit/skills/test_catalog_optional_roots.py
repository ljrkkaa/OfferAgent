from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from offeragent_harness.ports.skills import SkillTrustVerificationRequest, SkillTrustVerificationResult
from offeragent_harness.skills import InMemorySkillStateStore, SkillCatalog, SkillLayer, SkillRoot
from offeragent_harness.skills.catalog import _is_missing_path_error
from offeragent_harness.testing import ManualCancellationToken


class _WindowsPathNotFound(OSError):
    winerror = 3


def test_optional_skill_root_accepts_windows_path_not_found() -> None:
    assert _is_missing_path_error(_WindowsPathNotFound(3, "The system cannot find the path specified"))


@dataclass(frozen=True, slots=True)
class _Verifier:
    async def verify(self, request: SkillTrustVerificationRequest) -> SkillTrustVerificationResult:
        return SkillTrustVerificationResult(True, "test", f"verified:{request.metadata_hash}", None)


@pytest.mark.asyncio
async def test_absent_builtin_root_does_not_hide_discovered_workspace_skills(tmp_path: Path) -> None:
    workspace = tmp_path / "vault"
    skill_root = workspace / ".claude" / "skills"
    skill_file = skill_root / "daily-study-workflow" / "SKILL.md"
    skill_file.parent.mkdir(parents=True)
    skill_file.write_text(
        "---\nname: daily-study-workflow\ndescription: Daily study planning.\n---\nUse local files only.\n",
        encoding="utf-8",
    )
    catalog = SkillCatalog(
        workspace_id="ws_test",
        roots=(
            SkillRoot(
                "ws_test",
                "runtime-builtin",
                SkillLayer.BUILTIN,
                (tmp_path / "runtime" / "skills").resolve(),
                workspace_trusted=True,
            ),
            SkillRoot(
                "ws_test",
                "workspace",
                SkillLayer.WORKSPACE,
                skill_root.resolve(),
                workspace_root=workspace.resolve(),
                workspace_trusted=True,
            ),
        ),
        trust_verifier=_Verifier(),
        state_store=InMemorySkillStateStore(),
    )

    result = await catalog.initialize(ManualCancellationToken())

    assert not result.partial
    assert [item.name for item in result.snapshot.descriptors] == ["daily-study-workflow"]
