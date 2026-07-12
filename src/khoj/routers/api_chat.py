import asyncio
import base64
import json
import logging
import time
import uuid
from datetime import datetime
from functools import partial
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
    search_indexed_evidence,
)
from khoj.processor.conversation.offeragent_intent_router import route_offeragent_intent
from khoj.processor.conversation.prompts import no_entries_found
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
from khoj.processor.tools.online_search import (
    deduplicate_organic_results,
    read_webpages,
    search_online,
)
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
    gather_raw_query_files,
    generate_summary_from_files,
    get_message_from_queue,
    is_query_empty,
    is_ready_to_chat,
    parse_conversation_command,
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
    is_none_or_empty,
    is_web_search_enabled,
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


@api_chat.get("/options", response_class=Response)
async def chat_options(
    request: Request,
    common: CommonQueryParams,
) -> Response:
    cmd_options = {}
    for cmd in ConversationCommand:
        if cmd in [ConversationCommand.Online, ConversationCommand.Webpage] and not is_web_search_enabled():
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
    compiled_references: List[Any] = []
    inferred_queries: List[Any] = []
    attached_file_context = gather_raw_query_files(query_files)

    vault_actions: list[dict[str, Any]] = []
    vault_action_event_payload: dict[str, Any] | None = None
    conversation_commands: List[ConversationCommand] = []
    program_execution_context: List[str] = []
    user_message_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    relevant_memories = []
    full_response = ""
    used_workspace_tools = False
    workspace_has_evidence = False
    workspace_tools_failed = False

    turn = ConversationTurn(
        user=user,
        user_message=q,
        turn_id=turn_id,
        conversation_id=conversation_id,
        user_message_time=user_message_time,
        compiled_references=compiled_references,
        online_results=online_results,
        research_results=research_results,
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
                            _enqueue_interrupt_signal(
                                child_interrupt_queue,
                                interrupt_query,
                                replace_pending=True,
                            )
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
    cmds_to_rate_limit = []
    automation_parts = q.lstrip().split(maxsplit=1)
    if automation_parts and automation_parts[0] == "/automated_task":
        q = automation_parts[1] if len(automation_parts) > 1 else ""
        cmds_to_rate_limit += [ConversationCommand.AutomatedTask]

    # Explicit slash commands are exact first-token routes, never semantic guesses.
    try:
        conversation_command, q, explicit_command = parse_conversation_command(q)
    except ValueError as error:
        async for result in send_llm_response(str(error), tracer.get("usage")):
            yield result
        return
    conversation_commands = [conversation_command]

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
        research_results.extend(
            ResearchIteration(**iter_dict)
            for iter_dict in last_message.researchContext or []
            if iter_dict.get("summarizedResult")
        )
        train_of_thought.extend(thought.model_dump() for thought in last_message.trainOfThought or [])
        logger.info(f"Loaded interrupted partial context from conversation {conversation_id}.")

    await conversation.arefresh_from_db(fields=["conversation_log"])
    chat_history = conversation.messages

    requires_write_action = False

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
    )
    if route_decision.needs_clarification:
        if not explicit_command:
            async for result in send_llm_response(route_decision.question, tracer.get("usage")):
                yield result
            return
        program_execution_context.append(
            "The explicit command fixed the route, but write intent classification was inconclusive. "
            "Do not claim a file change unless a write tool result confirms it."
        )
    else:
        requires_write_action = route_decision.requires_vault_write

    if conversation_commands == [ConversationCommand.Default] and not explicit_command:
        routed_command = ConversationCommand(route_decision.route)
        if routed_command != ConversationCommand.Default:
            conversation_commands = [routed_command]
            inferred_queries.append(f"router:{route_decision.intent}:{route_decision.route}")
            async for result in send_event(
                ChatEvent.STATUS,
                f"**Routed by intent:** {route_decision.route}",
            ):
                yield result

    if requires_write_action and not vault_actions_supported:
        program_execution_context.append(
            "The user explicitly requested a persistent file change, but this client cannot prepare reviewed "
            "VaultActions. No file change is pending or applied; the final answer must report that exact state."
        )
    elif requires_write_action and conversation_commands not in (
        [ConversationCommand.Default],
        [ConversationCommand.Notes],
    ):
        program_execution_context.append(
            "The selected specialized command does not prepare reviewed VaultActions. No file change is pending or "
            "applied; the final answer must not claim the requested write succeeded."
        )

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
                require_write_action=requires_write_action and vault_actions_supported,
            )
            compiled_references.extend(agent_result.references)
            used_workspace_tools = used_workspace_tools or agent_result.used_workspace_tools
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
            if requires_write_action:
                program_execution_context.append(
                    "The write-capable agent runtime failed before confirming a VaultAction. No file change is "
                    "pending or applied; the final answer must report that exact state."
                )
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
    notes_requested = ConversationCommand.Notes in conversation_commands

    async def collect_notes_evidence():
        nonlocal used_workspace_tools, workspace_has_evidence, workspace_tools_failed
        allow_local_kb = notes_local_source_available
        allow_openkb = notes_openkb_source_available
        if used_workspace_tools or not (allow_local_kb or allow_openkb):
            return
        status_messages = []

        used_workspace_tools = True
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

            notes_result = await collect_agent_context_and_actions(
                q,
                chat_history,
                user=user,
                agent=agent,
                send_message=notes_send_message,
                send_status=status_messages.append,
                client_app=common.client,
                allow_local_kb=allow_local_kb,
                allow_openkb=allow_openkb,
                allow_web=False,
                conversation_id=conversation_id,
                write_mode="client_actions" if vault_actions_supported else "disabled",
                vault_policy=vault_policy,
                require_notes_evidence=True,
                require_write_action=requires_write_action and vault_actions_supported,
            )
            compiled_references.extend(notes_result.references)
            vault_actions.extend(_collect_vault_actions(notes_result.references))
            inferred_queries.extend(notes_result.inferred_queries)
            program_execution_context.extend(notes_result.program_context)
            if notes_result.errors:
                program_execution_context.append("Notes tool errors: " + "; ".join(notes_result.errors[:8]))
            if notes_result.planner_failed:
                workspace_tools_failed = True
                async for result in send_event(
                    ChatEvent.STATUS,
                    "Notes evidence tools failed. I did not read or modify the local knowledge base",
                ):
                    yield result
                return
            workspace_has_evidence = not is_none_or_empty(notes_result.references)
            for message in status_messages:
                async for result in send_event(ChatEvent.STATUS, message):
                    yield result
            if not workspace_has_evidence and notes_result.searched:
                program_execution_context.append(
                    "No Notes evidence found. Tools used: " + ", ".join(notes_result.searched[:8])
                )
        except Exception as e:
            workspace_tools_failed = True
            logger.error(f"Error using Notes evidence tools: {e}", exc_info=True)
            if requires_write_action:
                program_execution_context.append(
                    "The Notes runtime failed before confirming a VaultAction. No file change is pending or applied; "
                    "the final answer must report that exact state."
                )
            async for result in send_event(
                ChatEvent.STATUS, "Notes evidence tools failed. I did not read or modify the local knowledge base"
            ):
                yield result

    if notes_requested:
        async for result in collect_notes_evidence():
            yield result

        compiled_references[:] = dedupe_workspace_evidence(compiled_references)

    if (
        (conversation_commands == [ConversationCommand.General] or notes_requested)
        and is_none_or_empty(compiled_references)
        and not used_workspace_tools
        and not workspace_tools_failed
    ):
        indexed_references = await search_indexed_evidence(user, defiltered_query, agent)
        if indexed_references:
            compiled_references.extend(indexed_references)
            inferred_queries.append(defiltered_query)
            async for result in send_event(ChatEvent.STATUS, "Searched synced knowledge base"):
                yield result

    if notes_requested and workspace_tools_failed:
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
        and (used_workspace_tools or not (notes_local_source_available or notes_openkb_source_available))
    ):
        message = (
            "I couldn't find enough local knowledge base evidence to answer that."
            if used_workspace_tools
            else f"{no_entries_found.format()}"
        )
        async for result in send_llm_response(message, tracer.get("usage")):
            yield result
        return

    compiled_references[:] = dedupe_workspace_evidence(compiled_references)

    if (
        ConversationCommand.Notes in conversation_commands
        and is_none_or_empty(compiled_references)
        and not used_workspace_tools
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
                    online_results.clear()
                    online_results.update(result)
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
        research_results,
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
