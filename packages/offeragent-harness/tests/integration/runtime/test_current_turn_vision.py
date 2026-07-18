from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from collections.abc import AsyncIterator, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any

import httpx
import pytest

from offeragent_harness.adapters.local_artifacts import LocalArtifactStore
from offeragent_harness.adapters.sqlite_stores import SqliteUnitOfWorkFactory
from offeragent_harness.agent import BudgetCheckpoint, BudgetLedger
from offeragent_harness.agent.loop import run_agent_loop
from offeragent_harness.agent.state import RunPhase, RunState
from offeragent_harness.config import HarnessConfig, ModelProvider
from offeragent_harness.models import ModelEvent, ModelRequest, ModelRole
from offeragent_harness.ports import CancellationToken
from offeragent_harness.providers import (
    CODEX_SUBSCRIPTION_BASE_URL,
    ModelCredentialLease,
    compose_model_gateway,
)
from offeragent_harness.providers.codex_subscription import CodexCatalogModel, CodexRunBinding
from offeragent_harness.runtime import CancellationScope
from offeragent_harness.runtime.approval_manager import ApprovalManager
from offeragent_harness.runtime.conversation_attachments import (
    AttachmentClaim,
    AttachmentUploadRequest,
    ConversationAttachmentStore,
)
from offeragent_harness.runtime.harness_service import CreateSessionCommand, HarnessService, StartTurnCommand
from offeragent_harness.runtime.production_worker_composition import ProductionRunComponentsFactory
from offeragent_harness.runtime.run_preparation import ConversationHistoryRunPreparationAdapter
from offeragent_harness.runtime.turn_manager import TurnManager
from offeragent_harness.sessions import AgentLineage, RunStatus
from offeragent_harness.testing import (
    DeterministicIdGenerator,
    InMemoryUnitOfWorkFactory,
    ManualCancellationToken,
    ManualClock,
    RecordingEventSink,
)
from offeragent_harness.tools import canonical_json_sha256

NOW = datetime(2026, 7, 18, 6, 0, tzinfo=timezone.utc)
MODEL_ID = "gpt-5.6-luna"
ACCOUNT_ID = "account-one"
ACCOUNT_FINGERPRINT = f"account-{ACCOUNT_ID}"
ACCOUNT_BINDING = f"sha256:{hashlib.sha256(ACCOUNT_FINGERPRINT.encode()).hexdigest()}"
ANSWER = "Both screenshots belong to one ordered interview submission."

# Independently generated, valid 1x1 red and blue PNG fixtures.
FIRST_IMAGE = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4z8AAAAMBAQDJ/pLvAAAAAElFTkSuQmCC"
)
SECOND_IMAGE = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGNgYPgPAAEDAQAIicLsAAAAAElFTkSuQmCC"
)


def _digest(value: bytes) -> str:
    return f"sha256:{hashlib.sha256(value).hexdigest()}"


class _UnusedSecrets:
    def consume(self, *args: object, **kwargs: object) -> object:
        del args, kwargs
        raise AssertionError("Codex subscription inference must use its account-bound credential lease")


class _CredentialSource:
    def __init__(self) -> None:
        self.leases = 0

    @contextmanager
    def lease(self) -> Iterator[ModelCredentialLease]:
        self.leases += 1
        token = bytearray(b"test-codex-access-token")
        material = memoryview(token)
        try:
            yield ModelCredentialLease(
                material=material,
                headers=MappingProxyType(
                    {
                        "ChatGPT-Account-ID": ACCOUNT_ID,
                        "originator": "codex_cli_rs",
                        "User-Agent": "codex_cli_rs/0.0.0 (OfferAgent integration test)",
                    }
                ),
                credential_fingerprint="credential-test",
                account_fingerprint=ACCOUNT_FINGERPRINT,
            )
        finally:
            material.release()
            for index in range(len(token)):
                token[index] = 0


