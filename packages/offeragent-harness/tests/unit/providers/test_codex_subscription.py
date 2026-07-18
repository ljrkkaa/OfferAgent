from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from types import MappingProxyType

import httpx
import pytest
from jsonschema import Draft202012Validator

from offeragent_harness.agent.model_planner import AgentStepCatalog
from offeragent_harness.config import ModelProvider, ModelSettings
from offeragent_harness.models import (
    ModelContentBlock,
    ModelEvent,
    ModelEventKind,
    ModelMessage,
    ModelOutputMode,
    ModelPurpose,
    ModelRequest,
    ModelRole,
    TraceContext,
)
from offeragent_harness.models.json_types import thaw_json
from offeragent_harness.providers import (
    CODEX_SUBSCRIPTION_BASE_URL,
    CodexSubscriptionProvider,
    ModelCredentialLease,
    ModelCredentialSourceError,
    ResponsesProviderKind,
    ResponsesProviderSelection,
    StaticModelEndpointPolicy,
    build_responses_provider,
    compose_model_gateway,
)
from offeragent_harness.runtime.codex_credentials import CodexFileCredentialSource
from offeragent_harness.runtime.plugin_tools import plugin_tool_definitions
from offeragent_harness.subagents.tools import subagent_tool_definitions
from offeragent_harness.testing.cancellation import ManualCancellationToken
from offeragent_harness.workspace import code_tool_definitions

_NOW = 2_000_000_000.0


def _account_binding(account: str) -> str:
    fingerprint = f"account-{account}"
    return f"sha256:{hashlib.sha256(fingerprint.encode()).hexdigest()}"


class _UnusedSecrets:
    def consume(self, *args: object, **kwargs: object) -> object:
        raise AssertionError("Codex subscription must not consume a SecretStore credential")


class _RotatingSource:
    def __init__(self, credentials: list[tuple[bytes, str]]) -> None:
        self.credentials = credentials
        self.leases = 0

    @contextmanager
    def lease(self) -> Iterator[ModelCredentialLease]:
        index = min(self.leases, len(self.credentials) - 1)
        token_bytes, account = self.credentials[index]
        self.leases += 1
        token = bytearray(token_bytes)
        view = memoryview(token)
        try:
            yield ModelCredentialLease(
                material=view,
                headers=MappingProxyType(
                    {
                        "ChatGPT-Account-ID": account,
                        "originator": "codex_cli_rs",
                        "User-Agent": "codex_cli_rs/0.0.0 (OfferAgent test)",
                    }
                ),
                credential_fingerprint=f"credential-{token_bytes.decode()}",
                account_fingerprint=f"account-{account}",
            )
        finally:
            view.release()
            for offset in range(len(token)):
                token[offset] = 0


def _jwt(expiry: float = _NOW + 3600) -> str:
    def encode(value: Mapping[str, object]) -> str:
        raw = json.dumps(value, separators=(",", ":"), sort_keys=True).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    return f"{encode({'alg': 'none'})}.{encode({'exp': expiry})}.signature"


