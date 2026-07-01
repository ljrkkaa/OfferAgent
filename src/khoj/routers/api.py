import json
import logging
from ipaddress import ip_address
from typing import List, Optional
from urllib.request import Request as UrlRequest
from urllib.request import urlopen

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response
from starlette.authentication import has_required_scope, requires

from khoj.configure import initialize_content
from khoj.database import adapters
from khoj.database.adapters import get_user_photo
from khoj.database.models import KhojUser, UserConversationConfig
from khoj.routers.helpers import (
    CommonQueryParams,
    ConversationCommandRateLimiter,
    get_user_config,
    has_user_document_source,
    update_telemetry_state,
)
from khoj.search_type import text_search
from khoj.utils import state
from khoj.utils.lexical import query_terms
from khoj.utils.local_kb import LocalKBError, get_local_kb_root, kb_grep, kb_read
from khoj.utils.openkb import dedupe_references, get_kb_engine, openkb_is_ready, wiki_search_documents
from khoj.utils.rawconfig import SearchResponse
from khoj.utils.state import SearchType

# Initialize Router
api = APIRouter()
logger = logging.getLogger(__name__)
conversation_command_rate_limiter = ConversationCommandRateLimiter(
    trial_rate_limit=2, subscribed_rate_limit=100, slug="command"
)


def _get_public_client_ip(request: Request) -> str | None:
    forwarded_for = request.headers.get("x-forwarded-for", "")
    candidates = [ip.strip() for ip in forwarded_for.split(",") if ip.strip()]
    real_ip = request.headers.get("x-real-ip")
    if real_ip:
        candidates.append(real_ip.strip())
    if request.client and request.client.host:
        candidates.append(request.client.host)

    for candidate in candidates:
        try:
            parsed = ip_address(candidate)
        except ValueError:
            continue
        if parsed.is_global:
            return candidate
    return None


def _location_response(data: dict) -> dict:
    response: dict[str, str] = {}
    field_map = {
        "city": "city",
        "region": "region",
        "country": "country",
        "country_code": "countryCode",
        "timezone": "timezone",
    }
    for source_key, target_key in field_map.items():
        value = data.get(source_key)
        if value:
            response[target_key] = value
    return response


def _local_kb_search(q: str, limit: int) -> list[SearchResponse]:
    results: list[SearchResponse] = []
    seen: set[tuple[str, int]] = set()
    terms = query_terms(q, max_terms=6, cjk_sizes=(4, 3, 2), ignore_prefixes=("file:", "dt:"))

    for term in terms:
        try:
            grep_result = kb_grep(term, mode="literal", before=1, after=2, max_results=max(limit * 2, 1))
        except LocalKBError:
            continue

        for match in grep_result.matches:
            key = (str(match.get("path") or ""), int(match.get("line") or 0))
            if key in seen:
                continue
            seen.add(key)
            try:
                read_result = kb_read(
                    key[0],
                    start_line=max(1, key[1] - 2),
                    end_line=key[1] + 4,
                    max_lines=80,
                )
            except LocalKBError:
                continue

            uri = f"local-kb://{read_result.path}#L{read_result.start_line}-L{read_result.end_line}"
            results.append(
                SearchResponse(
                    entry=read_result.text,
                    score=float(len(results)),
                    additional={
                        "file": read_result.path,
                        "uri": uri,
                        "query": term,
                        "source": "local_kb",
                    },
                    corpus_id=uri,
                )
            )
            if len(results) >= limit:
                return results

    return results


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
    n: int = Query(5, ge=1),
    t: Optional[SearchType] = SearchType.All,
):
    user = request.user.object
    limit = n
    searchable_types = {SearchType.All, SearchType.Org, SearchType.Markdown, SearchType.Plaintext, SearchType.Pdf}

    results: list[SearchResponse] = []
    if not q.strip():
        return results

    if t in searchable_types:
        engine = get_kb_engine()
        uses_evidence_source = False

        if get_local_kb_root() is not None and engine in {"file_first", "hybrid"}:
            uses_evidence_source = True
            results.extend(_local_kb_search(q, limit - len(results)))

        if len(results) < limit and engine in {"openkb", "hybrid"} and openkb_is_ready():
            uses_evidence_source = True
            refs, _, _ = await wiki_search_documents(q, limit - len(results), user, [], "api-search")
            refs = dedupe_references(refs)
            results.extend(
                SearchResponse(
                    entry=str(ref.get("compiled") or ""),
                    score=float(index),
                    additional={
                        "file": ref.get("file"),
                        "uri": ref.get("uri"),
                        "query": ref.get("query"),
                        "source": "openkb",
                    },
                    corpus_id=str(ref.get("uri") or ref.get("file") or index),
                )
                for index, ref in enumerate(refs[: limit - len(results)])
            )

        if not uses_evidence_source:
            indexed_hits = await text_search.query(q, user, t)
            results.extend(list(text_search.collate_results(indexed_hits))[:limit])

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


@api.get("/ip", response_class=Response)
def get_ip_location(request: Request) -> Response:
    client_ip = _get_public_client_ip(request)
    if not client_ip:
        return Response(content=json.dumps({}), media_type="application/json", status_code=200)

    try:
        ipapi_request = UrlRequest(
            f"https://ipapi.co/{client_ip}/json",
            headers={"User-Agent": "Khoj"},
        )
        with urlopen(ipapi_request, timeout=3) as ipapi_response:
            data = json.loads(ipapi_response.read().decode("utf-8"))
    except (OSError, ValueError):
        data = {}

    return Response(content=json.dumps(_location_response(data)), media_type="application/json", status_code=200)


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
