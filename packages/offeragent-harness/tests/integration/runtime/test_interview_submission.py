from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from collections.abc import AsyncIterator, Mapping, Sequence
from datetime import date, datetime, timezone
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from offeragent_harness.adapters.local_artifacts import LocalArtifactStore
from offeragent_harness.adapters.sqlite_stores import SqliteUnitOfWorkFactory
from offeragent_harness.config import HarnessConfig, ModelProvider
from offeragent_harness.models import (
    ModelEvent,
    ModelEventKind,
    ModelFinishReason,
    ModelPurpose,
    ModelRequest,
    ModelRole,
    ModelUsage,
    thaw_json,
)
from offeragent_harness.ports import ApplicationCommandContext, CancellationToken, StoredEvent
from offeragent_harness.protocol._base import WireModel
from offeragent_harness.protocol.messages import COMMAND_REGISTRY
from offeragent_harness.providers.codex_subscription import CodexCatalogModel, CodexRunBinding
from offeragent_harness.runtime.application_dispatcher import RuntimeApplicationCommandDispatcher
from offeragent_harness.runtime.application_domain_handlers import DomainCommandIdentity, _turn_handlers
from offeragent_harness.runtime.approval_manager import ApprovalManager
from offeragent_harness.runtime.conversation_attachments import (
    AttachmentUploadRequest,
    ConversationAttachmentStore,
)
from offeragent_harness.runtime.harness_service import CreateSessionCommand, HarnessService
from offeragent_harness.runtime.plugin_tools import (
    PluginToolCompletion,
    PluginToolExecutor,
    plugin_tool_definitions,
)
from offeragent_harness.runtime.production_worker_composition import ProductionRunComponentsFactory
from offeragent_harness.runtime.turn_manager import TurnManager
from offeragent_harness.sessions import RunStatus
from offeragent_harness.testing import (
    DeterministicIdGenerator,
    ManualCancellationToken,
    ManualClock,
    RecordingEventSink,
)
from offeragent_harness.tools import (
    SideEffect,
    SideEffectKind,
    SideEffectState,
    ToolResult,
    ToolResultStatus,
    canonical_json_sha256,
)

NOW = datetime(2026, 7, 18, 6, 0, tzinfo=timezone.utc)
MODEL_ID = "gpt-5.6-luna"
ACCOUNT_BINDING = "sha256:" + "0" * 64

FAKE_CANDIDATE_NAME = "林测试"
FAKE_ACCOUNT = "@offeragent-fixture"
FAKE_EMAIL = "lin.fixture@example.test"
FAKE_PHONE = "13800138000"
PAGE_ONE_TEXT = (
    "第1页: 一场未注明公司、岗位、日期和轮次的技术面试。\n"
    "问题: 请解释 Node.js 事件循环。\n"
    f"候选人: {FAKE_CANDIDATE_NAME}  账号: {FAKE_ACCOUNT}\n"
    f"邮箱: {FAKE_EMAIL}  电话: {FAKE_PHONE}"
)
PAGE_TWO_TEXT = "第2页: 追问微任务与计时器的执行顺序。\n公司、岗位、日期、轮次均未提供。"
UNREADABLE_PAGE_TWO_TEXT = "第2页: 模拟模糊, 语义不可辨认。"

RAW_SOURCE_URL = "https://example.com/interview/42?utm_source=feed&token=temporary#reply"
CANONICAL_SOURCE_URL = "https://example.com/interview/42"
EXPERIENCE_INDEX_PATH = "experiences/index.md"
QUESTION_INDEX_PATH = "interview/index.md"
EXPERIENCE_INDEX_VERSION = "mtime:100:size:64"
QUESTION_INDEX_VERSION = "mtime:101:size:72"
EXPERIENCE_INDEX_HASH = "sha256:" + "a" * 64
QUESTION_INDEX_HASH = "sha256:" + "b" * 64


def _digest(value: bytes) -> str:
    return f"sha256:{hashlib.sha256(value).hexdigest()}"


def _cjk_font(size: int) -> ImageFont.FreeTypeFont:
    candidates = (
        Path("C:/Windows/Fonts/msyh.ttc"),
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
        Path("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc"),
        Path("/System/Library/Fonts/PingFang.ttc"),
    )
    for candidate in candidates:
        if candidate.is_file():
            return ImageFont.truetype(str(candidate), size=size)
    raise RuntimeError("the Windows-local integration fixture requires an installed CJK font")


def _visible_chinese_page(
    text: str,
    accent: tuple[int, int, int],
    *,
    blur_body: bool = False,
) -> bytes:
    """Render screenshot-like Chinese material into pixels without text metadata."""

    image = Image.new("RGB", (960, 320), (248, 249, 251))
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, 960, 58), fill=accent)
    draw.text((32, 12), "OfferAgent 面经截图", font=_cjk_font(28), fill=(255, 255, 255))
    body_font = _cjk_font(24)
    for index, line in enumerate(text.splitlines()):
        draw.text((42, 82 + index * 51), line, font=body_font, fill=(24, 29, 36))
    if blur_body:
        body = image.crop((32, 70, 928, 300)).filter(ImageFilter.GaussianBlur(radius=9))
        image.paste(body, (32, 70))
    output = BytesIO()
    image.save(output, format="PNG", compress_level=9)
    return output.getvalue()


