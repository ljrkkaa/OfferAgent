import json
import logging
import math
from typing import List, Optional, Union

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from starlette.authentication import has_required_scope, requires

from khoj.configure import initialize_content
from khoj.database import adapters
from khoj.database.adapters import get_user_photo
from khoj.database.models import KhojUser, UserConversationConfig
from khoj.routers.helpers import (
    CommonQueryParams,
    ConversationCommandRateLimiter,
    execute_search,
    get_user_config,
    has_user_document_source,
    update_telemetry_state,
)
from khoj.utils import state
from khoj.utils.rawconfig import SearchResponse
from khoj.utils.state import SearchType

# Initialize Router
api = APIRouter()
logger = logging.getLogger(__name__)
conversation_command_rate_limiter = ConversationCommandRateLimiter(
    trial_rate_limit=2, subscribed_rate_limit=100, slug="command"
)


@api.delete("/self")
@requires(["authenticated"])
def delete_self(request: Request):
    user = request.user.object
    user.delete()
    return {"status": "ok"}


@api.get("/search", response_model=List[SearchResponse])
@requires(["authenticated"])
async def search(
    q: str,
    request: Request,
    common: CommonQueryParams,
    n: Optional[int] = 5,
    t: Optional[SearchType] = SearchType.All,
    r: Optional[bool] = False,
    max_distance: Optional[Union[float, None]] = None,
    dedupe: Optional[bool] = True,
):
    user = request.user.object

    results = await execute_search(
        user=user,
        q=q,
        n=n,
        t=t,
        r=r,
        max_distance=max_distance or math.inf,
        dedupe=dedupe,
    )

    update_telemetry_state(
        request=request,
        telemetry_type="api",
        api="search",
        **common.__dict__,
    )

    return results


@api.get("/update")
@requires(["authenticated"])
def update(
    request: Request,
    common: CommonQueryParams,
    t: Optional[SearchType] = None,
    force: Optional[bool] = False,
):
    user = request.user.object
    try:
        initialize_content(user=user, regenerate=force, search_type=t)
    except Exception as e:
        error_msg = f"🚨 Failed to update server indexed content via API: {e}"
        logger.error(error_msg, exc_info=True)
        raise HTTPException(status_code=500, detail=error_msg)
    else:
        logger.info("📪 Server indexed content updated via API")

    update_telemetry_state(
        request=request,
        telemetry_type="api",
        api="update",
        **common.__dict__,
    )

    return {"status": "ok", "message": "khoj reloaded"}


@api.get("/settings", response_class=Response)
@requires(["authenticated"])
def get_settings(request: Request, detailed: Optional[bool] = False) -> Response:
    user = request.user.object
    user_config = get_user_config(user, request, is_detailed=detailed)
    del user_config["request"]

    # Return config data as a JSON response
    return Response(content=json.dumps(user_config), media_type="application/json", status_code=200)


@api.patch("/user/name", status_code=200)
@requires(["authenticated"])
def set_user_name(
    request: Request,
    name: str,
    client: Optional[str] = None,
):
    user = request.user.object

    split_name = name.split(" ")

    if len(split_name) > 2:
        raise HTTPException(status_code=400, detail="Name must be in the format: Firstname Lastname")

    if len(split_name) == 1:
        first_name = split_name[0]
        last_name = ""
    else:
        first_name, last_name = split_name[0], split_name[-1]

    adapters.set_user_name(user, first_name, last_name)

    update_telemetry_state(
        request=request,
        telemetry_type="api",
        api="set_user_name",
        client=client,
    )

    return {"status": "ok"}


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

    update_telemetry_state(
        request=request,
        telemetry_type="api",
        api="set_user_memory_enabled",
        client=client,
    )

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
    is_active = has_required_scope(request, ["premium"])
    has_documents = has_user_document_source(user)

    # Collect user information in a dictionary
    user_info = {
        "email": user.email,
        "username": user.username,
        "photo": user_picture,
        "is_active": is_active,
        "has_documents": has_documents,
        "khoj_version": state.khoj_version,
    }

    # Return user information as a JSON response
    return Response(content=json.dumps(user_info), media_type="application/json", status_code=200)
