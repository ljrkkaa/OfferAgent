from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator, Mapping
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Protocol, cast

import pytest

from offeragent_harness.adapters.local_artifacts import LocalArtifactStore
from offeragent_harness.agent import BudgetCheckpoint, BudgetLedger, RunPreparationFailure
from offeragent_harness.agent.context_manager import ContextBudgetExceeded
from offeragent_harness.agent.state import RunState
from offeragent_harness.config import HarnessConfig, ModelProvider
from offeragent_harness.models import ModelEvent, ModelRequest, ModelRole
from offeragent_harness.ports import CancellationToken
from offeragent_harness.providers.codex_subscription import (
    CodexCatalogModel,
    CodexRunBinding,
    CodexRunBindingError,
)
from offeragent_harness.runtime.approval_manager import ApprovalManager
from offeragent_harness.runtime.attachment_errors import AttachmentError
from offeragent_harness.runtime.cancellation import CancellationScope
from offeragent_harness.runtime.conversation_attachments import AttachmentClaim
from offeragent_harness.runtime.harness_service import StartTurnCommand
from offeragent_harness.runtime.production_worker_composition import ProductionRunComponentsFactory
from offeragent_harness.sessions import AgentLineage
from offeragent_harness.testing import (
    DeterministicIdGenerator,
    InMemoryUnitOfWorkFactory,
    ManualClock,
)
from offeragent_harness.tools import canonical_json_sha256

NOW = datetime(2026, 7, 18, 6, 0, tzinfo=timezone.utc)
ACCOUNT_BINDING = "sha256:" + "b" * 64


class _RequestCreatingPlanner(Protocol):
    def create_request(self, state: RunState) -> ModelRequest: ...


def _cancellation() -> CancellationScope:
    return CancellationScope(name="test-codex-run-binding")


def _create_model_request(planner: object, state: RunState) -> ModelRequest:
    return cast(_RequestCreatingPlanner, planner).create_request(state)


class _Gateway:
    async def stream(
        self,
        request: ModelRequest,
        cancellation: CancellationToken,
    ) -> AsyncIterator[ModelEvent]:
        del request, cancellation
        if False:
            yield  # pragma: no cover


class _ModelModule:
    def __init__(
        self,
        outcome: CodexRunBinding | CodexRunBindingError,
        *,
        restore_outcome: CodexRunBinding | CodexRunBindingError | None = None,
    ) -> None:
        self.outcome = outcome
        self.restore_outcome = outcome if restore_outcome is None else restore_outcome
        self.bind_calls: list[tuple[str, str]] = []
        self.restore_calls: list[tuple[str, str]] = []

    def bind_for_run(self, model_id: str, account_binding: str) -> CodexRunBinding:
        self.bind_calls.append((model_id, account_binding))
        if isinstance(self.outcome, CodexRunBindingError):
            raise self.outcome
        return self.outcome

    def restore_for_run(
        self,
        value: object,
        *,
        model_id: str,
        account_binding: str,
    ) -> CodexRunBinding:
        self.restore_calls.append((model_id, account_binding))
        if not isinstance(value, Mapping):
            raise ValueError("durable Codex model binding is invalid")
        CodexRunBinding.from_durable_snapshot(
            value,
            expected_model_id=model_id,
            expected_account_binding=account_binding,
        )
        if isinstance(self.restore_outcome, CodexRunBindingError):
            raise self.restore_outcome
        return self.restore_outcome


class _PolicyAudit:
    async def append(self, record: object) -> None:
        del record


def _binding(
    model_id: str = "gpt-selected",
    *,
    input_modalities: tuple[str, ...] = ("text",),
    supports_image_detail_original: bool = False,
    context_window: int = 128_000,
    effective_context_window_percent: int = 95,
) -> CodexRunBinding:
    return CodexRunBinding(
        model=CodexCatalogModel(
            model_id=model_id,
            display_name="GPT Selected",
            description=None,
            input_modalities=input_modalities,
            supports_image_detail_original=supports_image_detail_original,
            supports_hosted_search=True,
            web_search_tool_type="text",
            context_window=context_window,
            max_context_window=context_window,
            effective_context_window_percent=effective_context_window_percent,
            additional_speed_tiers=(),
            service_tiers=(),
            default_service_tier=None,
        ),
        catalog_revision="sha256:" + "a" * 64,
        bound_at=NOW,
        account_binding=ACCOUNT_BINDING,
    )