def _ordered_source_fingerprint(*content_hashes: str) -> str:
    digest = hashlib.sha256()
    for order, content_hash in enumerate(content_hashes):
        digest.update(f"{order}\0{content_hash}\n".encode())
    return f"sha256:{digest.hexdigest()}"


PAGE_ONE = _visible_chinese_page(PAGE_ONE_TEXT, (32, 96, 160))
PAGE_TWO = _visible_chinese_page(PAGE_TWO_TEXT, (192, 112, 32))
UNREADABLE_PAGE_TWO = _visible_chinese_page(
    UNREADABLE_PAGE_TWO_TEXT,
    (96, 96, 96),
    blur_body=True,
)
PAGE_ONE_HASH = _digest(PAGE_ONE)
PAGE_TWO_HASH = _digest(PAGE_TWO)
UNREADABLE_PAGE_TWO_HASH = _digest(UNREADABLE_PAGE_TWO)
SOURCE_FINGERPRINT = _ordered_source_fingerprint(PAGE_ONE_HASH, PAGE_TWO_HASH)
UNREADABLE_SOURCE_FINGERPRINT = _ordered_source_fingerprint(PAGE_ONE_HASH, UNREADABLE_PAGE_TWO_HASH)


def _tool_call(name: str, arguments: Mapping[str, object], reason: str) -> dict[str, object]:
    return {
        "name": name,
        "version": "1",
        "arguments": dict(arguments),
        "reason": reason,
    }


def _agent_step(
    *calls: Mapping[str, object],
    final_response: str | None = None,
    requires_write_outcome: bool = False,
) -> dict[str, object]:
    return {
        "requiresWriteOutcome": requires_write_outcome,
        "calls": [dict(call) for call in calls],
        "finalResponse": final_response,
    }


def _experience_content() -> str:
    return (
        "---\n"
        "type: interview-experience\n"
        "experience-id: exp_unknown_technical_20260718\n"
        "source-kind: mixed\n"
        "captured-on: 2026-07-18\n"
        f"source-url: {CANONICAL_SOURCE_URL}\n"
        f"source-fingerprint: {SOURCE_FINGERPRINT}\n"
        "company: unknown\n"
        "role: unknown\n"
        "event-date: unknown\n"
        "round: unknown\n"
        "---\n"
        "# 匿名技术面试经历\n\n"
        "## Questions\n"
        "- [[../interview/node-event-loop-scheduling]]\n"
    )


def _question_content() -> str:
    return (
        "---\n"
        "type: interview-question\n"
        "question-id: question_node_event_loop_scheduling\n"
        "title: Explain Node.js event-loop scheduling\n"
        "answer-state: needs-research\n"
        "frequency: 1\n"
        "---\n"
        "# Explain Node.js event-loop scheduling\n\n"
        "## Occurrences\n"
        "- [[../experiences/unknown-technical-20260718]] · unknown\n\n"
        "## Source Points\n"
        "- Unverified source point: 来源材料提到微任务与计时器的执行顺序。\n"
    )


def _interview_batch() -> dict[str, object]:
    return {
        "batchId": "interview_submission_20260718_42",
        "task": "Atomically ingest one ordered multi-image Interview Submission",
        "changeKind": "interview_submission",
        "sourceBindings": [
            {
                "path": EXPERIENCE_INDEX_PATH,
                "expectedModifiedVersion": EXPERIENCE_INDEX_VERSION,
                "expectedContentHash": EXPERIENCE_INDEX_HASH,
            },
            {
                "path": QUESTION_INDEX_PATH,
                "expectedModifiedVersion": QUESTION_INDEX_VERSION,
                "expectedContentHash": QUESTION_INDEX_HASH,
            },
        ],
        "interviewSubmission": {
            "sourceKind": "mixed",
            "capturedOn": "2026-07-18",
            "canonicalUrls": [CANONICAL_SOURCE_URL],
            "orderedImageContentHashes": [PAGE_ONE_HASH, PAGE_TWO_HASH],
            "sourceFingerprint": SOURCE_FINGERPRINT,
        },
        "operations": [
            {
                "op": "create",
                "path": "experiences/unknown-technical-20260718.md",
                "content": _experience_content(),
                "expectedContentHash": "absent",
                "expectedModifiedVersion": "missing",
            },
            {
                "op": "create",
                "path": "interview/node-event-loop-scheduling.md",
                "content": _question_content(),
                "expectedContentHash": "absent",
                "expectedModifiedVersion": "missing",
            },
            {
                "op": "append",
                "path": EXPERIENCE_INDEX_PATH,
                "content": "\n- [[unknown-technical-20260718]]\n",
                "expectedContentHash": EXPERIENCE_INDEX_HASH,
                "expectedModifiedVersion": EXPERIENCE_INDEX_VERSION,
            },
            {
                "op": "append",
                "path": QUESTION_INDEX_PATH,
                "content": "\n- [[node-event-loop-scheduling]]\n",
                "expectedContentHash": QUESTION_INDEX_HASH,
                "expectedModifiedVersion": QUESTION_INDEX_VERSION,
            },
        ],
    }


