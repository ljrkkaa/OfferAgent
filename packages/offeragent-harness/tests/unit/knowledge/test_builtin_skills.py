from __future__ import annotations

from pathlib import Path

from offeragent_harness.skills import SkillLimits
from offeragent_harness.skills.frontmatter import parse_skill_document


def test_builtin_knowledge_skills_have_narrow_nonoverlapping_authority() -> None:
    package_root = Path(__file__).resolve().parents[3]
    skill_root = package_root / "packaging" / "runtime-skills"
    ingestion = parse_skill_document(
        (skill_root / "knowledge-ingestion" / "SKILL.md").read_bytes(),
        SkillLimits(),
    )
    retrieval = parse_skill_document(
        (skill_root / "knowledge-retrieval" / "SKILL.md").read_bytes(),
        SkillLimits(),
    )

    assert ingestion.metadata.allowed_tools == frozenset(
        {"knowledge.status", "knowledge.prepare", "knowledge.compile", "knowledge.publish", "grep", "read"}
    )
    assert retrieval.metadata.allowed_tools == frozenset({"grep", "read"})
    assert "knowledge.publish" not in retrieval.metadata.allowed_tools
    assert "fixed domain vocabulary" in retrieval.body
    assert "canned text" in ingestion.body
