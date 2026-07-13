import json
import logging
from typing import List, Optional

from fastapi import APIRouter, Query, Request
from fastapi.responses import Response
from starlette.authentication import requires

from khoj.database.adapters import get_user_photo
from khoj.database.models import KhojUser, UserConversationConfig
from khoj.processor.conversation.knowledge_workspace import search_workspace
from khoj.routers.helpers import (
    CommonQueryParams,
    get_user_config,
    has_user_document_source,
)
from khoj.utils import state
from khoj.utils.rawconfig import SearchResponse
from khoj.utils.state import SearchType

# Initialize Router
api = APIRouter()
logger = logging.getLogger(__name__)


@api.get("/search", response_model=List[SearchResponse])
@requires(["authenticated"])
async def search(
    q: str,
    request: Request,
    common: CommonQueryParams,
    n: int = Query(5, ge=1),
    t: Optional[SearchType] = SearchType.All,
):
    user = request.user.object
    return await search_workspace(q, user, limit=n, search_type=t or SearchType.All)


@api.get("/settings", response_class=Response)
@requires(["authenticated"])
def get_settings(request: Request, detailed: Optional[bool] = False) -> Response:
    user = request.user.object
    user_config = get_user_config(user, request, is_detailed=detailed)
    del user_config["request"]

    # Return config data as a JSON response
    return Response(content=json.dumps(user_config), media_type="application/json", status_code=200)


@api.patch("/user/memory", status_code=200)
@requires(["authenticated"])
def set_user_memory_enabled(
    request: Request,
    enable_memory: bool,
    client: Optional[str] = None,
):
    user = request.user.object

    user_config, _ = UserConversationConfig.objects.get_or_create(user=user)
    user_config.enable_memory = enable_memory
    user_config.save()

    return {"status": "ok", "enable_memory": enable_memory}


@api.get("/health", response_class=Response)
@requires(["authenticated"], status_code=200)
def health_check(request: Request) -> Response:
    response_obj = {"email": request.user.object.email}
    return Response(content=json.dumps(response_obj), media_type="application/json", status_code=200)


@api.get("/v1/user", response_class=Response)
@requires(["authenticated"])
def user_info(request: Request) -> Response:
    # Get user information
    user: KhojUser = request.user.object
    user_picture = get_user_photo(user=user)
    has_documents = has_user_document_source(user)

    # Collect user information in a dictionary
    user_info = {
        "email": user.email,
        "username": user.username,
        "photo": user_picture,
        "has_documents": has_documents,
        "khoj_version": state.khoj_version,
    }

    # Return user information as a JSON response
    return Response(content=json.dumps(user_info), media_type="application/json", status_code=200)
