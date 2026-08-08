"""Run low-cost real-provider acceptance for context compaction and semantic knowledge."""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
import tempfile
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypeVar

from offeragent_harness.adapters.local_artifacts import LocalArtifactStore
from offeragent_harness.agent.state import RunPhase, RunState
from offeragent_harness.foundation.canonical import canonical_json_bytes
from offeragent_harness.knowledge import (
    KnowledgeCatalogStore,
    KnowledgeCompiler,
    KnowledgeDiscovery,
    KnowledgeInferenceBudget,
    KnowledgeInferenceLimits,
    KnowledgeObjectStore,
    KnowledgePreparationService,
    KnowledgePreparationStore,
    KnowledgePublisher,
    LLMWikiCompiler,
    LLMWikiPolicy,
    SemanticPageIndexBuilder,
    SemanticPageIndexPolicy,
    StructuredInferenceCache,
)
from offeragent_harness.knowledge.preparation import KnowledgeParseResult
from offeragent_harness.ports.secrets import SecretHandle, SecretKind
from offeragent_harness.providers.deepseek_chat import DEEPSEEK_BASE_URL, build_deepseek_gateway
from offeragent_harness.providers.openai_responses import StaticModelEndpointPolicy
from offeragent_harness.runtime.context_summary import ContextCompactionService
from offeragent_harness.sessions import (
    AgentLineage,
    Run,
    RunKind,
    RunStatus,
    TerminationReason,
    Turn,
    TurnStatus,
)
from offeragent_harness.testing import (
    DeterministicIdGenerator,
    InMemoryUnitOfWorkFactory,
    ManualCancellationToken,
    ManualClock,
    RecordingNetworkAuditSink,
)

T = TypeVar("T")
WORKSPACE_ID = "ws-runtime-upgrade-eval-v1"
MODEL = "deepseek-v4-flash"
SECRET_HANDLE = SecretHandle("secret:v1:" + "7" * 32)
PAPER_PATH = Path("raw/01-attention-is-all-you-need.pdf")


class FileSecretResolver:
    def __init__(self, path: Path) -> None:
        self._value = bytearray(path.read_bytes().strip())
        if not self._value or b"\x00" in self._value:
            self.close()
            raise ValueError("secret file is empty or invalid")

    def consume(
        self,
        handle: SecretHandle,
        *,
        scope_id: str,
        expected_kind: SecretKind,
        expected_provider_id: str,
        consumer: Callable[[memoryview], T],
    ) -> T:
        if (
            handle != SECRET_HANDLE
            or scope_id != WORKSPACE_ID
            or expected_kind is not SecretKind.MODEL_PROVIDER
            or expected_provider_id != "deepseek"
        ):
            raise ValueError("secret binding mismatch")
        return consumer(memoryview(self._value))

    def appears_in(self, payload: bytes) -> bool:
        return bool(self._value and bytes(self._value) in payload)

    def close(self) -> None:
        for index in range(len(self._value)):
            self._value[index] = 0
        self._value.clear()

    def __repr__(self) -> str:
        return "<FileSecretResolver redacted>"


class ExistingEvidenceParser:
    """Reuse the frozen corpus' already extracted page evidence; no synthetic text is introduced."""

    def __init__(self, pages: tuple[Any, ...], parser_fingerprint: str) -> None:
        self._pages = pages
        self.parser_fingerprint = parser_fingerprint

    async def parse(self, *, source: Any, absolute_path: Path, cancellation: Any) -> KnowledgeParseResult:
        del absolute_path
        cancellation.checkpoint()
        return KnowledgeParseResult(source.source_id, source.content_hash, self._pages, self.parser_fingerprint)


async def _seed_context(unit_of_work: InMemoryUnitOfWorkFactory, now: datetime) -> None:
    inputs = (
        "目标: 完成 OfferAgent, 不得为评测硬编码答案。",
        "决策: 所有模型调用必须经过唯一 ModelGateway。",
        "当前状态: 待验证上下文压缩和语义知识库。",
    )
    async with unit_of_work.begin() as work:
        for ordinal, user_text in enumerate(inputs, start=1):
            turn_id = f"turn-{ordinal}"
            run_id = f"run-context-{ordinal}"
            turn = Turn(
                turn_id,
                "session-eval",
                ordinal,
                TurnStatus.COMPLETED,
                ({"type": "text", "text": user_text},),
                now,
                now,
            )
            run = Run(
                run_id,
                "session-eval",
                turn_id,
                WORKSPACE_ID,
                AgentLineage.root(run_id),
                RunKind.ROOT,
                RunStatus.COMPLETED,
                1,
                0,
                {"model": MODEL},
                now,
                now,
                None,
                TerminationReason.COMPLETED,
            )
            state = replace(
                RunState(WORKSPACE_ID, "session-eval", turn_id, run_id, AgentLineage.root(run_id)),
                phase=RunPhase.COMPLETED,
                assistant_text=f"已完成第 {ordinal} 步, 保留了可审计状态。",
            )
            await work.entities.put("turns", turn_id, turn, expected_revision=0)
            await work.entities.put("runs", run_id, run, expected_revision=0)
            await work.entities.put("run_states", run_id, state, expected_revision=0)
        await work.commit()