def _successful_script() -> tuple[dict[str, object], ...]:
    return (
        _agent_step(
            _tool_call(
                "agent_contract.read",
                {},
                "Read the Vault Agent Contract before taking action.",
            )
        ),
        _agent_step(
            _tool_call(
                "planning_memory.list",
                {},
                "Check bounded Planning Memory metadata before planning the ingestion.",
            )
        ),
        _agent_step(
            _tool_call(
                "interview_catalog.search",
                {
                    "sourceUrls": [RAW_SOURCE_URL],
                    "orderedImageContentHashes": [PAGE_ONE_HASH, PAGE_TWO_HASH],
                    "company": "unknown",
                    "role": "unknown",
                    "questionTerms": ["Node.js event loop", "microtasks and timers"],
                },
                "Normalize the public source and locate duplicate Experience or Question candidates.",
            )
        ),
        _agent_step(
            _tool_call(
                "vault.read",
                {
                    "path": EXPERIENCE_INDEX_PATH,
                    "expectedModifiedVersion": EXPERIENCE_INDEX_VERSION,
                    "expectedContentHash": EXPERIENCE_INDEX_HASH,
                },
                "Read the exact primary Experience index version before proposing its update.",
            ),
            _tool_call(
                "vault.read",
                {
                    "path": QUESTION_INDEX_PATH,
                    "expectedModifiedVersion": QUESTION_INDEX_VERSION,
                    "expectedContentHash": QUESTION_INDEX_HASH,
                },
                "Read the exact primary Question index version before proposing its update.",
            ),
        ),
        _agent_step(
            _tool_call(
                "vault.changes.apply",
                _interview_batch(),
                "Preview, explicitly confirm, and atomically apply the bound Interview Submission batch.",
            ),
            requires_write_outcome=True,
        ),
        _agent_step(final_response="已确认并原子入库一份面经；变更批次已应用。"),  # noqa: RUF001
    )


def _semantic_unreadable_script() -> tuple[dict[str, object], ...]:
    return (
        _agent_step(
            _tool_call(
                "agent_contract.read",
                {},
                "Read the Vault Agent Contract before taking action.",
            )
        ),
        _agent_step(
            _tool_call(
                "planning_memory.list",
                {},
                "Check bounded Planning Memory metadata before deciding whether ingestion is possible.",
            )
        ),
        _agent_step(final_response="第 2 页语义不可辨认；整份提交未入库，未产生任何变更。"),  # noqa: RUF001
    )


def _attachment_authority_mismatch_script() -> tuple[dict[str, object], ...]:
    return (
        _agent_step(
            _tool_call(
                "agent_contract.read",
                {},
                "Read the Vault Agent Contract before taking action.",
            )
        ),
        _agent_step(
            _tool_call(
                "planning_memory.list",
                {},
                "Check bounded Planning Memory metadata before planning the ingestion.",
            )
        ),
        _agent_step(
            _tool_call(
                "interview_catalog.search",
                {
                    "sourceUrls": [RAW_SOURCE_URL],
                    "orderedImageContentHashes": [PAGE_ONE_HASH],
                    "company": "unknown",
                    "role": "unknown",
                    "questionTerms": ["Node.js event loop"],
                },
                "Attempt to omit the second Store-authoritative page.",
            )
        ),
        _agent_step(final_response="附件权威校验拒绝了不完整的图片清单；未产生任何变更。"),  # noqa: RUF001
    )


class _DeterministicScriptedGateway:
    """A request-recording Model boundary with an exact AgentStep script."""

    def __init__(self, script: Sequence[Mapping[str, object]], expected_tool_result_counts: Sequence[int]) -> None:
        self._script = tuple(dict(step) for step in script)
        self._expected_tool_result_counts = tuple(expected_tool_result_counts)
        self.requests: list[ModelRequest] = []

    async def stream(
        self,
        request: ModelRequest,
        cancellation: CancellationToken,
    ) -> AsyncIterator[ModelEvent]:
        cancellation.checkpoint()
        index = len(self.requests)
        if index >= len(self._script):
            raise AssertionError("the model received an unexpected request after script exhaustion")
        assert request.purpose is ModelPurpose.PLANNING
        assert request.output_schema is not None
        tool_results = [message for message in request.messages if message.role is ModelRole.TOOL]
        assert len(tool_results) == self._expected_tool_result_counts[index]
        self.requests.append(request)
        yield ModelEvent(request.request_id, 1, ModelEventKind.STARTED)
        yield ModelEvent(
            request.request_id,
            2,
            ModelEventKind.STRUCTURED_OUTPUT,
            data=cast(Any, self._script[index]),
        )
        yield ModelEvent(
            request.request_id,
            3,
            ModelEventKind.USAGE,
            usage=ModelUsage(32, 16, 0, 0),
        )
        yield ModelEvent(
            request.request_id,
            4,
            ModelEventKind.COMPLETED,
            finish_reason=ModelFinishReason.STOP,
        )

    def assert_exhausted(self) -> None:
        assert len(self.requests) == len(self._script)