def _write_auth(path: Path, *, token: str | None = None, account: str = "account-test") -> None:
    path.write_text(
        json.dumps(
            {
                "auth_mode": "chatgpt",
                "OPENAI_API_KEY": None,
                "tokens": {
                    "access_token": token or _jwt(),
                    "account_id": account,
                    "id_token": "unused",
                    "refresh_token": "unused",
                },
            },
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )


def _request(*, blocks: tuple[ModelContentBlock, ...] | None = None) -> ModelRequest:
    return ModelRequest(
        request_id="req_subscription",
        model="gpt-5.6-luna",
        purpose=ModelPurpose.RESPONDING,
        messages=(ModelMessage(ModelRole.USER, blocks or (ModelContentBlock.text("Reply OK"),)),),
        output_mode=ModelOutputMode.TEXT,
        output_schema=None,
        max_output_tokens=16,
        reasoning_effort="medium",
        temperature=0.0,
        seed=None,
        trace_context=TraceContext("trace_subscription"),
    )


def _completed(text: str = "OK") -> bytes:
    events = (
        {"type": "response.created", "sequence_number": 0, "response": {"id": "resp_subscription"}},
        {
            "type": "response.output_text.delta",
            "sequence_number": 1,
            "output_index": 0,
            "content_index": 0,
            "delta": text,
        },
        {
            "type": "response.completed",
            "sequence_number": 2,
            "response": {
                "status": "completed",
                "output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}],
                "usage": {
                    "input_tokens": 4,
                    "output_tokens": 1,
                    "input_tokens_details": {"cached_tokens": 0},
                    "output_tokens_details": {"reasoning_tokens": 0},
                },
            },
        },
    )
    return b"".join(
        f"event: {event['type']}\ndata: {json.dumps(event, separators=(',', ':'))}\n\n".encode() for event in events
    )


async def _collect(gateway: CodexSubscriptionProvider) -> tuple[ModelEvent, ...]:
    return tuple([event async for event in gateway.stream(_request(), ManualCancellationToken())])


def _provider(
    source: _RotatingSource | CodexFileCredentialSource,
    handler: Callable[[httpx.Request], httpx.Response],
) -> CodexSubscriptionProvider:
    selection = ResponsesProviderSelection(
        kind=ResponsesProviderKind.CODEX_SUBSCRIPTION_EXPERIMENTAL,
        secret_scope_id="workspace:wsi_test",
        credential_handle=None,
        service_tier="default",
    )
    provider = build_responses_provider(
        selection,
        secrets=_UnusedSecrets(),  # type: ignore[arg-type]
        endpoint_policy=StaticModelEndpointPolicy(frozenset({f"{CODEX_SUBSCRIPTION_BASE_URL}/responses"})),
        transport=httpx.MockTransport(handler),
        credential_source=source,
    )
    assert isinstance(provider, CodexSubscriptionProvider)
    return provider


def test_file_source_is_read_only_bounded_and_zeroes_the_lease(tmp_path: Path) -> None:
    auth = tmp_path / "auth.json"
    _write_auth(auth)
    before = auth.read_bytes()
    before_stat = auth.stat()
    source = CodexFileCredentialSource(auth, now=lambda: _NOW)

    with source.lease() as lease:
        view = lease.material
        assert len(view) > 0
        assert set(lease.headers) == {"ChatGPT-Account-ID", "originator", "User-Agent"}
        assert len(lease.credential_fingerprint) == 64
        assert len(lease.account_fingerprint) == 64

    with pytest.raises(ValueError, match="released memoryview"):
        _ = view[0]
    assert auth.read_bytes() == before
    after_stat = auth.stat()
    assert after_stat.st_size == before_stat.st_size
    assert after_stat.st_mtime_ns == before_stat.st_mtime_ns


def test_file_source_fails_closed_for_expired_invalid_and_hardlinked_auth(tmp_path: Path) -> None:
    expired = tmp_path / "expired.json"
    _write_auth(expired, token=_jwt(_NOW - 1))
    with pytest.raises(ModelCredentialSourceError):
        with CodexFileCredentialSource(expired, now=lambda: _NOW).lease():
            pass

    invalid = tmp_path / "invalid.json"
    invalid.write_text('{"auth_mode":"chatgpt","auth_mode":"chatgpt"}', encoding="utf-8")
    with pytest.raises(ModelCredentialSourceError):
        with CodexFileCredentialSource(invalid, now=lambda: _NOW).lease():
            pass

    original = tmp_path / "original.json"
    linked = tmp_path / "linked.json"
    _write_auth(original)
    linked.hardlink_to(original)
    with pytest.raises(ModelCredentialSourceError):
        with CodexFileCredentialSource(original, now=lambda: _NOW).lease():
            pass


@pytest.mark.asyncio
async def test_subscription_uses_fixed_endpoint_headers_and_dialect_without_temperature() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, content=_completed(), request=request)

    source = _RotatingSource([(b"access-one", "account-one")])
    events = await _collect(_provider(source, handler))
    assert events[-1].kind is ModelEventKind.COMPLETED
    assert captured["url"] == f"{CODEX_SUBSCRIPTION_BASE_URL}/responses"
    headers = captured["headers"]
    assert isinstance(headers, dict)
    assert headers["authorization"] == "Bearer access-one"
    assert headers["chatgpt-account-id"] == "account-one"
    assert headers["originator"] == "codex_cli_rs"
    body = captured["body"]
    assert isinstance(body, dict)
    assert body["model"] == "gpt-5.6-luna"
    assert body["reasoning"] == {"effort": "medium", "summary": "auto"}
    assert body["service_tier"] == "default"
    assert body["store"] is False and body["tools"] == [] and body["parallel_tool_calls"] is False
    assert "max_output_tokens" not in body
    assert "temperature" not in body


