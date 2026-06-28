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
from khoj.routers.helpers import update_telemetry_state

api_model = APIRouter()
logger = logging.getLogger(__name__)


@api_model.get("/chat/options", response_model=Dict[str, Union[str, int]])
def get_chat_model_options(
    request: Request,
    client: Optional[str] = None,
):
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

    chat_model = ConversationAdapters.get_chat_model(user)

    if chat_model is None:
        chat_model = ConversationAdapters.get_default_chat_model(user)

    return Response(status_code=200, content=json.dumps({"id": chat_model.id, "chat_model": chat_model.friendly_name}))


@api_model.post("/chat", status_code=200)
@requires(["authenticated"])
async def update_chat_model(
    request: Request,
    id: str,
    client: Optional[str] = None,
):
    user = request.user.object
    subscribed = has_required_scope(request, ["premium"])

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

