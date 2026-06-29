import json
import logging
from typing import Dict, Optional, Union

from fastapi import APIRouter, Request
from fastapi.responses import Response
from starlette.authentication import has_required_scope, requires

from khoj.database.adapters import ConversationAdapters
from khoj.database.models import (
    ChatModel,
    PriceTier,
)
from khoj.processor.conversation.codex.auth import (
    get_codex_chat_model_options,
    get_codex_fast_mode,
    get_codex_model,
    get_codex_model_by_option_id,
    get_codex_model_option_id,
    set_codex_fast_mode,
    set_codex_model,
)
from khoj.processor.conversation.codex.utils import use_codex_runtime
from khoj.routers.helpers import update_telemetry_state

api_model = APIRouter()
logger = logging.getLogger(__name__)


@api_model.get("/chat/options", response_model=Dict[str, Union[str, int]])
def get_chat_model_options(
    request: Request,
    client: Optional[str] = None,
):
    if use_codex_runtime():
        return Response(
            content=json.dumps(get_codex_chat_model_options()), media_type="application/json", status_code=200
        )

    chat_models = ConversationAdapters.get_conversation_processor_options().all()

    chat_model_options = list()
    for chat_model in chat_models:
        chat_model_options.append(
            {
                "name": chat_model.friendly_name,
                "id": chat_model.id,
                "strengths": chat_model.strengths,
                "description": chat_model.description,
            }
        )

    return Response(content=json.dumps(chat_model_options), media_type="application/json", status_code=200)


@api_model.get("/chat")
@requires(["authenticated"])
def get_user_chat_model(
    request: Request,
    client: Optional[str] = None,
):
    user = request.user.object

    if use_codex_runtime():
        model = get_codex_model()
        return Response(
            status_code=200,
            content=json.dumps({"id": get_codex_model_option_id(model), "chat_model": model}),
        )

    chat_model = ConversationAdapters.get_chat_model(user)

    if chat_model is None:
        chat_model = ConversationAdapters.get_default_chat_model(user)

    return Response(status_code=200, content=json.dumps({"id": chat_model.id, "chat_model": chat_model.friendly_name}))


@api_model.get("/chat/fast")
@requires(["authenticated"])
def get_chat_fast_mode(
    request: Request,
    client: Optional[str] = None,
):
    return {"available": use_codex_runtime(), "enabled": get_codex_fast_mode() if use_codex_runtime() else False}


@api_model.post("/chat/fast", status_code=200)
@requires(["authenticated"])
def update_chat_fast_mode(
    request: Request,
    enabled: bool,
    client: Optional[str] = None,
):
    if not use_codex_runtime():
        return Response(status_code=400, content=json.dumps({"status": "error", "message": "Fast mode requires Codex"}))

    set_codex_fast_mode(enabled)
    update_telemetry_state(
        request=request,
        telemetry_type="api",
        api="set_conversation_fast_mode",
        client=client,
        metadata={"processor_conversation_type": "codex", "fast_mode": enabled},
    )
    return {"status": "ok", "enabled": enabled}


@api_model.post("/chat", status_code=200)
@requires(["authenticated"])
async def update_chat_model(
    request: Request,
    id: str,
    client: Optional[str] = None,
):
    user = request.user.object
    subscribed = has_required_scope(request, ["premium"])

    if use_codex_runtime():
        model = get_codex_model_by_option_id(id)
        if model is None:
            return Response(status_code=404, content=json.dumps({"status": "error", "message": "Codex model not found"}))
        set_codex_model(model)
        update_telemetry_state(
            request=request,
            telemetry_type="api",
            api="set_conversation_chat_model",
            client=client,
            metadata={"processor_conversation_type": "codex", "chat_model": model},
        )
        return {"status": "ok"}

    # Validate if model can be switched
    chat_model = await ChatModel.objects.filter(id=int(id)).afirst()
    if chat_model is None:
        return Response(status_code=404, content=json.dumps({"status": "error", "message": "Chat model not found"}))
    if not subscribed and chat_model.price_tier != PriceTier.FREE:
        return Response(
            status_code=403,
            content=json.dumps({"status": "error", "message": "Subscribe to switch to this chat model"}),
        )

    new_config = await ConversationAdapters.aset_user_conversation_processor(user, int(id))

    update_telemetry_state(
        request=request,
        telemetry_type="api",
        api="set_conversation_chat_model",
        client=client,
        metadata={"processor_conversation_type": "conversation"},
    )

    if new_config is None:
        return {"status": "error", "message": "Model not found"}

    return {"status": "ok"}