@pytest.mark.asyncio
async def test_subscription_projects_production_tool_plan_to_supported_strict_schema_subset() -> None:
    captured: dict[str, object] = {}
    output = '{"requiresWriteOutcome":false,"calls":[],"finalResponse":"no tools needed"}'

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, content=_completed(output), request=request)

    catalog = AgentStepCatalog(
        (*code_tool_definitions(), *subagent_tool_definitions()),
        max_calls=32,
    )
    request = ModelRequest(
        request_id="req_subscription_tool_plan",
        model="gpt-5.6-luna",
        purpose=ModelPurpose.PLANNING,
        messages=(ModelMessage(ModelRole.USER, (ModelContentBlock.text("Do not call tools"),)),),
        output_mode=ModelOutputMode.JSON,
        output_schema=thaw_json(catalog.schema),
        max_output_tokens=1_024,
        reasoning_effort="medium",
        temperature=0.0,
        seed=None,
        trace_context=TraceContext("trace_subscription_tool_plan"),
    )
    gateway = _provider(_RotatingSource([(b"access-one", "account-one")]), handler)
    events = tuple([event async for event in gateway.stream(request, ManualCancellationToken())])

    structured = [event for event in events if event.kind is ModelEventKind.STRUCTURED_OUTPUT]
    assert len(structured) == 1
    assert thaw_json(structured[0].data) == {
        "requiresWriteOutcome": False,
        "calls": [],
        "finalResponse": "no tools needed",
    }
    body = captured["body"]
    assert isinstance(body, dict)
    schema = body["text"]["format"]["schema"]
    forbidden = {
        "$id",
        "$schema",
        "allOf",
        "maxProperties",
        "oneOf",
        "uniqueItems",
    }

    def walk(value: object) -> Iterator[Mapping[str, object]]:
        if isinstance(value, dict):
            yield value
            for child in value.values():
                yield from walk(child)
        elif isinstance(value, list):
            for child in value:
                yield from walk(child)

    mappings = tuple(walk(schema))
    assert not any(forbidden.intersection(item) for item in mappings)
    strict_objects = [item for item in mappings if item.get("type") == "object"]
    assert strict_objects
    assert all(item.get("additionalProperties") is False for item in strict_objects)
    for item in strict_objects:
        required = item.get("required")
        properties = item.get("properties")
        assert isinstance(required, list)
        assert isinstance(properties, dict)
        assert set(required) == set(properties)

    calls = schema["properties"]["calls"]
    variants = calls["items"]["anyOf"]
    spawn = next(item for item in variants if item["properties"]["name"]["const"] == "agent.spawn")
    definition_key = spawn["properties"]["arguments"]["$ref"].rsplit("/", 1)[-1]
    spawn_arguments = schema["$defs"][definition_key]
    for field in ("toolVersions", "toolConstraints"):
        assert spawn_arguments["properties"][field] == {
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        }


