from __future__ import annotations

from pathlib import Path

from offeragent_harness.knowledge import KnowledgeDiscovery, SourceState


def test_discovery_is_content_addressed_and_incremental(tmp_path: Path) -> None:
    raw = tmp_path / "raw" / "nested"
    raw.mkdir(parents=True)
    source = raw / "arbitrary-subject.md"
    source.write_text("# Unseen subject\n\nA fact with no evaluator vocabulary.", encoding="utf-8")
    discovery = KnowledgeDiscovery(workspace_id="ws_test", vault_root=tmp_path)

    initial = discovery.status(catalog_revision=0, catalog=())
    assert len(initial.candidates) == 1
    candidate = initial.candidates[0]
    assert candidate.state is SourceState.NEW
    assert candidate.source.relative_path == "raw/nested/arbitrary-subject.md"
    assert candidate.source.source_id.startswith("src-arbitrary-subject-")
    assert len(candidate.source.source_id) <= 48
    assert len(candidate.source.source_id.rsplit("-", maxsplit=1)[-1]) == 10

    unchanged = discovery.status(catalog_revision=1, catalog=(candidate.source,))
    assert unchanged.candidates[0].state is SourceState.UNCHANGED
    source.write_text("# Unseen subject\n\nThe source changed.", encoding="utf-8")
    changed = discovery.status(catalog_revision=1, catalog=(candidate.source,))
    assert changed.candidates[0].state is SourceState.CHANGED
    assert changed.candidates[0].candidate_id != unchanged.candidates[0].candidate_id


def test_discovery_reports_missing_without_deleting_catalog_state(tmp_path: Path) -> None:
    (tmp_path / "raw").mkdir()
    discovery = KnowledgeDiscovery(workspace_id="ws_test", vault_root=tmp_path)
    source_file = tmp_path / "raw" / "source.md"
    source_file.write_text("data", encoding="utf-8")
    record = discovery.status(catalog_revision=0, catalog=()).candidates[0].source
    source_file.unlink()

    status = discovery.status(catalog_revision=1, catalog=(record,))
    assert status.candidates == ()
    assert status.missing == (record,)