class _CodexModels:
    def __init__(self) -> None:
        self.bind_calls: list[tuple[str, str]] = []

    def bind_for_run(self, model_id: str, account_binding: str) -> CodexRunBinding:
        self.bind_calls.append((model_id, account_binding))
        return CodexRunBinding(
            model=CodexCatalogModel(
                model_id=MODEL_ID,
                display_name="GPT Vision Test",
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
            catalog_revision="sha256:" + "a" * 64,
            bound_at=NOW,
            account_binding=ACCOUNT_BINDING,
        )


class _PolicyAudit:
    async def append(self, record: object) -> None:
        del record


class _Recorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, Mapping[str, object], bool]] = []

    async def commit(
        self,
        state: RunState,
        *,
        event_type: str,
        payload: Mapping[str, object],
        terminal: bool = False,
    ) -> None:
        del state
        self.events.append((event_type, payload, terminal))


class _CapturingGateway:
    def __init__(self, delegate: object) -> None:
        self._delegate = delegate
        self.requests: list[ModelRequest] = []

    async def stream(
        self,
        request: ModelRequest,
        cancellation: CancellationToken,
    ) -> AsyncIterator[ModelEvent]:
        self.requests.append(request)
        stream = self._delegate.stream(request, cancellation)  # type: ignore[attr-defined]
        async for event in stream:
            yield event


def _config() -> HarnessConfig:
    base = HarnessConfig()
    return base.model_copy(
        update={
            "model": base.model.model_copy(
                update={
                    "provider": ModelProvider.CODEX_SUBSCRIPTION_EXPERIMENTAL,
                    "model": MODEL_ID,
                    "account_binding": ACCOUNT_BINDING,
                }
            )
        }
    )


def _state() -> RunState:
    return RunState(
        workspace_id="ws_test",
        session_id="ses_test",
        turn_id="turn_test",
        run_id="run_test",
        lineage=AgentLineage.root("run_test"),
    )


def _sse(*events: Mapping[str, object]) -> bytes:
    return b"".join(
        f"event: {event['type']}\ndata: {json.dumps(event, separators=(',', ':'))}\n\n".encode() for event in events
    )


def _response(answer: str = ANSWER, *, response_id: str = "resp_vision") -> bytes:
    structured = json.dumps(
        {
            "requiresWriteOutcome": False,
            "calls": [],
            "finalResponse": answer,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return _sse(
        {"type": "response.created", "sequence_number": 0, "response": {"id": response_id}},
        {
            "type": "response.output_text.delta",
            "sequence_number": 1,
            "output_index": 0,
            "content_index": 0,
            "delta": structured,
        },
        {
            "type": "response.output_text.done",
            "sequence_number": 2,
            "output_index": 0,
            "content_index": 0,
            "text": structured,
        },
        {
            "type": "response.completed",
            "sequence_number": 3,
            "response": {
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": structured}],
                    }
                ],
                "usage": {
                    "input_tokens": 32,
                    "output_tokens": 16,
                    "input_tokens_details": {"cached_tokens": 0},
                    "output_tokens_details": {"reasoning_tokens": 0},
                },
            },
        },
    )


async def _upload(
    store: ConversationAttachmentStore,
    token: ManualCancellationToken,
    *,
    index: int,
    payload: bytes,
    session_id: str = "ses_test",
) -> Any:
    begun = await store.begin(
        AttachmentUploadRequest(
            session_id=session_id,
            client_request_id=f"req_upload_{index}",
            file_name=f"page-{index}.png",
            media_type="image/png",
            byte_length=len(payload),
            content_hash=_digest(payload),
        ),
        token,
    )
    await store.append(begun.upload_id, 0, payload, token)
    return (await store.commit(begun.upload_id, token)).artifact