@pytest.mark.asyncio
async def test_subscription_projection_can_express_an_honest_pure_screenshot_catalog_search() -> None:
    captured: dict[str, object] = {}
    digest = "sha256:" + "a" * 64
    output_value = {
        "requiresWriteOutcome": False,
        "calls": [
            {
                "name": "interview_catalog.search",
                "version": "1",
                "arguments": {
                    "sourceUrls": [],
                    "orderedImageContentHashes": [digest],
                    "company": "unknown",
                    "role": "unknown",
                    "questionTerms": ["Node.js event loop"],
                },
                "reason": "Find duplicate source events and semantically related questions.",
            }
        ],
        "finalResponse": None,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, content=_completed(json.dumps(output_value)), request=request)

    catalog = AgentStepCatalog(plugin_tool_definitions(), max_calls=32)
    request = ModelRequest(
        request_id="req_subscription_screenshot_catalog",
        model="gpt-5.6-luna",
        purpose=ModelPurpose.PLANNING,
        messages=(ModelMessage(ModelRole.USER, (ModelContentBlock.text("Catalog these screenshots"),)),),
        output_mode=ModelOutputMode.JSON,
        output_schema=thaw_json(catalog.schema),
        max_output_tokens=1_024,
        reasoning_effort="medium",
        temperature=0.0,
        seed=None,
        trace_context=TraceContext("trace_subscription_screenshot_catalog"),
    )
    gateway = _provider(_RotatingSource([(b"access-one", "account-one")]), handler)
    events = tuple([event async for event in gateway.stream(request, ManualCancellationToken())])

    assert any(event.kind is ModelEventKind.STRUCTURED_OUTPUT for event in events)
    assert catalog.violations(output_value) == ()
    body = captured["body"]
    assert isinstance(body, dict)
    schema = body["text"]["format"]["schema"]
    Draft202012Validator(schema).validate(output_value)
    variants = schema["properties"]["calls"]["items"]["anyOf"]
    catalog_call = next(item for item in variants if item["properties"]["name"]["const"] == "interview_catalog.search")
    definition_key = catalog_call["properties"]["arguments"]["$ref"].rsplit("/", 1)[-1]
    catalog_arguments = schema["$defs"][definition_key]
    assert set(catalog_arguments["properties"]) == {
        "sourceUrls",
        "orderedImageContentHashes",
        "company",
        "role",
        "questionTerms",
    }
    assert set(catalog_arguments["required"]) == set(catalog_arguments["properties"])


@pytest.mark.asyncio
async def test_401_reloads_only_a_changed_token_for_the_same_account() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if request.headers["authorization"] == "Bearer access-one":
            return httpx.Response(401, json={"error": {"code": "token_expired"}}, request=request)
        return httpx.Response(200, content=_completed(), request=request)

    source = _RotatingSource([(b"access-one", "account-one"), (b"access-two", "account-one")])
    events = await _collect(_provider(source, handler))
    assert calls == 2 and source.leases == 2
    assert events[-1].kind is ModelEventKind.COMPLETED


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("credentials", "expected_code"),
    [
        ([(b"access-one", "account-one")], "auth_required"),
        ([(b"access-one", "account-one"), (b"access-two", "account-two")], "auth_account_changed"),
    ],
)
async def test_401_without_safe_same_account_rotation_fails_closed(
    credentials: list[tuple[bytes, str]],
    expected_code: str,
) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(401, json={"error": {"code": "token_expired"}}, request=request)

    events = await _collect(_provider(_RotatingSource(credentials), handler))
    assert calls == 1
    assert events[-1].kind is ModelEventKind.ERROR
    assert events[-1].error is not None and events[-1].error.code == expected_code


