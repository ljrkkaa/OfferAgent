import base64
import json
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import openai
import pytest
from langchain_core.messages.chat import ChatMessage

from khoj.database.models import ChatModel
from khoj.processor.conversation.codex.auth import (
    CodexAuthError,
    CodexAuthResolver,
    get_codex_chat_model_options,
    get_codex_fast_mode,
    get_codex_model_by_option_id,
    get_codex_model_option_id,
    get_codex_models,
    get_codex_service_tier,
    set_codex_fast_mode,
    set_codex_model,
)
from khoj.processor.conversation.codex.gpt import codex_send_message_to_model
from khoj.processor.conversation.codex.utils import build_codex_response_kwargs, normalize_codex_response
from khoj.processor.conversation.utils import ResponseWithThought
from khoj.routers import helpers as router_helpers
from khoj.utils.helpers import ToolDefinition


def _jwt(payload: dict) -> str:
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"header.{body}.sig"


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_reads_hermes_style_auth_file(tmp_path):
    auth_file = tmp_path / "auth.json"
    _write_json(
        auth_file,
        {"providers": {"openai-codex": {"tokens": {"access_token": "access", "refresh_token": "refresh"}}}},
    )

    tokens = CodexAuthResolver(auth_file).load_tokens()

    assert tokens.access_token == "access"
    assert tokens.refresh_token == "refresh"
    assert tokens.shape == "hermes"


def test_reads_codex_cli_style_default_auth_file(tmp_path, monkeypatch):
    auth_file = tmp_path / "auth.json"
    _write_json(auth_file, {"tokens": {"access_token": "access", "refresh_token": "refresh"}})
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    monkeypatch.delenv("KHOJ_CODEX_AUTH_FILE", raising=False)

    assert CodexAuthResolver().access_token() == "access"


