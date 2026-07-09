import asyncio
import base64
import json
import logging
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from functools import partial
from typing import Any, Dict, List, Optional
from urllib.parse import unquote

from fastapi import (
    APIRouter,
    Body,
    Depends,
    HTTPException,
    Query,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import Response, StreamingResponse
from fastapi.websockets import WebSocketState
from starlette.authentication import requires
from starlette.requests import URL, Headers

from khoj.app.settings import ALLOWED_HOSTS
from khoj.database.adapters import (
    AgentAdapters,
    ConversationAdapters,
    EntryAdapters,
    FileObjectAdapters,
    aget_user_name,
)
from khoj.database.models import Agent, KhojUser
from khoj.processor.conversation import prompts
from khoj.processor.conversation.agent_tool_loop import collect_agent_context_and_actions
from khoj.processor.conversation.notes_tool_loop import collect_notes_evidence_with_tools
from khoj.processor.conversation.offeragent_intent_router import RouteDecision, route_offeragent_intent
from khoj.processor.conversation.prompts import no_entries_found
from khoj.processor.conversation.utils import (
    ResponseWithThought,
    defilter_query,
    save_to_conversation_log,
)
from khoj.processor.conversation.vault_policy import load_vault_policy
from khoj.processor.tools.online_search import (
    deduplicate_organic_results,
    read_webpages,
    search_online,
)
from khoj.processor.tools.run_code import run_code
from khoj.routers.helpers import (
    ApiImageRateLimiter,
    ApiUserRateLimiter,
    ChatEvent,
    ChatRequestBody,
    CommonQueryParams,
    ConversationCommandRateLimiter,
    DeleteMessageRequestBody,
    WebSocketConnectionManager,
    acreate_title_from_history,
    agenerate_chat_response,
    execute_search,
    gather_raw_query_files,
    generate_mermaidjs_diagram,
    generate_summary_from_files,
    get_conversation_command,
    get_message_from_queue,
    is_query_empty,
    is_ready_to_chat,
    read_chat_stream,
    select_offeragent_memories,
    send_message_to_model_wrapper,
    validate_chat_model,
)
from khoj.routers.research import ResearchIteration, research
from khoj.utils import state
from khoj.utils.helpers import (
    ConversationCommand,
    clean_text_for_db,
    command_descriptions,
    convert_image_to_webp,
    get_country_code_from_timezone,
    get_country_name_from_timezone,
    is_code_sandbox_enabled,
    is_none_or_empty,
    is_web_search_enabled,
)
from khoj.utils.local_kb import get_local_kb_root
from khoj.utils.openkb import (
    OpenKBError,
    dedupe_references,
    get_kb_engine,
    openkb_is_ready,
    save_exploration,
    wants_openkb_exploration_save,
)
from khoj.utils.rawconfig import (
    FileFilterRequest,
    FilesFilterRequest,
    LocationData,
)

# Initialize Router
logger = logging.getLogger(__name__)
conversation_command_rate_limiter = ConversationCommandRateLimiter(rate_limit=20, slug="command")

api_chat = APIRouter()


async def search_indexed_notes(
    user: KhojUser, query: str, agent: Optional[Agent], limit: int = 8
) -> list[dict[str, Any]]:
    if not getattr(user, "uuid", None):
        return []
    searchable_agent = agent if getattr(agent, "pk", None) else None
    results = await execute_search(user=user, q=query, n=limit * 5, agent=searchable_agent)

    unique_results = []
    seen_files = set()
    for result in results:
        file_name = (result.additional or {}).get("file") or result.corpus_id
        if file_name in seen_files:
            continue
        seen_files.add(file_name)
        unique_results.append(result)
        if len(unique_results) >= limit:
            break

    file_names = [(result.additional or {}).get("file") for result in unique_results]
    file_objects = await FileObjectAdapters.aget_file_objects_by_names(user, [name for name in file_names if name])
    raw_text_by_file = {file_object.file_name: file_object.raw_text for file_object in file_objects}

    references: list[dict[str, Any]] = []
    for result in unique_results:
        additional = result.additional or {}
        file_name = additional.get("file")
        raw_text = raw_text_by_file.get(file_name)
        references.append(
            {
                "query": additional.get("query") or query,
                "file": file_name,
                "uri": additional.get("uri") or file_name,
                "compiled": f"# {file_name}\n{raw_text}" if raw_text else result.entry,
                "score": result.score,
                "source": additional.get("source") or "indexed",
                "heading": additional.get("heading"),
            }
        )
    return references


def _client_supports_vault_actions(body: ChatRequestBody, client: Any) -> bool:
    client_name = str(client or "").lower()
    capabilities = body.client_capabilities or {}
    return "obsidian" in client_name and bool(capabilities.get("vaultActions"))


def _collect_vault_actions(references: list[dict[str, Any]]) -> list[dict[str, Any]]:
    actions = []
    for reference in references:
        action = reference.get("vault_action")
        if isinstance(action, dict):
            actions.append(action)
    return actions


NON_STREAM_STRUCTURED_EVENTS = {
    ChatEvent.REFERENCES,
    ChatEvent.GENERATED_ASSETS,
    ChatEvent.VAULT_ACTIONS,
    ChatEvent.METADATA,
    ChatEvent.USAGE,
}


def _should_emit_structured_event(event_type: ChatEvent, stream: bool) -> bool:
    return stream or event_type in NON_STREAM_STRUCTURED_EVENTS


def _hostname(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return URL(value if "://" in value else f"http://{value}").hostname
    except Exception:
        return None


def is_allowed_websocket_origin(origin: str | None, host: str | None) -> bool:
    origin_host = _hostname(origin)
    host_name = _hostname(host)
    return bool(origin_host and (origin_host in ALLOWED_HOSTS or origin_host == host_name))


@api_chat.get("/stats", response_class=Response)
@requires(["authenticated"])
def chat_stats(request: Request, common: CommonQueryParams) -> Response:
    num_conversations = ConversationAdapters.get_num_conversations(request.user.object)
    return Response(
        content=json.dumps({"num_conversations": num_conversations}), media_type="application/json", status_code=200
    )


@api_chat.get("/export", response_class=Response)
@requires(["authenticated"])
def export_conversation(request: Request, common: CommonQueryParams, page: int = Query(0, ge=0)) -> Response:
    all_conversations = ConversationAdapters.get_all_conversations_for_export(request.user.object, page=page)
    return Response(content=json.dumps(all_conversations), media_type="application/json", status_code=200)


@api_chat.get("/conversation/file-filters/{conversation_id}", response_class=Response)
@requires(["authenticated"])
def get_file_filter(request: Request, conversation_id: str) -> Response:
    conversation = ConversationAdapters.get_conversation_by_user(request.user.object, conversation_id=conversation_id)
    if not conversation:
        return Response(content=json.dumps({"status": "error", "message": "Conversation not found"}), status_code=404)

    # get all files from "computer"
    file_list = EntryAdapters.get_all_filenames_by_source(request.user.object, "computer")
    file_filters = []
    for file in conversation.file_filters:
        if file in file_list:
            file_filters.append(file)
    return Response(content=json.dumps(file_filters), media_type="application/json", status_code=200)


@api_chat.delete("/conversation/file-filters/bulk", response_class=Response)
@requires(["authenticated"])
def remove_files_filter(request: Request, filter: FilesFilterRequest) -> Response:
    conversation_id = filter.conversation_id
    files_filter = filter.filenames
    file_filters = ConversationAdapters.remove_files_from_filter(request.user.object, conversation_id, files_filter)
    if file_filters is None:
        return Response(content=json.dumps({"status": "error", "message": "Conversation not found"}), status_code=404)
    return Response(content=json.dumps(file_filters), media_type="application/json", status_code=200)


@api_chat.post("/conversation/file-filters/bulk", response_class=Response)
@requires(["authenticated"])
def add_files_filter(request: Request, filter: FilesFilterRequest):
    try:
        conversation_id = filter.conversation_id
        files_filter = filter.filenames
        file_filters = ConversationAdapters.add_files_to_filter(request.user.object, conversation_id, files_filter)
        if file_filters is None:
            return Response(
                content=json.dumps({"status": "error", "message": "Conversation not found"}), status_code=404
            )
        return Response(content=json.dumps(file_filters), media_type="application/json", status_code=200)
    except Exception as e:
        logger.error(f"Error adding file filter {filter.filenames}: {e}", exc_info=True)
        raise HTTPException(status_code=422, detail=str(e))


@api_chat.post("/conversation/file-filters", response_class=Response)
@requires(["authenticated"])
def add_file_filter(request: Request, filter: FileFilterRequest):
    try:
        conversation_id = filter.conversation_id
        files_filter = [filter.filename]
        file_filters = ConversationAdapters.add_files_to_filter(request.user.object, conversation_id, files_filter)
        if file_filters is None:
            return Response(
                content=json.dumps({"status": "error", "message": "Conversation not found"}), status_code=404
            )
        return Response(content=json.dumps(file_filters), media_type="application/json", status_code=200)
    except Exception as e:
        logger.error(f"Error adding file filter {filter.filename}: {e}", exc_info=True)
        raise HTTPException(status_code=422, detail=str(e))


@api_chat.delete("/conversation/file-filters", response_class=Response)
@requires(["authenticated"])
def remove_file_filter(request: Request, filter: FileFilterRequest) -> Response:
    conversation_id = filter.conversation_id
    files_filter = [filter.filename]
    file_filters = ConversationAdapters.remove_files_from_filter(request.user.object, conversation_id, files_filter)
    if file_filters is None:
        return Response(content=json.dumps({"status": "error", "message": "Conversation not found"}), status_code=404)
    return Response(content=json.dumps(file_filters), media_type="application/json", status_code=200)


@api_chat.get("/starters", response_class=Response)
@requires(["authenticated"])
async def chat_starters(
    request: Request,
    common: CommonQueryParams,
) -> Response:
    user: KhojUser = request.user.object
    starter_questions = await ConversationAdapters.aget_conversation_starters(user)
    return Response(content=json.dumps(starter_questions), media_type="application/json", status_code=200)


@api_chat.get("/history")
@requires(["authenticated"])
def chat_history(
    request: Request,
    common: CommonQueryParams,
    conversation_id: Optional[str] = None,
    n: Optional[int] = None,
):
    user = request.user.object
    validate_chat_model(user)

    # Load Conversation History
    conversation = ConversationAdapters.get_conversation_by_user(
        user=user, client_application=request.user.client_app, conversation_id=conversation_id
    )

    if conversation is None:
        return Response(
            content=json.dumps({"status": "error", "message": f"Conversation: {conversation_id} not found"}),
            status_code=404,
        )

    agent_metadata = None
    if conversation.agent:
        if not conversation.agent.managed_by_admin and conversation.agent.creator != user:
            conversation.agent = None
        else:
            agent_metadata = {
                "slug": conversation.agent.slug,
                "name": conversation.agent.name,
                "is_creator": conversation.agent.creator == user,
                "color": conversation.agent.style_color,
                "icon": conversation.agent.style_icon,
                "persona": conversation.agent.personality,
                "is_hidden": conversation.agent.is_hidden,
            }

    meta_log = conversation.conversation_log
    meta_log.update(
        {
            "conversation_id": conversation.id,
            "slug": conversation.title if conversation.title else conversation.slug,
            "agent": agent_metadata,
            "is_owner": conversation.user == user,
        }
    )

    if n:
        # Get latest N messages if N > 0
        if n > 0 and meta_log.get("chat"):
            meta_log["chat"] = meta_log["chat"][-n:]
        # Else return all messages except latest N
        elif n < 0 and meta_log.get("chat"):
            meta_log["chat"] = meta_log["chat"][:n]

    return {"status": "ok", "response": meta_log}


@api_chat.delete("/history")
@requires(["authenticated"])
async def clear_chat_history(
    request: Request,
    common: CommonQueryParams,
    conversation_id: Optional[str] = None,
):
    user = request.user.object
    target_conversation_id = conversation_id.strip() if conversation_id is not None else None
    if conversation_id is not None and not target_conversation_id:
        return Response(
            content=json.dumps({"status": "error", "message": "Conversation not found"}),
            media_type="application/json",
            status_code=404,
        )

    # Clear Conversation History
    deleted_count, _ = await ConversationAdapters.adelete_conversation_by_user(
        user, request.user.client_app, target_conversation_id
    )
    if target_conversation_id and deleted_count == 0:
        return Response(
            content=json.dumps({"status": "error", "message": "Conversation not found"}),
            media_type="application/json",
            status_code=404,
        )

    return {"status": "ok", "message": "Conversation history cleared"}


@api_chat.get("/sessions")
@requires(["authenticated"])
def chat_sessions(
    request: Request,
    common: CommonQueryParams,
    recent: Optional[bool] = False,
):
    user = request.user.object

    # Load Conversation Sessions
    conversations = ConversationAdapters.get_conversation_sessions(user, request.user.client_app)
    if recent:
        conversations = conversations[:8]

    sessions = conversations.values_list(
        "id",
        "slug",
        "title",
        "agent__slug",
        "agent__name",
        "created_at",
        "updated_at",
        "agent__style_icon",
        "agent__style_color",
        "agent__is_hidden",
    )

    session_values = [
        {
            "conversation_id": str(session[0]),
            "slug": session[2] or session[1],
            "agent_name": session[4],
            "created": session[5].strftime("%Y-%m-%d %H:%M:%S"),
            "updated": session[6].strftime("%Y-%m-%d %H:%M:%S"),
            "agent_icon": session[7],
            "agent_color": session[8],
            "agent_is_hidden": session[9],
        }
        for session in sessions
    ]

    return Response(content=json.dumps(session_values), media_type="application/json", status_code=200)


@api_chat.post("/sessions")
@requires(["authenticated"])
async def create_chat_session(
    request: Request,
    common: CommonQueryParams,
    agent_slug: Optional[str] = None,
    body: Optional[Dict[str, Any]] = Body(default=None),
    # Add parameters here to create a custom hidden agent on the fly
):
    user = request.user.object
    requested_agent_slug = agent_slug or str((body or {}).get("agent_slug") or "").strip() or None

    # Create new Conversation Session
    conversation = await ConversationAdapters.acreate_conversation_session(
        user, request.user.client_app, requested_agent_slug
    )

    response = {"conversation_id": str(conversation.id)}

    return Response(content=json.dumps(response), media_type="application/json", status_code=200)


@api_chat.get("/options", response_class=Response)
async def chat_options(
    request: Request,
    common: CommonQueryParams,
) -> Response:
    cmd_options = {}
    for cmd in ConversationCommand:
        if cmd in [ConversationCommand.Online, ConversationCommand.Webpage] and not is_web_search_enabled():
            continue
        if cmd == ConversationCommand.Code and not is_code_sandbox_enabled():
            continue
        if cmd in command_descriptions:
            cmd_options[cmd.value] = command_descriptions[cmd]

    return Response(content=json.dumps(cmd_options), media_type="application/json", status_code=200)


@api_chat.patch("/title", response_class=Response)
@requires(["authenticated"])
async def set_conversation_title(
    request: Request,
    common: CommonQueryParams,
    title: str,
    conversation_id: Optional[str] = None,
) -> Response:
    user = request.user.object
    title = title.strip()[:200]

    # Set Conversation Title
    conversation = await ConversationAdapters.aset_conversation_title(
        user, request.user.client_app, conversation_id, title
    )

    success = True if conversation else False

    return Response(
        content=json.dumps({"status": "ok", "success": success}), media_type="application/json", status_code=200
    )


@api_chat.post("/title")
@requires(["authenticated"])
async def generate_chat_title(
    request: Request,
    common: CommonQueryParams,
    conversation_id: str,
):
    user: KhojUser = request.user.object
    conversation = await ConversationAdapters.aget_conversation_by_user(user=user, conversation_id=conversation_id)

    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")

    # Conversation.title is explicitly set by the user. Do not override.
    if conversation.title:
        return {"status": "ok", "title": conversation.title}

    new_title = await acreate_title_from_history(request.user.object, conversation=conversation)
    conversation.slug = clean_text_for_db(new_title[:200])

    await conversation.asave()

    return {"status": "ok", "title": new_title}


@api_chat.delete("/conversation/message", response_class=Response)
@requires(["authenticated"])
def delete_message(request: Request, delete_request: DeleteMessageRequestBody) -> Response:
    user = request.user.object
    success = ConversationAdapters.delete_message_by_turn_id(
        user, delete_request.conversation_id, delete_request.turn_id
    )
    if success:
        return Response(content=json.dumps({"status": "ok"}), media_type="application/json", status_code=200)
    else:
        return Response(content=json.dumps({"status": "error", "message": "Message not found"}), status_code=404)


async def event_generator(
    body: ChatRequestBody,
    user_scope: Any,
    common: CommonQueryParams,
    headers: Headers,
    request_obj: Request | WebSocket,
    parent_interrupt_queue: asyncio.Queue = None,
):
    # Access the parameters from the body
    q = body.q
    stream = body.stream
    title = body.title
    conversation_id = body.conversation_id
    turn_id = str(body.turn_id or uuid.uuid4())
    city = body.city
    region = body.region
    country = body.country or get_country_name_from_timezone(body.timezone)
    country_code = body.country_code or get_country_code_from_timezone(body.timezone)
    raw_images = body.images
    raw_query_files = body.files

    start_time = time.perf_counter()
    ttft = None
    conversation = None
    user: KhojUser = user_scope.object
    q = unquote(q)
    defiltered_query = defilter_query(q)
    train_of_thought = []
    cancellation_event = asyncio.Event()
    child_interrupt_queue: asyncio.Queue = asyncio.Queue(maxsize=10)

    tracer: dict = {
        "mid": turn_id,
        "cid": conversation_id,
        "uid": user.id,
        "khoj_version": state.khoj_version,
    }

    uploaded_images: list[str] = []
    if raw_images:
        for image in raw_images:
            decoded_string = unquote(image)
            base64_data = decoded_string.split(",", 1)[1]
            image_bytes = base64.b64decode(base64_data)
            webp_image_bytes = convert_image_to_webp(image_bytes)
            base64_webp_image = base64.b64encode(webp_image_bytes).decode("utf-8")
            uploaded_image = f"data:image/webp;base64,{base64_webp_image}"
            uploaded_images.append(uploaded_image)

    query_files: Dict[str, str] = {}
    if raw_query_files:
        for file in raw_query_files:
            query_files[file.name] = file.content

    research_results: List[ResearchIteration] = []
    online_results: Dict = dict()
    code_results: Dict = dict()
    compiled_references: List[Any] = []
    inferred_queries: List[Any] = []
    attached_file_context = gather_raw_query_files(query_files)

    generated_images: List[str] = []
    generated_mermaidjs_diagram: str = None
    generated_asset_results: Dict = dict()
    vault_actions: list[dict[str, Any]] = []
    conversation_commands: List[ConversationCommand] = []
    program_execution_context: List[str] = []
    user_message_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Create a task to monitor for disconnections
    disconnect_monitor_task = None

    async def monitor_disconnection():
        nonlocal q, defiltered_query
        if isinstance(request_obj, Request):
            try:
                msg = await request_obj.receive()
                if msg["type"] == "http.disconnect":
                    logger.debug(f"Request cancelled. User {user} disconnected from {common.client} client.")
                    cancellation_event.set()
                    # ensure partial chat state saved on interrupt
                    # shield the save against task cancellation
                    if conversation:
                        await asyncio.shield(
                            save_to_conversation_log(
                                q,
                                chat_response="",
                                user=user,
                                compiled_references=compiled_references,
                                online_results=online_results,
                                code_results=code_results,
                                research_results=research_results,
                                inferred_queries=inferred_queries,
                                client_application=user_scope.client_app,
                                conversation_id=conversation_id,
                                query_images=uploaded_images,
                                train_of_thought=train_of_thought,
                                raw_query_files=raw_query_files,
                                generated_images=generated_images,
                                generated_mermaidjs_diagram=generated_mermaidjs_diagram,
                                user_message_time=user_message_time,
                                tracer=tracer,
                            )
                        )
            except Exception as e:
                logger.error(f"Error in disconnect monitor: {e}")
        elif isinstance(request_obj, WebSocket):
            while request_obj.client_state == WebSocketState.CONNECTED and not cancellation_event.is_set():
                await asyncio.sleep(1)

                # Check if any interrupt query is received
                if interrupt_query := get_message_from_queue(parent_interrupt_queue):
                    if interrupt_query == ChatEvent.END_EVENT.value:
                        cancellation_event.set()
                        logger.debug(f"Chat cancelled by user {user} via interrupt queue.")
                    elif interrupt_query == ChatEvent.INTERRUPT.value:
                        cancellation_event.set()
                        logger.debug("Chat interrupted.")
                    else:
                        # Pass the interrupt query to child tasks
                        logger.info(f"Continuing chat with the new instruction: {interrupt_query}")
                        await child_interrupt_queue.put(interrupt_query)
                        # Append the interrupt query to the main query
                        q += f"\n\n{interrupt_query}"
                        defiltered_query += f"\n\n{defilter_query(interrupt_query)}"

            logger.debug(f"WebSocket disconnected or chat cancelled by user {user} from {common.client} client.")
            if conversation and cancellation_event.is_set():
                await asyncio.shield(
                    save_to_conversation_log(
                        q,
                        chat_response="",
                        user=user,
                        compiled_references=compiled_references,
                        online_results=online_results,
                        code_results=code_results,
                        research_results=research_results,
                        inferred_queries=inferred_queries,
                        client_application=user_scope.client_app,
                        conversation_id=conversation_id,
                        query_images=uploaded_images,
                        train_of_thought=train_of_thought,
                        raw_query_files=raw_query_files,
                        generated_images=generated_images,
                        generated_mermaidjs_diagram=generated_mermaidjs_diagram,
                        user_message_time=user_message_time,
                        tracer=tracer,
                    )
                )

    # Cancel the disconnect monitor task if it is still running
    async def cancel_disconnect_monitor():
        if disconnect_monitor_task and not disconnect_monitor_task.done():
            logger.debug(f"Cancelling disconnect monitor task for user {user}")
            disconnect_monitor_task.cancel()
            try:
                await disconnect_monitor_task
            except asyncio.CancelledError:
                pass

    async def send_event(event_type: ChatEvent, data: str | dict):
        nonlocal ttft, train_of_thought
        if cancellation_event.is_set():
            return
        try:
            if event_type == ChatEvent.END_LLM_RESPONSE:
                collect_telemetry()
            elif event_type == ChatEvent.START_LLM_RESPONSE:
                ttft = time.perf_counter() - start_time
            elif event_type == ChatEvent.STATUS:
                train_of_thought.append({"type": event_type.value, "data": data})
            elif event_type == ChatEvent.THOUGHT:
                # Append the data to the last thought as thoughts are streamed
                if (
                    len(train_of_thought) > 0
                    and train_of_thought[-1]["type"] == ChatEvent.THOUGHT.value
                    and isinstance(train_of_thought[-1]["data"], str)
                    and isinstance(data, str)
                ):
                    train_of_thought[-1]["data"] += data
                else:
                    train_of_thought.append({"type": event_type.value, "data": data})

            if event_type == ChatEvent.MESSAGE:
                yield data
            elif _should_emit_structured_event(event_type, stream):
                yield json.dumps({"type": event_type.value, "data": data}, ensure_ascii=False)
        except Exception as e:
            if not cancellation_event.is_set():
                logger.error(
                    f"Failed to stream chat API response to {user} on {common.client}: {e}.",
                    exc_info=True,
                )
        finally:
            if not cancellation_event.is_set():
                yield ChatEvent.END_EVENT.value
            # Cancel the disconnect monitor task if it is still running
            if cancellation_event.is_set() or event_type == ChatEvent.END_RESPONSE:
                await cancel_disconnect_monitor()

    async def send_llm_response(response: str, usage: dict = None):
        # Check if the client is still connected
        if cancellation_event.is_set():
            return
        # Send Chat Response
        async for result in send_event(ChatEvent.START_LLM_RESPONSE, ""):
            yield result
        async for result in send_event(ChatEvent.MESSAGE, response):
            yield result
        async for result in send_event(ChatEvent.END_LLM_RESPONSE, ""):
            yield result
        # Send Usage Metadata once llm interactions are complete
        if usage:
            async for event in send_event(ChatEvent.USAGE, usage):
                yield event
        async for result in send_event(ChatEvent.END_RESPONSE, ""):
            yield result

    def collect_telemetry():
        latency = time.perf_counter() - start_time
        cost = (tracer.get("usage", {}) or {}).get("cost", 0)
        if ttft:
            logger.info(f"Chat response time to first token: {ttft:.3f} seconds")
        logger.info(f"Chat response total time: {latency:.3f} seconds")
        logger.info(f"Chat response cost: ${cost:.5f}")

    # Start the disconnect monitor in the background
    disconnect_monitor_task = asyncio.create_task(monitor_disconnection())

    if is_query_empty(q):
        async for result in send_llm_response("Please ask your query to get started.", tracer.get("usage")):
            yield result
        return

    # Automated tasks are handled before to allow mixing them with other conversation commands
    cmds_to_rate_limit = []
    if q.startswith("/automated_task"):
        q = q.replace("/automated_task", "").lstrip()
        cmds_to_rate_limit += [ConversationCommand.AutomatedTask]

    # Extract conversation command from query
    conversation_commands = [get_conversation_command(query=q)]

    conversation = await ConversationAdapters.aget_conversation_by_user(
        user,
        client_application=user_scope.client_app,
        conversation_id=conversation_id,
        title=title,
        create_new=body.create_new,
    )
    if not conversation:
        async for result in send_llm_response(f"Conversation {conversation_id} not found", tracer.get("usage")):
            yield result
        return
    conversation_id = str(conversation.id)

    async for event in send_event(ChatEvent.METADATA, {"conversationId": conversation_id, "turnId": turn_id}):
        yield event

    agent: Agent | None = None
    default_agent = await AgentAdapters.aget_default_agent()
    if conversation.agent and conversation.agent != default_agent:
        agent = conversation.agent

    if not conversation.agent:
        conversation.agent = default_agent
        await conversation.asave()
        agent = default_agent

    await is_ready_to_chat(user)
    user_name = await aget_user_name(user)
    location = None
    if city or region or country or country_code:
        location = LocationData(city=city, region=region, country=country, country_code=country_code)
    chat_history = conversation.messages

    relevant_memories = []
    if await ConversationAdapters.ais_memory_enabled(user):
        relevant_memories = await select_offeragent_memories(user, q, agent, tracer=tracer)

    local_kb_root = get_local_kb_root()
    kb_engine = get_kb_engine()
    notes_local_source_available = local_kb_root is not None and kb_engine in {"file_first", "hybrid"}
    notes_openkb_source_available = kb_engine in {"openkb", "hybrid"} and openkb_is_ready()
    vault_policy = load_vault_policy(local_kb_root)
    vault_actions_supported = _client_supports_vault_actions(body, common.client)

    # If interrupted message in DB
    if last_message := await conversation.pop_message(interrupted=True):
        # Populate context from interrupted message
        online_results = {key: val.model_dump() for key, val in last_message.onlineContext.items() or []}
        code_results = {key: val.model_dump() for key, val in last_message.codeContext.items() or []}
        compiled_references = [ref.model_dump() for ref in last_message.context or []]
        research_results = [
            ResearchIteration(**iter_dict)
            for iter_dict in last_message.researchContext or []
            if iter_dict.get("summarizedResult")
        ]
        train_of_thought = [thought.model_dump() for thought in last_message.trainOfThought or []]
        logger.info(f"Loaded interrupted partial context from conversation {conversation_id}.")

    explicit_command = q.lstrip().startswith("/")
    route_decision: RouteDecision | None = None
    if conversation_commands == [ConversationCommand.Default] and not explicit_command:
        async def router_send_message(**kwargs):
            return await send_message_to_model_wrapper(
                user=user,
                query_files=attached_file_context,
                query_images=uploaded_images,
                relevant_memories=relevant_memories,
                tracer=tracer,
                **kwargs,
            )

        route_decision = await route_offeragent_intent(
            q,
            chat_history,
            send_message=router_send_message,
            vault_policy=vault_policy,
            client_app=common.client,
            client_capabilities=body.client_capabilities,
        )
        if route_decision.needs_confirmation and route_decision.question:
            async for result in send_llm_response(route_decision.question, tracer.get("usage")):
                yield result
            return
        try:
            routed_command = ConversationCommand(route_decision.command)
        except ValueError:
            routed_command = ConversationCommand.Default
        if routed_command != ConversationCommand.Default:
            conversation_commands = [routed_command]
            inferred_queries.append(f"router:{route_decision.intent}:{route_decision.route}")
            async for result in send_event(
                ChatEvent.STATUS,
                f"**Routed by intent:** {route_decision.route}",
            ):
                yield result

    if conversation_commands == [ConversationCommand.Default]:
        try:
            status_messages = []
            agent_chat_model = (
                AgentAdapters.get_agent_chat_model(agent, user)
                if agent and hasattr(agent, "slug") and hasattr(agent, "chat_model")
                else None
            )

            async def agent_runtime_send_message(**kwargs):
                return await send_message_to_model_wrapper(
                    user=user,
                    query_files=attached_file_context,
                    query_images=uploaded_images,
                    relevant_memories=relevant_memories,
                    agent_chat_model=agent_chat_model,
                    tracer=tracer,
                    **kwargs,
                )

            async def check_agent_tool_rate_limit(command: ConversationCommand) -> None:
                await conversation_command_rate_limiter.update_and_check_if_valid(request_obj, command)

            agent_result = await collect_agent_context_and_actions(
                q,
                chat_history,
                user=user,
                agent=agent,
                send_message=agent_runtime_send_message,
                send_status=status_messages.append,
                before_tool_call=check_agent_tool_rate_limit,
                client_app=user_scope.client_app,
                allow_local_kb=notes_local_source_available,
                allow_openkb=notes_openkb_source_available,
                allow_web=is_web_search_enabled(),
                conversation_id=conversation_id,
                write_mode="client_actions" if vault_actions_supported else "server",
                vault_policy=vault_policy,
                location=location,
                query_images=uploaded_images,
                query_files=attached_file_context,
                relevant_memories=relevant_memories,
                tracer=tracer,
            )
            compiled_references.extend(agent_result.references)
            vault_actions.extend(_collect_vault_actions(agent_result.references))
            inferred_queries.extend(agent_result.inferred_queries)
            online_results.update(agent_result.online_results)
            program_execution_context.extend(agent_result.program_context)
            if agent_result.errors:
                program_execution_context.append(
                    "Unified agent runtime tool errors: " + "; ".join(agent_result.errors[:8])
                )
            for message in status_messages:
                async for result in send_event(ChatEvent.STATUS, message):
                    yield result
        except HTTPException as e:
            async for result in send_llm_response(str(e.detail), tracer.get("usage")):
                yield result
            return
        except Exception as e:
            logger.error(f"Error running unified agent runtime: {e}. Falling back to general response.", exc_info=True)
            async for result in send_event(
                ChatEvent.STATUS, "Unified agent runtime failed. I'll answer without tool results."
            ):
                yield result

        conversation_commands = [ConversationCommand.General]

        conversation_commands_str = ", ".join([cmd.value for cmd in conversation_commands])
        async for result in send_event(ChatEvent.STATUS, f"**Selected Tools:** {conversation_commands_str}"):
            yield result

    cmds_to_rate_limit += conversation_commands
    for cmd in cmds_to_rate_limit:
        try:
            await conversation_command_rate_limiter.update_and_check_if_valid(request_obj, cmd)
            q = q.replace(f"/{cmd.value}", "").strip()
        except HTTPException as e:
            async for result in send_llm_response(str(e.detail), tracer.get("usage")):
                yield result
            return

    defiltered_query = defilter_query(q)

    if conversation_commands == [ConversationCommand.Summarize]:
        no_files_selected = "No files selected for summarization. Please add files using the section on the left."
        if is_none_or_empty(conversation.file_filters) and not attached_file_context:
            async for result in send_llm_response(no_files_selected, tracer.get("usage")):
                yield result
            return

        async for summary_result in generate_summary_from_files(
            defiltered_query,
            user,
            conversation.file_filters,
            chat_history,
            query_images=uploaded_images,
            query_files=attached_file_context,
            agent=agent,
            send_status_func=partial(send_event, ChatEvent.STATUS),
            tracer=tracer,
        ):
            if isinstance(summary_result, dict) and ChatEvent.STATUS in summary_result:
                yield summary_result[ChatEvent.STATUS]
            else:
                async for result in send_llm_response(str(summary_result), tracer.get("usage")):
                    yield result
                return

    if conversation_commands == [ConversationCommand.Research]:
        async for research_result in research(
            user=user,
            query=defiltered_query,
            conversation_id=conversation_id,
            conversation_history=chat_history,
            previous_iterations=list(research_results),
            query_images=uploaded_images,
            query_files=attached_file_context,
            relevant_memories=relevant_memories,
            user_name=user_name,
            location=location,
            send_status_func=partial(send_event, ChatEvent.STATUS),
            cancellation_event=cancellation_event,
            interrupt_queue=child_interrupt_queue,
            abort_message=ChatEvent.END_EVENT.value,
            agent=agent,
            tracer=tracer,
        ):
            if isinstance(research_result, ResearchIteration):
                if research_result.summarizedResult:
                    if research_result.onlineContext:
                        online_results.update(research_result.onlineContext)
                    if research_result.codeContext:
                        code_results.update(research_result.codeContext)
                    if research_result.context:
                        compiled_references.extend(research_result.context)
                if not research_results or research_results[-1] is not research_result:
                    research_results.append(research_result)
            else:
                yield research_result

        # researched_results = await extract_relevant_info(q, researched_results, agent)
        if state.verbose > 1:
            logger.debug(f"Researched Results: {''.join(r.summarizedResult or '' for r in research_results)}")

    # Gather Context
    ## Gather Document References
    used_notes_tool_loop = False
    notes_tool_loop_has_evidence = False
    notes_tool_loop_failed = False
    notes_requested = ConversationCommand.Notes in conversation_commands

    async def collect_notes_evidence():
        nonlocal used_notes_tool_loop, notes_tool_loop_has_evidence, notes_tool_loop_failed
        allow_local_kb = notes_local_source_available
        allow_openkb = notes_openkb_source_available
        if used_notes_tool_loop or not (allow_local_kb or allow_openkb):
            return
        status_messages = []

        used_notes_tool_loop = True
        try:
            agent_chat_model = (
                AgentAdapters.get_agent_chat_model(agent, user)
                if agent and hasattr(agent, "slug") and hasattr(agent, "chat_model")
                else None
            )

            async def notes_send_message(**kwargs):
                return await send_message_to_model_wrapper(
                    user=user,
                    query_files=attached_file_context,
                    query_images=uploaded_images,
                    relevant_memories=relevant_memories,
                    agent_chat_model=agent_chat_model,
                    tracer=tracer,
                    **kwargs,
                )

            notes_result = await collect_notes_evidence_with_tools(
                q,
                chat_history,
                user,
                agent,
                send_message=notes_send_message,
                send_status=status_messages.append,
                client_app=user_scope.client_app,
                allow_local_kb=allow_local_kb,
                allow_openkb=allow_openkb,
                conversation_id=conversation_id,
                write_mode="client_actions" if vault_actions_supported else "server",
                vault_policy=vault_policy,
            )
            compiled_references.extend(notes_result.references)
            vault_actions.extend(_collect_vault_actions(notes_result.references))
            inferred_queries.extend(notes_result.inferred_queries)
            notes_tool_loop_has_evidence = not is_none_or_empty(notes_result.references)
            for reference in notes_result.references:
                if reference.get("action") in {"append_note", "propose_edit"}:
                    write_result = {
                        "action": reference.get("action"),
                        "status": reference.get("status"),
                        "file": reference.get("file"),
                        "changed": reference.get("changed"),
                        "result": reference.get("compiled", ""),
                    }
                    write_instruction = "Final answer must report this exact Notes write tool result."
                    if reference.get("status") == "written":
                        write_instruction += " Do not say writing is unavailable."
                    elif reference.get("status") == "action_prepared":
                        write_instruction = "Final answer should say a local vault action was prepared for the client to apply."
                    program_execution_context.append(
                        "Notes write tool result: "
                        f"{json.dumps(write_result, ensure_ascii=False, default=str)}. "
                        f"{write_instruction}"
                    )
            for message in status_messages:
                async for result in send_event(ChatEvent.STATUS, message):
                    yield result
            if not notes_tool_loop_has_evidence and notes_result.searched:
                program_execution_context.append(
                    "No Notes evidence found. Tools used: " + ", ".join(notes_result.searched[:8])
                )
        except Exception as e:
            notes_tool_loop_failed = True
            logger.error(f"Error using Notes evidence tools: {e}", exc_info=True)
            async for result in send_event(
                ChatEvent.STATUS, "Notes evidence tools failed. I did not read or modify the local knowledge base"
            ):
                yield result

    if notes_requested:
        async for result in collect_notes_evidence():
            yield result

        compiled_references[:] = dedupe_references(compiled_references)

    if (
        (conversation_commands == [ConversationCommand.General] or notes_requested)
        and is_none_or_empty(compiled_references)
        and not used_notes_tool_loop
        and not notes_tool_loop_failed
    ):
        indexed_references = await search_indexed_notes(user, defiltered_query, agent)
        if indexed_references:
            compiled_references.extend(indexed_references)
            inferred_queries.append(defiltered_query)
            async for result in send_event(ChatEvent.STATUS, "Searched synced knowledge base"):
                yield result

    if notes_requested and notes_tool_loop_failed:
        async for result in send_llm_response(
            "Notes evidence tools failed, so I did not read or modify the local knowledge base.",
            tracer.get("usage"),
        ):
            yield result
        return

    if (
        notes_requested
        and is_none_or_empty(compiled_references)
        and conversation_commands == [ConversationCommand.Notes]
        and (used_notes_tool_loop or not (notes_local_source_available or notes_openkb_source_available))
    ):
        message = (
            "I couldn't find enough local knowledge base evidence to answer that."
            if used_notes_tool_loop
            else f"{no_entries_found.format()}"
        )
        async for result in send_llm_response(message, tracer.get("usage")):
            yield result
        return

    compiled_references[:] = dedupe_references(compiled_references)

    if (
        ConversationCommand.Notes in conversation_commands
        and is_none_or_empty(compiled_references)
        and not used_notes_tool_loop
    ):
        conversation_commands.remove(ConversationCommand.Notes)

    ## Gather Online References
    if ConversationCommand.Online in conversation_commands:
        try:
            async for result in search_online(
                defiltered_query,
                chat_history,
                location,
                user,
                partial(send_event, ChatEvent.STATUS),
                custom_filters=[],
                max_online_searches=3,
                query_images=uploaded_images,
                query_files=attached_file_context,
                relevant_memories=relevant_memories,
                agent=agent,
                tracer=tracer,
            ):
                if isinstance(result, dict) and ChatEvent.STATUS in result:
                    yield result[ChatEvent.STATUS]
                else:
                    online_results = result
        except Exception as e:
            error_message = f"Error searching online: {e}. Attempting to respond without online results"
            logger.warning(error_message)
            async for result in send_event(
                ChatEvent.STATUS, "Online search failed. I'll try respond without online references"
            ):
                yield result

    ## Gather Webpage References
    if ConversationCommand.Webpage in conversation_commands:
        try:
            async for result in read_webpages(
                defiltered_query,
                chat_history,
                location,
                user,
                partial(send_event, ChatEvent.STATUS),
                max_webpages_to_read=1,
                query_images=uploaded_images,
                query_files=attached_file_context,
                relevant_memories=relevant_memories,
                agent=agent,
                tracer=tracer,
            ):
                if isinstance(result, dict) and ChatEvent.STATUS in result:
                    yield result[ChatEvent.STATUS]
                else:
                    direct_web_pages = result
            webpages = []
            for query in direct_web_pages:
                if online_results.get(query):
                    online_results[query]["webpages"] = direct_web_pages[query]["webpages"]
                else:
                    online_results[query] = {"webpages": direct_web_pages[query]["webpages"]}

                for webpage in direct_web_pages[query]["webpages"]:
                    webpages.append(webpage["link"])
            async for result in send_event(ChatEvent.STATUS, f"**Read web pages**: {webpages}"):
                yield result
        except Exception as e:
            logger.warning(
                f"Error reading webpages: {e}. Attempting to respond without webpage results",
                exc_info=True,
            )
            async for result in send_event(
                ChatEvent.STATUS, "Webpage read failed. I'll try respond without webpage references"
            ):
                yield result

    ## Run Code
    if ConversationCommand.Code in conversation_commands:
        try:
            context = f"# Iteration 1:\n#---\nNotes:\n{compiled_references}\n\nOnline Results:{online_results}"
            async for result in run_code(
                defiltered_query,
                chat_history,
                context,
                location,
                user,
                partial(send_event, ChatEvent.STATUS),
                query_images=uploaded_images,
                query_files=attached_file_context,
                relevant_memories=relevant_memories,
                agent=agent,
                tracer=tracer,
            ):
                if isinstance(result, dict) and ChatEvent.STATUS in result:
                    yield result[ChatEvent.STATUS]
                else:
                    code_results = result
        except ValueError as e:
            program_execution_context.append("Failed to run code")
            logger.warning(
                f"Failed to use code tool: {e}. Attempting to respond without code results",
                exc_info=True,
            )

    ## Send Gathered References
    unique_online_results = deduplicate_organic_results(online_results)
    async for result in send_event(
        ChatEvent.REFERENCES,
        {
            "inferredQueries": inferred_queries,
            "context": compiled_references,
            "onlineContext": unique_online_results,
            "codeContext": code_results,
        },
    ):
        yield result

    # Generate Output
    if ConversationCommand.Diagram in conversation_commands:
        async for result in send_event(ChatEvent.STATUS, "Creating diagram"):
            yield result

        inferred_queries = []
        async for result in generate_mermaidjs_diagram(
            q=defiltered_query,
            chat_history=chat_history,
            location_data=location,
            note_references=compiled_references,
            online_results=online_results,
            query_images=uploaded_images,
            query_files=attached_file_context,
            relevant_memories=relevant_memories,
            user=user,
            agent=agent,
            send_status_func=partial(send_event, ChatEvent.STATUS),
            tracer=tracer,
        ):
            if isinstance(result, dict) and ChatEvent.STATUS in result:
                yield result[ChatEvent.STATUS]
            else:
                better_diagram_description_prompt, mermaidjs_diagram_description = result
                if better_diagram_description_prompt and mermaidjs_diagram_description:
                    inferred_queries.append(better_diagram_description_prompt)
                    generated_mermaidjs_diagram = mermaidjs_diagram_description

                    generated_asset_results["diagrams"] = {
                        "query": better_diagram_description_prompt,
                    }

                    async for result in send_event(
                        ChatEvent.GENERATED_ASSETS,
                        {
                            "mermaidjsDiagram": mermaidjs_diagram_description,
                        },
                    ):
                        yield result
                else:
                    error_message = "Failed to generate diagram. Please try again later."
                    program_execution_context.append(
                        prompts.failed_diagram_generation.format(attempted_diagram=better_diagram_description_prompt)
                    )

                    async for result in send_event(ChatEvent.STATUS, error_message):
                        yield result

    # Check if the user has disconnected
    if cancellation_event.is_set():
        logger.debug(f"Stopping LLM response to user {user} on {common.client} client.")
        # Cancel the disconnect monitor task if it is still running
        await cancel_disconnect_monitor()
        return

    if vault_actions:
        async for result in send_event(ChatEvent.VAULT_ACTIONS, {"actions": vault_actions}):
            yield result

    ## Generate Text Output
    async for result in send_event(ChatEvent.STATUS, "**Generating a well-informed response**"):
        yield result

    llm_response, _ = await agenerate_chat_response(
        defiltered_query,
        chat_history,
        conversation,
        compiled_references,
        online_results,
        code_results,
        research_results,
        user,
        location,
        user_name,
        uploaded_images,
        attached_file_context,
        relevant_memories,
        program_execution_context,
        generated_asset_results,
        tracer,
    )

    full_response = ""
    message_start = True
    async for item in llm_response:
        # Should not happen with async generator. Skip.
        if item is None or not isinstance(item, ResponseWithThought):
            logger.warning(f"Unexpected item type in LLM response: {type(item)}. Skipping.")
            continue
        if cancellation_event.is_set():
            break
        message = item.text
        full_response += message if message else ""
        if item.thought:
            async for result in send_event(ChatEvent.THOUGHT, item.thought):
                yield result
            continue
        # Start sending response
        elif message_start:
            message_start = False
            async for result in send_event(ChatEvent.START_LLM_RESPONSE, ""):
                yield result

        try:
            async for result in send_event(ChatEvent.MESSAGE, message):
                yield result
        except Exception as e:
            if not cancellation_event.is_set():
                logger.warning(f"Error during streaming. Stopping send: {e}")
            break

    # Check if the user has disconnected
    if cancellation_event.is_set():
        logger.debug(f"Stopping LLM response to user {user} on {common.client} client.")
        # Cancel the disconnect monitor task if it is still running
        await cancel_disconnect_monitor()
        return

    if wants_openkb_exploration_save(q):
        try:
            save_result = save_exploration(
                q,
                full_response,
                compiled_references,
                conversation_id=str(conversation.id),
                client_app=user_scope.client_app,
            )
        except OpenKBError as e:
            save_message = f"Could not save exploration: {e}"
            program_execution_context.append(save_message)
        else:
            save_message = save_result.message
            if save_result.path:
                compiled_references.append(save_result.to_reference(q))
                compiled_references[:] = dedupe_references(compiled_references)
            program_execution_context.append(f"Exploration save result: {save_result.status}. {save_message}")
        async for result in send_event(ChatEvent.STATUS, save_message):
            yield result

    # Save conversation once finish streaming
    asyncio.create_task(
        save_to_conversation_log(
            q,
            chat_response=full_response,
            user=user,
            compiled_references=compiled_references,
            online_results=online_results,
            code_results=code_results,
            research_results=research_results,
            inferred_queries=inferred_queries,
            client_application=user_scope.client_app,
            conversation_id=str(conversation.id),
            query_images=uploaded_images,
            train_of_thought=train_of_thought,
            raw_query_files=raw_query_files,
            relevant_memories=relevant_memories,
            generated_images=generated_images,
            generated_mermaidjs_diagram=generated_mermaidjs_diagram,
            used_notes_tool_loop=used_notes_tool_loop,
            tracer=tracer,
        )
    )

    # Signal end of LLM response after the loop finishes
    async for result in send_event(ChatEvent.END_LLM_RESPONSE, ""):
        yield result

    # Send Usage Metadata once llm interactions are complete
    if tracer.get("usage"):
        async for event in send_event(ChatEvent.USAGE, tracer.get("usage")):
            yield event
    async for result in send_event(ChatEvent.END_RESPONSE, ""):
        yield result
    logger.debug("Finished streaming response")

    # Cancel the disconnect monitor task if it is still running
    await cancel_disconnect_monitor()


@api_chat.websocket("/ws")
@requires(["authenticated"])
async def chat_ws(
    websocket: WebSocket,
    common: CommonQueryParams,
):
    # Validate WebSocket Origin
    origin = websocket.headers.get("origin")
    if not is_allowed_websocket_origin(origin, websocket.headers.get("host")):
        await websocket.close(code=1008, reason="Origin not allowed")
        return

    # Limit open websocket connections per user
    user = websocket.scope["user"].object
    connection_manager = WebSocketConnectionManager(max_connections=10)
    connection_id = str(uuid.uuid4())

    if not await connection_manager.can_connect(websocket):
        await websocket.close(code=1008, reason="Connection limit exceeded")
        logger.info(f"WebSocket connection rejected for user {user.id}: connection limit exceeded")
        return

    await websocket.accept()

    # Note new websocket connection for the user
    await connection_manager.register_connection(user, connection_id)

    # Initialize rate limiters
    rate_limiter_per_minute = ApiUserRateLimiter(requests=20, window=60, slug="chat_minute")
    rate_limiter_per_day = ApiUserRateLimiter(requests=100, window=60 * 60 * 24, slug="chat_day")
    image_rate_limiter = ApiImageRateLimiter(max_images=10, max_combined_size_mb=20)

    # Shared interrupt queue for communicating interrupts to ongoing research
    interrupt_queue: asyncio.Queue = asyncio.Queue(maxsize=10)
    current_task = None

    try:
        while True:
            data = await websocket.receive_json()

            # Check if this is an interrupt message
            if data.get("type") == "interrupt":
                if current_task and not current_task.done():
                    # Send interrupt signal to the ongoing task
                    await interrupt_queue.put(data.get("query") or ChatEvent.END_EVENT.value)
                    logger.info(
                        f"Interrupt signal sent to ongoing task for user {websocket.scope['user'].object.id} with query: {data.get('query')}"
                    )
                    if data.get("query"):
                        ack_type = "interrupt_message_acknowledged"
                        await websocket.send_text(json.dumps({"type": ack_type}))
                    else:
                        ack_type = "interrupt_acknowledged"
                        await websocket.send_text(json.dumps({"type": ack_type}))
                else:
                    ack_type = "interrupt_acknowledged"
                    await websocket.send_text(json.dumps({"type": ack_type}))
                    logger.info(f"No ongoing task to interrupt for user {websocket.scope['user'].object.id}")
                continue

            # Handle regular chat messages - ensure data has required fields
            if "q" not in data:
                await websocket.send_text(json.dumps({"error": "Missing required field 'q' in chat message"}))
                continue

            body = ChatRequestBody(**data)

            # Apply rate limiting manually
            try:
                await rate_limiter_per_minute.check_websocket(websocket)
                await rate_limiter_per_day.check_websocket(websocket)
                image_rate_limiter.check_websocket(websocket, body)
            except HTTPException as e:
                await websocket.send_text(json.dumps({"error": e.detail}))
                continue

            # Cancel any ongoing task before starting a new one
            if current_task and not current_task.done():
                current_task.cancel()
                try:
                    await current_task
                except asyncio.CancelledError:
                    pass

            # Create a new task for processing the chat request
            current_task = asyncio.create_task(process_chat_request(websocket, body, common, interrupt_queue))

    except WebSocketDisconnect:
        logger.info(f"WebSocket disconnected for user {websocket.scope['user'].object.id}")
        if current_task and not current_task.done():
            interrupt_queue.put_nowait(ChatEvent.INTERRUPT.value)
    except Exception as e:
        logger.error(f"Error in websocket chat: {e}", exc_info=True)
        if current_task and not current_task.done():
            current_task.cancel()
        await websocket.close(code=1011, reason="Internal Server Error")
    finally:
        # Always unregister the connection on disconnect
        await connection_manager.unregister_connection(user, connection_id)


async def process_chat_request(
    websocket: WebSocket,
    body: ChatRequestBody,
    common: CommonQueryParams,
    interrupt_queue: asyncio.Queue,
):
    """Process a single chat request with interrupt support"""

    # Server-side message buffering for better streaming performance
    @dataclass
    class MessageBuffer:
        """Buffer for managing streamed chat messages with timing control."""

        content: str = ""
        timeout: Optional[asyncio.Task] = None
        last_flush: float = 0.0

        def __post_init__(self):
            """Initialize last_flush with current time if not provided."""
            if self.last_flush == 0.0:
                self.last_flush = time.perf_counter()

    message_buffer = MessageBuffer()
    thought_buffer = MessageBuffer()
    BUFFER_FLUSH_INTERVAL = 0.1  # 100ms buffer interval
    BUFFER_MAX_SIZE = 512  # Flush if buffer reaches this size

    async def flush_message_buffer():
        """Flush the accumulated message buffer to the client"""
        nonlocal message_buffer
        if message_buffer.content:
            buffered_content = message_buffer.content
            message_buffer.content = ""
            message_buffer.last_flush = time.perf_counter()
            if message_buffer.timeout:
                message_buffer.timeout.cancel()
                message_buffer.timeout = None
            yield buffered_content

    async def flush_thought_buffer():
        """Flush the accumulated thought buffer to the client"""
        nonlocal thought_buffer
        if thought_buffer.content:
            thought_event = json.dumps({"type": ChatEvent.THOUGHT.value, "data": thought_buffer.content})
            thought_buffer.content = ""
            thought_buffer.last_flush = time.perf_counter()
            if thought_buffer.timeout:
                thought_buffer.timeout.cancel()
                thought_buffer.timeout = None
            yield thought_event

    try:
        # Since we are using websockets, we can ignore the stream parameter and always stream
        response_iterator = event_generator(
            body,
            websocket.scope["user"],
            common,
            websocket.headers,
            websocket,
            interrupt_queue,
        )
        async for event in response_iterator:
            if not event:
                continue
            elif event.startswith("{") and event.endswith("}"):
                evt_json = json.loads(event)
                if evt_json["type"] == ChatEvent.END_LLM_RESPONSE.value:
                    thought_event = "".join([chunk async for chunk in flush_thought_buffer()])
                    if thought_event:
                        await websocket.send_text(thought_event)
                        await websocket.send_text(ChatEvent.END_EVENT.value)
                    # Flush remaining buffer content on end llm response event
                    chunks = "".join([chunk async for chunk in flush_message_buffer()])
                    if chunks:
                        await websocket.send_text(chunks)
                    await websocket.send_text(ChatEvent.END_EVENT.value)
                elif evt_json["type"] == ChatEvent.THOUGHT.value:
                    # Buffer THOUGHT events for better streaming performance
                    thought_buffer.content += str(evt_json.get("data", ""))

                    # Flush if buffer is too large or enough time has passed
                    current_time = time.perf_counter()
                    should_flush_time = (current_time - thought_buffer.last_flush) >= BUFFER_FLUSH_INTERVAL
                    should_flush_size = len(thought_buffer.content) >= BUFFER_MAX_SIZE

                    if should_flush_size or should_flush_time:
                        thought_event = "".join([chunk async for chunk in flush_thought_buffer()])
                        await websocket.send_text(thought_event)
                        await websocket.send_text(ChatEvent.END_EVENT.value)
                    else:
                        # Cancel any previous timeout tasks to reset the flush timer
                        if thought_buffer.timeout:
                            thought_buffer.timeout.cancel()

                        async def delayed_thought_flush():
                            """Flush thought buffer if no new messages arrive within debounce interval."""
                            await asyncio.sleep(BUFFER_FLUSH_INTERVAL)
                            # Check if there's still content to flush
                            thought_event = "".join([chunk async for chunk in flush_thought_buffer()])
                            if thought_event:
                                await websocket.send_text(thought_event)
                                await websocket.send_text(ChatEvent.END_EVENT.value)

                        # Flush buffer if no new thoughts arrive within debounce interval
                        thought_buffer.timeout = asyncio.create_task(delayed_thought_flush())
                    continue
                await websocket.send_text(event)
                await websocket.send_text(ChatEvent.END_EVENT.value)
            elif event != ChatEvent.END_EVENT.value:
                # Buffer MESSAGE events for better streaming performance
                message_buffer.content += str(event)

                # Flush if buffer is too large or enough time has passed
                current_time = time.perf_counter()
                should_flush_time = (current_time - message_buffer.last_flush) >= BUFFER_FLUSH_INTERVAL
                should_flush_size = len(message_buffer.content) >= BUFFER_MAX_SIZE

                if should_flush_size or should_flush_time:
                    chunks = "".join([chunk async for chunk in flush_message_buffer()])
                    await websocket.send_text(chunks)
                    await websocket.send_text(ChatEvent.END_EVENT.value)
                else:
                    # Cancel any previous timeout tasks to reset the flush timer
                    if message_buffer.timeout:
                        message_buffer.timeout.cancel()

                    async def delayed_flush():
                        """Flush message buffer if no new messages arrive within debounce interval."""
                        await asyncio.sleep(BUFFER_FLUSH_INTERVAL)
                        # Check if there's still content to flush
                        chunks = "".join([chunk async for chunk in flush_message_buffer()])
                        if chunks:
                            await websocket.send_text(chunks)
                            await websocket.send_text(ChatEvent.END_EVENT.value)

                    # Flush buffer if no new messages arrive within debounce interval
                    message_buffer.timeout = asyncio.create_task(delayed_flush())
    except asyncio.CancelledError:
        logger.debug(f"Chat request cancelled for user {websocket.scope['user'].object.id}")
        raise
    except Exception as e:
        await websocket.send_text(json.dumps({"error": "Internal server error"}))
        logger.error(f"Error processing chat request: {e}", exc_info=True)
        raise


@api_chat.post("")
@requires(["authenticated"])
async def chat(
    request: Request,
    common: CommonQueryParams,
    body: ChatRequestBody,
    rate_limiter_per_minute=Depends(ApiUserRateLimiter(requests=20, window=60, slug="chat_minute")),
    rate_limiter_per_day=Depends(ApiUserRateLimiter(requests=100, window=60 * 60 * 24, slug="chat_day")),
    image_rate_limiter=Depends(ApiImageRateLimiter(max_images=10, max_combined_size_mb=20)),
):
    if body.conversation_id is not None and not body.create_new:
        conversation = await ConversationAdapters.aget_conversation_by_user(
            request.user.object,
            request.user.client_app,
            body.conversation_id,
        )
        if conversation is None:
            response_data = {
                "response": f"Conversation {body.conversation_id} not found",
                "references": {},
                "usage": {},
                "images": [],
                "files": [],
                "mermaidjsDiagram": [],
            }
            return Response(content=json.dumps(response_data), media_type="application/json", status_code=404)

    response_iterator = event_generator(
        body,
        request.user,
        common,
        request.headers,
        request,
    )

    # Stream Text Response
    if body.stream:
        return StreamingResponse(response_iterator, media_type="text/plain")
    # Non-Streaming Text Response
    else:
        response_data = await read_chat_stream(response_iterator)
        status_code = 200
        if (
            body.conversation_id is not None
            and response_data.get("response") == f"Conversation {body.conversation_id} not found"
        ):
            status_code = 404
        return Response(content=json.dumps(response_data), media_type="application/json", status_code=status_code)
