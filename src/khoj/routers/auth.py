from typing import Optional

from fastapi import APIRouter
from starlette.authentication import requires
from starlette.requests import Request

from khoj.database.adapters import acreate_khoj_token, delete_khoj_token, get_khoj_tokens

auth_router = APIRouter()


@auth_router.post("/token")
@requires(["authenticated"])
async def generate_token(request: Request, token_name: Optional[str] = None):
    "Generate API token for given user"
    token = await acreate_khoj_token(user=request.user.object, name=token_name)
    return {
        "token": token.token,
        "name": token.name,
    }


@auth_router.get("/token")
@requires(["authenticated"])
def get_tokens(request: Request):
    "Get API tokens enabled for given user"
    return get_khoj_tokens(user=request.user.object)


@auth_router.delete("/token")
@requires(["authenticated"])
async def delete_token(request: Request, token: str):
    "Delete API token for given user"
    return await delete_khoj_token(user=request.user.object, token=token)
