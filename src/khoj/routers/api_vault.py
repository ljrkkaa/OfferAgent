import hmac
import secrets
from urllib.parse import urlsplit
from uuid import UUID

from django.utils import timezone
from fastapi import APIRouter, HTTPException, Request
from starlette.authentication import requires

from khoj.database.models import Conversation, VaultActionBatch
from khoj.processor.conversation.vault_actions import (
    VaultActionError,
    apply_vault_action_batch,
    cancel_vault_action_batch,
    expire_vault_action_batch_if_pending,
    recover_vault_action_batch,
    serialize_vault_action_batch,
    web_vault_write_enabled,
)

api_vault = APIRouter()
VAULT_CSRF_SESSION_KEY = "vault_action_csrf"


def _vault_csrf_token(request: Request) -> str:
    token = request.session.get(VAULT_CSRF_SESSION_KEY)
    if not isinstance(token, str) or len(token) < 32:
        token = secrets.token_urlsafe(32)
        request.session[VAULT_CSRF_SESSION_KEY] = token
    return token


def _require_web_mutation(request: Request) -> None:
    expected_token = request.session.get(VAULT_CSRF_SESSION_KEY)
    supplied_token = request.headers.get("X-Vault-CSRF")
    if (
        not isinstance(expected_token, str)
        or not supplied_token
        or not hmac.compare_digest(expected_token, supplied_token)
    ):
        raise HTTPException(status_code=403, detail="Invalid vault action CSRF token.")

    origin = request.headers.get("Origin")
    if not origin:
        raise HTTPException(status_code=403, detail="Vault action mutations require a same-origin request.")
    parsed = urlsplit(origin)
    if parsed.scheme != request.url.scheme or parsed.netloc != request.url.netloc:
        raise HTTPException(status_code=403, detail="Vault action request origin does not match this server.")


def _http_batch_error(error: Exception) -> HTTPException:
    if isinstance(error, VaultActionBatch.DoesNotExist):
        return HTTPException(status_code=404, detail="Vault action batch not found.")
    if isinstance(error, VaultActionError):
        status = 403 if "disabled" in str(error).lower() else 409
        return HTTPException(status_code=status, detail=str(error))
    return HTTPException(status_code=500, detail="Vault action operation failed.")


@api_vault.get("/actions/capabilities")
@requires(["authenticated"])
def get_vault_action_capabilities(request: Request):
    return {
        "enabled": web_vault_write_enabled(),
        "review_required": True,
        "allowed_extensions": [".md", ".txt"],
        "csrf_token": _vault_csrf_token(request),
    }


@api_vault.get("/actions")
@requires(["authenticated"])
def list_vault_action_batches(request: Request, conversation_id: UUID):
    user = request.user.object
    if not Conversation.objects.filter(id=conversation_id, user=user).exists():
        raise HTTPException(status_code=404, detail="Conversation not found.")

    batches = list(
        VaultActionBatch.objects.filter(user=user, conversation_id=conversation_id)
        .select_related("user")
        .order_by("created_at")
    )
    refreshed = []
    now = timezone.now()
    for batch in batches:
        if batch.status == VaultActionBatch.Status.APPLYING:
            batch = recover_vault_action_batch(batch)
        elif batch.status == VaultActionBatch.Status.PENDING and batch.expires_at <= now:
            batch = expire_vault_action_batch_if_pending(batch.id, user)
        refreshed.append(serialize_vault_action_batch(batch))
    return refreshed


@api_vault.post("/actions/{batch_id}/apply")
@requires(["authenticated"])
def apply_vault_actions(batch_id: UUID, request: Request):
    _require_web_mutation(request)
    try:
        batch = apply_vault_action_batch(batch_id, request.user.object)
    except Exception as error:
        raise _http_batch_error(error) from error
    return serialize_vault_action_batch(batch)


@api_vault.post("/actions/{batch_id}/cancel")
@requires(["authenticated"])
def cancel_vault_actions(batch_id: UUID, request: Request):
    _require_web_mutation(request)
    try:
        batch = cancel_vault_action_batch(batch_id, request.user.object)
    except Exception as error:
        raise _http_batch_error(error) from error
    return serialize_vault_action_batch(batch)
