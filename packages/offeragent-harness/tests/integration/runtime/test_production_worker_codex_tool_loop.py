from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any, cast

import httpx
import pytest

from offeragent_harness.config import HarnessConfig, ModelProvider, ModelSettings
from offeragent_harness.ports import ApplicationCommandContext, CancellationToken
from offeragent_harness.ports.model import ModelGateway
from offeragent_harness.ports.processes import SupervisedProcessRequest, SupervisedProcessResult
from offeragent_harness.ports.worker_runtime import WorkerBootstrap
from offeragent_harness.protocol._base import WireModel
from offeragent_harness.providers import (
    CODEX_SUBSCRIPTION_BASE_URL,
    CODEX_SUBSCRIPTION_MODELS_ENDPOINT,
    CODEX_SUBSCRIPTION_PROVIDER_ID,
    CodexCatalogHttpRequest,
    CodexCatalogHttpResponse,
    ModelCredentialLease,
    compose_model_gateway,
)
from offeragent_harness.runtime.production_worker_composition import (
    ProductionWorkerApplication,
    ProductionWorkerCompositionRoot,
    ProductionWorkerOverrides,
)
from offeragent_harness.runtime.worker_entrypoint import WorkerEntrypoint
from offeragent_harness.sessions import RunStatus
from offeragent_harness.testing import DeterministicIdGenerator, ManualCancellationToken, ManualClock
from offeragent_harness.vault import content_hash
from offeragent_harness.workspace import identify_workspace_root
from offeragent_harness.workspace.portable_config import ensure_portable_workspace_config
from offeragent_harness.workspace.runtime_identity import workspace_database_identity

pytestmark = pytest.mark.skipif(os.name != "nt", reason="production Worker composition requires Windows")

NOW = datetime(2026, 7, 18, 8, 0, tzinfo=timezone.utc)
WORKSPACE_INSTANCE_ID = "wsi_bbeea466-83b2-4a13-b5c0-84578a37e71a"
MODEL_ID = "gpt-fused-production-fixture"
ACCOUNT_ID = "account-fused-production"
ACCOUNT_FINGERPRINT = f"fingerprint-{ACCOUNT_ID}"
ACCOUNT_BINDING = f"sha256:{hashlib.sha256(ACCOUNT_FINGERPRINT.encode()).hexdigest()}"
FINAL_ANSWER = "The Python production Agent Loop completed the plugin-backed request."
AGENT_CONTRACT = "# OfferAgent\n\nUse current Vault evidence and complete plugin reads before answering.\n"


class _UnusedSecrets:
    def consume(self, *args: object, **kwargs: object) -> object:
        del args, kwargs
        raise AssertionError("Codex subscription inference must use its account-bound credential lease")


class _CodexCredentials:
    def __init__(self) -> None:
        self.lease_count = 0

    @contextmanager
    def lease(self) -> Iterator[ModelCredentialLease]:
        self.lease_count += 1
        token = bytearray(b"fixture-codex-access-token")
        material = memoryview(token)
        try:
            yield ModelCredentialLease(
                material=material,
                headers=MappingProxyType(
                    {
                        "ChatGPT-Account-ID": ACCOUNT_ID,
                        "originator": "codex_cli_rs",
                        "User-Agent": "codex_cli_rs/test (OfferAgent production integration)",
                    }
                ),
                credential_fingerprint="credential-fused-production",
                account_fingerprint=ACCOUNT_FINGERPRINT,
            )
        finally:
            material.release()
            token[:] = b"\0" * len(token)


