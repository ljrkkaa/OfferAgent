from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from offeragent_harness.foundation.canonical import canonical_json_bytes
from offeragent_harness.knowledge import (
    KnowledgeCatalogStore,
    KnowledgeDiscovery,
    KnowledgeObjectStore,
    KnowledgeParseResult,
    KnowledgePreparationError,
    KnowledgePreparationService,
    KnowledgePreparationStore,
    PageEvidence,
    SourceState,
)


class _Cancellation:
    def checkpoint(self) -> None:
        return


def _service(tmp_path: Path) -> tuple[KnowledgePreparationService, KnowledgePreparationStore]:
    state = tmp_path / ".offeragent" / "knowledge"
    objects = KnowledgeObjectStore(state)
    preparations = KnowledgePreparationStore(state, objects)
    service = KnowledgePreparationService(
        vault_root=tmp_path,
        discovery=KnowledgeDiscovery(workspace_id="ws-test", vault_root=tmp_path),
        catalog=KnowledgeCatalogStore(state),
        objects=objects,
        preparations=preparations,
        binary_parser=None,
    )
    return service, preparations


async def test_markdown_preparation_is_durable_and_content_bound(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "arbitrary.md").write_text("# Arbitrary\n\nEvidence without domain keywords.", encoding="utf-8")
    service, store = _service(tmp_path)
    candidate = service.status().candidates[0]

    prepared = await service.prepare(candidate_ids=(candidate.candidate_id,), cancellation=_Cancellation())
    restored = store.load(prepared.ingestion_id)

    assert restored == prepared
    assert prepared.prepared_sources[0].page_index.nodes[0].title == "arbitrary"
    assert prepared.target_sources == (candidate.source,)

    record = tmp_path / ".offeragent" / "knowledge" / "preparations" / f"{prepared.ingestion_id}.json"
    value = json.loads(record.read_text(encoding="utf-8"))
    value["targetSources"][0]["byteSize"] += 1
    record.write_bytes(canonical_json_bytes(value))
    with pytest.raises(KnowledgePreparationError, match="invalid"):
        store.load(prepared.ingestion_id)


async def test_preparation_rejects_stale_and_unchanged_candidates(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    raw.mkdir()
    source = raw / "source.md"
    source.write_text("# Initial\nEvidence.", encoding="utf-8")
    service, _ = _service(tmp_path)
    candidate = service.status().candidates[0]
    source.write_text("# Changed\nDifferent evidence.", encoding="utf-8")
    with pytest.raises(KnowledgePreparationError, match="stale"):
        await service.prepare(candidate_ids=(candidate.candidate_id,), cancellation=_Cancellation())

    state = tmp_path / ".offeragent" / "knowledge"
    current = service.status().candidates[0]
    KnowledgeCatalogStore(state).commit(expected_revision=0, sources=(current.source,))
    assert service.status().candidates[0].state is SourceState.UNCHANGED
    with pytest.raises(KnowledgePreparationError, match="unchanged"):
        await service.prepare(
            candidate_ids=(service.status().candidates[0].candidate_id,), cancellation=_Cancellation()
        )


async def test_binary_preparation_uses_parser_output_without_semantic_fallbacks(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    raw.mkdir()
    payload = b"%PDF-test-fixture"
    (raw / "opaque.pdf").write_bytes(payload)
    state = tmp_path / ".offeragent" / "knowledge"
    objects = KnowledgeObjectStore(state)
    preparations = KnowledgePreparationStore(state, objects)

    class Parser:
        parser_fingerprint = "sha256:" + "f" * 64

        def __init__(self) -> None:
            self.calls = 0

        async def parse(self, *, source: object, absolute_path: Path, cancellation: object) -> KnowledgeParseResult:
            del source, absolute_path, cancellation
            self.calls += 1
            text = "An observed page from the configured parser."
            page = PageEvidence(1, text, "sha256:" + hashlib.sha256(text.encode()).hexdigest())
            return KnowledgeParseResult(
                candidate.source.source_id,
                candidate.source.content_hash,
                (page,),
                "sha256:" + "f" * 64,
            )

    parser = Parser()
    service = KnowledgePreparationService(
        vault_root=tmp_path,
        discovery=KnowledgeDiscovery(workspace_id="ws-test", vault_root=tmp_path),
        catalog=KnowledgeCatalogStore(state),
        objects=objects,
        preparations=preparations,
        binary_parser=parser,
    )
    candidate = service.status().candidates[0]
    prepared = await service.prepare(candidate_ids=(candidate.candidate_id,), cancellation=_Cancellation())
    cached = await service.prepare(candidate_ids=(candidate.candidate_id,), cancellation=_Cancellation())
    assert prepared.prepared_sources[0].pages[0].text.startswith("An observed page")
    assert cached.prepared_sources == prepared.prepared_sources
    assert parser.calls == 1


async def test_binary_preparation_rejects_parser_source_identity_drift(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "opaque.pdf").write_bytes(b"%PDF-test-fixture")
    state = tmp_path / ".offeragent" / "knowledge"
    objects = KnowledgeObjectStore(state)

    class Parser:
        parser_fingerprint = "sha256:" + "f" * 64

        async def parse(self, *, source: object, absolute_path: Path, cancellation: object) -> KnowledgeParseResult:
            del source, absolute_path, cancellation
            text = "Observed."
            page = PageEvidence(1, text, "sha256:" + hashlib.sha256(text.encode()).hexdigest())
            return KnowledgeParseResult("src-0000000000000000", "sha256:" + "0" * 64, (page,), "sha256:" + "f" * 64)

    service = KnowledgePreparationService(
        vault_root=tmp_path,
        discovery=KnowledgeDiscovery(workspace_id="ws-test", vault_root=tmp_path),
        catalog=KnowledgeCatalogStore(state),
        objects=objects,
        preparations=KnowledgePreparationStore(state, objects),
        binary_parser=Parser(),
    )
    candidate = service.status().candidates[0]
    with pytest.raises(KnowledgePreparationError, match="does not match"):
        await service.prepare(candidate_ids=(candidate.candidate_id,), cancellation=_Cancellation())