@pytest.mark.asyncio
async def test_current_turn_images_produce_one_locally_validated_answer_without_probe(tmp_path: Path) -> None:
    token = ManualCancellationToken()
    clock = ManualClock(NOW)
    ids = DeterministicIdGenerator()
    attachments = ConversationAttachmentStore(
        tmp_path / "attachments",
        workspace_id="ws_test",
        clock=clock,
        ids=ids,
    )
    artifacts = [
        await _upload(attachments, token, index=1, payload=FIRST_IMAGE),
        await _upload(attachments, token, index=2, payload=SECOND_IMAGE),
    ]
    await attachments.claim_submission(
        "ses_test",
        "turn_test",
        tuple(
            AttachmentClaim(
                artifact_id=artifact.artifact_id,
                order=index,
                content_hash=artifact.content_hash,
                media_type=artifact.media_type,
                byte_length=artifact.size_bytes,
            )
            for index, artifact in enumerate(artifacts)
        ),
        token,
    )

    transport_requests: list[httpx.Request] = []
    transport_bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        transport_requests.append(request)
        body = json.loads(request.content)
        assert isinstance(body, dict)
        transport_bodies.append(body)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_response(),
            request=request,
        )

    credential_source = _CredentialSource()
    captured_gateway: list[_CapturingGateway] = []

    def gateway_factory(settings: Any) -> _CapturingGateway:
        gateway = _CapturingGateway(
            compose_model_gateway(
                settings,
                secret_scope_id="workspace:ws_test",
                secrets=_UnusedSecrets(),  # type: ignore[arg-type]
                network_enabled=True,
                responses_transport=httpx.MockTransport(handler),
                codex_credential_source=credential_source,
            )
        )
        captured_gateway.append(gateway)
        return gateway

    config = _config()
    command = StartTurnCommand(
        workspace_id="ws_test",
        session_id="ses_test",
        turn_id="turn_test",
        idempotency_key="turn-test",
        input_blocks=(
            {"type": "text", "text": "按顺序比较这两张面试截图。", "format": "markdown", "references": []},
            *(
                {"type": "image", "artifact": artifact.to_wire(), "altText": f"page {index}"}
                for index, artifact in enumerate(artifacts, start=1)
            ),
        ),
        run_config={
            "provider": ModelProvider.CODEX_SUBSCRIPTION_EXPERIMENTAL.value,
            "model": MODEL_ID,
            "reasoningEffort": "medium",
            "permissionMode": "read-only",
        },
        effective_config=config,
        effective_config_fingerprint=canonical_json_sha256(config.model_dump(mode="json")),
    )
    codex_models = _CodexModels()
    factory = ProductionRunComponentsFactory(
        workspace_id="ws_test",
        clock=clock,
        ids=ids,
        gateway_factory=gateway_factory,
        codex_models=codex_models,  # type: ignore[arg-type]
        default_config=config,
        approvals=ApprovalManager(unit_of_work=InMemoryUnitOfWorkFactory(), clock=clock),
        policy_audit=_PolicyAudit(),  # type: ignore[arg-type]
        journal=object(),
        artifacts=LocalArtifactStore(tmp_path / "artifacts", workspace_id="ws_test"),
        attachments=attachments,
        local_transaction=None,
        parent_authorities=object(),  # type: ignore[arg-type]
    )
    initial_state = _state()
    prepared = await factory.prepare_root(command, initial_state, token, None)  # type: ignore[arg-type]
    components = factory.build_prepared_root(command, initial_state, prepared)
    budget = BudgetLedger(components.budget, started_at=NOW)
    initial_state = replace(
        initial_state,
        budget_checkpoint=await BudgetCheckpoint.capture(budget, now=NOW),
    )
    recorder = _Recorder()

    result = await run_agent_loop(
        initial_state,
        planner=components.planner_factory(budget),
        tool_kernel=components.tool_kernel_factory(budget),
        recorder=recorder,
        budget=budget,
        cancellation=CancellationScope(name="current-turn-vision"),
        now=clock.utcnow,
    )

    assert result.phase is RunPhase.COMPLETED
    assert result.assistant_text == ANSWER
    assert result.model_rounds == 1
    assert recorder.events[-1][0::2] == ("turn.completed", True)

    assert len(captured_gateway) == 1
    assert len(captured_gateway[0].requests) == 1
    model_request = captured_gateway[0].requests[0]
    image_messages = [
        message for message in model_request.messages if any(block.kind == "image" for block in message.content)
    ]
    assert len(image_messages) == 1
    assert image_messages[0].role is ModelRole.USER
    image_blocks = [block for block in image_messages[0].content if block.kind == "image"]
    assert [block.binary_data for block in image_blocks] == [FIRST_IMAGE, SECOND_IMAGE]
    assert [block.data["artifactId"] for block in image_blocks] == [
        artifacts[0].artifact_id,
        artifacts[1].artifact_id,
    ]
    assert [block.data["detail"] for block in image_blocks] == ["original", "original"]

    assert codex_models.bind_calls == [(MODEL_ID, ACCOUNT_BINDING)]
    assert credential_source.leases == 1
    assert len(transport_requests) == 1
    assert str(transport_requests[0].url) == f"{CODEX_SUBSCRIPTION_BASE_URL}/responses"
    assert len(transport_bodies) == 1
    encoded_user_images = [
        block
        for message in transport_bodies[0]["input"]
        if message["role"] == "user"
        for block in message["content"]
        if block["type"] == "input_image"
    ]
    assert [block["image_url"] for block in encoded_user_images] == [
        "data:image/png;base64," + base64.b64encode(FIRST_IMAGE).decode("ascii"),
        "data:image/png;base64," + base64.b64encode(SECOND_IMAGE).decode("ascii"),
    ]
    assert [block["detail"] for block in encoded_user_images] == ["original", "original"]
    assert transport_bodies[0]["store"] is False
    assert "previous_response_id" not in transport_bodies[0]