def test_codex_model_reads_codex_config_when_env_unset(tmp_path, monkeypatch):
    (tmp_path / "config.toml").write_text('model = "gpt-config-default"\n', encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    monkeypatch.delenv("KHOJ_CODEX_MODEL", raising=False)

    assert get_codex_models() == ["gpt-config-default"]


def test_codex_models_prefer_env_list(monkeypatch):
    monkeypatch.setenv("KHOJ_CODEX_MODELS", "gpt-test-a,gpt-test-b\ngpt-test-a")
    monkeypatch.setenv("KHOJ_CODEX_MODEL", "gpt-test-b")

    assert get_codex_models() == ["gpt-test-a", "gpt-test-b"]
    assert get_codex_model_option_id() == 2
    assert get_codex_model_by_option_id("1") == "gpt-test-a"
    assert get_codex_chat_model_options()[1]["name"] == "gpt-test-b"


def test_codex_models_read_codex_cache(tmp_path, monkeypatch):
    _write_json(
        tmp_path / "models_cache.json",
        {
            "models": [
                {"slug": "gpt-5.5", "display_name": "GPT-5.5"},
                {"slug": "codex-auto-review", "display_name": "Codex Auto Review"},
                {"slug": "gpt-5.4-mini", "display_name": "GPT-5.4-Mini"},
            ]
        },
    )
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    monkeypatch.delenv("KHOJ_CODEX_MODELS", raising=False)
    monkeypatch.setenv("KHOJ_CODEX_MODEL", "gpt-5.5")

    assert get_codex_models() == ["gpt-5.5", "gpt-5.4-mini"]


def test_set_codex_model_updates_current_process_env(monkeypatch):
    monkeypatch.setenv("KHOJ_CODEX_MODELS", "gpt-test-a,gpt-test-b")
    monkeypatch.setenv("KHOJ_CODEX_MODEL", "gpt-test-a")

    set_codex_model("gpt-test-b")

    assert get_codex_model_option_id() == 2
    with pytest.raises(CodexAuthError, match="codex_model_not_available"):
        set_codex_model("not-in-list")


def test_codex_fast_mode_reads_env_and_config(tmp_path, monkeypatch):
    (tmp_path / "config.toml").write_text('service_tier = "priority"\n', encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    monkeypatch.delenv("KHOJ_CODEX_FAST", raising=False)
    monkeypatch.delenv("KHOJ_CODEX_SERVICE_TIER", raising=False)

    assert get_codex_service_tier() == "priority"
    assert get_codex_fast_mode() is True

    monkeypatch.setenv("KHOJ_CODEX_FAST", "false")
    assert get_codex_service_tier() is None
    assert get_codex_fast_mode() is False

    set_codex_fast_mode(True)
    assert get_codex_service_tier() == "priority"


def test_payload_includes_service_tier_when_fast_enabled(monkeypatch):
    monkeypatch.setenv("KHOJ_CODEX_FAST", "true")

    kwargs = build_codex_response_kwargs([ChatMessage(role="user", content="hi")], model="gpt-5.4")

    assert kwargs["service_tier"] == "priority"


def test_payload_omits_service_tier_when_fast_disabled(monkeypatch):
    monkeypatch.setenv("KHOJ_CODEX_FAST", "false")

    kwargs = build_codex_response_kwargs([ChatMessage(role="user", content="hi")], model="gpt-5.4")

    assert "service_tier" not in kwargs


def test_refresh_writes_back_current_auth_shape(tmp_path, monkeypatch):
    old_access = _jwt({"exp": int(time.time()) - 10})
    auth_file = tmp_path / "auth.json"
    _write_json(
        auth_file,
        {"providers": {"openai-codex": {"tokens": {"access_token": old_access, "refresh_token": "old-refresh"}}}},
    )
    monkeypatch.setenv("KHOJ_CODEX_AUTH_FILE", str(auth_file))
    monkeypatch.setattr(
        "khoj.processor.conversation.codex.auth.httpx.Client",
        lambda **_kwargs: _FakeRefreshClient({"access_token": "new-access", "refresh_token": "new-refresh"}),
    )

    assert CodexAuthResolver().access_token() == "new-access"
    payload = json.loads(auth_file.read_text(encoding="utf-8"))

    assert payload["providers"]["openai-codex"]["tokens"] == {
        "access_token": "new-access",
        "refresh_token": "new-refresh",
    }


def test_malformed_token_still_builds_basic_headers(tmp_path):
    auth_file = tmp_path / "auth.json"
    _write_json(auth_file, {"tokens": {"access_token": "not-a-jwt", "refresh_token": "refresh"}})

    headers = CodexAuthResolver(auth_file).headers()

    assert headers["Authorization"] == "Bearer not-a-jwt"
    assert headers["originator"] == "codex_cli_rs"
    assert "ChatGPT-Account-ID" not in headers


def test_jwt_account_id_adds_canonical_header(tmp_path):
    token = _jwt({"https://api.openai.com/auth": {"chatgpt_account_id": "acct_123"}})
    auth_file = tmp_path / "auth.json"
    _write_json(auth_file, {"tokens": {"access_token": token, "refresh_token": "refresh"}})

    headers = CodexAuthResolver(auth_file).headers()

    assert headers["ChatGPT-Account-ID"] == "acct_123"
    assert "ChatGPT-Account-Id" not in headers


def test_payload_omits_empty_tools():
    kwargs = build_codex_response_kwargs([ChatMessage(role="user", content="hi")], model="gpt-5.4", tools=[])

    assert "tools" not in kwargs
    assert kwargs["reasoning"] == {"effort": "low", "summary": "auto"}


def test_tool_definition_converts_to_responses_function_tool():
    tool = ToolDefinition(
        name="lookup",
        description="Lookup a thing",
        schema={"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]},
    )

    kwargs = build_codex_response_kwargs([ChatMessage(role="user", content="hi")], model="gpt-5.4", tools=[tool])

    assert kwargs["tools"][0]["type"] == "function"
    assert kwargs["tools"][0]["name"] == "lookup"
    assert kwargs["tools"][0]["description"] == "Lookup a thing"
    assert kwargs["tools"][0]["parameters"]["properties"] == {"q": {"type": "string"}}
    assert kwargs["tools"][0]["strict"] is True
    assert "function" not in kwargs["tools"][0]
    assert kwargs["tool_choice"] == "auto"
    assert kwargs["parallel_tool_calls"] is True


def test_response_function_tool_call_normalizes_to_json_text():
    response = SimpleNamespace(
        output_text="",
        output=[SimpleNamespace(type="function_call", name="lookup", arguments='{"q":"java"}', call_id="call_1")],
    )

    result = normalize_codex_response(response)

    assert json.loads(result.text) == [{"name": "lookup", "args": {"q": "java"}, "id": "call_1"}]


def test_codex_send_falls_back_to_stream_when_backend_requires_it():
    class FakeStream:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def __iter__(self):
            return iter(
                [
                    SimpleNamespace(type="response.output_text.delta", delta="o"),
                    SimpleNamespace(type="response.output_text.delta", delta="k"),
                ]
            )

        def get_final_response(self):
            return SimpleNamespace(output_text="", output=[])

    def raise_stream_required(**_kwargs):
        request = httpx.Request("POST", "https://chatgpt.com/backend-api/codex/responses")
        response = httpx.Response(400, request=request, json={"detail": "Stream must be set to true"})
        raise openai.BadRequestError(
            "Stream must be set to true", response=response, body={"detail": "Stream must be set to true"}
        )

    client = SimpleNamespace(
        responses=SimpleNamespace(create=raise_stream_required, stream=lambda **_kwargs: FakeStream())
    )

    result = codex_send_message_to_model(messages=[ChatMessage(role="user", content="hi")], client=client)

    assert result.text == "ok"


def test_empty_response_raises_value_error():
    with pytest.raises(ValueError, match="Empty response returned by Codex backend"):
        normalize_codex_response(SimpleNamespace(output_text="", output=[]))


def test_api_runtime_does_not_call_codex_adapter(monkeypatch):
    monkeypatch.setenv("KHOJ_CONVERSATION_RUNTIME", "api")
    monkeypatch.setattr(router_helpers, "codex_send_message_to_model", lambda **_kwargs: pytest.fail("called codex"))
    monkeypatch.setattr(router_helpers, "openai_send_message_to_model", lambda **_kwargs: "openai")
    chat_model = SimpleNamespace(
        model_type=ChatModel.ModelType.OPENAI,
        name="gpt-test",
        ai_model_api=SimpleNamespace(api_key="key", api_base_url=None),
    )

    result = router_helpers.send_message_to_model(chat_model, [], "text", None, [], False, {})

    assert result == "openai"


def test_codex_runtime_does_not_read_configured_api_key(monkeypatch):
    class ExplodingAiModelApi:
        @property
        def api_key(self):
            raise AssertionError("api_key should not be read in codex runtime")

    monkeypatch.setenv("KHOJ_CONVERSATION_RUNTIME", "codex")
    monkeypatch.setenv("KHOJ_CODEX_MODEL", "gpt-test-codex")
    monkeypatch.setattr(router_helpers, "codex_send_message_to_model", lambda **kwargs: kwargs)
    chat_model = SimpleNamespace(
        model_type=ChatModel.ModelType.OPENAI, name="gpt-test", ai_model_api=ExplodingAiModelApi()
    )

    result = router_helpers.send_message_to_model(chat_model, [], "text", None, [], False, {})

    assert result["model"] == "gpt-test-codex"


def test_codex_runtime_skips_chat_model_validation(monkeypatch):
    monkeypatch.setenv("KHOJ_CONVERSATION_RUNTIME", "codex")
    monkeypatch.setattr(
        router_helpers.ConversationAdapters,
        "get_default_chat_model",
        lambda *_args, **_kwargs: pytest.fail("database chat model should not be read"),
    )

    router_helpers.validate_chat_model(None)


@pytest.mark.asyncio
async def test_codex_runtime_is_ready_without_configured_api_key(monkeypatch):
    monkeypatch.setenv("KHOJ_CONVERSATION_RUNTIME", "codex")
    monkeypatch.setattr(
        router_helpers.ConversationAdapters,
        "aget_user_chat_model",
        lambda *_args, **_kwargs: pytest.fail("database chat model should not be read"),
    )

    assert await router_helpers.is_ready_to_chat(None) is True


@pytest.mark.asyncio
async def test_chat_response_generator_uses_codex_runtime(monkeypatch):
    async def fake_converse_codex(_messages, model=None, deepthought=False, tracer=None):
        yield ResponseWithThought(text=f"model={model}, deepthought={deepthought}")

    monkeypatch.setenv("KHOJ_CONVERSATION_RUNTIME", "codex")
    monkeypatch.setenv("KHOJ_CODEX_MODEL", "gpt-test-codex")
    monkeypatch.setattr(
        router_helpers.ConversationAdapters,
        "aget_valid_chat_model",
        lambda *_args, **_kwargs: pytest.fail("database chat model should not be read"),
    )
    monkeypatch.setattr(
        router_helpers.ConversationAdapters,
        "aget_max_context_size",
        lambda *_args, **_kwargs: pytest.fail("database chat model context should not be read"),
    )
    monkeypatch.setattr(router_helpers, "converse_codex", fake_converse_codex)

    generator, metadata = await router_helpers.agenerate_chat_response("hello", [], SimpleNamespace(agent=None))
    chunks = [chunk async for chunk in generator]

    assert metadata == {"chat_model": "codex:gpt-test-codex"}
    assert chunks[0].text == "model=gpt-test-codex, deepthought=False"


@pytest.mark.asyncio
async def test_codex_message_wrapper_skips_database_chat_models(monkeypatch):
    monkeypatch.setenv("KHOJ_CONVERSATION_RUNTIME", "codex")
    monkeypatch.setenv("KHOJ_CODEX_MODEL", "gpt-test-codex")
    monkeypatch.setattr(
        router_helpers.ConversationAdapters,
        "aget_default_chat_model",
        lambda *_args, **_kwargs: pytest.fail("database default chat model should not be read"),
    )
    monkeypatch.setattr(
        router_helpers.ConversationAdapters,
        "aget_chat_model_slot",
        lambda *_args, **_kwargs: pytest.fail("database fallback slot should not be read"),
    )
    monkeypatch.setattr(
        router_helpers.ConversationAdapters,
        "aget_max_context_size",
        lambda *_args, **_kwargs: pytest.fail("database chat model context should not be read"),
    )
    monkeypatch.setattr(router_helpers, "codex_send_message_to_model", lambda **kwargs: kwargs)

    result = await router_helpers.send_message_to_model_wrapper(query="hello")

    assert result["model"] == "gpt-test-codex"


@pytest.mark.asyncio
async def test_extract_facts_allows_missing_agent(monkeypatch):
    async def fake_send_message_to_model_wrapper(*_args, **kwargs):
        assert kwargs["agent_chat_model"] is None
        return ResponseWithThought(text='{"create": [], "delete": []}')

    monkeypatch.setattr(router_helpers, "send_message_to_model_wrapper", fake_send_message_to_model_wrapper)

    result = await router_helpers.extract_facts_from_query(user=None, conversation_history=[], agent=None)

    assert result.create == []
    assert result.delete == []


def test_cloudflare_challenge_is_classified():
    class CloudflareError(Exception):
        response = SimpleNamespace(status_code=403, headers={"cf-mitigated": "challenge"})

    with pytest.raises(CodexAuthError, match="codex_cloudflare_challenge"):
        router_helpers.codex_send_message_to_model(
            messages=[ChatMessage(role="user", content="hi")],
            client=SimpleNamespace(
                responses=SimpleNamespace(create=lambda **_kwargs: (_ for _ in ()).throw(CloudflareError()))
            ),
        )


class _FakeRefreshClient:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def post(self, *_args, **_kwargs):
        return SimpleNamespace(status_code=200, json=lambda: self.payload)
