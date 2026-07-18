"""Explicit, test-only Codex Subscription vision qualification."""

# ruff: noqa: RUF001 -- Chinese punctuation is part of the OCR qualification material.

from __future__ import annotations

import gc
import hashlib
import json
import shutil
import subprocess
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from importlib.metadata import version
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal, Protocol

from jsonschema import Draft202012Validator
from PIL import Image, ImageDraw, ImageFont

from offeragent_harness.adapters.local_artifacts import LocalArtifactStore
from offeragent_harness.agent import BudgetCheckpoint, BudgetLedger
from offeragent_harness.agent.loop import run_agent_loop
from offeragent_harness.agent.preparation import RunPreparationFailure
from offeragent_harness.agent.state import RunPhase, RunState
from offeragent_harness.config import HarnessConfig, ModelSettings
from offeragent_harness.models import ModelEvent, ModelRequest, ModelRole
from offeragent_harness.ports import CancellationToken
from offeragent_harness.protocol.content import ArtifactRef
from offeragent_harness.providers import compose_model_gateway
from offeragent_harness.providers.codex_subscription import (
    CODEX_CATALOG_CLIENT_VERSION,
    CodexCatalogModel,
    CodexModelCatalogSnapshot,
    CodexRunBinding,
    CodexSubscriptionModelModule,
    HttpxCodexCatalogHttpAdapter,
)
from offeragent_harness.runtime import CancellationScope
from offeragent_harness.runtime.approval_manager import ApprovalManager
from offeragent_harness.runtime.codex_credentials import CodexFileCredentialSource, default_codex_auth_path
from offeragent_harness.runtime.conversation_attachments import (
    AttachmentClaim,
    AttachmentLimits,
    AttachmentUploadRequest,
    ConversationAttachmentStore,
    MaterializedClaimedAttachment,
)
from offeragent_harness.runtime.harness_service import StartTurnCommand
from offeragent_harness.runtime.production_worker_composition import ProductionRunComponentsFactory
from offeragent_harness.sessions import AgentLineage
from offeragent_harness.testing import (
    DeterministicIdGenerator,
    InMemoryUnitOfWorkFactory,
    ManualClock,
    RecordingNetworkAuditSink,
)
from offeragent_harness.tools import canonical_json_sha256

_SEMANTIC_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "company": {"type": "string", "minLength": 1, "maxLength": 256},
        "role": {"type": "string", "minLength": 1, "maxLength": 256},
        "rounds": {
            "type": "array",
            "minItems": 3,
            "maxItems": 3,
            "items": {"type": "string", "minLength": 1, "maxLength": 64},
        },
        "pageOrder": {
            "type": "array",
            "minItems": 3,
            "maxItems": 3,
            "items": {"type": "integer"},
        },
        "crossPageQuestionId": {"type": "string", "minLength": 1, "maxLength": 64},
        "crossPageQuestion": {"type": "string", "minLength": 1, "maxLength": 2_048},
        "finalPageTopic": {"type": "string", "minLength": 1, "maxLength": 2_048},
    },
    "required": [
        "company",
        "role",
        "rounds",
        "pageOrder",
        "crossPageQuestionId",
        "crossPageQuestion",
        "finalPageTopic",
    ],
    "additionalProperties": False,
}
_SEMANTIC_OUTPUT_VALIDATOR = Draft202012Validator(_SEMANTIC_OUTPUT_SCHEMA)


@dataclass(frozen=True, slots=True)
class SyntheticInterviewPage:
    index: int
    path: Path
    content_hash: str
    media_type: str
    byte_length: int


@dataclass(frozen=True, slots=True)
class SyntheticInterviewFixture:
    pages: tuple[SyntheticInterviewPage, ...]
    font_sha256: str