def _config(model_id: str = "gpt-selected", *, proxy_url: str | None = None) -> HarnessConfig:
    base = HarnessConfig()
    return base.model_copy(
        update={
            "model": base.model.model_copy(
                update={
                    "provider": ModelProvider.CODEX_SUBSCRIPTION_EXPERIMENTAL,
                    "model": model_id,
                    "account_binding": ACCOUNT_BINDING,
                    "proxy_url": proxy_url,
                }
            )
        }
    )


def _command(config: HarnessConfig, model_id: str = "gpt-selected") -> StartTurnCommand:
    return StartTurnCommand(
        workspace_id="ws_test",
        session_id="ses_test",
        turn_id="turn_test",
        idempotency_key="idem-test",
        input_blocks=({"type": "text", "text": "hello"},),
        run_config={
            "provider": "codex-subscription-experimental",
            "model": model_id,
            "reasoningEffort": "medium",
            "permissionMode": "read-only",
        },
        effective_config=config,
        effective_config_fingerprint=canonical_json_sha256(config.model_dump(mode="json")),
    )


def _state() -> RunState:
    return RunState(
        workspace_id="ws_test",
        session_id="ses_test",
        turn_id="turn_test",
        run_id="run_test",
        lineage=AgentLineage.root("run_test"),
    )


def _factory(
    tmp_path: Path,
    module: _ModelModule,
    gateway_calls: list[Any],
    *,
    attachments: object | None = None,
) -> ProductionRunComponentsFactory:
    clock = ManualClock(NOW)

    def gateway(settings: Any) -> _Gateway:
        gateway_calls.append(settings)
        return _Gateway()

    return ProductionRunComponentsFactory(
        workspace_id="ws_test",
        clock=clock,
        ids=DeterministicIdGenerator(),
        gateway_factory=gateway,
        codex_models=module,  # type: ignore[arg-type]
        default_config=_config(),
        approvals=ApprovalManager(unit_of_work=InMemoryUnitOfWorkFactory(), clock=clock),
        policy_audit=_PolicyAudit(),  # type: ignore[arg-type]
        journal=object(),
        artifacts=LocalArtifactStore(tmp_path / "artifacts", workspace_id="ws_test"),
        attachments=attachments,  # type: ignore[arg-type]
        local_transaction=None,
        parent_authorities=object(),  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_prepare_root_binds_fresh_catalog_model_and_build_uses_only_that_exact_id(tmp_path: Path) -> None:
    module = _ModelModule(_binding())
    gateway_calls: list[Any] = []
    factory = _factory(tmp_path, module, gateway_calls)
    config = _config(proxy_url="http://127.0.0.1:7896")
    command = _command(config)

    prepared = await factory.prepare_root(command, _state(), _cancellation(), None)
    components = factory.build_prepared_root(command, _state(), prepared)

    assert module.bind_calls == [("gpt-selected", ACCOUNT_BINDING)]
    assert len(gateway_calls) == 1
    settings = gateway_calls[0]
    assert settings.provider is ModelProvider.CODEX_SUBSCRIPTION_EXPERIMENTAL
    assert settings.model == "gpt-selected"
    assert settings.proxy_url == "http://127.0.0.1:7896"
    assert components.budget.max_model_rounds > 0
    ledger = BudgetLedger(components.budget, started_at=NOW)
    request_state = replace(
        _state(),
        budget_checkpoint=await BudgetCheckpoint.capture(ledger, now=NOW),
    )
    model_request = _create_model_request(components.planner_factory(ledger), request_state)
    assert model_request.model == "gpt-selected"
    proof = prepared.durable_snapshot
    assert proof["modelBinding"]["modelId"] == "gpt-selected"
    assert proof["modelBinding"]["catalogRevision"] == "sha256:" + "a" * 64
    assert proof["modelBinding"]["boundAt"] == NOW.isoformat()
    assert proof["modelBinding"]["accountBinding"] == ACCOUNT_BINDING
    assert tuple(proof["modelBinding"]["modelCapabilities"]["inputModalities"]) == ("text",)


@pytest.mark.asyncio
async def test_bound_catalog_context_window_limits_the_local_context_projection(tmp_path: Path) -> None:
    module = _ModelModule(_binding(context_window=512, effective_context_window_percent=100))
    gateway_calls: list[Any] = []
    factory = _factory(tmp_path, module, gateway_calls)
    command = _command(_config())
    state = _state()

    prepared = await factory.prepare_root(command, state, _cancellation(), None)
    components = factory.build_prepared_root(command, state, prepared)
    ledger = BudgetLedger(components.budget, started_at=NOW)
    request_state = replace(
        state,
        budget_checkpoint=await BudgetCheckpoint.capture(ledger, now=NOW),
    )

    with pytest.raises(ContextBudgetExceeded, match="system rules"):
        _create_model_request(components.planner_factory(ledger), request_state)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("code", "wire_code"),
    [
        ("auth_required", "provider.auth_required"),
        ("auth_account_changed", "provider.auth_required"),
        ("catalog_unreachable", "provider.unreachable"),
        ("model_unavailable", "provider.unsupported"),
    ],
)
async def test_prepare_root_fails_before_gateway_creation_when_binding_is_invalid(
    tmp_path: Path,
    code: str,
    wire_code: str,
) -> None:
    module = _ModelModule(CodexRunBindingError(code, retryable=code == "catalog_unreachable"))  # type: ignore[arg-type]
    gateway_calls: list[Any] = []
    factory = _factory(tmp_path, module, gateway_calls)

    with pytest.raises(RunPreparationFailure) as caught:
        await factory.prepare_root(_command(_config()), _state(), _cancellation(), None)

    assert caught.value.code == code
    assert caught.value.error_code.value == wire_code
    assert caught.value.details == {"runBindingCode": code}
    assert module.bind_calls == [("gpt-selected", ACCOUNT_BINDING)]
    assert gateway_calls == []