class _CodexModels:
    def __init__(self) -> None:
        self.bind_calls: list[tuple[str, str]] = []

    def bind_for_run(self, model_id: str, account_binding: str) -> CodexRunBinding:
        self.bind_calls.append((model_id, account_binding))
        return CodexRunBinding(
            model=CodexCatalogModel(
                model_id=MODEL_ID,
                display_name="Deterministic interview vision model",
                description=None,
                input_modalities=("text", "image"),
                supports_image_detail_original=True,
                supports_hosted_search=False,
                web_search_tool_type=None,
                context_window=128_000,
                max_context_window=128_000,
                effective_context_window_percent=95,
                additional_speed_tiers=(),
                service_tiers=(),
                default_service_tier=None,
            ),
            catalog_revision="sha256:" + "c" * 64,
            bound_at=NOW,
            account_binding=ACCOUNT_BINDING,
        )


class _PolicyAudit:
    async def record(self, record: object) -> None:
        del record


class _FakePluginToolAdapter(RecordingEventSink):
    """Complete real PluginToolExecutor calls only after their durable started Event is published."""

    def __init__(self, executor: PluginToolExecutor) -> None:
        super().__init__()
        self._executor = executor
        self._handled_event_ids: set[str] = set()
        self._completion_tasks: list[asyncio.Task[object]] = []
        self.started_calls: list[dict[str, Any]] = []
        self.executed_calls: list[dict[str, Any]] = []
        self.catalog_result: dict[str, object] | None = None

    async def publish(self, events: Sequence[StoredEvent]) -> None:
        await super().publish(events)
        for event in events:
            if event.event_type != "tool.started" or event.event_id in self._handled_event_ids:
                continue
            self._handled_event_ids.add(event.event_id)
            domain_payload = cast(dict[str, Any], thaw_json(event.payload["payload"]))
            call = cast(dict[str, Any], domain_payload["call"])
            self.started_calls.append(call)
            self._completion_tasks.append(asyncio.create_task(self._complete(call)))

    async def _complete(self, call: Mapping[str, Any]) -> None:
        self.executed_calls.append(dict(call))
        result = self._result_for(call)
        await self._executor.complete(
            PluginToolCompletion(
                workspace_id=cast(str, call["workspaceId"]),
                run_id=cast(str, call["runId"]),
                tool_call_id=cast(str, call["toolCallId"]),
                definition_fingerprint=cast(str, call["definitionFingerprint"]),
                args_hash=cast(str, call["argsHash"]),
                idempotency_key=cast(str, call["idempotencyKey"]),
                result=result,
            )
        )

    def _result_for(self, call: Mapping[str, Any]) -> ToolResult:
        name = cast(str, call["name"])
        arguments = cast(dict[str, Any], call["arguments"])
        side_effects: tuple[SideEffect, ...] = ()
        if name == "agent_contract.read":
            data: object = {
                "path": "agent.md",
                "content": "# OfferAgent\n\nInterview Submission is atomic and excludes unrelated PII.",
                "contentHash": "sha256:" + "1" * 64,
            }
        elif name == "planning_memory.list":
            data = {"topics": [], "truncated": False}
        elif name == "interview_catalog.search":
            data = {
                "normalizedSource": {
                    "canonicalUrls": [CANONICAL_SOURCE_URL],
                    "sourceFingerprint": SOURCE_FINGERPRINT,
                    "orderedImageContentHashes": [PAGE_ONE_HASH, PAGE_TWO_HASH],
                },
                "experienceCandidates": [],
                "questionCandidates": [],
                "indexes": [
                    {
                        "kind": "experience",
                        "path": EXPERIENCE_INDEX_PATH,
                        "exists": True,
                        "modifiedVersion": EXPERIENCE_INDEX_VERSION,
                        "contentHash": EXPERIENCE_INDEX_HASH,
                    },
                    {
                        "kind": "question",
                        "path": QUESTION_INDEX_PATH,
                        "exists": True,
                        "modifiedVersion": QUESTION_INDEX_VERSION,
                        "contentHash": QUESTION_INDEX_HASH,
                    },
                ],
                "truncated": False,
            }
            self.catalog_result = data
        elif name == "vault.read":
            path = cast(str, arguments["path"])
            if path == EXPERIENCE_INDEX_PATH:
                version = EXPERIENCE_INDEX_VERSION
                content_hash = EXPERIENCE_INDEX_HASH
                content = "# Interview Experiences\n"
            else:
                version = QUESTION_INDEX_VERSION
                content_hash = QUESTION_INDEX_HASH
                content = "# Interview Questions\n"
            data = {
                "path": path,
                "lineStart": 1,
                "lineEnd": 1,
                "modifiedVersion": version,
                "contentHash": content_hash,
                "content": content,
                "truncated": False,
            }
        else:
            operations = cast(list[dict[str, Any]], arguments["operations"])
            paths = [cast(str, operation["path"]) for operation in operations]
            data = {
                "batchId": arguments["batchId"],
                "state": "applied",
                "checkpointRef": "refs/offeragent/checkpoints/interview-submission-20260718-42",
                "paths": paths,
                "beforeStateHash": "sha256:" + "d" * 64,
                "afterStateHash": "sha256:" + "e" * 64,
                "undoAvailable": True,
            }
            side_effects = tuple(
                SideEffect(
                    kind=SideEffectKind.FILE_WRITE,
                    state=SideEffectState.COMMITTED,
                    resource_id=path,
                    before_state=None,
                    after_state={"contentHash": "sha256:" + "f" * 64},
                    metadata={"confirmedBy": "fake-plugin-preview"},
                )
                for path in paths
            )
        return ToolResult(
            tool_call_id=cast(str, call["toolCallId"]),
            status=ToolResultStatus.SUCCEEDED,
            data=cast(Any, data),
            user_visible_summary=f"Plugin completed {name}.",
            artifact_ids=(),
            source_refs=(),
            side_effects=side_effects,
            retryable=False,
            before_state=None,
            after_state=None,
            error=None,
        )

    async def join(self) -> None:
        if self._completion_tasks:
            await asyncio.gather(*self._completion_tasks)