class _CodexCatalog:
    def __init__(self, *, supports_hosted_search: bool = False) -> None:
        self.requests: list[CodexCatalogHttpRequest] = []
        self.supports_hosted_search = supports_hosted_search

    def get(self, request: CodexCatalogHttpRequest) -> CodexCatalogHttpResponse:
        self.requests.append(request)
        body = {
            "models": [
                {
                    "slug": MODEL_ID,
                    "display_name": "Fused Production Fixture",
                    "description": "Deterministic Codex production-composition integration fixture",
                    "visibility": "list",
                    "input_modalities": ["text"],
                    "supports_image_detail_original": False,
                    "supports_search_tool": self.supports_hosted_search,
                    "web_search_tool_type": "text" if self.supports_hosted_search else None,
                    "context_window": 128000,
                    "max_context_window": 128000,
                    "effective_context_window_percent": 95,
                    "additional_speed_tiers": [],
                    "service_tiers": [],
                    "default_service_tier": None,
                }
            ]
        }
        return CodexCatalogHttpResponse(
            status=200,
            headers={"Content-Type": "application/json"},
            body=json.dumps(body, separators=(",", ":")).encode(),
        )


class _NoopProcessSupervisor:
    def __init__(self) -> None:
        self.shutdown_count = 0

    async def execute(
        self,
        request: SupervisedProcessRequest,
        cancellation: CancellationToken,
    ) -> SupervisedProcessResult:
        del request, cancellation
        raise AssertionError("the model/plugin completion integration must not start a process")

    async def shutdown(self) -> None:
        self.shutdown_count += 1


def _config() -> HarnessConfig:
    return HarnessConfig.model_validate(
        {
            "model": {
                "provider": CODEX_SUBSCRIPTION_PROVIDER_ID,
                "model": MODEL_ID,
                "account_binding": ACCOUNT_BINDING,
            },
            "policy": {"read_only": True, "workspace_trusted": True},
        }
    )


def _powershell_executable() -> Path:
    executable = shutil.which("powershell.exe")
    if executable is None:
        pytest.skip("Windows PowerShell is required for production Worker composition")
    return Path(executable).resolve(strict=True)


def _structured_response(response_id: str, value: Mapping[str, object]) -> bytes:
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    events: tuple[Mapping[str, object], ...] = (
        {"type": "response.created", "sequence_number": 0, "response": {"id": response_id}},
        {
            "type": "response.output_text.delta",
            "sequence_number": 1,
            "output_index": 0,
            "content_index": 0,
            "delta": text,
        },
        {
            "type": "response.output_text.done",
            "sequence_number": 2,
            "output_index": 0,
            "content_index": 0,
            "text": text,
        },
        {
            "type": "response.completed",
            "sequence_number": 3,
            "response": {
                "status": "completed",
                "output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}],
                "usage": {
                    "input_tokens": 32,
                    "output_tokens": 16,
                    "input_tokens_details": {"cached_tokens": 0},
                    "output_tokens_details": {"reasoning_tokens": 0},
                },
            },
        },
    )
    return b"".join(
        f"event: {event['type']}\ndata: {json.dumps(event, separators=(',', ':'))}\n\n".encode() for event in events
    )


def _structured_hosted_search_response(response_id: str, value: Mapping[str, object]) -> bytes:
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    citation = {
        "type": "url_citation",
        "start_index": 0,
        "end_index": 5,
        "url": "https://example.com/interview/research",
        "title": "Interview research source",
    }
    search_item = {
        "id": "ws_production",
        "type": "web_search_call",
        "status": "completed",
        "action": {
            "type": "search",
            "queries": ["OfferAgent interview research"],
            "sources": [{"type": "url", "url": citation["url"]}],
        },
    }
    message_item = {
        "id": "msg_production",
        "type": "message",
        "status": "completed",
        "content": [{"type": "output_text", "text": text, "annotations": [citation]}],
    }
    events: tuple[Mapping[str, object], ...] = (
        {"type": "response.created", "sequence_number": 0, "response": {"id": response_id}},
        {
            "type": "response.output_item.added",
            "sequence_number": 1,
            "output_index": 0,
            "item": {"id": "ws_production", "type": "web_search_call", "status": "in_progress"},
        },
        {
            "type": "response.web_search_call.in_progress",
            "sequence_number": 2,
            "output_index": 0,
            "item_id": "ws_production",
        },
        {
            "type": "response.web_search_call.searching",
            "sequence_number": 3,
            "output_index": 0,
            "item_id": "ws_production",
        },
        {
            "type": "response.web_search_call.completed",
            "sequence_number": 4,
            "output_index": 0,
            "item_id": "ws_production",
        },
        {
            "type": "response.output_item.done",
            "sequence_number": 5,
            "output_index": 0,
            "item": search_item,
        },
        {
            "type": "response.output_text.delta",
            "sequence_number": 6,
            "item_id": "msg_production",
            "output_index": 1,
            "content_index": 0,
            "delta": text,
        },
        {
            "type": "response.output_text.annotation.added",
            "sequence_number": 7,
            "item_id": "msg_production",
            "output_index": 1,
            "content_index": 0,
            "annotation_index": 0,
            "annotation": citation,
        },
        {
            "type": "response.output_text.done",
            "sequence_number": 8,
            "item_id": "msg_production",
            "output_index": 1,
            "content_index": 0,
            "text": text,
        },
        {
            "type": "response.output_item.done",
            "sequence_number": 9,
            "output_index": 1,
            "item": message_item,
        },
        {
            "type": "response.completed",
            "sequence_number": 10,
            "response": {
                "status": "completed",
                "output": [search_item, message_item],
                "usage": {
                    "input_tokens": 32,
                    "output_tokens": 16,
                    "input_tokens_details": {"cached_tokens": 0},
                    "output_tokens_details": {"reasoning_tokens": 0},
                },
            },
        },
    )
    return b"".join(
        f"event: {event['type']}\ndata: {json.dumps(event, separators=(',', ':'))}\n\n".encode() for event in events
    )


