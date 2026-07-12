import asyncio
import base64
import json
import logging
import time
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional
from urllib.parse import unquote

from asgiref.sync import sync_to_async
from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
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
    aget_user_name,
)
from khoj.database.models import Agent, KhojUser
from khoj.processor.conversation.agent_tool_loop import collect_agent_context_and_actions
from khoj.processor.conversation.conversation_turn import ConversationTurn, persist_conversation_turn
from khoj.processor.conversation.knowledge_workspace import (
    dedupe_workspace_evidence,
    get_workspace_sources,
    read_workspace_document,
    search_indexed_evidence,
)
from khoj.processor.conversation.utils import (
    ResponseWithThought,
    defilter_query,
)
from khoj.processor.conversation.vault_actions import (
    VaultActionError,
    VaultActionTurnConflict,
    create_vault_action_batch,
    delete_conversations_with_vault_protection,
    serialize_vault_action_batch,
    web_vault_write_enabled,
)
from khoj.processor.conversation.vault_policy import load_vault_policy
from khoj.processor.tools.online_search import deduplicate_organic_results
from khoj.routers.helpers import (
    ApiImageRateLimiter,
    ApiUserRateLimiter,
    ChatEvent,
    ChatRequestBody,
    CommonQueryParams,
    DeleteMessageRequestBody,
    WebSocketConnectionManager,
    acreate_title_from_history,
    agenerate_chat_response,
    gather_raw_query_files,
    get_message_from_queue,
    is_query_empty,
    is_ready_to_chat,
    parse_summary_command,
    read_chat_stream,
    select_offeragent_memories,
    send_message_to_model_wrapper,
    validate_chat_model,
)
from khoj.utils import state
from khoj.utils.helpers import (
    clean_text_for_db,
    convert_image_to_webp,
    get_country_code_from_timezone,
    get_country_name_from_timezone,
    is_web_search_enabled,
)
from khoj.utils.rawconfig import (
    FileFilterRequest,
    FilesFilterRequest,
    LocationData,
)

# Initialize Router
logger = logging.getLogger(__name__)
api_chat = APIRouter()
WEBSOCKET_INTERRUPT_GRACE_SECONDS = 5.0


def _enqueue_interrupt_signal(
    interrupt_queue: asyncio.Queue | None,
    item: Any,
    *,
    replace_pending: bool = False,
) -> bool:
    """Enqueue without blocking; hard interrupts may replace stale pending signals."""

    if interrupt_queue is None:
        return False
    try:
        interrupt_queue.put_nowait(item)
        return True
    except asyncio.QueueFull:
        if not replace_pending:
            return False

    while True:
        try:
            interrupt_queue.get_nowait()
        except asyncio.QueueEmpty:
            break
        interrupt_queue.task_done()

    try:
        interrupt_queue.put_nowait(item)
        return True
    except asyncio.QueueFull:
        return False


async def _wait_for_http_disconnect(request: Request, shutdown_event: asyncio.Event) -> bool:
    """Wait for disconnect or shutdown and always reap both watcher tasks."""

    receive_task = asyncio.create_task(request.receive())
    shutdown_task = asyncio.create_task(shutdown_event.wait())
    watcher_tasks = {receive_task, shutdown_task}
    try:
        done, _ = await asyncio.wait(watcher_tasks, return_when=asyncio.FIRST_COMPLETED)
        return receive_task in done and receive_task.result().get("type") == "http.disconnect"
    finally:
        for watcher_task in watcher_tasks:
            if not watcher_task.done():
                watcher_task.cancel()
        await asyncio.gather(*watcher_tasks, return_exceptions=True)


async def _shutdown_monitor_task(monitor_task: asyncio.Task | None, shutdown_event: asyncio.Event) -> None:
    """Ask a monitor to finish, only cancelling after a bounded grace period."""

    shutdown_event.set()
    if monitor_task is None:
        return
    if monitor_task.done():
        await asyncio.gather(monitor_task, return_exceptions=True)
        return
    done, _ = await asyncio.wait({monitor_task}, timeout=WEBSOCKET_INTERRUPT_GRACE_SECONDS)
    if done:
        await asyncio.gather(monitor_task, return_exceptions=True)
        return

    monitor_task.cancel()
    await asyncio.gather(monitor_task, return_exceptions=True)