@pytest.mark.asyncio
async def test_recovery_restores_the_fingerprinted_binding_without_catalog_io_or_reselection(tmp_path: Path) -> None:
    first_module = _ModelModule(_binding())
    first = _factory(tmp_path / "first", first_module, [])
    command = _command(_config())
    durable = (await first.prepare_root(command, _state(), _cancellation(), None)).durable_snapshot
    unavailable = _ModelModule(
        CodexRunBindingError("catalog_unreachable", retryable=True),
        restore_outcome=_binding(),
    )
    gateway_calls: list[Any] = []
    restarted = _factory(tmp_path / "restarted", unavailable, gateway_calls)

    prepared = await restarted.prepare_root(
        command,
        _state(),
        _cancellation(),
        durable,
    )
    restarted.build_prepared_root(command, _state(), prepared)

    assert unavailable.bind_calls == []
    assert unavailable.restore_calls == [("gpt-selected", ACCOUNT_BINDING)]
    assert prepared.durable_snapshot == durable
    assert gateway_calls[0].model == "gpt-selected"


@pytest.mark.asyncio
async def test_recovery_rejects_a_durable_binding_for_a_different_model(tmp_path: Path) -> None:
    first = _factory(tmp_path / "first", _ModelModule(_binding("gpt-other")), [])
    other_command = _command(_config("gpt-other"), "gpt-other")
    durable = (await first.prepare_root(other_command, _state(), _cancellation(), None)).durable_snapshot
    restarted = _factory(tmp_path / "restarted", _ModelModule(_binding()), [])

    with pytest.raises(ValueError, match="durable Codex model binding"):
        await restarted.prepare_root(
            _command(_config()),
            _state(),
            _cancellation(),
            durable,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("drift", ["run_provider", "persisted_provider", "persisted_model"])
async def test_new_run_rejects_provider_or_model_drift_before_catalog_and_gateway(
    tmp_path: Path,
    drift: str,
) -> None:
    module = _ModelModule(_binding())
    gateway_calls: list[Any] = []
    config = _config()
    command = _command(config)
    if drift == "run_provider":
        command = replace(command, run_config={**command.run_config, "provider": "deepseek"})
    elif drift == "persisted_provider":
        config = config.model_copy(
            update={"model": config.model.model_copy(update={"provider": ModelProvider.DEEPSEEK})}
        )
        command = _command(config)
    else:
        config = _config("gpt-other")
        command = _command(config, "gpt-selected")
    factory = _factory(tmp_path, module, gateway_calls)

    with pytest.raises(ValueError):
        await factory.prepare_root(command, _state(), _cancellation(), None)

    assert module.bind_calls == []
    assert gateway_calls == []


@pytest.mark.asyncio
async def test_text_only_catalog_model_rejects_images_before_attachment_or_inference_io(
    tmp_path: Path,
) -> None:
    class _UnreadAttachments:
        calls = 0

        async def read_all_for_conversation(self, *args: object) -> object:
            del args
            self.calls += 1
            raise AssertionError("text-only capability gate must run before attachment I/O")

    attachments = _UnreadAttachments()
    module = _ModelModule(_binding(input_modalities=("text",)))
    gateway_calls: list[Any] = []
    factory = _factory(tmp_path, module, gateway_calls, attachments=attachments)
    command = replace(
        _command(_config()),
        input_blocks=(
            {"type": "text", "text": "inspect", "format": "markdown", "references": []},
            {
                "type": "image",
                "artifact": {
                    "artifactId": "art_one",
                    "contentHash": "sha256:" + "1" * 64,
                    "mediaType": "image/png",
                    "sizeBytes": 16,
                    "sensitivity": "private",
                },
                "altText": "page one",
            },
        ),
    )

    with pytest.raises(RunPreparationFailure) as caught:
        await factory.prepare_root(command, _state(), _cancellation(), None)

    assert caught.value.code == "image_modality_unsupported"
    assert caught.value.error_code.value == "provider.image_unsupported"
    assert attachments.calls == 0
    assert gateway_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("supports_original", "expected_detail"),
    [(False, "high"), (True, "original")],
)
async def test_current_turn_images_reach_one_user_request_in_order_with_catalog_detail(
    tmp_path: Path,
    supports_original: bool,
    expected_detail: str,
) -> None:
    first = b"\x89PNG\r\n\x1a\nfirst-image"
    second = b"\x89PNG\r\n\x1a\nsecond-image"

    class _Attachments:
        def __init__(self) -> None:
            self.blobs = {"art_one": first, "art_two": second}
            self.calls: list[str] = []

        async def materialize_claimed_submission(
            self,
            session_id: str,
            turn_id: str,
            claims: tuple[AttachmentClaim, ...],
            cancellation: CancellationToken,
        ) -> object:
            assert session_id == "ses_test"
            assert turn_id == "turn_test"
            cancellation.checkpoint()
            result = []
            for claim in claims:
                artifact_id = claim.artifact_id
                blob = self.blobs[artifact_id]
                self.calls.append(artifact_id)
                result.append(
                    SimpleNamespace(
                        attachment=SimpleNamespace(
                            artifact_id=artifact_id,
                            order=claim.order,
                            media_type=claim.media_type,
                            content_hash=claim.content_hash,
                            byte_length=claim.byte_length,
                        ),
                        content=blob,
                        width=1,
                        height=1,
                    )
                )
            return tuple(result)

    def image_block(artifact_id: str, payload: bytes, alt_text: str) -> dict[str, object]:
        return {
            "type": "image",
            "artifact": {
                "artifactId": artifact_id,
                "contentHash": "sha256:" + hashlib.sha256(payload).hexdigest(),
                "mediaType": "image/png",
                "sizeBytes": len(payload),
                "sensitivity": "private",
                "state": "complete",
            },
            "altText": alt_text,
        }

    attachments = _Attachments()
    module = _ModelModule(
        _binding(
            input_modalities=("text", "image"),
            supports_image_detail_original=supports_original,
        )
    )
    gateway_calls: list[Any] = []
    factory = _factory(tmp_path, module, gateway_calls, attachments=attachments)
    command = replace(
        _command(_config()),
        input_blocks=(
            {"type": "text", "text": "compare", "format": "markdown", "references": []},
            image_block("art_one", first, "first"),
            image_block("art_two", second, "second"),
        ),
    )

    prepared = await factory.prepare_root(command, _state(), _cancellation(), None)
    components = factory.build_prepared_root(command, _state(), prepared)
    ledger = BudgetLedger(components.budget, started_at=NOW)
    request_state = replace(
        _state(),
        budget_checkpoint=await BudgetCheckpoint.capture(ledger, now=NOW),
    )
    request = _create_model_request(components.planner_factory(ledger), request_state)

    image_messages = [
        message for message in request.messages if any(block.kind == "image" for block in message.content)
    ]
    assert len(image_messages) == 1
    assert image_messages[0].role is ModelRole.USER
    images = [block for block in image_messages[0].content if block.kind == "image"]
    assert [block.binary_data for block in images] == [first, second]
    assert [block.data["artifactId"] for block in images] == ["art_one", "art_two"]
    assert [block.data["detail"] for block in images] == [expected_detail, expected_detail]
    assert attachments.calls == ["art_one", "art_two"]
    assert len(gateway_calls) == 1