async def _dispatch(
    application: ProductionWorkerApplication,
    method: str,
    params: dict[str, object],
    *,
    context: ApplicationCommandContext,
) -> dict[str, Any]:
    result = await application.dispatcher.dispatch(
        method,
        params,
        ManualCancellationToken(),
        context=context,
    )
    if isinstance(result, WireModel):
        return result.to_wire()
    if not isinstance(result, dict):
        raise TypeError(f"unexpected {method} result: {type(result).__name__}")
    return cast(dict[str, Any], result)


async def _wait_for_plugin_call(
    application: ProductionWorkerApplication,
    run_id: str,
    tool_name: str,
) -> Mapping[str, object]:
    for _ in range(10_000):
        events = await application.harness.replay_events(run_id)
        for event in reversed(events):
            if event.event_type != "tool.started":
                continue
            payload = cast(Mapping[str, object], event.payload["payload"])
            call = cast(Mapping[str, object], payload["call"])
            if call["name"] == tool_name:
                return call
        run = await application.harness.get_run(run_id)
        if run.status.is_terminal:
            raise AssertionError(f"Run terminated as {run.status.value} before {tool_name}")
        await asyncio.sleep(0)
    raise AssertionError(f"Run did not request {tool_name}")


async def _wait_terminal(application: ProductionWorkerApplication, run_id: str) -> RunStatus:
    for _ in range(10_000):
        status = (await application.harness.get_run(run_id)).status
        if status.is_terminal:
            return status
        await asyncio.sleep(0)
    raise AssertionError("Run did not reach a terminal state")