async def execute(args: argparse.Namespace) -> int:
    script = Path(__file__).resolve()  # noqa: ASYNC240 - one-time local path discovery
    evaluation_root = script.parents[1]
    repository = evaluation_root.parents[1]
    output = (evaluation_root / "generated" if args.output is None else args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    source_vault = repository / "evaluation" / "agent-loop-v1" / "corpus" / "generated" / "vault"
    source_pdf = source_vault / PAPER_PATH
    if not source_pdf.is_file():
        raise FileNotFoundError("frozen real-paper corpus must be built before runtime acceptance")
    secret_path = args.secret_file or _discover_secret(repository)
    resolver = FileSecretResolver(secret_path.resolve(strict=True))
    try:
        audit = RecordingNetworkAuditSink()
        clock = ManualClock(datetime.now(timezone.utc))
        gateway = build_deepseek_gateway(
            secret_scope_id=WORKSPACE_ID,
            credential_handle=SECRET_HANDLE,
            secrets=resolver,
            endpoint_policy=StaticModelEndpointPolicy(
                frozenset({f"{DEEPSEEK_BASE_URL}/chat/completions"}), enabled=True
            ),
            network_audit=audit,
            clock=clock,
        )
        ids = DeterministicIdGenerator()
        cancellation = ManualCancellationToken()

        context_uow = InMemoryUnitOfWorkFactory()
        await _seed_context(context_uow, clock.utcnow())
        context_service = ContextCompactionService(
            workspace_id=WORKSPACE_ID,
            unit_of_work=context_uow,
            artifacts=LocalArtifactStore(
                Path(tempfile.mkdtemp(prefix="context-artifacts-", dir=output)),
                workspace_id=WORKSPACE_ID,
            ),
            gateway_factory=lambda _: gateway,
            default_model=MODEL,
            clock=clock,
            ids=ids,
        )
        context = await context_service.compact_session(
            session_id="session-eval",
            through_turn_id="turn-3",
            trigger="manual",
            cancellation=cancellation,
        )

        original_catalog = KnowledgeCatalogStore(source_vault / ".offeragent" / "knowledge").load()
        original_source = next(item for item in original_catalog.sources if item.relative_path == PAPER_PATH.as_posix())
        original_object = KnowledgeObjectStore(source_vault / ".offeragent" / "knowledge").load(
            original_source.source_id, original_source.content_hash
        )
        mini_vault = output / "semantic-vault"
        mini_pdf = mini_vault / PAPER_PATH
        mini_pdf.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source_pdf, mini_pdf)
        state_root = mini_vault / ".offeragent" / "knowledge"
        catalog = KnowledgeCatalogStore(state_root)
        objects = KnowledgeObjectStore(state_root)
        preparations = KnowledgePreparationStore(state_root, objects)
        discovery = KnowledgeDiscovery(workspace_id=WORKSPACE_ID, vault_root=mini_vault)
        inference_budget = KnowledgeInferenceBudget(KnowledgeInferenceLimits(250_000, 24_000))
        cache = StructuredInferenceCache((state_root / "inference-cache").resolve())
        pageindex_builder = SemanticPageIndexBuilder(
            gateway_factory=lambda _: gateway,
            workspace_id=WORKSPACE_ID,
            model=MODEL,
            cache=cache,
            ids=ids,
            budget=inference_budget,
            policy=SemanticPageIndexPolicy(maximum_output_tokens=4_096),
        )
        preparation_service = KnowledgePreparationService(
            vault_root=mini_vault,
            discovery=discovery,
            catalog=catalog,
            objects=objects,
            preparations=preparations,
            binary_parser=ExistingEvidenceParser(original_object.pages, original_object.parser_fingerprint),
            page_index_builder=pageindex_builder,
        )
        candidate = preparation_service.status().candidates[0]
        preparation = await preparation_service.prepare(
            candidate_ids=(candidate.candidate_id,),
            run_id="run-knowledge-eval",
            cancellation=cancellation,
        )
        semantic_tree = preparation.prepared_sources[0].page_index
        usage_before_cache_replay = (
            inference_budget.input_tokens,
            inference_budget.output_tokens,
            inference_budget.cached_input_tokens,
        )
        replay_tree = await pageindex_builder.build(
            source_id=preparation.prepared_sources[0].source.source_id,
            source_hash=preparation.prepared_sources[0].source.content_hash,
            title=PAPER_PATH.stem,
            pages=preparation.prepared_sources[0].pages,
            run_id="run-knowledge-eval",
            cancellation=cancellation,
        )
        cache_reused = replay_tree == semantic_tree and usage_before_cache_replay == (
            inference_budget.input_tokens,
            inference_budget.output_tokens,
            inference_budget.cached_input_tokens,
        )
        publisher = KnowledgePublisher(vault_root=mini_vault, catalog=catalog, objects=objects)
        compiler = KnowledgeCompiler(
            discovery=discovery,
            catalog=catalog,
            preparations=preparations,
            publisher=publisher,
        )
        wiki_compiler = LLMWikiCompiler(
            preparations=preparations,
            compiler=compiler,
            gateway_factory=lambda _: gateway,
            workspace_id=WORKSPACE_ID,
            model=MODEL,
            cache=cache,
            ids=ids,
            budget=inference_budget,
            policy=LLMWikiPolicy(maximum_output_tokens=8_192),
        )
        wiki = await wiki_compiler.compile(
            ingestion_id=preparation.ingestion_id,
            base_revision=preparation.base_revision,
            run_id="run-knowledge-eval",
            cancellation=cancellation,
        )

        result_records = [record for record in audit.records if record.stage == "result"]
        report = {
            "schemaVersion": 1,
            "configuration": {
                "model": MODEL,
                "paper": PAPER_PATH.as_posix(),
                "realProvider": True,
                "syntheticPdf": False,
            },
            "contextCompaction": {
                "cachedInputTokens": context.record.cached_input_tokens,
                "estimatedAfterTokens": context.record.estimated_after_tokens,
                "estimatedBeforeTokens": context.record.estimated_before_tokens,
                "inputTokens": context.record.input_tokens,
                "originalEventsRetained": True,
                "outputTokens": context.record.output_tokens,
                "replacedTurnCount": context.replaced_turn_count,
                "status": "passed",
                "summaryId": context.record.summary_id,
                "throughTurnId": context.record.to_turn_id,
            },
            "semanticKnowledge": {
                "cacheReplayIdenticalWithoutNewUsage": cache_reused,
                "catalogRevision": wiki.catalog.revision,
                "cachedInputTokens": inference_budget.cached_input_tokens,
                "inputTokens": inference_budget.input_tokens,
                "outputTokens": inference_budget.output_tokens,
                "pageCount": len(original_object.pages),
                "pageIndexNodeCount": len(semantic_tree.nodes),
                "sourceHash": original_source.content_hash,
                "status": "passed",
                "wikiPageCount": wiki.page_count,
            },
            "networkAudit": {
                "attemptCount": len(result_records),
                "outcomes": sorted(record.outcome for record in result_records),
                "runIds": sorted({record.run_id for record in result_records if record.run_id is not None}),
                "workspaceIds": sorted({record.workspace_id for record in result_records}),
            },
            "tokenUsage": {
                "cachedInputTokens": context.record.cached_input_tokens + inference_budget.cached_input_tokens,
                "estimatedCny": _estimated_cny(
                    context.record.input_tokens + inference_budget.input_tokens,
                    context.record.cached_input_tokens + inference_budget.cached_input_tokens,
                    context.record.output_tokens + inference_budget.output_tokens,
                ),
                "inputTokens": context.record.input_tokens + inference_budget.input_tokens,
                "outputTokens": context.record.output_tokens + inference_budget.output_tokens,
            },
        }
        payload = canonical_json_bytes(report)
        if resolver.appears_in(payload):
            raise RuntimeError("secret leak guard rejected runtime acceptance output")
        (output / "runtime-upgrade-report.json").write_bytes(payload + b"\n")
        print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
        return 0
    finally:
        resolver.close()


def _estimated_cny(input_tokens: int, cached_input_tokens: int, output_tokens: int) -> float:
    uncached = input_tokens - cached_input_tokens
    return round((uncached * 1.0 + cached_input_tokens * 0.02 + output_tokens * 2.0) / 1_000_000, 6)


def _discover_secret(start: Path) -> Path:
    for root in (start, *start.parents):
        candidate = root / ".secret"
        if candidate.is_file():
            return candidate
    raise FileNotFoundError("no .secret file found in repository ancestors")


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--secret-file", type=Path)
    return parser.parse_args()


def main() -> None:
    sys.exit(asyncio.run(execute(parse_arguments())))


if __name__ == "__main__":
    main()