@pytest.mark.asyncio
async def test_current_image_batch_failure_preserves_the_corrupt_page_index(tmp_path: Path) -> None:
    first = b"first-image"
    second = b"second-image"

    class _Attachments:
        async def materialize_claimed_submission(self, *args: object) -> object:
            del args
            raise AttachmentError("attachment_corrupt", "second page is corrupt", item_order=1)

    def image_block(artifact_id: str, payload: bytes) -> dict[str, object]:
        return {
            "type": "image",
            "artifact": {
                "artifactId": artifact_id,
                "contentHash": "sha256:" + hashlib.sha256(payload).hexdigest(),
                "mediaType": "image/png",
                "sizeBytes": len(payload),
                "sensitivity": "private",
                "state": "complete",
            },
        }

    factory = _factory(
        tmp_path,
        _ModelModule(_binding(input_modalities=("text", "image"))),
        [],
        attachments=_Attachments(),
    )
    command = replace(
        _command(_config()),
        input_blocks=(
            {"type": "text", "text": "compare", "format": "markdown", "references": []},
            image_block("art_one", first),
            image_block("art_two", second),
        ),
    )

    with pytest.raises(RunPreparationFailure) as caught:
        await factory.prepare_root(command, _state(), _cancellation(), None)

    assert caught.value.error_code.value == "input.image_invalid"
    assert caught.value.details == {"reason": "attachment_corrupt", "imageIndex": 2}


@pytest.mark.asyncio
async def test_current_text_input_rejects_an_orphaned_durable_attachment_claim(tmp_path: Path) -> None:
    class _Attachments:
        calls = 0

        async def materialize_claimed_submission(
            self,
            session_id: str,
            turn_id: str,
            claims: tuple[AttachmentClaim, ...],
            cancellation: CancellationToken,
        ) -> object:
            assert session_id == "ses_test"
            assert turn_id == "turn_test"
            assert claims == ()
            cancellation.checkpoint()
            self.calls += 1
            raise AttachmentError("claim_conflict", "text metadata omits a durable attachment claim")

    attachments = _Attachments()
    factory = _factory(
        tmp_path,
        _ModelModule(_binding(input_modalities=("text", "image"))),
        [],
        attachments=attachments,
    )

    with pytest.raises(RunPreparationFailure) as caught:
        await factory.prepare_root(_command(_config()), _state(), _cancellation(), None)

    assert caught.value.error_code.value == "input.image_invalid"
    assert caught.value.details == {"reason": "claim_conflict"}
    assert attachments.calls == 1