@pytest.mark.asyncio
async def test_text_follow_up_rematerializes_historical_user_images_after_runtime_restart(tmp_path: Path) -> None:
    state_path = tmp_path / "runtime.sqlite"
    attachment_root = tmp_path / "attachments"
    transport_bodies: list[dict[str, Any]] = []
    credential_source = _CredentialSource()

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert isinstance(body, dict)
        transport_bodies.append(body)
        index = len(transport_bodies)
        answer = ANSWER if index == 1 else "The follow-up answer used both retained historical screenshots."
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_response(answer, response_id=f"resp_history_{index}"),
            request=request,
        )

    def gateway_factory(settings: Any) -> _CapturingGateway:
        return _CapturingGateway(
            compose_model_gateway(
                settings,
                secret_scope_id="workspace:ws_test",
                secrets=_UnusedSecrets(),  # type: ignore[arg-type]
                network_enabled=True,
                responses_transport=httpx.MockTransport(handler),
                codex_credential_source=credential_source,
            )
        )

    config = _config()
    config_fingerprint = canonical_json_sha256(config.model_dump(mode="json"))
    codex_models = _CodexModels()

    def runtime(start: int) -> tuple[HarnessService, ConversationAttachmentStore]:
        uow = SqliteUnitOfWorkFactory(state_path)
        clock = ManualClock(NOW)
        attachments = ConversationAttachmentStore(
            attachment_root,
            workspace_id="ws_test",
            clock=clock,
            ids=DeterministicIdGenerator(start=start + 500),
        )
        approvals = ApprovalManager(unit_of_work=uow, clock=clock)
        history = ConversationHistoryRunPreparationAdapter(
            workspace_id="ws_test",
            unit_of_work=uow,
            attachments=attachments,
        )
        components = ProductionRunComponentsFactory(
            workspace_id="ws_test",
            clock=clock,
            ids=DeterministicIdGenerator(start=start + 100),
            gateway_factory=gateway_factory,
            codex_models=codex_models,  # type: ignore[arg-type]
            default_config=config,
            approvals=approvals,
            policy_audit=_PolicyAudit(),  # type: ignore[arg-type]
            journal=object(),
            artifacts=LocalArtifactStore(tmp_path / "artifacts", workspace_id="ws_test"),
            attachments=attachments,
            conversation_history=history,
            local_transaction=None,
            parent_authorities=object(),  # type: ignore[arg-type]
        )
        harness = HarnessService(
            unit_of_work=uow,
            event_sink=RecordingEventSink(),
            clock=clock,
            ids=DeterministicIdGenerator(start=start),
            components=components,
            async_components=components,
            turn_manager=TurnManager(),
            approval_manager=approvals,
        )
        return harness, attachments

    async def wait_completed(harness: HarnessService, run_id: str) -> None:
        for _ in range(1_000):
            run = await harness.get_run(run_id)
            if run.status is RunStatus.COMPLETED:
                return
            if run.status in {RunStatus.FAILED, RunStatus.CANCELLED, RunStatus.INTERRUPTED}:
                pytest.fail(f"Run terminated as {run.status.value}")
            await asyncio.sleep(0)
        pytest.fail("Run did not complete")

    harness, attachments = runtime(1_000)
    created = await harness.create_session(
        CreateSessionCommand("ws_test", "profile_test", "Historical vision", "create-history")
    )
    token = ManualCancellationToken()
    artifacts = [
        await _upload(attachments, token, index=1, payload=FIRST_IMAGE, session_id=created.session_id),
        await _upload(attachments, token, index=2, payload=SECOND_IMAGE, session_id=created.session_id),
    ]
    await attachments.claim_submission(
        created.session_id,
        "turn_image",
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
        token,
    )
    first = await harness.start_turn(
        StartTurnCommand(
            workspace_id="ws_test",
            session_id=created.session_id,
            turn_id="turn_image",
            idempotency_key="image-first",
            input_blocks=(
                {"type": "text", "text": "Summarize both interview pages.", "format": "markdown", "references": []},
                *(
                    {"type": "image", "artifact": artifact.to_wire(), "altText": f"page {index}"}
                    for index, artifact in enumerate(artifacts, start=1)
                ),
            ),
            run_config={
                "provider": ModelProvider.CODEX_SUBSCRIPTION_EXPERIMENTAL.value,
                "model": MODEL_ID,
                "reasoningEffort": "medium",
                "permissionMode": "read-only",
            },
            effective_config=config,
            effective_config_fingerprint=config_fingerprint,
        )
    )
    await wait_completed(harness, first.run_id)
    await harness.shutdown()

    restarted, _ = runtime(10_000)
    follow_up = await restarted.start_turn(
        StartTurnCommand(
            workspace_id="ws_test",
            session_id=created.session_id,
            turn_id="turn_followup",
            idempotency_key="text-follow-up",
            input_blocks=(
                {
                    "type": "text",
                    "text": "Correct the summary using the exact facts in those screenshots.",
                    "format": "markdown",
                    "references": [],
                },
            ),
            run_config={
                "provider": ModelProvider.CODEX_SUBSCRIPTION_EXPERIMENTAL.value,
                "model": MODEL_ID,
                "reasoningEffort": "medium",
                "permissionMode": "read-only",
            },
            effective_config=config,
            effective_config_fingerprint=config_fingerprint,
        )
    )
    await wait_completed(restarted, follow_up.run_id)
    await restarted.shutdown()

    assert len(transport_bodies) == 2
    follow_up_input = transport_bodies[1]["input"]
    image_messages = [
        message
        for message in follow_up_input
        if message["role"] == "user" and any(block["type"] == "input_image" for block in message["content"])
    ]
    assert len(image_messages) == 1
    retained_images = [block for block in image_messages[0]["content"] if block["type"] == "input_image"]
    assert [block["image_url"] for block in retained_images] == [
        "data:image/png;base64," + base64.b64encode(FIRST_IMAGE).decode("ascii"),
        "data:image/png;base64," + base64.b64encode(SECOND_IMAGE).decode("ascii"),
    ]
    assert any(message["role"] == "assistant" for message in follow_up_input)
    current_messages = [message for message in follow_up_input if message["role"] == "user"]
    assert all(block["type"] != "input_image" for block in current_messages[-1]["content"])
    assert (await restarted.get_run_state(follow_up.run_id)).assistant_text == (
        "The follow-up answer used both retained historical screenshots."
    )

    durable_state = state_path.read_bytes()
    assert FIRST_IMAGE not in durable_state
    assert SECOND_IMAGE not in durable_state
    assert base64.b64encode(FIRST_IMAGE) not in durable_state
    assert base64.b64encode(SECOND_IMAGE) not in durable_state