@pytest.mark.asyncio
async def test_all_production_structured_blocks_encode_deterministically_and_unknown_blocks_fail_closed() -> None:
    captured: dict[str, object] = {}
    kinds = (
        "compaction_records",
        "context",
        "context_reference",
        "invalid_structured_output",
        "run_control_message",
        "run_snapshot",
        "tool_result",
        "tool_result_reference",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, content=_completed(), request=request)

    gateway = _provider(_RotatingSource([(b"access-one", "account-one")]), handler)
    request = _request(blocks=tuple(ModelContentBlock(kind, {"z": 1, "a": kind}) for kind in kinds))
    events = tuple([event async for event in gateway.stream(request, ManualCancellationToken())])
    assert events[-1].kind is ModelEventKind.COMPLETED
    body = captured["body"]
    assert isinstance(body, dict)
    texts = [block["text"] for block in body["input"][0]["content"]]
    assert all(text.endswith(f'{{"a":"{kind}","z":1}}') for kind, text in zip(kinds, texts, strict=True))

    invalid = _request(blocks=(ModelContentBlock("future_unknown", {"value": 1}),))
    rejected = tuple([event async for event in gateway.stream(invalid, ManualCancellationToken())])
    assert rejected[-1].kind is ModelEventKind.ERROR
    assert rejected[-1].error is not None and rejected[-1].error.code == "provider_configuration"


def test_subscription_config_rejects_override_credentials_temperature_and_non_loopback_proxy() -> None:
    with pytest.raises(ValueError, match="temperature"):
        ModelSettings(provider=ModelProvider.CODEX_SUBSCRIPTION_EXPERIMENTAL, temperature=0.1)
    with pytest.raises(ValueError, match="cannot be overridden"):
        ModelSettings(
            provider=ModelProvider.CODEX_SUBSCRIPTION_EXPERIMENTAL,
            credential_handle="secret:v1:0123456789abcdef0123456789abcdef",
        )
    with pytest.raises(ValueError, match="loopback"):
        ModelSettings(
            provider=ModelProvider.CODEX_SUBSCRIPTION_EXPERIMENTAL,
            proxy_url="http://192.168.1.2:7896",
        )
    settings = ModelSettings(
        provider=ModelProvider.CODEX_SUBSCRIPTION_EXPERIMENTAL,
        model="gpt-5.6-luna",
        reasoning_effort="medium",
        proxy_url="http://127.0.0.1:7896",
    )
    assert settings.proxy_url == "http://127.0.0.1:7896"


def test_composition_preserves_harness_as_the_only_runtime() -> None:
    settings = ModelSettings(
        provider=ModelProvider.CODEX_SUBSCRIPTION_EXPERIMENTAL,
        model="gpt-5.6-luna",
        account_binding=_account_binding("account-one"),
        reasoning_effort="medium",
    )
    gateway = compose_model_gateway(
        settings,
        secret_scope_id="workspace:wsi_test",
        secrets=_UnusedSecrets(),  # type: ignore[arg-type]
        network_enabled=True,
        responses_transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=_completed(), request=request)
        ),
        codex_credential_source=_RotatingSource([(b"access-one", "account-one")]),
    )
    assert isinstance(gateway, CodexSubscriptionProvider)


@pytest.mark.asyncio
async def test_composed_inference_rejects_an_account_switch_before_http() -> None:
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(200, content=_completed(), request=request)

    settings = ModelSettings(
        provider=ModelProvider.CODEX_SUBSCRIPTION_EXPERIMENTAL,
        model="gpt-5.6-luna",
        account_binding=_account_binding("selected-account"),
        reasoning_effort="medium",
    )
    gateway = compose_model_gateway(
        settings,
        secret_scope_id="workspace:wsi_test",
        secrets=_UnusedSecrets(),  # type: ignore[arg-type]
        network_enabled=True,
        responses_transport=httpx.MockTransport(handler),
        codex_credential_source=_RotatingSource([(b"access-other", "other-account")]),
    )

    events = tuple([event async for event in gateway.stream(_request(), ManualCancellationToken())])

    assert requests == 0
    assert events[-1].kind is ModelEventKind.ERROR
    assert events[-1].error is not None
    assert events[-1].error.code == "auth_account_changed"