class SyntheticInterviewFixtureGenerator:
    """Render fixed fictional Chinese interview pages into one temporary root."""

    _PAGE_LINES = (
        (
            "OfferAgent 视觉资格素材 · 第 1 / 3 页",
            "以下公司与经历均为人工虚构",
            "公司：星河科技（虚构）",
            "岗位：后端工程师",
            "轮次：一面",
            "题目 Q1：如何处理 Redis 缓存击穿？",
            "跨页题 Q2：订单创建成功后，",
            "问题将在下一页继续。",
        ),
        (
            "OfferAgent 视觉资格素材 · 第 2 / 3 页",
            "跨页题 Q2（续）：支付回调重复到达时，",
            "如何保证幂等并避免重复扣款？",
            "轮次：二面",
            "题目 Q3：如何定位数据库慢查询？",
            "提示：本页承接上一页的 Q2。",
        ),
        (
            "OfferAgent 视觉资格素材 · 第 3 / 3 页",
            "轮次：终面",
            "题目 Q4：灰度发布失败后，",
            "如何设计快速回滚与验证方案？",
            "材料结束 · 页序为 1 → 2 → 3",
        ),
    )

    def __init__(self, *, font_path: Path) -> None:
        if not font_path.is_absolute() or not font_path.is_file():
            raise ValueError("qualification font must be an absolute regular file")
        self._font_path = font_path

    def generate(self, root: Path) -> SyntheticInterviewFixture:
        if not root.is_absolute():
            raise ValueError("qualification fixture root must be absolute")
        root.mkdir(parents=True, exist_ok=False)
        font_bytes = self._font_path.read_bytes()
        font_sha256 = f"sha256:{hashlib.sha256(font_bytes).hexdigest()}"
        font = ImageFont.truetype(str(self._font_path), 38)
        pages: list[SyntheticInterviewPage] = []
        for index, lines in enumerate(self._PAGE_LINES, start=1):
            path = root / f"page-{index}.png"
            image = Image.new("RGB", (1200, 1600), "white")
            draw = ImageDraw.Draw(image)
            draw.rounded_rectangle((55, 55, 1145, 1545), radius=18, outline="#263238", width=4)
            for line_index, line in enumerate(lines):
                draw.text((105, 110 + line_index * 145), line, fill="#111111", font=font)
            image.save(path, format="PNG", optimize=False, compress_level=9)
            payload = path.read_bytes()
            pages.append(
                SyntheticInterviewPage(
                    index=index,
                    path=path,
                    content_hash=f"sha256:{hashlib.sha256(payload).hexdigest()}",
                    media_type="image/png",
                    byte_length=len(payload),
                )
            )
        return SyntheticInterviewFixture(tuple(pages), font_sha256)


@dataclass(frozen=True, slots=True)
class TextModelGateEvidence:
    error_code: str
    attachment_materialization_count: int
    response_send_count: int
    gateway_factory_count: int


@dataclass(frozen=True, slots=True)
class ImageModelEvidence:
    semantics: Mapping[str, object] | None
    response_send_count: int
    attachment_materialization_count: int
    gateway_factory_count: int
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class QualificationTeardownEvidence:
    auth_unchanged: bool
    temporary_state_removed: bool


@dataclass(frozen=True, slots=True)
class ModelQualificationResult:
    model_id: str
    input_modalities: tuple[str, ...]
    status: Literal["qualified", "blocked_locally", "failed"]
    detail: Literal["high", "original"] | None
    response_send_count: int
    attachment_materialization_count: int
    gateway_factory_count: int
    error_code: str | None = None
    missing_facts: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class QualificationReport:
    status: Literal["passed", "failed"]
    catalog_revision: str
    catalog_freshness: str
    font_sha256: str
    page_hashes: tuple[str, ...]
    models: tuple[ModelQualificationResult, ...]
    image_models_qualified: int
    text_models_blocked: int
    auth_unchanged: bool
    temporary_state_removed: bool
    metadata: Mapping[str, str]

    def to_json(self) -> str:
        payload = {
            "status": self.status,
            "metadata": dict(self.metadata),
            "catalog": {
                "freshness": self.catalog_freshness,
                "revision": self.catalog_revision,
                "modelCount": len(self.models),
            },
            "fixture": {
                "fontSha256": self.font_sha256,
                "pageHashes": list(self.page_hashes),
            },
            "models": [
                {
                    "modelId": item.model_id,
                    "inputModalities": list(item.input_modalities),
                    "status": item.status,
                    "detail": item.detail,
                    "responseSendCount": item.response_send_count,
                    "attachmentMaterializationCount": item.attachment_materialization_count,
                    "gatewayFactoryCount": item.gateway_factory_count,
                    "errorCode": item.error_code,
                    "missingFacts": list(item.missing_facts),
                }
                for item in self.models
            ],
            "totals": {
                "imageModelsQualified": self.image_models_qualified,
                "textModelsBlocked": self.text_models_blocked,
            },
            "authUnchanged": self.auth_unchanged,
            "temporaryStateRemoved": self.temporary_state_removed,
        }
        return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)