def _trusted_config() -> HarnessConfig:
    base = HarnessConfig()
    return base.model_copy(
        update={
            "model": base.model.model_copy(
                update={
                    "provider": ModelProvider.CODEX_SUBSCRIPTION_EXPERIMENTAL,
                    "model": MODEL_ID,
                    "account_binding": ACCOUNT_BINDING,
                }
            ),
            "policy": base.policy.model_copy(
                update={
                    "read_only": False,
                    "workspace_trusted": True,
                    "approve_vault_writes": False,
                }
            ),
        }
    )


class _ReadyApplication:
    def require_ready(self) -> None:
        return None


class _StaticConfig:
    def __init__(self, config: HarnessConfig) -> None:
        self._config = config

    async def snapshot(self, **keys: str) -> object:
        del keys
        return SimpleNamespace(
            config=self._config,
            fingerprint=canonical_json_sha256(self._config.model_dump(mode="json")),
        )


class _TransportPolicy:
    async def resolve_run_route(self, context: object, permission_mode: object) -> object:
        del context
        return SimpleNamespace(permission_mode=permission_mode)


async def _unreachable_command(
    raw: WireModel,
    cancellation: CancellationToken,
    context: ApplicationCommandContext,
) -> WireModel:
    del raw, cancellation, context
    raise AssertionError("the Interview Submission scenario dispatched an unrelated command")


def _runtime(
    tmp_path: Path,
    gateway: _DeterministicScriptedGateway,
) -> tuple[
    HarnessService,
    RuntimeApplicationCommandDispatcher,
    ConversationAttachmentStore,
    _FakePluginToolAdapter,
    Path,
    _CodexModels,
]:
    state_path = tmp_path / "runtime.sqlite"
    uow = SqliteUnitOfWorkFactory(state_path)
    clock = ManualClock(NOW)
    attachments = ConversationAttachmentStore(
        tmp_path / "attachments",
        workspace_id="ws_interview",
        clock=clock,
        ids=DeterministicIdGenerator(start=20_000),
    )
    approvals = ApprovalManager(unit_of_work=uow, clock=clock)
    executor = PluginToolExecutor()
    adapter = _FakePluginToolAdapter(executor)
    definitions = plugin_tool_definitions()
    config = _trusted_config()
    codex_models = _CodexModels()
    components = ProductionRunComponentsFactory(
        workspace_id="ws_interview",
        clock=clock,
        current_local_date=lambda: date(2026, 7, 18),
        ids=DeterministicIdGenerator(start=10_000),
        gateway_factory=lambda settings: gateway,
        codex_models=codex_models,  # type: ignore[arg-type]
        default_config=config,
        approvals=approvals,
        policy_audit=_PolicyAudit(),
        journal=uow.invocation_journal,
        artifacts=LocalArtifactStore(tmp_path / "artifacts", workspace_id="ws_interview"),
        attachments=attachments,
        optional_definitions=definitions,
        plugin_executor=executor,
        local_transaction=None,
        parent_authorities=object(),  # type: ignore[arg-type]
    )
    harness = HarnessService(
        unit_of_work=uow,
        event_sink=adapter,
        clock=clock,
        ids=DeterministicIdGenerator(start=1_000),
        components=components,
        async_components=components,
        turn_manager=TurnManager(),
        approval_manager=approvals,
        required_root_initial_tool="agent_contract.read",
    )
    handlers: dict[str, Any] = {method: _unreachable_command for method in COMMAND_REGISTRY}
    handlers.update(
        _turn_handlers(
            identity=DomainCommandIdentity("ws_interview", "profile_interview", "managed_interview", "actor_interview"),
            harness=harness,
            projections=SimpleNamespace(),
            config=_StaticConfig(config),  # type: ignore[arg-type]
            transport_policy=_TransportPolicy(),  # type: ignore[arg-type]
            attachments=attachments,
        )
    )
    dispatcher = RuntimeApplicationCommandDispatcher(application=_ReadyApplication(), handlers=handlers)
    return harness, dispatcher, attachments, adapter, state_path, codex_models


