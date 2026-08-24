from __future__ import annotations

import pytest
from pydantic import ValidationError

from offeragent_harness.protocol.messages import SkillCatalogStatusSnapshot

HASH = "sha256:" + "a" * 64


def test_skill_catalog_status_is_typed_and_count_coherent() -> None:
    status = SkillCatalogStatusSnapshot.model_validate(
        {
            "revision": 3,
            "snapshotHash": HASH,
            "discoveredCount": 2,
            "enabledCount": 1,
            "partial": True,
            "diagnostics": [
                {
                    "severity": "warning",
                    "code": "invalid_frontmatter",
                    "message": "invalid Skill metadata",
                    "rootId": "workspace",
                    "path": "answer/SKILL.md",
                }
            ],
        }
    )
    assert status.revision == 3 and status.diagnostics[0].root_id == "workspace"

    with pytest.raises(ValidationError, match="enabledCount"):
        SkillCatalogStatusSnapshot.model_validate(
            {
                "revision": 1,
                "snapshotHash": HASH,
                "discoveredCount": 0,
                "enabledCount": 1,
                "partial": False,
                "diagnostics": [],
            }
        )
