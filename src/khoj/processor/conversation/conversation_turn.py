import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Awaitable, Callable, Optional

from khoj.database.adapters import ConversationAdapters
from khoj.database.models import KhojUser
from khoj.processor.conversation.utils import (
    is_promptrace_enabled,
    merge_message_into_conversation_trace,
    message_to_log,
)
from khoj.utils.rawconfig import FileAttachment

logger = logging.getLogger(__name__)

TurnWriter = Callable[..., Awaitable[Any]]
_background_memory_tasks: set[asyncio.Task] = set()

_ARTIFACT_SOURCE_KEYS = ("query", "file", "uri", "action", "status", "start_line", "end_line", "kb_root")


@dataclass
class ConversationTurn:
    """Own the mutable result and exactly-once persistence boundary for one chat turn."""

    user: KhojUser
    user_message: str
    turn_id: str
    conversation_id: Optional[str] = None
    user_message_time: str = field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    response: str = ""
    compiled_references: list[dict[str, Any]] = field(default_factory=list)
    online_results: dict[str, Any] = field(default_factory=dict)
    research_results: list[Any] = field(default_factory=list)
    inferred_queries: list[str] = field(default_factory=list)
    query_images: list[str] = field(default_factory=list)
    raw_query_files: list[FileAttachment] = field(default_factory=list)
    train_of_thought: list[dict[str, Any]] = field(default_factory=list)
    used_workspace_tools: bool = False
    tracer: dict[str, Any] = field(default_factory=dict)
    writer: Optional[TurnWriter] = field(default=None, repr=False)
    _persisted: bool = field(default=False, init=False, repr=False)
    _persistence_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.writer is None:
            self.writer = persist_conversation_turn

    @property
    def persisted(self) -> bool:
        return self._persisted

    async def persist(self, *, interrupted: bool = False, update_memory: bool = True) -> bool:
        """Persist this turn at most once, even when disconnect and completion race."""

        if not self.conversation_id:
            return False
        async with self._persistence_lock:
            if self._persisted:
                return False
            response = self.response
            if interrupted:
                self.response = ""
            try:
                assert self.writer is not None
                await self.writer(self, update_memory=update_memory and not interrupted)
            finally:
                self.response = response
            self._persisted = True
            return True


async def _run_memory_update(turn: ConversationTurn, agent: Any) -> None:
    from khoj.routers.helpers import ai_update_offeragent_memory

    await ai_update_offeragent_memory(
        user=turn.user,
        latest_user_message=turn.user_message,
        agent=agent,
        source_turn_id=turn.turn_id,
        used_workspace_tools=turn.used_workspace_tools,
        tracer=turn.tracer,
    )


def _log_memory_update_result(task: asyncio.Task) -> None:
    try:
        task.result()
    except asyncio.CancelledError:
        logger.debug("OfferAgent background memory update was cancelled")
    except Exception:
        logger.exception("OfferAgent background memory update failed")


def schedule_memory_update(turn: ConversationTurn, agent: Any = None) -> asyncio.Task:
    task = asyncio.create_task(
        _run_memory_update(turn, agent),
        name=f"offeragent-memory-update-{turn.turn_id}",
    )
    _background_memory_tasks.add(task)
    task.add_done_callback(_log_memory_update_result)
    task.add_done_callback(_background_memory_tasks.discard)
    return task


async def persist_conversation_turn(turn: ConversationTurn, *, update_memory: bool = True):
    user_message_metadata = {
        "created": turn.user_message_time,
        "images": turn.query_images or None,
        "turnId": turn.turn_id,
    }
    if turn.raw_query_files:
        user_message_metadata["queryFiles"] = [file.model_dump(mode="json") for file in turn.raw_query_files]

    assistant_metadata = {
        "context": turn.compiled_references,
        "intent": {"inferred-queries": turn.inferred_queries, "type": "remember"},
        "onlineContext": turn.online_results,
        "researchContext": [result.to_dict() for result in turn.research_results]
        if turn.research_results and not turn.response
        else None,
        "trainOfThought": turn.train_of_thought,
        "turnId": turn.turn_id,
    }
    artifact = assistant_response_artifact(turn.response, turn.turn_id, turn.compiled_references)
    if artifact:
        assistant_metadata["artifacts"] = [artifact]
    messages = message_to_log(
        user_message=turn.user_message,
        chat_response=turn.response,
        user_message_metadata=user_message_metadata,
        khoj_message_metadata=assistant_metadata,
    )
    conversation = await ConversationAdapters.save_conversation(
        turn.user,
        messages,
        conversation_id=turn.conversation_id,
        user_message=turn.user_message,
    )
    if conversation is None:
        raise RuntimeError(f"Conversation {turn.conversation_id} could not be persisted")

    if update_memory:
        schedule_memory_update(turn, conversation.agent)
    if is_promptrace_enabled():
        merge_message_into_conversation_trace(turn.user_message, turn.response, turn.tracer)

    logger.info(
        'Saved Conversation Turn (%s):\nYou (%s): "%s"\n\nOfferAgent: "%s"',
        conversation.id,
        turn.user.username,
        turn.user_message,
        turn.response,
    )
    return conversation


def assistant_response_artifact(
    response: str,
    turn_id: str,
    references: list[dict[str, Any]],
) -> Optional[dict[str, Any]]:
    if not turn_id or not str(response or "").strip():
        return None
    source_refs = []
    for item in references:
        if not isinstance(item, dict):
            continue
        ref = {key: item[key] for key in _ARTIFACT_SOURCE_KEYS if item.get(key) not in (None, "", [])}
        if ref:
            source_refs.append(ref)
    return {
        "id": f"assistant:{turn_id}",
        "type": "assistant_response",
        "content": response,
        "source_refs": source_refs,
    }