async def _upload(
    store: ConversationAttachmentStore,
    token: ManualCancellationToken,
    *,
    session_id: str,
    index: int,
    payload: bytes,
) -> Any:
    begun = await store.begin(
        AttachmentUploadRequest(
            session_id=session_id,
            client_request_id=f"req_interview_page_{index}",
            file_name=f"synthetic-chinese-interview-page-{index}.png",
            media_type="image/png",
            byte_length=len(payload),
            content_hash=_digest(payload),
        ),
        token,
    )
    await store.append(begun.upload_id, 0, payload, token)
    return (await store.commit(begun.upload_id, token)).artifact


async def _start_submission(
    harness: HarnessService,
    dispatcher: RuntimeApplicationCommandDispatcher,
    attachments: ConversationAttachmentStore,
    pages: Sequence[bytes],
    *,
    turn_id: str,
) -> str:
    created = await harness.create_session(
        CreateSessionCommand(
            "ws_interview",
            "profile_interview",
            "Interview submission integration",
            f"create-{turn_id}",
        )
    )
    token = ManualCancellationToken()
    artifacts = [
        await _upload(attachments, token, session_id=created.session_id, index=index, payload=page)
        for index, page in enumerate(pages, start=1)
    ]
    result = cast(
        dict[str, Any],
        await dispatcher.dispatch(
            "turn/start",
            {
                "sessionId": created.session_id,
                "turnId": turn_id,
                "idempotencyKey": f"submit-{turn_id}",
                "input": [
                    {
                        "type": "text",
                        "text": f"请将这两页面经与来源 {RAW_SOURCE_URL} 作为一个提交原子入库。",
                        "format": "markdown",
                        "references": [],
                    },
                    *(
                        {
                            "type": "image",
                            "artifact": artifact.to_wire(),
                            "altText": f"synthetic interview page {index}",
                        }
                        for index, artifact in enumerate(artifacts, start=1)
                    ),
                ],
                "runConfig": {
                    "provider": ModelProvider.CODEX_SUBSCRIPTION_EXPERIMENTAL.value,
                    "model": MODEL_ID,
                    "reasoningEffort": "medium",
                    "permissionMode": "trusted-workspace",
                },
            },
            token,
            context=ApplicationCommandContext(transport="stdio", client_id="obsidian-interview-test"),
        ),
    )
    return cast(str, result["runId"])


async def _wait_terminal(harness: HarnessService, run_id: str) -> RunStatus:
    for _ in range(10_000):
        status = (await harness.get_run(run_id)).status
        if status in {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED, RunStatus.INTERRUPTED}:
            return status
        await asyncio.sleep(0)
    raise AssertionError("Agent Run did not reach a terminal state")


def _assert_user_owned_ordered_images(
    request: ModelRequest,
    pages: Sequence[bytes],
    hashes: Sequence[str],
    source_fingerprint: str,
) -> None:
    image_messages = [
        message for message in request.messages if any(block.kind == "image" for block in message.content)
    ]
    assert len(image_messages) == 1
    message = image_messages[0]
    assert message.role is ModelRole.USER
    image_blocks = [block for block in message.content if block.kind == "image"]
    assert [block.binary_data for block in image_blocks] == list(pages)
    assert [block.data["contentHash"] for block in image_blocks] == list(hashes)
    context = next(block for block in message.content if block.kind == "context")
    metadata = json.loads(cast(str, context.data["text"]))
    manifest = next(item for item in metadata if item.get("type") == "runtimeOrderedImageSource")
    assert manifest == {
        "type": "runtimeOrderedImageSource",
        "capturedOn": "2026-07-18",
        "imageCount": 2,
        "orderedImageContentHashes": list(hashes),
        "sourceFingerprint": source_fingerprint,
    }
    serialized = json.dumps(manifest, ensure_ascii=False, sort_keys=True)
    assert "data:image" not in serialized
    assert "iVBOR" not in serialized
    assert all(block.binary_data is None for block in message.content if block.kind != "image")