class QualificationEnvironment(Protocol):
    def generate_fixture(self, root: Path) -> SyntheticInterviewFixture: ...

    def fetch_catalog(self) -> CodexModelCatalogSnapshot: ...

    async def execute_image_model(
        self,
        model: CodexCatalogModel,
        account_binding: str,
        fixture: SyntheticInterviewFixture,
        detail: str,
    ) -> ImageModelEvidence: ...

    async def exercise_text_model_gate(
        self,
        model: CodexCatalogModel,
        account_binding: str,
        fixture: SyntheticInterviewFixture,
    ) -> TextModelGateEvidence: ...

    def verify_teardown(self) -> QualificationTeardownEvidence: ...

    def report_metadata(self) -> Mapping[str, str]: ...


class QualificationFailure(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class _ExecutionCounts:
    response_send_count: int
    attachment_materialization_count: int
    gateway_factory_count: int

    def __post_init__(self) -> None:
        if (
            min(
                self.response_send_count,
                self.attachment_materialization_count,
                self.gateway_factory_count,
            )
            < 0
        ):
            raise ValueError("qualification execution counts cannot be negative")


@dataclass(frozen=True, slots=True)
class _ModelExecutionResult:
    semantics: Mapping[str, object] | None
    counts: _ExecutionCounts


class _ModelExecutionFailure(QualificationFailure):
    def __init__(self, error_code: str, counts: _ExecutionCounts) -> None:
        self.error_code = error_code
        self.counts = counts
        super().__init__("qualification model execution failed")


class CodexVisionQualification:
    """Qualify one frozen live catalog and return value-safe evidence."""

    def __init__(self, environment: QualificationEnvironment) -> None:
        self._environment = environment

    async def run(self, root: Path) -> QualificationReport:
        if not root.is_absolute():
            raise ValueError("qualification root must be absolute")
        try:
            catalog = self._environment.fetch_catalog()
            if (
                catalog.freshness != "fresh"
                or catalog.error is not None
                or not catalog.models
                or catalog.catalog_revision is None
                or catalog.account_binding is None
            ):
                raise QualificationFailure("live Codex catalog is not a fresh complete population")
            fixture = self._environment.generate_fixture(root)
            results: list[ModelQualificationResult] = []
            for model in catalog.models:
                if "image" not in model.input_modalities:
                    text_evidence = await self._environment.exercise_text_model_gate(
                        model,
                        catalog.account_binding,
                        fixture,
                    )
                    locally_blocked = (
                        text_evidence.error_code == "image_modality_unsupported"
                        and text_evidence.attachment_materialization_count == 0
                        and text_evidence.response_send_count == 0
                        and text_evidence.gateway_factory_count == 0
                    )
                    results.append(
                        ModelQualificationResult(
                            model_id=model.model_id,
                            input_modalities=model.input_modalities,
                            status="blocked_locally" if locally_blocked else "failed",
                            detail=None,
                            response_send_count=text_evidence.response_send_count,
                            attachment_materialization_count=text_evidence.attachment_materialization_count,
                            gateway_factory_count=text_evidence.gateway_factory_count,
                            error_code=text_evidence.error_code,
                        )
                    )
                    continue
                detail: Literal["high", "original"] = "original" if model.supports_image_detail_original else "high"
                image_evidence = await self._environment.execute_image_model(
                    model,
                    catalog.account_binding,
                    fixture,
                    detail,
                )
                missing = () if image_evidence.semantics is None else _missing_semantic_facts(image_evidence.semantics)
                qualified = (
                    image_evidence.error_code is None
                    and image_evidence.semantics is not None
                    and not missing
                    and image_evidence.response_send_count > 0
                    and image_evidence.attachment_materialization_count > 0
                    and image_evidence.gateway_factory_count > 0
                )
                results.append(
                    ModelQualificationResult(
                        model_id=model.model_id,
                        input_modalities=model.input_modalities,
                        status="qualified" if qualified else "failed",
                        detail=detail,
                        response_send_count=image_evidence.response_send_count,
                        attachment_materialization_count=image_evidence.attachment_materialization_count,
                        gateway_factory_count=image_evidence.gateway_factory_count,
                        error_code=image_evidence.error_code,
                        missing_facts=missing,
                    )
                )
        finally:
            teardown = self._environment.verify_teardown()
        image_qualified = sum(item.status == "qualified" for item in results)
        text_blocked = sum(item.status == "blocked_locally" for item in results)
        passed = (
            image_qualified > 0
            and all(item.status != "failed" for item in results)
            and teardown.auth_unchanged
            and teardown.temporary_state_removed
        )
        return QualificationReport(
            status="passed" if passed else "failed",
            catalog_revision=catalog.catalog_revision,
            catalog_freshness=catalog.freshness,
            font_sha256=fixture.font_sha256,
            page_hashes=tuple(page.content_hash for page in fixture.pages),
            models=tuple(results),
            image_models_qualified=image_qualified,
            text_models_blocked=text_blocked,
            auth_unchanged=teardown.auth_unchanged,
            temporary_state_removed=teardown.temporary_state_removed,
            metadata=MappingProxyType(dict(self._environment.report_metadata())),
        )


def _missing_semantic_facts(value: Mapping[str, object]) -> tuple[str, ...]:
    missing: list[str] = []
    if not _SEMANTIC_OUTPUT_VALIDATOR.is_valid(value):
        missing.append("semanticSchema")
    company = value.get("company")
    if not isinstance(company, str) or "星河科技" not in company:
        missing.append("company")
    role = value.get("role")
    if not isinstance(role, str) or "后端工程师" not in role:
        missing.append("role")
    rounds = value.get("rounds")
    if rounds != ["一面", "二面", "终面"]:
        missing.append("rounds")
    if value.get("pageOrder") != [1, 2, 3]:
        missing.append("pageOrder")
    question_id = value.get("crossPageQuestionId")
    if not isinstance(question_id, str) or question_id.strip().upper() != "Q2":
        missing.append("crossPageQuestionId")
    question = value.get("crossPageQuestion")
    if not isinstance(question, str) or any(
        token not in question for token in ("订单", "支付回调", "幂等", "重复扣款")
    ):
        missing.append("crossPageQuestion")
    final_topic = value.get("finalPageTopic")
    if not isinstance(final_topic, str) or any(token not in final_topic for token in ("灰度", "回滚")):
        missing.append("finalPageTopic")
    return tuple(missing)


def _qualification_prompt() -> str:
    schema = json.dumps(
        _SEMANTIC_OUTPUT_SCHEMA,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return (
        "这是显式视觉资格测试。只读取三张用户图片，不调用任何工具。"
        "按图片页码顺序合并跨页内容。最终回答只能是一个 JSON 对象，不要 Markdown。"
        "下面的 JSON Schema 只说明输出字段和类型，不包含答案；必须逐字段从图片读取并满足它。"
        f"不要返回 Schema 本身，不得省略、更名或增加字段：{schema}"
    )


@dataclass(frozen=True, slots=True)
class _AuthFileSnapshot:
    device: int
    inode: int
    size: int
    modified_ns: int
    changed_ns: int
    content_sha256: str

    @classmethod
    def capture(cls, path: Path) -> _AuthFileSnapshot:
        before = path.stat()
        content = path.read_bytes()
        after = path.stat()
        before_identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
        after_identity = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
        if before_identity != after_identity:
            raise QualificationFailure("Codex auth file changed while qualification snapshotted it")
        return cls(
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
            hashlib.sha256(content).hexdigest(),
        )


@dataclass(frozen=True, slots=True)
class _OwnedQualificationRoot:
    path: Path
    device: int
    inode: int

    @classmethod
    def create(cls, path: Path) -> _OwnedQualificationRoot:
        if path.exists():
            raise QualificationFailure("qualification root must not already exist")
        if not path.parent.is_dir():
            raise QualificationFailure("qualification root parent must be an existing directory")
        path.mkdir()
        identity = path.lstat()
        return cls(path, identity.st_dev, identity.st_ino)

    def still_owns_path(self) -> bool:
        try:
            identity = self.path.lstat()
        except FileNotFoundError:
            return False
        return not self.path.is_symlink() and identity.st_dev == self.device and identity.st_ino == self.inode


class _CountingAttachmentStore(ConversationAttachmentStore):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.materialization_count = 0

    async def materialize_claimed_submission(
        self,
        session_id: str,
        turn_id: str,
        expected_claims: Sequence[AttachmentClaim],
        cancellation: CancellationToken,
    ) -> tuple[MaterializedClaimedAttachment, ...]:
        self.materialization_count += 1
        return await super().materialize_claimed_submission(session_id, turn_id, expected_claims, cancellation)


class _CapturingGateway:
    def __init__(self, delegate: Any) -> None:
        self._delegate = delegate
        self.requests: list[ModelRequest] = []

    async def stream(
        self,
        request: ModelRequest,
        cancellation: CancellationToken,
    ) -> AsyncIterator[ModelEvent]:
        self.requests.append(request)
        async for event in self._delegate.stream(request, cancellation):
            yield event


class _FrozenModelModule:
    def __init__(self, binding: CodexRunBinding) -> None:
        self._binding = binding

    def bind_for_run(self, model_id: str, account_binding: str) -> CodexRunBinding:
        if model_id != self._binding.model.model_id or account_binding != self._binding.account_binding:
            raise ValueError("qualification model selection drifted from its frozen catalog")
        return self._binding


class _PolicyAudit:
    async def append(self, record: object) -> None:
        del record


class _Recorder:
    async def commit(
        self,
        state: RunState,
        *,
        event_type: str,
        payload: Mapping[str, object],
        terminal: bool = False,
    ) -> None:
        del state, event_type, payload, terminal


class LiveOfferAgentQualificationEnvironment:
    """Use the real attachment, Run preparation, Agent Loop, and Codex gateway path."""

    def __init__(self, *, proxy_url: str, font_path: Path) -> None:
        ModelSettings(proxy_url=proxy_url)
        self._proxy_url = proxy_url
        self._generator = SyntheticInterviewFixtureGenerator(font_path=font_path)
        self._credentials = CodexFileCredentialSource()
        self._catalog_module = CodexSubscriptionModelModule(
            credentials=self._credentials,
            http=HttpxCodexCatalogHttpAdapter(proxy_url=proxy_url),
        )
        self._auth_path = default_codex_auth_path()
        self._auth_before: _AuthFileSnapshot | None = None
        self._catalog: CodexModelCatalogSnapshot | None = None
        self._working_root: _OwnedQualificationRoot | None = None
        self._execution_index = 0

    def generate_fixture(self, root: Path) -> SyntheticInterviewFixture:
        self._working_root = _OwnedQualificationRoot.create(root)
        return self._generator.generate(root / "fixture")

    def fetch_catalog(self) -> CodexModelCatalogSnapshot:
        self._auth_before = _AuthFileSnapshot.capture(self._auth_path)
        self._catalog = self._catalog_module.refresh(timeout_seconds=30.0)
        return self._catalog

    async def execute_image_model(
        self,
        model: CodexCatalogModel,
        account_binding: str,
        fixture: SyntheticInterviewFixture,
        detail: str,
    ) -> ImageModelEvidence:
        try:
            execution = await self._execute_model(
                model,
                account_binding,
                fixture,
                expected_detail=detail,
                run_agent=True,
            )
        except _ModelExecutionFailure as error:
            return ImageModelEvidence(
                None,
                error.counts.response_send_count,
                error.counts.attachment_materialization_count,
                error.counts.gateway_factory_count,
                error_code=error.error_code,
            )
        except Exception as error:
            return ImageModelEvidence(
                None,
                0,
                0,
                0,
                error_code=f"qualification_{type(error).__name__}",
            )
        if execution.semantics is None:
            return ImageModelEvidence(
                None,
                execution.counts.response_send_count,
                execution.counts.attachment_materialization_count,
                execution.counts.gateway_factory_count,
                error_code="agent_run_failed",
            )
        return ImageModelEvidence(
            execution.semantics,
            execution.counts.response_send_count,
            execution.counts.attachment_materialization_count,
            execution.counts.gateway_factory_count,
        )

    async def exercise_text_model_gate(
        self,
        model: CodexCatalogModel,
        account_binding: str,
        fixture: SyntheticInterviewFixture,
    ) -> TextModelGateEvidence:
        try:
            execution = await self._execute_model(
                model,
                account_binding,
                fixture,
                expected_detail=None,
                run_agent=False,
            )
        except _ModelExecutionFailure as error:
            return TextModelGateEvidence(
                error.error_code,
                error.counts.attachment_materialization_count,
                error.counts.response_send_count,
                error.counts.gateway_factory_count,
            )
        return TextModelGateEvidence(
            "image_modality_gate_missing",
            execution.counts.attachment_materialization_count,
            execution.counts.response_send_count,
            execution.counts.gateway_factory_count,
        )

    def verify_teardown(self) -> QualificationTeardownEvidence:
        auth_unchanged = self._auth_before is not None and self._auth_before == _AuthFileSnapshot.capture(
            self._auth_path
        )
        owned_root = self._working_root
        if owned_root is not None and owned_root.still_owns_path():
            gc.collect()
            shutil.rmtree(owned_root.path)
        return QualificationTeardownEvidence(
            auth_unchanged=auth_unchanged,
            temporary_state_removed=owned_root is not None and not owned_root.path.exists(),
        )

    def report_metadata(self) -> Mapping[str, str]:
        return MappingProxyType(
            {
                "offeragentPackageVersion": version("offeragent-harness"),
                "codexCliVersion": _safe_command_version(("codex", "--version")),
                "codexCatalogClientVersion": CODEX_CATALOG_CLIENT_VERSION,
                "gitCommit": _safe_command_version(("git", "rev-parse", "HEAD")),
                "qualifiedAt": datetime.now(timezone.utc).isoformat(),
            }
        )

    async def _store_fixture_pages(
        self,
        attachments: _CountingAttachmentStore,
        fixture: SyntheticInterviewFixture,
        *,
        session_id: str,
        turn_id: str,
        cancellation: CancellationToken,
    ) -> tuple[ArtifactRef, ...]:
        artifacts: list[ArtifactRef] = []
        for page in fixture.pages:
            payload = page.path.read_bytes()
            begun = await attachments.begin(
                AttachmentUploadRequest(
                    session_id=session_id,
                    client_request_id=f"req_page_{page.index}",
                    file_name=page.path.name,
                    media_type=page.media_type,
                    byte_length=len(payload),
                    content_hash=page.content_hash,
                ),
                cancellation,
            )
            chunk_bytes = AttachmentLimits().max_chunk_bytes
            for offset in range(0, len(payload), chunk_bytes):
                await attachments.append(
                    begun.upload_id,
                    offset,
                    payload[offset : offset + chunk_bytes],
                    cancellation,
                )
            artifacts.append((await attachments.commit(begun.upload_id, cancellation)).artifact)
        await attachments.claim_submission(
            session_id,
            turn_id,
            tuple(
                AttachmentClaim(
                    artifact.artifact_id,
                    index,
                    artifact.content_hash,
                    artifact.media_type,
                    artifact.size_bytes,
                )
                for index, artifact in enumerate(artifacts)
            ),
            cancellation,
        )
        return tuple(artifacts)

    def _start_turn_command(
        self,
        model: CodexCatalogModel,
        artifacts: Sequence[ArtifactRef],
        *,
        session_id: str,
        turn_id: str,
        config: HarnessConfig,
    ) -> StartTurnCommand:
        return StartTurnCommand(
            workspace_id="ws_live_vision",
            session_id=session_id,
            turn_id=turn_id,
            idempotency_key=f"qualification-{self._execution_index:03d}",
            input_blocks=(
                {"type": "text", "text": _qualification_prompt(), "format": "markdown", "references": []},
                *(
                    {"type": "image", "artifact": artifact.to_wire(), "altText": f"page {index}"}
                    for index, artifact in enumerate(artifacts, start=1)
                ),
            ),
            run_config={
                "model": model.model_id,
                "reasoningEffort": "high",
                "permissionMode": "read-only",
            },
            effective_config=config,
            effective_config_fingerprint=canonical_json_sha256(config.model_dump(mode="json")),
        )

    @staticmethod
    def _assert_ordered_user_images(
        gateways: Sequence[_CapturingGateway],
        fixture: SyntheticInterviewFixture,
        expected_detail: str,
    ) -> None:
        first_request = next((request for gateway in gateways for request in gateway.requests), None)
        if first_request is None:
            raise QualificationFailure("completed qualification Run sent no model request")
        image_messages = [
            message for message in first_request.messages if any(block.kind == "image" for block in message.content)
        ]
        if len(image_messages) != 1 or image_messages[0].role is not ModelRole.USER:
            raise QualificationFailure("qualification images were not bound to one USER message")
        image_blocks = [block for block in image_messages[0].content if block.kind == "image"]
        if [block.data.get("detail") for block in image_blocks] != [expected_detail] * len(fixture.pages):
            raise QualificationFailure("qualification images did not use the catalog-selected detail")
        if [block.data.get("contentHash") for block in image_blocks] != [page.content_hash for page in fixture.pages]:
            raise QualificationFailure("qualification image order or content hashes changed before inference")

    async def _execute_model(
        self,
        model: CodexCatalogModel,
        account_binding: str,
        fixture: SyntheticInterviewFixture,
        *,
        expected_detail: str | None,
        run_agent: bool,
    ) -> _ModelExecutionResult:
        catalog = self._catalog
        owned_root = self._working_root
        if (
            catalog is None
            or catalog.catalog_revision is None
            or catalog.fetched_at is None
            or owned_root is None
            or model not in catalog.models
        ):
            raise QualificationFailure("qualification execution lacks its frozen catalog or fixture root")
        root = owned_root.path
        self._execution_index += 1
        execution_root = root / f"model-{self._execution_index:03d}"
        clock = ManualClock(catalog.fetched_at)
        token = CancellationScope(name=f"live-vision:{model.model_id}")
        attachments = _CountingAttachmentStore(
            execution_root / "attachments",
            workspace_id="ws_live_vision",
            clock=clock,
            ids=DeterministicIdGenerator(),
        )
        session_id = f"ses_live_{self._execution_index:03d}"
        turn_id = f"turn_live_{self._execution_index:03d}"
        run_id = f"run_live_{self._execution_index:03d}"
        artifacts = await self._store_fixture_pages(
            attachments,
            fixture,
            session_id=session_id,
            turn_id=turn_id,
            cancellation=token,
        )
        config = HarnessConfig(
            model=ModelSettings(
                model=model.model_id,
                account_binding=account_binding,
                reasoning_effort="high",
                proxy_url=self._proxy_url,
            )
        )
        command = self._start_turn_command(
            model,
            artifacts,
            session_id=session_id,
            turn_id=turn_id,
            config=config,
        )
        binding = CodexRunBinding(model, catalog.catalog_revision, catalog.fetched_at, account_binding)
        gateways: list[_CapturingGateway] = []
        network_audit = RecordingNetworkAuditSink()

        def gateway_factory(settings: ModelSettings) -> _CapturingGateway:
            gateway = _CapturingGateway(
                compose_model_gateway(
                    settings,
                    workspace_id="ws_live_vision",
                    network_enabled=True,
                    network_audit=network_audit,
                    clock=clock,
                    codex_credential_source=self._credentials,
                )
            )
            gateways.append(gateway)
            return gateway

        factory = ProductionRunComponentsFactory(
            workspace_id="ws_live_vision",
            clock=clock,
            ids=DeterministicIdGenerator(),
            gateway_factory=gateway_factory,
            codex_models=_FrozenModelModule(binding),  # type: ignore[arg-type]
            default_config=config,
            approvals=ApprovalManager(unit_of_work=InMemoryUnitOfWorkFactory(), clock=clock),
            policy_audit=_PolicyAudit(),  # type: ignore[arg-type]
            journal=object(),
            artifacts=LocalArtifactStore(execution_root / "artifacts", workspace_id="ws_live_vision"),
            attachments=attachments,
            parent_authorities=object(),  # type: ignore[arg-type]
        )
        state = RunState(
            workspace_id="ws_live_vision",
            session_id=session_id,
            turn_id=turn_id,
            run_id=run_id,
            lineage=AgentLineage.root(run_id),
        )

        def execution_counts() -> _ExecutionCounts:
            return _ExecutionCounts(
                response_send_count=sum(record.stage == "intent" for record in network_audit.records),
                attachment_materialization_count=attachments.materialization_count,
                gateway_factory_count=len(gateways),
            )

        try:
            prepared = await factory.prepare_root(command, state, token, None)
        except RunPreparationFailure as error:
            raise _ModelExecutionFailure(error.code, execution_counts()) from error
        try:
            if expected_detail is None or not run_agent:
                return _ModelExecutionResult(None, execution_counts())
            components = factory.build_prepared_root(command, state, prepared)
            budget = BudgetLedger(components.budget, started_at=catalog.fetched_at)
            state = replace(state, budget_checkpoint=await BudgetCheckpoint.capture(budget, now=catalog.fetched_at))
            result = await run_agent_loop(
                state,
                planner=components.planner_factory(budget),
                tool_kernel=components.tool_kernel_factory(budget),
                recorder=_Recorder(),
                budget=budget,
                cancellation=token,
                now=clock.utcnow,
            )
            if result.phase is not RunPhase.COMPLETED:
                return _ModelExecutionResult(None, execution_counts())
            self._assert_ordered_user_images(gateways, fixture, expected_detail)
            return _ModelExecutionResult(_strict_semantic_json(result.assistant_text), execution_counts())
        except Exception as error:
            raise _ModelExecutionFailure(
                f"qualification_{type(error).__name__}",
                execution_counts(),
            ) from error


def _strict_semantic_json(value: str) -> Mapping[str, object]:
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > 16 * 1024:
        raise QualificationFailure("qualification semantic result is missing or oversized")

    def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, item in pairs:
            if key in result:
                raise QualificationFailure("qualification semantic result has duplicate fields")
            result[key] = item
        return result

    decoded = json.loads(value, object_pairs_hook=unique_object)
    if not isinstance(decoded, dict):
        raise QualificationFailure("qualification semantic result is not an object")
    return decoded


def _safe_command_version(command: tuple[str, ...]) -> str:
    executable = shutil.which(command[0])
    if executable is None:
        return "unavailable"
    try:
        completed = subprocess.run(
            (executable, *command[1:]),
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
            encoding="utf-8",
            errors="replace",
        )
    except (OSError, subprocess.SubprocessError):
        return "unavailable"
    value = completed.stdout.strip()
    return value if value and len(value) <= 256 else "unavailable"


__all__ = [
    "CodexVisionQualification",
    "ImageModelEvidence",
    "LiveOfferAgentQualificationEnvironment",
    "ModelQualificationResult",
    "QualificationEnvironment",
    "QualificationFailure",
    "QualificationReport",
    "QualificationTeardownEvidence",
    "SyntheticInterviewFixture",
    "SyntheticInterviewFixtureGenerator",
    "SyntheticInterviewPage",
    "TextModelGateEvidence",
]