def _vault_action_mode(body: ChatRequestBody, client: str | None) -> str:
    capabilities = body.client_capabilities or {}
    if capabilities.get("vaultActions") is not True:
        return "disabled"
    if client == "obsidian":
        return "client_actions"
    if client == "web" and web_vault_write_enabled():
        return "server_review"
    return "disabled"


def _collect_vault_actions(references: list[dict[str, Any]]) -> list[dict[str, Any]]:
    actions = []
    for reference in references:
        action = reference.get("vault_action")
        if isinstance(action, dict):
            actions.append(action)
    return actions


NON_STREAM_STRUCTURED_EVENTS = {
    ChatEvent.MESSAGE,
    ChatEvent.REFERENCES,
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
    conversation = ConversationAdapters.get_conversation_by_user(user=user, conversation_id=conversation_id)

    if conversation is None:
        return Response(
            content=json.dumps({"status": "error", "message": f"Conversation: {conversation_id} not found"}),
            status_code=404,
        )

    agent_metadata = {
        "slug": AgentAdapters.DEFAULT_AGENT_SLUG,
        "name": AgentAdapters.DEFAULT_AGENT_NAME,
        "color": "orange",
        "icon": "Lightbulb",
        "persona": conversation.agent.personality if conversation.agent else "",
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

    try:
        deleted_count, _ = await sync_to_async(delete_conversations_with_vault_protection, thread_sensitive=True)(
            user=user,
            conversation_id=target_conversation_id,
        )
    except VaultActionError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
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
    conversations = ConversationAdapters.get_conversation_sessions(user)
    if recent:
        conversations = conversations[:8]

    sessions = conversations.values_list(
        "id",
        "slug",
        "title",
        "created_at",
        "updated_at",
    )

    session_values = [
        {
            "conversation_id": str(session[0]),
            "slug": session[2] or session[1],
            "agent_name": AgentAdapters.DEFAULT_AGENT_NAME,
            "created": session[3].strftime("%Y-%m-%d %H:%M:%S"),
            "updated": session[4].strftime("%Y-%m-%d %H:%M:%S"),
            "agent_icon": "Lightbulb",
            "agent_color": "orange",
        }
        for session in sessions
    ]

    return Response(content=json.dumps(session_values), media_type="application/json", status_code=200)


@api_chat.post("/sessions")
@requires(["authenticated"])
async def create_chat_session(
    request: Request,
    common: CommonQueryParams,
):
    user = request.user.object

    conversation = await ConversationAdapters.acreate_conversation_session(user)

    response = {"conversation_id": str(conversation.id)}

    return Response(content=json.dumps(response), media_type="application/json", status_code=200)


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
    conversation = await ConversationAdapters.aset_conversation_title(user, conversation_id, title)

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

    await conversation.asave(update_fields=["slug", "updated_at"])

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


async def run_conversation_turn(
    body: ChatRequestBody,
    user_scope: Any,
    common: CommonQueryParams,
    headers: Headers,
    request_obj: Request | WebSocket,
    parent_interrupt_queue: asyncio.Queue = None,
):
    shutdown_event = asyncio.Event()
    monitor_tasks: list[asyncio.Task] = []
    iterator = _run_conversation_turn_impl(
        body,
        user_scope,
        common,
        headers,
        request_obj,
        parent_interrupt_queue,
        shutdown_event=shutdown_event,
        monitor_tasks=monitor_tasks,
    )
    try:
        async for event in iterator:
            yield event
    finally:
        shutdown_event.set()
        try:
            await iterator.aclose()
        finally:
            for monitor_task in monitor_tasks:
                await _shutdown_monitor_task(monitor_task, shutdown_event)


async def _run_conversation_turn_impl(
    body: ChatRequestBody,
    user_scope: Any,
    common: CommonQueryParams,
    headers: Headers,
    request_obj: Request | WebSocket,
    parent_interrupt_queue: asyncio.Queue = None,
    *,
    shutdown_event: asyncio.Event,
    monitor_tasks: list[asyncio.Task],
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

    online_results: Dict = dict()
    compiled_references: List[Any] = []
    inferred_queries: List[Any] = []
    attached_file_context = gather_raw_query_files(query_files)

    vault_actions: list[dict[str, Any]] = []
    vault_action_event_payload: dict[str, Any] | None = None
    program_execution_context: List[str] = []
    user_message_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    relevant_memories = []
    full_response = ""
    used_workspace_tools = False

    turn = ConversationTurn(
        user=user,
        user_message=q,
        turn_id=turn_id,
        conversation_id=conversation_id,
        user_message_time=user_message_time,
        compiled_references=compiled_references,
        online_results=online_results,
        inferred_queries=inferred_queries,
        query_images=uploaded_images,
        raw_query_files=raw_query_files or [],
        train_of_thought=train_of_thought,
        tracer=tracer,
        writer=persist_conversation_turn,
    )

    def sync_turn_state() -> None:
        turn.user_message = q
        turn.conversation_id = conversation_id
        turn.response = full_response
        turn.used_workspace_tools = used_workspace_tools

    # Create a task to monitor for disconnections
    disconnect_monitor_task = None

    async def monitor_disconnection():
        nonlocal q, defiltered_query
        interrupt_acknowledged: asyncio.Event | None = None
        try:
            if isinstance(request_obj, Request):
                if await _wait_for_http_disconnect(request_obj, shutdown_event):
                    logger.debug(f"Request cancelled. User {user} disconnected from {common.client} client.")
                    cancellation_event.set()
            elif isinstance(request_obj, WebSocket):
                while not cancellation_event.is_set() and not shutdown_event.is_set():
                    if request_obj.client_state != WebSocketState.CONNECTED:
                        cancellation_event.set()
                        break
                    queued_message = get_message_from_queue(parent_interrupt_queue)
                    if queued_message:
                        if (
                            isinstance(queued_message, tuple)
                            and len(queued_message) == 2
                            and isinstance(queued_message[1], asyncio.Event)
                        ):
                            interrupt_query, interrupt_acknowledged = queued_message
                        else:
                            interrupt_query = queued_message
                        if interrupt_query == ChatEvent.END_EVENT.value:
                            cancellation_event.set()
                            logger.debug(f"Chat cancelled by user {user} via interrupt queue.")
                        elif interrupt_query == ChatEvent.INTERRUPT.value:
                            cancellation_event.set()
                            logger.debug("Chat interrupted.")
                        else:
                            logger.info(f"Continuing chat with the new instruction: {interrupt_query}")
                            q += f"\n\n{interrupt_query}"
                            defiltered_query += f"\n\n{defilter_query(interrupt_query)}"
                    await asyncio.sleep(0.1)

                logger.debug(f"WebSocket disconnected or chat cancelled by user {user} from {common.client} client.")
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.error(f"Error in disconnect monitor: {error}", exc_info=True)
        finally:
            if conversation and (cancellation_event.is_set() or shutdown_event.is_set()):
                sync_turn_state()
                await turn.persist(interrupted=True, update_memory=False)
            if interrupt_acknowledged:
                interrupt_acknowledged.set()

    # Cancel the disconnect monitor task if it is still running
    async def cancel_disconnect_monitor():
        if disconnect_monitor_task and not disconnect_monitor_task.done():
            logger.debug(f"Stopping disconnect monitor task for user {user}")
        await _shutdown_monitor_task(disconnect_monitor_task, shutdown_event)

    async def send_event(event_type: ChatEvent, data: str | dict):
        nonlocal ttft
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

            if _should_emit_structured_event(event_type, stream):
                yield json.dumps({"type": event_type.value, "data": data})
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
        nonlocal full_response
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
        if conversation:
            full_response = response
            sync_turn_state()
            await turn.persist(update_memory=False)
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
    monitor_tasks.append(disconnect_monitor_task)

    if is_query_empty(q):
        async for result in send_llm_response("Please ask your query to get started.", tracer.get("usage")):
            yield result
        return

    # Automated task execution is an exact transport marker, not a semantic intent guess.
    automation_parts = q.lstrip().split(maxsplit=1)
    if automation_parts and automation_parts[0] == "/automated_task":
        q = automation_parts[1] if len(automation_parts) > 1 else ""

    # Summarization changes input preparation, not the runtime that executes the turn.
    try:
        q, summary_requested = parse_summary_command(q)
    except ValueError as error:
        async for result in send_llm_response(str(error), tracer.get("usage")):
            yield result
        return
    defiltered_query = defilter_query(q)

    conversation = await ConversationAdapters.aget_conversation_by_user(
        user,
        conversation_id=conversation_id,
        title=title,
        create_new=body.create_new,
    )
    if not conversation:
        async for result in send_llm_response(f"Conversation {conversation_id} not found", tracer.get("usage")):
            yield result
        return
    conversation_id = str(conversation.id)
    turn.conversation_id = conversation_id

    async for event in send_event(ChatEvent.METADATA, {"conversationId": conversation_id, "turnId": turn_id}):
        yield event

    agent: Agent | None = None
    default_agent = await AgentAdapters.aget_default_agent()
    if conversation.agent and conversation.agent != default_agent:
        agent = conversation.agent

    if not conversation.agent:
        conversation.agent = default_agent
        await conversation.asave(update_fields=["agent", "updated_at"])
        agent = default_agent

    await is_ready_to_chat(user)
    user_name = await aget_user_name(user)
    location = None
    if city or region or country or country_code:
        location = LocationData(city=city, region=region, country=country, country_code=country_code)
    if await ConversationAdapters.ais_memory_enabled(user):
        relevant_memories.extend(await select_offeragent_memories(user, q, agent, tracer=tracer))

    workspace_sources = get_workspace_sources()
    notes_local_source_available = workspace_sources.local_enabled
    notes_openkb_source_available = workspace_sources.openkb_enabled
    vault_policy = load_vault_policy(workspace_sources.local_root if workspace_sources.local_enabled else None)
    vault_action_mode = _vault_action_mode(body, common.client)
    vault_actions_supported = vault_action_mode != "disabled"

    # If interrupted message in DB
    if last_message := await ConversationAdapters.apop_message(
        user,
        conversation_id,
        interrupted=True,
    ):
        # Populate context from interrupted message
        online_results.update({key: val.model_dump() for key, val in last_message.onlineContext.items() or []})
        compiled_references.extend(ref.model_dump() for ref in last_message.context or [])
        train_of_thought.extend(thought.model_dump() for thought in last_message.trainOfThought or [])
        logger.info(f"Loaded interrupted partial context from conversation {conversation_id}.")

    await conversation.arefresh_from_db(fields=["conversation_log"])
    chat_history = conversation.messages

    if summary_requested:
        if not conversation.file_filters and not attached_file_context:
            async for result in send_llm_response(
                "No files selected for summarization. Please select one or more files first.",
                tracer.get("usage"),
            ):
                yield result
            return

        selected_documents: list[str] = []
        missing_documents: list[str] = []
        for selected_path in conversation.file_filters:
            document = await read_workspace_document(str(selected_path), user, max_lines=200)
            if document is None:
                missing_documents.append(str(selected_path))
                continue
            path, content = document
            selected_documents.append(f"File: {path}\n\n{content}")
            compiled_references.append(
                {
                    "query": "selected-file-summary",
                    "file": path,
                    "uri": path,
                    "compiled": content,
                    "source": "selected_file",
                }
            )

        if missing_documents:
            program_execution_context.append("Selected files that could not be read: " + ", ".join(missing_documents))
        if not selected_documents and conversation.file_filters and not attached_file_context:
            async for result in send_llm_response(
                "I couldn't read the selected files, so no summary was generated.",
                tracer.get("usage"),
            ):
                yield result
            return
        if selected_documents:
            attached_file_context = "\n\n".join(part for part in (attached_file_context, *selected_documents) if part)
            used_workspace_tools = True
        program_execution_context.append(
            "The user requested a summary of the explicitly selected files. Base the response on those files."
        )

    planner_failed = False
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

        agent_result = await collect_agent_context_and_actions(
            q,
            chat_history,
            user=user,
            agent=agent,
            send_message=agent_runtime_send_message,
            send_status=status_messages.append,
            client_app=common.client,
            allow_local_kb=notes_local_source_available,
            allow_openkb=notes_openkb_source_available,
            allow_web=is_web_search_enabled(),
            conversation_id=conversation_id,
            write_mode="client_actions" if vault_actions_supported else "disabled",
            vault_policy=vault_policy,
            location=location,
            query_images=uploaded_images,
            query_files=attached_file_context,
            relevant_memories=relevant_memories,
            tracer=tracer,
        )
        planner_failed = agent_result.planner_failed
        compiled_references.extend(agent_result.references)
        used_workspace_tools = used_workspace_tools or agent_result.used_workspace_tools
        vault_actions.extend(_collect_vault_actions(agent_result.references))
        inferred_queries.extend(agent_result.inferred_queries)
        online_results.update(agent_result.online_results)
        program_execution_context.extend(agent_result.program_context)
        if agent_result.errors:
            program_execution_context.append("Agent tool errors: " + "; ".join(agent_result.errors[:8]))
        for message in status_messages:
            async for result in send_event(ChatEvent.STATUS, message):
                yield result
    except HTTPException as error:
        async for result in send_llm_response(str(error.detail), tracer.get("usage")):
            yield result
        return
    except Exception as error:
        planner_failed = True
        logger.error(f"Error running agent tools: {error}. Falling back to a context-free response.", exc_info=True)
        program_execution_context.append(
            "The agent planner failed before confirming any file change. No file change is pending or applied."
        )
        async for result in send_event(ChatEvent.STATUS, "Agent tools failed. I'll answer without tool results."):
            yield result

    if not compiled_references and not used_workspace_tools and not planner_failed and not summary_requested:
        indexed_references = await search_indexed_evidence(user, defiltered_query, agent)
        if indexed_references:
            compiled_references.extend(indexed_references)
            inferred_queries.append(defiltered_query)
            async for result in send_event(ChatEvent.STATUS, "Searched synced knowledge base"):
                yield result

    compiled_references[:] = dedupe_workspace_evidence(compiled_references)

    if vault_actions and vault_action_mode == "server_review":
        batch = None
        turn_conflict = False
        try:
            batch = await sync_to_async(create_vault_action_batch, thread_sensitive=True)(
                user=user,
                conversation=conversation,
                turn_id=turn_id,
                actions=vault_actions,
            )
        except VaultActionTurnConflict as error:
            batch = error.batch
            turn_conflict = True
            compiled_references[:] = [
                reference for reference in compiled_references if not reference.get("vault_action")
            ]
            logger.warning("Reusing existing VaultAction batch %s for repeated turn", batch.id)
        except Exception as error:
            logger.error("Failed to create Web VaultAction batch", exc_info=True)
            compiled_references[:] = [
                reference for reference in compiled_references if not reference.get("vault_action")
            ]
            vault_actions.clear()
            program_execution_context.append(
                f"Web VaultAction batch creation failed: {error}. "
                "Final answer must say no file change is pending or applied."
            )

        if batch is not None:
            vault_action_event_payload = serialize_vault_action_batch(batch)
            paths = [action.get("path") for action in batch.actions if action.get("path")]
            compiled_references.append(
                {
                    "query": "vault_action_batch",
                    "file": ", ".join(paths),
                    "uri": f"vault-action://{batch.id}",
                    "compiled": f"VaultAction batch {batch.id} has status {batch.status}.",
                    "action": "vault_action_batch",
                    "status": batch.status,
                    "batch_id": str(batch.id),
                    "files": paths,
                }
            )
            if batch.status == "pending":
                qualifier = "Existing conflicting turn batch" if turn_conflict else "Web VaultAction batch"
                program_execution_context.append(
                    f"{qualifier} {batch.id} is pending review for: {', '.join(paths)}. "
                    "Final answer must say these changes are waiting for user confirmation, not already written."
                )
            else:
                program_execution_context.append(
                    f"Existing Web VaultAction batch {batch.id} has status {batch.status} for: {', '.join(paths)}. "
                    "Final answer must report this exact state and must not claim a new batch was created."
                )
    elif vault_actions and vault_action_mode == "client_actions":
        vault_action_event_payload = {"actions": vault_actions}

    ## Send Gathered References
    unique_online_results = deduplicate_organic_results(online_results)
    async for result in send_event(
        ChatEvent.REFERENCES,
        {
            "inferredQueries": inferred_queries,
            "context": compiled_references,
            "onlineContext": unique_online_results,
        },
    ):
        yield result

    # Check if the user has disconnected
    if cancellation_event.is_set():
        logger.debug(f"Stopping LLM response to user {user} on {common.client} client.")
        # Cancel the disconnect monitor task if it is still running
        await cancel_disconnect_monitor()
        return

    if vault_action_event_payload:
        async for result in send_event(ChatEvent.VAULT_ACTIONS, vault_action_event_payload):
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
        user,
        location,
        user_name,
        uploaded_images,
        attached_file_context,
        relevant_memories,
        program_execution_context,
        tracer,
    )

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

    # Disconnect and normal completion share one exactly-once persistence boundary.
    sync_turn_state()
    await turn.persist()

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


async def _interrupt_chat_task(task: asyncio.Task | None, interrupt_queue: asyncio.Queue | None) -> None:
    if task is None:
        return
    if not task.done():
        if interrupt_queue is not None:
            acknowledged = asyncio.Event()
            queued = _enqueue_interrupt_signal(
                interrupt_queue,
                (ChatEvent.INTERRUPT.value, acknowledged),
                replace_pending=True,
            )
            if queued:
                acknowledgement_task = asyncio.create_task(acknowledged.wait())
                await asyncio.wait(
                    {task, acknowledgement_task},
                    timeout=WEBSOCKET_INTERRUPT_GRACE_SECONDS,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if not acknowledgement_task.done():
                    acknowledgement_task.cancel()
                    await asyncio.gather(acknowledgement_task, return_exceptions=True)
        if not task.done():
            task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception:
        logger.warning("Previous WebSocket chat task failed while being interrupted.", exc_info=True)


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

    current_interrupt_queue: asyncio.Queue | None = None
    current_task: asyncio.Task | None = None

    try:
        while True:
            data = await websocket.receive_json()

            # Check if this is an interrupt message
            if data.get("type") == "interrupt":
                if current_task and not current_task.done():
                    interrupt_query = data.get("query")
                    if interrupt_query:
                        queued = _enqueue_interrupt_signal(current_interrupt_queue, interrupt_query)
                        if not queued:
                            await websocket.send_text(json.dumps({"error": "Interrupt queue is busy"}))
                            continue
                        ack_type = "interrupt_message_acknowledged"
                    else:
                        await _interrupt_chat_task(current_task, current_interrupt_queue)
                        ack_type = "interrupt_acknowledged"
                    logger.info(
                        f"Interrupt signal handled for user {websocket.scope['user'].object.id} with query: {interrupt_query}"
                    )
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
            if current_task:
                await _interrupt_chat_task(current_task, current_interrupt_queue)

            # Create a new task for processing the chat request
            current_interrupt_queue = asyncio.Queue(maxsize=10)
            current_task = asyncio.create_task(process_chat_request(websocket, body, common, current_interrupt_queue))

    except WebSocketDisconnect:
        logger.info(f"WebSocket disconnected for user {websocket.scope['user'].object.id}")
    except Exception as e:
        logger.error(f"Error in websocket chat: {e}", exc_info=True)
        if websocket.client_state == WebSocketState.CONNECTED:
            await websocket.close(code=1011, reason="Internal Server Error")
    finally:
        await _interrupt_chat_task(current_task, current_interrupt_queue)
        # Always unregister the connection on disconnect
        await connection_manager.unregister_connection(user, connection_id)


async def process_chat_request(
    websocket: WebSocket,
    body: ChatRequestBody,
    common: CommonQueryParams,
    interrupt_queue: asyncio.Queue,
):
    """Process a single chat request with interrupt support"""

    try:
        async for event in run_conversation_turn(
            body,
            websocket.scope["user"],
            common,
            websocket.headers,
            websocket,
            interrupt_queue,
        ):
            if event and event != ChatEvent.END_EVENT.value:
                await websocket.send_text(event)
                await websocket.send_text(ChatEvent.END_EVENT.value)
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
            body.conversation_id,
        )
        if conversation is None:
            response_data = {
                "response": f"Conversation {body.conversation_id} not found",
                "references": {},
                "usage": {},
            }
            return Response(content=json.dumps(response_data), media_type="application/json", status_code=404)

    response_iterator = run_conversation_turn(
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