@pytest.mark.asyncio
async def test_public_turn_atomically_ingests_one_ordered_multi_image_interview_submission(
    tmp_path: Path,
) -> None:
    with Image.open(BytesIO(PAGE_ONE)) as page_one:
        assert page_one.size == (960, 320)
        assert cast(float, page_one.convert("L").getextrema()[0]) < 64
        assert "Description" not in page_one.info
    with Image.open(BytesIO(PAGE_TWO)) as page_two:
        assert page_two.size == (960, 320)
        assert cast(float, page_two.convert("L").getextrema()[0]) < 64
        assert "Description" not in page_two.info
    assert _digest(PAGE_ONE) == PAGE_ONE_HASH
    assert _digest(PAGE_TWO) == PAGE_TWO_HASH
    gateway = _DeterministicScriptedGateway(_successful_script(), (0, 1, 2, 3, 5, 6))
    harness, dispatcher, attachments, adapter, state_path, codex_models = _runtime(tmp_path, gateway)
    try:
        run_id = await _start_submission(
            harness,
            dispatcher,
            attachments,
            (PAGE_ONE, PAGE_TWO),
            turn_id="turn_interview_success",
        )
        status = await _wait_terminal(harness, run_id)
        await adapter.join()
        assert status is RunStatus.COMPLETED
        state = await harness.get_run_state(run_id)
    finally:
        await harness.shutdown()

    gateway.assert_exhausted()
    capability_record = await SqliteUnitOfWorkFactory(state_path).get_entity(
        "run_capability_snapshots",
        run_id,
    )
    assert isinstance(capability_record, Mapping)
    capability_snapshot = cast(dict[str, Any], capability_record["snapshot"])
    assert capability_snapshot["interviewSubmissionAuthority"] == {
        "schemaVersion": 1,
        "capturedOn": "2026-07-18",
        "orderedImageContentHashes": [PAGE_ONE_HASH, PAGE_TWO_HASH],
        "sourceFingerprint": SOURCE_FINGERPRINT,
    }
    _assert_user_owned_ordered_images(
        gateway.requests[0],
        (PAGE_ONE, PAGE_TWO),
        (PAGE_ONE_HASH, PAGE_TWO_HASH),
        SOURCE_FINGERPRINT,
    )
    assert codex_models.bind_calls == [(MODEL_ID, ACCOUNT_BINDING)]

    started_names = [cast(str, call["name"]) for call in adapter.started_calls]
    assert started_names == [
        "agent_contract.read",
        "planning_memory.list",
        "interview_catalog.search",
        "vault.read",
        "vault.read",
        "vault.changes.apply",
    ]
    catalog_call = adapter.started_calls[2]
    assert catalog_call["arguments"] == {
        "sourceUrls": [RAW_SOURCE_URL],
        "orderedImageContentHashes": [PAGE_ONE_HASH, PAGE_TWO_HASH],
        "company": "unknown",
        "role": "unknown",
        "questionTerms": ["Node.js event loop", "microtasks and timers"],
    }
    assert sorted(
        (cast(dict[str, Any], call["arguments"]) for call in adapter.started_calls[3:5]),
        key=lambda arguments: cast(str, arguments["path"]),
    ) == [
        {
            "path": EXPERIENCE_INDEX_PATH,
            "expectedModifiedVersion": EXPERIENCE_INDEX_VERSION,
            "expectedContentHash": EXPERIENCE_INDEX_HASH,
        },
        {
            "path": QUESTION_INDEX_PATH,
            "expectedModifiedVersion": QUESTION_INDEX_VERSION,
            "expectedContentHash": QUESTION_INDEX_HASH,
        },
    ]

    apply_call = adapter.started_calls[-1]
    batch = cast(dict[str, Any], apply_call["arguments"])
    operations = cast(list[dict[str, Any]], batch["operations"])
    normalized_source = cast(dict[str, Any], cast(dict[str, Any], adapter.catalog_result)["normalizedSource"])
    assert batch["changeKind"] == "interview_submission"
    assert batch["interviewSubmission"]["canonicalUrls"] == normalized_source["canonicalUrls"]
    assert batch["interviewSubmission"]["sourceFingerprint"] == normalized_source["sourceFingerprint"]
    assert batch["interviewSubmission"]["orderedImageContentHashes"] == normalized_source["orderedImageContentHashes"]
    assert batch["sourceBindings"] == [
        {
            "path": EXPERIENCE_INDEX_PATH,
            "expectedModifiedVersion": EXPERIENCE_INDEX_VERSION,
            "expectedContentHash": EXPERIENCE_INDEX_HASH,
        },
        {
            "path": QUESTION_INDEX_PATH,
            "expectedModifiedVersion": QUESTION_INDEX_VERSION,
            "expectedContentHash": QUESTION_INDEX_HASH,
        },
    ]
    assert all("expectedModifiedVersion" in operation for operation in operations)
    experience_operations = [
        operation for operation in operations if "type: interview-experience" in cast(str, operation.get("content", ""))
    ]
    assert len(experience_operations) == 1
    experience = cast(str, experience_operations[0]["content"])
    assert "company: unknown" in experience
    assert "role: unknown" in experience
    assert "event-date: unknown" in experience
    assert "round: unknown" in experience
    question = next(
        cast(str, operation["content"])
        for operation in operations
        if "type: interview-question" in cast(str, operation.get("content", ""))
    )
    assert "answer-state: needs-research" in question
    assert "Unverified source point:" in question
    assert "standard answer" not in question.casefold()
    assert "标准答案" not in question

    serialized_batch = json.dumps(batch, ensure_ascii=False, sort_keys=True)
    assert RAW_SOURCE_URL not in serialized_batch
    assert "utm_source" not in serialized_batch
    assert "token=temporary" not in serialized_batch
    assert "data:image" not in serialized_batch
    assert base64.b64encode(PAGE_ONE).decode("ascii") not in serialized_batch
    assert base64.b64encode(PAGE_TWO).decode("ascii") not in serialized_batch
    assert FAKE_CANDIDATE_NAME not in serialized_batch
    assert FAKE_ACCOUNT not in serialized_batch
    assert FAKE_EMAIL not in serialized_batch
    assert FAKE_PHONE not in serialized_batch
    assert not any(
        marker in serialized_batch.casefold()
        for marker in ("candidate:", "candidate-name", "account:", "phone:", "email:")
    )
    assert sum(call["name"] == "vault.changes.apply" for call in adapter.started_calls) == 1
    assert state.assistant_text == "已确认并原子入库一份面经；变更批次已应用。"  # noqa: RUF001
    assert not any(event.event_type == "approval.required" for event in adapter.events)

    durable_state = state_path.read_bytes()
    assert PAGE_ONE not in durable_state
    assert PAGE_TWO not in durable_state
    assert base64.b64encode(PAGE_ONE) not in durable_state
    assert base64.b64encode(PAGE_TWO) not in durable_state