@pytest.mark.asyncio
@pytest.mark.parametrize("supports_hosted_search", [False, True])
async def test_production_worker_codex_loop_round_trips_one_plugin_tool_without_local_provider(
    tmp_path: Path,
    supports_hosted_search: bool,
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    portable = ensure_portable_workspace_config(
        vault,
        new_uuid=lambda: uuid.UUID("7f43cfcc-6350-44e7-a26d-fc2d1d761ae7"),
    )
    (vault / "agent.md").write_text(AGENT_CONTRACT, encoding="utf-8")
    state_directory = tmp_path / "state"
    skill_runtime_root = tmp_path / "runtime"
    skill_runtime_root.mkdir()
    skill_user_home = tmp_path / "home"
    skill_user_home.mkdir()

    response_requests: list[httpx.Request] = []
    response_bodies: list[dict[str, Any]] = []

    def responses_handler(request: httpx.Request) -> httpx.Response:
        response_requests.append(request)
        body = json.loads(request.content)
        assert isinstance(body, dict)
        response_bodies.append(body)
        if len(response_bodies) == 1:
            output: Mapping[str, object] = {
                "requiresWriteOutcome": False,
                "calls": [
                    {
                        "name": "agent_contract.read",
                        "version": "1",
                        "arguments": {},
                        "reason": "Load the current Vault Agent Contract before answering.",
                    }
                ],
                "finalResponse": None,
            }
        elif len(response_bodies) == 2:
            output = {
                "requiresWriteOutcome": False,
                "calls": [],
                "finalResponse": FINAL_ANSWER,
            }
        else:
            raise AssertionError("the deterministic Agent Loop must complete in two model rounds")
        response_content = (
            _structured_hosted_search_response(f"resp_fused_{len(response_bodies)}", output)
            if supports_hosted_search and len(response_bodies) == 2
            else _structured_response(f"resp_fused_{len(response_bodies)}", output)
        )
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=response_content,
            request=request,
        )

    credentials = _CodexCredentials()
    catalog = _CodexCatalog(supports_hosted_search=supports_hosted_search)
    gateway_settings: list[ModelSettings] = []
    transport = httpx.MockTransport(responses_handler)

    def gateway_factory(settings: ModelSettings) -> ModelGateway:
        gateway_settings.append(settings)
        return compose_model_gateway(
            settings,
            secret_scope_id=f"workspace:{portable.portable_workspace_id}",
            secrets=_UnusedSecrets(),  # type: ignore[arg-type]
            network_enabled=True,
            responses_transport=transport,
            codex_credential_source=credentials,
        )

    process_supervisor = _NoopProcessSupervisor()
    composition = ProductionWorkerCompositionRoot(
        canonical_root_identity=identify_workspace_root(vault).identity_hash,
        database_identity=workspace_database_identity(WORKSPACE_INSTANCE_ID),
        runtime_version="1.2.3",
        build_commit="abcdef0",
        overrides=ProductionWorkerOverrides(
            clock=ManualClock(NOW),
            ids=DeterministicIdGenerator(start=1_000),
            model_gateway_factory=gateway_factory,
            codex_credential_source=credentials,
            codex_catalog_http=catalog,
            runtime_config=_config(),
            powershell_path=_powershell_executable(),
            skill_runtime_root=skill_runtime_root,
            skill_user_home=skill_user_home,
            process_supervisor=process_supervisor,
        ),
    )
    entrypoint = WorkerEntrypoint(composition)
    application = cast(
        ProductionWorkerApplication,
        await entrypoint.start(WorkerBootstrap(WORKSPACE_INSTANCE_ID, vault, state_directory)),
    )
    web_context = ApplicationCommandContext(
        transport="loopback-http",
        client_id="web-fused-production-test",
        peer="127.0.0.1",
    )
    try:
        created = await _dispatch(
            application,
            "session/create",
            {"title": "Fused production Codex loop", "clientRequestId": "req_fused_production_session"},
            context=web_context,
        )
        started = await _dispatch(
            application,
            "turn/start",
            {
                "sessionId": created["session"]["sessionId"],
                "turnId": "turn_fused_production",
                "idempotencyKey": "turn-fused-production",
                "input": [
                    {
                        "type": "text",
                        "text": "Read the Vault Agent Contract, then confirm the production loop completed.",
                        "format": "markdown",
                        "references": [],
                    }
                ],
                "runConfig": {
                    "provider": CODEX_SUBSCRIPTION_PROVIDER_ID,
                    "model": MODEL_ID,
                    "reasoningEffort": "medium",
                    "permissionMode": "read-only",
                },
            },
            context=web_context,
        )
        run_id = cast(str, started["runId"])
        call = await _wait_for_plugin_call(application, run_id, "agent_contract.read")
        contract_hash = content_hash(AGENT_CONTRACT.encode())
        completion = await _dispatch(
            application,
            "plugin-tools/complete",
            {
                "workspaceId": call["workspaceId"],
                "runId": call["runId"],
                "definitionFingerprint": call["definitionFingerprint"],
                "argsHash": call["argsHash"],
                "idempotencyKey": call["idempotencyKey"],
                "result": {
                    "toolCallId": call["toolCallId"],
                    "status": "succeeded",
                    "summary": "Read the current Vault Agent Contract.",
                    "data": {
                        "path": "agent.md",
                        "content": AGENT_CONTRACT,
                        "contentHash": contract_hash,
                    },
                    "sourceRefs": [
                        {
                            "type": "vault",
                            "file": {
                                "workspaceId": call["workspaceId"],
                                "path": "agent.md",
                                "contentHash": contract_hash,
                            },
                            "freshness": "fresh",
                        }
                    ],
                },
            },
            context=ApplicationCommandContext(transport="stdio", client_id="obsidian-plugin"),
        )

        assert completion["replayed"] is False
        assert await _wait_terminal(application, run_id) is RunStatus.COMPLETED
        assert (await application.harness.get_run_state(run_id)).assistant_text == FINAL_ANSWER

        events = await application.harness.replay_events(run_id)
        event_types = [event.event_type for event in events]
        assert event_types.index("tool.started") < event_types.index("tool.completed")
        assert event_types.index("tool.completed") < event_types.index("turn.completed")
        if supports_hosted_search:
            reference_event = next(event for event in events if event.event_type == "references.updated")
            reference_payload = cast(Mapping[str, Any], reference_event.payload["payload"])
            references = cast(list[Mapping[str, Any]], reference_payload["references"])
            model_attempts = [event for event in events if event.event_type == "model.attempt"]
            final_attempt = cast(Mapping[str, Any], model_attempts[-1].payload["payload"])
            assert len(references) == 1
            reference = references[0]
            assert reference["type"] == "hostedWeb"
            assert reference["url"] == "https://example.com/interview/research"
            assert reference["title"] == "Interview research source"
            assert reference["providerId"] == CODEX_SUBSCRIPTION_PROVIDER_ID
            assert reference["model"] == MODEL_ID
            assert reference["modelRequestId"] == final_attempt["requestId"]
            assert reference["freshness"] == "unknown"
            assert "contentHash" not in reference
        else:
            assert "references.updated" not in event_types

        assert len(catalog.requests) == 1
        assert catalog.requests[0].endpoint == CODEX_SUBSCRIPTION_MODELS_ENDPOINT
        assert len(gateway_settings) == 1
        assert gateway_settings[0].provider is ModelProvider.CODEX_SUBSCRIPTION_EXPERIMENTAL
        assert gateway_settings[0].model == MODEL_ID
        assert gateway_settings[0].account_binding == ACCOUNT_BINDING

        assert credentials.lease_count == 3
        assert len(response_requests) == 2
        assert all(str(request.url) == f"{CODEX_SUBSCRIPTION_BASE_URL}/responses" for request in response_requests)
        assert all(
            request.headers["authorization"] == "Bearer fixture-codex-access-token" for request in response_requests
        )
        assert all(request.headers["chatgpt-account-id"] == ACCOUNT_ID for request in response_requests)
        assert all(body["model"] == MODEL_ID and body["store"] is False for body in response_bodies)
        expected_tools = [{"type": "web_search"}] if supports_hosted_search else []
        assert all(body["tools"] == expected_tools for body in response_bodies)
        assert all(("include" in body) is supports_hosted_search for body in response_bodies)
        first_input = json.dumps(response_bodies[0]["input"], ensure_ascii=False)
        second_messages = cast(list[dict[str, Any]], response_bodies[1]["input"])
        assert "Read the Vault Agent Contract" in first_input
        tool_blocks = [
            block
            for message in second_messages
            for block in cast(list[dict[str, Any]], message["content"])
            if "structured block tool_result" in cast(str, block.get("text", ""))
        ]
        assert len(tool_blocks) == 1
        encoded_tool_result = cast(str, tool_blocks[0]["text"])
        tool_result = json.loads(encoded_tool_result.split("\n", 2)[2])
        assert tool_result["toolCallId"] == call["toolCallId"]
        assert tool_result["status"] == "succeeded"
        assert tool_result["data"] == {
            "path": "agent.md",
            "content": AGENT_CONTRACT,
            "contentHash": contract_hash,
        }
    finally:
        await entrypoint.shutdown()

    assert process_supervisor.shutdown_count == 1
