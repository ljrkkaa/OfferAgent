from __future__ import annotations

from pathlib import Path
from typing import Any

from offeragent_harness.knowledge import (
    KnowledgeCatalogStore,
    KnowledgeCompiler,
    KnowledgeDiscovery,
    KnowledgeObjectStore,
    KnowledgePreparationService,
    KnowledgePreparationStore,
    KnowledgePublisher,
    KnowledgeToolExecutor,
    knowledge_tool_definitions,
)
from offeragent_harness.models import thaw_json
from offeragent_harness.sessions import AgentLineage
from offeragent_harness.testing import ManualCancellationToken
from offeragent_harness.tools import ToolCall, ToolResultStatus, canonical_json_sha256


def _executor(tmp_path: Path) -> KnowledgeToolExecutor:
    state = tmp_path / ".offeragent" / "knowledge"
    catalog = KnowledgeCatalogStore(state)
    objects = KnowledgeObjectStore(state)
    preparations = KnowledgePreparationStore(state, objects)
    discovery = KnowledgeDiscovery(workspace_id="ws-test", vault_root=tmp_path)
    preparation = KnowledgePreparationService(
        vault_root=tmp_path,
        discovery=discovery,
        catalog=catalog,
        objects=objects,
        preparations=preparations,
        binary_parser=None,
    )
    compiler = KnowledgeCompiler(
        discovery=discovery,
        catalog=catalog,
        preparations=preparations,
        publisher=KnowledgePublisher(vault_root=tmp_path, catalog=catalog, objects=objects),
    )
    return KnowledgeToolExecutor(workspace_id="ws-test", preparation=preparation, compiler=compiler)


def _call(name: str, arguments: dict[str, Any]) -> ToolCall:
    definition = next(item for item in knowledge_tool_definitions() if item.name == name)
    return ToolCall(
        tool_call_id=f"call-{name.replace('.', '-')}",
        run_id="run-test",
        workspace_id="ws-test",
        name=name,
        version=definition.version,
        arguments=arguments,
        args_hash=canonical_json_sha256(arguments),
        idempotency_key=f"idem-{name}",
        deadline=None,
        lineage=AgentLineage.root("run-test"),
        definition_fingerprint=definition.fingerprint,
        result_sensitivity=definition.result_sensitivity,
    )


async def test_knowledge_tools_drive_the_existing_loop_through_two_phase_ingestion(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "source.md").write_text("# Tool surface\n\nGrounded content.", encoding="utf-8")
    executor = _executor(tmp_path)
    cancellation = ManualCancellationToken()

    status = await executor.execute(_call("knowledge.status", {}), cancellation)
    status_data = thaw_json(status.data)
    assert status.status is ToolResultStatus.SUCCEEDED
    candidate_id = status_data["candidates"][0]["candidateId"]

    prepared = await executor.execute(
        _call("knowledge.prepare", {"candidateIds": [candidate_id]}),
        cancellation,
    )
    prepared_data = thaw_json(prepared.data)
    assert prepared.status is ToolResultStatus.SUCCEEDED
    source = prepared_data["sources"][0]["source"]
    nodes = prepared_data["sources"][0]["nodes"]
    summaries = [
        {
            "sourceId": source["sourceId"],
            "sourceHash": source["contentHash"],
            "nodeId": node["nodeId"],
            "summary": f"Summary of {node['title']}",
        }
        for node in nodes
    ]
    leaf = nodes[-1]
    published = await executor.execute(
        _call(
            "knowledge.publish",
            {
                "ingestionId": prepared_data["ingestionId"],
                "baseRevision": prepared_data["baseRevision"],
                "pageIndexSummaries": summaries,
                "pages": [
                    {
                        "wikiId": "summary-source",
                        "pageType": "summary",
                        "path": "summaries/source.md",
                        "title": "Source summary",
                        "aliases": [],
                        "body": "A grounded summary compiled in the current Agent Loop.",
                        "citations": [
                            {
                                "sourceId": source["sourceId"],
                                "sourceHash": source["contentHash"],
                                "nodeId": leaf["nodeId"],
                                "startPage": leaf["startPage"],
                                "endPage": leaf["endPage"],
                            }
                        ],
                        "relatedWikiIds": [],
                    }
                ],
            },
        ),
        cancellation,
    )

    assert published.status is ToolResultStatus.SUCCEEDED
    assert (tmp_path / "knowledge" / "summaries" / "source.md").exists()
    repeated = await executor.execute(_call("knowledge.status", {}), cancellation)
    assert thaw_json(repeated.data)["candidates"][0]["state"] == "unchanged"


def test_knowledge_tool_schemas_are_closed_and_effectful_calls_are_serial() -> None:
    definitions = {item.name: item for item in knowledge_tool_definitions()}
    assert definitions["knowledge.status"].input_schema["additionalProperties"] is False
    assert definitions["knowledge.prepare"].concurrency_safe is False
    assert definitions["knowledge.publish"].idempotent is False