@pytest.mark.asyncio
async def test_public_turn_rejects_semantically_unreadable_second_page_without_partial_change(
    tmp_path: Path,
) -> None:
    assert _digest(UNREADABLE_PAGE_TWO) == UNREADABLE_PAGE_TWO_HASH
    gateway = _DeterministicScriptedGateway(_semantic_unreadable_script(), (0, 1, 2))
    harness, dispatcher, attachments, adapter, _, _ = _runtime(tmp_path, gateway)
    try:
        run_id = await _start_submission(
            harness,
            dispatcher,
            attachments,
            (PAGE_ONE, UNREADABLE_PAGE_TWO),
            turn_id="turn_interview_unreadable",
        )
        status = await _wait_terminal(harness, run_id)
        await adapter.join()
        assert status is RunStatus.COMPLETED
        state = await harness.get_run_state(run_id)
    finally:
        await harness.shutdown()

    gateway.assert_exhausted()
    _assert_user_owned_ordered_images(
        gateway.requests[0],
        (PAGE_ONE, UNREADABLE_PAGE_TWO),
        (PAGE_ONE_HASH, UNREADABLE_PAGE_TWO_HASH),
        UNREADABLE_SOURCE_FINGERPRINT,
    )
    started_names = [cast(str, call["name"]) for call in adapter.started_calls]
    assert started_names == ["agent_contract.read", "planning_memory.list"]
    assert "第 2 页" in state.assistant_text
    assert "未产生任何变更" in state.assistant_text
    assert "interview_catalog.search" not in started_names
    assert "vault.changes.apply" not in started_names


@pytest.mark.asyncio
async def test_public_turn_rejects_model_dropped_attachment_before_plugin_tool_started(
    tmp_path: Path,
) -> None:
    gateway = _DeterministicScriptedGateway(_attachment_authority_mismatch_script(), (0, 1, 2, 3))
    harness, dispatcher, attachments, adapter, _, _ = _runtime(tmp_path, gateway)
    try:
        run_id = await _start_submission(
            harness,
            dispatcher,
            attachments,
            (PAGE_ONE, PAGE_TWO),
            turn_id="turn_interview_authority_mismatch",
        )
        status = await _wait_terminal(harness, run_id)
        await adapter.join()
        state = await harness.get_run_state(run_id)
    finally:
        await harness.shutdown()

    gateway.assert_exhausted()
    assert status is RunStatus.COMPLETED
    started_names = [cast(str, call["name"]) for call in adapter.started_calls]
    executed_names = [cast(str, call["name"]) for call in adapter.executed_calls]
    assert started_names == ["agent_contract.read", "planning_memory.list"]
    assert executed_names == ["agent_contract.read", "planning_memory.list"]
    assert "interview_catalog.search" not in started_names
    assert "interview_catalog.search" not in executed_names
    assert "vault.changes.apply" not in started_names
    assert state.assistant_text == "附件权威校验拒绝了不完整的图片清单；未产生任何变更。"  # noqa: RUF001
