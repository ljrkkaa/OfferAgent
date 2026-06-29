import base64
import json
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import httpx
import tomllib

CODEX_OAUTH_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
CODEX_OAUTH_TOKEN_URL = "https://auth.openai.com/oauth/token"
CODEX_REFRESH_SKEW_SECONDS = 120
DEFAULT_CODEX_BASE_URL = "https://chatgpt.com/backend-api/codex"
DEFAULT_CODEX_MODEL = "gpt-5.4"
DEFAULT_CODEX_ORIGINATOR = "codex_cli_rs"
DEFAULT_CODEX_USER_AGENT = "codex_cli_rs/0.0.0 (Khoj Interview Agent)"
CODEX_CONFIG_NAME = "config.toml"
CODEX_MODELS_CACHE_NAME = "models_cache.json"

AuthShape = Literal["codex_cli", "hermes"]


class CodexAuthError(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(f"{code}: {message}")


@dataclass
class CodexTokens:
    access_token: str
    refresh_token: str | None
    auth_file: Path
    shape: AuthShape
    payload: dict[str, Any]


def get_codex_base_url() -> str:
    return os.getenv("KHOJ_CODEX_BASE_URL", DEFAULT_CODEX_BASE_URL).strip().rstrip("/") or DEFAULT_CODEX_BASE_URL


def _codex_home() -> Path:
    codex_home = os.getenv("CODEX_HOME", "").strip()
    if codex_home:
        return Path(codex_home).expanduser()
    return Path.home() / ".codex"


def _codex_config_file() -> Path:
    override = os.getenv("KHOJ_CODEX_CONFIG_FILE", "").strip()
    if override:
        return Path(override).expanduser()
    return _codex_home() / CODEX_CONFIG_NAME


def _model_from_codex_config() -> str | None:
    config_file = _codex_config_file()
    if not config_file.is_file():
        return None
    try:
        payload = tomllib.loads(config_file.read_text(encoding="utf-8"))
    except Exception:
        return None
    model = payload.get("model") if isinstance(payload, dict) else None
    return model.strip() if isinstance(model, str) and model.strip() else None


def _codex_config_value(key: str) -> Any:
    config_file = _codex_config_file()
    if not config_file.is_file():
        return None
    try:
        payload = tomllib.loads(config_file.read_text(encoding="utf-8"))
    except Exception:
        return None
    return payload.get(key) if isinstance(payload, dict) else None


def get_codex_model() -> str:
    return os.getenv("KHOJ_CODEX_MODEL", "").strip() or _model_from_codex_config() or DEFAULT_CODEX_MODEL


def _parse_service_tier(raw: Any) -> str | None:
    if isinstance(raw, bool):
        return "priority" if raw else None
    value = str(raw or "").strip().lower()
    if value in {"fast", "priority", "on", "true", "1", "yes"}:
        return "priority"
    return None


def get_codex_service_tier() -> str | None:
    fast_override = os.getenv("KHOJ_CODEX_FAST")
    if fast_override is not None:
        return _parse_service_tier(fast_override)

    tier_override = os.getenv("KHOJ_CODEX_SERVICE_TIER")
    if tier_override is not None:
        return _parse_service_tier(tier_override)

    return _parse_service_tier(_codex_config_value("service_tier"))


def get_codex_fast_mode() -> bool:
    return get_codex_service_tier() == "priority"


def set_codex_fast_mode(enabled: bool) -> None:
    os.environ["KHOJ_CODEX_FAST"] = "true" if enabled else "false"


def _dedupe_models(models) -> list[str]:
    seen = set()
    result = []
    for model in models:
        model = str(model).strip()
        if model and model not in seen:
            result.append(model)
            seen.add(model)
    return result


def _split_model_list(value: str) -> list[str]:
    return _dedupe_models(value.replace("\n", ",").split(","))


def _models_cache_file() -> Path:
    override = os.getenv("KHOJ_CODEX_MODELS_CACHE", "").strip()
    if override:
        return Path(override).expanduser()
    return _codex_home() / CODEX_MODELS_CACHE_NAME


def _models_from_cache() -> list[str]:
    cache_file = _models_cache_file()
    if not cache_file.is_file():
        return []

    try:
        payload = json.loads(cache_file.read_text(encoding="utf-8"))
    except Exception:
        return []

    models = payload.get("models") if isinstance(payload, dict) else payload
    if not isinstance(models, list):
        return []

    slugs = []
    for item in models:
        if isinstance(item, str):
            slug = item
        elif isinstance(item, dict):
            slug = item.get("slug") or item.get("id") or item.get("name")
        else:
            slug = None
        if isinstance(slug, str) and slug.startswith("gpt-"):
            slugs.append(slug)
    return _dedupe_models(slugs)


def get_codex_models() -> list[str]:
    configured_models = _split_model_list(os.getenv("KHOJ_CODEX_MODELS", ""))
    models = configured_models or _models_from_cache() or [get_codex_model()]
    return _dedupe_models([*models, get_codex_model()])


def get_codex_model_option_id(model: str | None = None) -> int:
    model = model or get_codex_model()
    return get_codex_models().index(model) + 1


def get_codex_model_by_option_id(model_id: str | int) -> str | None:
    try:
        index = int(model_id) - 1
    except (TypeError, ValueError):
        return None
    models = get_codex_models()
    if index < 0 or index >= len(models):
        return None
    return models[index]


def set_codex_model(model: str) -> None:
    model = model.strip()
    if model not in get_codex_models():
        raise CodexAuthError("codex_model_not_available", f"Codex model is not available: {model}")
    os.environ["KHOJ_CODEX_MODEL"] = model


def get_codex_chat_model_options() -> list[dict[str, str | int]]:
    return [
        {
            "name": model,
            "id": index + 1,
            "strengths": "Codex reasoning and coding",
            "description": "Codex Responses API via local Codex authentication.",
            "tier": "free",
        }
        for index, model in enumerate(get_codex_models())
    ]


def _json_b64_decode(value: str) -> dict[str, Any] | None:
    try:
        padded = value + "=" * (-len(value) % 4)
        decoded = base64.urlsafe_b64decode(padded.encode("utf-8"))
        payload = json.loads(decoded)
        return payload if isinstance(payload, dict) else None
    except Exception:
        return None


def _jwt_claims(access_token: str) -> dict[str, Any] | None:
    parts = access_token.split(".")
    if len(parts) < 2:
        return None
    return _json_b64_decode(parts[1])


def _jwt_exp(access_token: str) -> int | None:
    claims = _jwt_claims(access_token)
    exp = claims.get("exp") if claims else None
    return int(exp) if isinstance(exp, (int, float)) else None


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as tmp:
        json.dump(payload, tmp, indent=2)
        tmp.write("\n")
        tmp_path = Path(tmp.name)
    os.replace(tmp_path, path)


class CodexAuthResolver:
    def __init__(self, auth_file: str | Path | None = None):
        self.auth_file = Path(auth_file).expanduser() if auth_file else self._default_auth_file()

    @staticmethod
    def _default_auth_file() -> Path:
        override = os.getenv("KHOJ_CODEX_AUTH_FILE", "").strip()
        if override:
            return Path(override).expanduser()
        codex_home = os.getenv("CODEX_HOME", "").strip()
        if codex_home:
            return Path(codex_home).expanduser() / "auth.json"
        return Path.home() / ".codex" / "auth.json"

    def _read_payload(self) -> dict[str, Any]:
        if not self.auth_file.is_file():
            raise CodexAuthError("codex_auth_missing", f"Codex auth file not found at {self.auth_file}")
        try:
            payload = json.loads(self.auth_file.read_text(encoding="utf-8"))
        except Exception as exc:
            raise CodexAuthError("codex_auth_invalid_shape", f"Codex auth file is not valid JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise CodexAuthError("codex_auth_invalid_shape", "Codex auth file root must be a JSON object")
        return payload

    @staticmethod
    def _extract_tokens(payload: dict[str, Any]) -> tuple[dict[str, Any], AuthShape]:
        tokens = payload.get("tokens")
        if isinstance(tokens, dict):
            return tokens, "codex_cli"

        providers = payload.get("providers")
        if isinstance(providers, dict):
            provider = providers.get("openai-codex")
            if isinstance(provider, dict) and isinstance(provider.get("tokens"), dict):
                return provider["tokens"], "hermes"

        raise CodexAuthError("codex_auth_invalid_shape", "Codex auth file has no supported token shape")

    def load_tokens(self) -> CodexTokens:
        payload = self._read_payload()
        tokens, shape = self._extract_tokens(payload)
        access_token = tokens.get("access_token")
        refresh_token = tokens.get("refresh_token")
        if not isinstance(access_token, str) or not access_token.strip():
            raise CodexAuthError("codex_auth_missing_access_token", "Codex auth is missing access_token")
        if refresh_token is not None and not isinstance(refresh_token, str):
            raise CodexAuthError("codex_auth_invalid_shape", "Codex refresh_token must be a string when present")
        return CodexTokens(
            access_token=access_token.strip(),
            refresh_token=refresh_token.strip() if isinstance(refresh_token, str) and refresh_token.strip() else None,
            auth_file=self.auth_file,
            shape=shape,
            payload=payload,
        )

    @staticmethod
    def is_expiring(access_token: str, skew_seconds: int = CODEX_REFRESH_SKEW_SECONDS) -> bool:
        exp = _jwt_exp(access_token)
        if exp is None:
            return False
        return exp - time.time() <= skew_seconds

    @staticmethod
    def account_id(access_token: str) -> str | None:
        claims = _jwt_claims(access_token)
        auth_claims = claims.get("https://api.openai.com/auth") if claims else None
        account_id = auth_claims.get("chatgpt_account_id") if isinstance(auth_claims, dict) else None
        return account_id if isinstance(account_id, str) and account_id else None

    def refresh(self, tokens: CodexTokens) -> CodexTokens:
        if not tokens.refresh_token:
            raise CodexAuthError(
                "codex_auth_missing_refresh_token", "Codex access token is expired and no refresh_token is available"
            )

        timeout_seconds = float(os.getenv("KHOJ_CODEX_REFRESH_TIMEOUT_SECONDS", "20"))
        with httpx.Client(timeout=httpx.Timeout(max(5.0, timeout_seconds))) as client:
            response = client.post(
                CODEX_OAUTH_TOKEN_URL,
                headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": tokens.refresh_token,
                    "client_id": CODEX_OAUTH_CLIENT_ID,
                },
            )

        if response.status_code != 200:
            code = "codex_refresh_failed"
            message = f"Codex token refresh failed with status {response.status_code}"
            try:
                body = response.json()
                if isinstance(body, dict):
                    raw_error = body.get("error")
                    if isinstance(raw_error, str):
                        code = raw_error
                    elif isinstance(raw_error, dict):
                        code = str(raw_error.get("code") or raw_error.get("type") or code)
                        message = str(raw_error.get("message") or message)
                    message = str(body.get("error_description") or body.get("message") or message)
            except Exception:
                pass
            if response.status_code in {401, 403} or code in {"invalid_grant", "invalid_token", "invalid_request"}:
                raise CodexAuthError("codex_relogin_required", message)
            raise CodexAuthError("codex_refresh_failed", message)

        body = response.json()
        access_token = body.get("access_token")
        if not isinstance(access_token, str) or not access_token.strip():
            raise CodexAuthError("codex_refresh_invalid_shape", "Codex refresh response was missing access_token")
        refresh_token = (
            body.get("refresh_token") if isinstance(body.get("refresh_token"), str) else tokens.refresh_token
        )

        payload = dict(tokens.payload)
        if tokens.shape == "hermes" and os.getenv("KHOJ_CODEX_AUTH_FILE", "").strip():
            payload.setdefault("providers", {}).setdefault("openai-codex", {})["tokens"] = {
                "access_token": access_token,
                "refresh_token": refresh_token,
            }
        else:
            payload["tokens"] = {"access_token": access_token, "refresh_token": refresh_token}

        _atomic_write_json(tokens.auth_file, payload)
        return CodexTokens(
            access_token=access_token,
            refresh_token=refresh_token,
            auth_file=tokens.auth_file,
            shape=tokens.shape,
            payload=payload,
        )

    def access_token(self) -> str:
        tokens = self.load_tokens()
        if self.is_expiring(tokens.access_token):
            tokens = self.refresh(tokens)
        return tokens.access_token

    def headers(self) -> dict[str, str]:
        access_token = self.access_token()
        headers = {
            "Authorization": f"Bearer {access_token}",
            "originator": os.getenv("KHOJ_CODEX_ORIGINATOR", DEFAULT_CODEX_ORIGINATOR),
            "User-Agent": os.getenv("KHOJ_CODEX_USER_AGENT", DEFAULT_CODEX_USER_AGENT),
        }
        account_id = self.account_id(access_token)
        if account_id:
            headers["ChatGPT-Account-ID"] = account_id
        return headers
