import json
import logging
import math
import os
import re
from datetime import date, datetime, timedelta, timezone
from functools import wraps
from typing import (
    Any,
    Callable,
    Coroutine,
    Iterable,
    List,
    Optional,
    ParamSpec,
    TypeVar,
)
from uuid import UUID

import cron_descriptor
from apscheduler.job import Job
from asgiref.sync import sync_to_async
from django.contrib.sessions.backends.db import SessionStore
from django.db import transaction
from django.db.models import Q
from django.db.models.manager import BaseManager
from django.db.utils import IntegrityError
from django.utils import timezone as django_timezone
from django_apscheduler import util
from django_apscheduler.models import DjangoJob, DjangoJobExecution
from fastapi import HTTPException
from pydantic import ValidationError

from khoj.database.models import (
    Agent,
    AiModelApi,
    ChatMessageModel,
    ChatModel,
    Conversation,
    Entry,
    FileObject,
    KhojUser,
    McpServer,
    ProcessLock,
    RateLimitRecord,
    ServerChatSettings,
    UserConversationConfig,
    UserRequests,
    WebScraper,
)
from khoj.processor.conversation import prompts
from khoj.search_filter.date_filter import DateFilter
from khoj.search_filter.file_filter import FileFilter
from khoj.search_filter.word_filter import WordFilter
from khoj.utils import state
from khoj.utils.helpers import (
    clean_object_for_db,
    clean_text_for_db,
    in_debug_mode,
    is_none_or_empty,
    timer,
)

logger = logging.getLogger(__name__)


P = ParamSpec("P")
T = TypeVar("T")


def require_valid_user(func: Callable[P, T]) -> Callable[P, T]:
    @wraps(func)
    def sync_wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
        # Extract user from args/kwargs
        user = next((arg for arg in args if isinstance(arg, KhojUser)), None)
        if not user:
            user = next((val for val in kwargs.values() if isinstance(val, KhojUser)), None)

        # Throw error if user is not found
        if not user:
            raise ValueError("Khoj user argument required but not provided.")

        return func(*args, **kwargs)

    return sync_wrapper


def arequire_valid_user(func: Callable[P, Coroutine[Any, Any, T]]) -> Callable[P, Coroutine[Any, Any, T]]:
    @wraps(func)
    async def async_wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
        # Extract user from args/kwargs
        user = next((arg for arg in args if isinstance(arg, KhojUser)), None)
        if not user:
            user = next((v for v in kwargs.values() if isinstance(v, KhojUser)), None)

        # Throw error if user is not found
        if not user:
            raise ValueError("Khoj user argument required but not provided.")

        return await func(*args, **kwargs)

    return async_wrapper


@require_valid_user
def get_user_name(user: KhojUser):
    full_name = user.get_full_name()
    if not is_none_or_empty(full_name):
        return full_name

    return None


@require_valid_user
def get_user_photo(user: KhojUser):
    return None


async def aget_user_by_email(email: str) -> KhojUser:
    return await KhojUser.objects.filter(email=email).afirst()


def get_user_by_email(email: str) -> KhojUser:
    return KhojUser.objects.filter(email=email).first()


async def aget_user_by_uuid(uuid: str) -> KhojUser:
    return await KhojUser.objects.filter(uuid=uuid).afirst()


async def retrieve_user(session_id: str) -> KhojUser:
    session = SessionStore(session_key=session_id)
    if not await sync_to_async(session.exists)(session_key=session_id):
        raise HTTPException(status_code=401, detail="Invalid session")
    session_data = await sync_to_async(session.load)()
    user = await KhojUser.objects.filter(id=session_data.get("_auth_user_id")).afirst()
    if not user:
        raise HTTPException(status_code=401, detail="Invalid user")
    return user


def get_all_users() -> BaseManager[KhojUser]:
    return KhojUser.objects.all()


def delete_user_requests(max_age: timedelta = timedelta(days=1)):
    """Deletes UserRequests entries older than the specified max_age."""
    cutoff = django_timezone.now() - max_age
    deleted_count, _ = UserRequests.objects.filter(created_at__lte=cutoff).delete()
    return deleted_count


def delete_ratelimit_records(max_age: timedelta = timedelta(days=1)):
    """Deletes RateLimitRecord entries older than the specified max_age."""
    cutoff = django_timezone.now() - max_age
    deleted_count, _ = RateLimitRecord.objects.filter(created_at__lt=cutoff).delete()
    return deleted_count


@arequire_valid_user
async def aget_user_name(user: KhojUser):
    full_name = user.get_full_name()
    if not is_none_or_empty(full_name):
        return full_name

    return None


class ProcessLockAdapters:
    @staticmethod
    def get_process_lock(process_name: str):
        process_lock = ProcessLock.objects.filter(name=process_name).first()
        if process_lock and not ProcessLockAdapters.is_process_locked(process_lock):
            return None
        return process_lock

    @staticmethod
    def set_process_lock(process_name: str, max_duration_in_seconds: int = 600):
        return ProcessLock.objects.create(name=process_name, max_duration_in_seconds=max_duration_in_seconds)

    @staticmethod
    def is_process_locked_by_name(process_name: str):
        process_lock = ProcessLock.objects.filter(name=process_name).first()
        if not process_lock:
            return False
        return ProcessLockAdapters.is_process_locked(process_lock)

    @staticmethod
    def is_process_locked(process_lock: ProcessLock):
        started_at_ts = process_lock.started_at
        # Ensure started_at_ts is timezone-aware (UTC) if it's naive
        if django_timezone.is_naive(started_at_ts):
            started_at_ts = django_timezone.make_aware(started_at_ts, timezone.utc)

        max_duration_in_seconds = process_lock.max_duration_in_seconds
        if started_at_ts + timedelta(seconds=max_duration_in_seconds) < datetime.now(tz=timezone.utc):
            process_lock.delete()
            logger.info(f"🔓 Deleted stale {process_lock.name} process lock on timeout")
            return False
        return True

    @staticmethod
    def remove_process_lock(process_lock: ProcessLock):
        return process_lock.delete()

    @staticmethod
    def run_with_lock(func: Callable, operation: ProcessLock.Operation, max_duration_in_seconds: int = 600, **kwargs):
        # Exit early if process lock is already taken
        if ProcessLockAdapters.is_process_locked_by_name(operation):
            logger.debug(f"🔒 Skip executing {func} as {operation} lock is already taken")
            return

        success = False
        process_lock = None
        try:
            # Set process lock
            process_lock = ProcessLockAdapters.set_process_lock(operation, max_duration_in_seconds)
            logger.info(f"🔐 Locked {operation} to execute {func}")

            # Execute Function
            with timer(f"🔒 Run {func} with {operation} process lock", logger):
                func(**kwargs)
            success = True
        except IntegrityError as e:
            logger.debug(f"⚠️ Unable to create the process lock for {func} with {operation}: {e}")
            success = False
        except Exception as e:
            logger.error(f"🚨 Error executing {func} with {operation} process lock: {e}", exc_info=True)
            success = False
        finally:
            # Remove Process Lock
            if process_lock:
                ProcessLockAdapters.remove_process_lock(process_lock)
                logger.info(
                    f"🔓 Unlocked {operation} process after executing {func} {'Succeeded' if success else 'Failed'}"
                )
            else:
                logger.debug(f"Skip removing {operation} process lock as it was not set")


@util.close_old_connections
def run_with_process_lock(*args, **kwargs):
    """Wrapper function used for scheduling jobs.
    Required as APScheduler can't discover the `ProcessLockAdapter.run_with_lock' method on its own.
    """
    return ProcessLockAdapters.run_with_lock(*args, **kwargs)


class AgentAdapters:
    DEFAULT_AGENT_NAME = "OfferAgent"
    LEGACY_DEFAULT_AGENT_NAME = "Khoj"
    DEFAULT_AGENT_SLUG = "khoj"

    @staticmethod
    def get_default_agent():
        return (
            Agent.objects.filter(slug=AgentAdapters.DEFAULT_AGENT_SLUG).first()
            or Agent.objects.filter(
                name__in=[AgentAdapters.DEFAULT_AGENT_NAME, AgentAdapters.LEGACY_DEFAULT_AGENT_NAME]
            ).first()
        )

    @staticmethod
    def create_default_agent():
        default_chat_model = ConversationAdapters.get_default_chat_model(user=None)
        if default_chat_model is None:
            logger.info("No default conversation config found, skipping default agent creation")
            return None
        default_personality = prompts.personality.format(current_date="placeholder", day_of_week="placeholder")

        agent = (
            Agent.objects.filter(slug=AgentAdapters.DEFAULT_AGENT_SLUG).first()
            or Agent.objects.filter(
                name__in=[AgentAdapters.DEFAULT_AGENT_NAME, AgentAdapters.LEGACY_DEFAULT_AGENT_NAME]
            ).first()
        )

        if agent:
            agent.personality = default_personality
            agent.chat_model = default_chat_model
            agent.slug = AgentAdapters.DEFAULT_AGENT_SLUG
            agent.name = AgentAdapters.DEFAULT_AGENT_NAME
            agent.save()
        else:
            agent = Agent.objects.create(
                name=AgentAdapters.DEFAULT_AGENT_NAME,
                chat_model=default_chat_model,
                personality=default_personality,
                slug=AgentAdapters.DEFAULT_AGENT_SLUG,
            )
            Conversation.objects.filter(agent=None).update(agent=agent)

        return agent

    @staticmethod
    async def aget_default_agent():
        agent = await Agent.objects.filter(slug=AgentAdapters.DEFAULT_AGENT_SLUG).afirst()
        if agent:
            return agent
        return await Agent.objects.filter(
            name__in=[AgentAdapters.DEFAULT_AGENT_NAME, AgentAdapters.LEGACY_DEFAULT_AGENT_NAME]
        ).afirst()

    @staticmethod
    def get_agent_chat_model(agent: Agent, user: Optional[KhojUser]) -> Optional[ChatModel]:
        return ConversationAdapters.get_default_chat_model(user)

    @staticmethod
    async def aget_agent_chat_model(agent: Agent, user: Optional[KhojUser]) -> Optional[ChatModel]:
        return await sync_to_async(AgentAdapters.get_agent_chat_model)(agent, user)


class ConversationAdapters:
    @staticmethod
    @require_valid_user
    def get_conversation_by_user(user: KhojUser, conversation_id: str = None) -> Optional[Conversation]:
        if conversation_id is not None:
            try:
                UUID(str(conversation_id))
            except ValueError:
                return None
            conversation = Conversation.objects.filter(user=user, id=conversation_id).order_by("-updated_at").first()
        else:
            agent = AgentAdapters.get_default_agent()
            conversation = Conversation.objects.filter(user=user).order_by(
                "-updated_at"
            ).first() or Conversation.objects.create(user=user, agent=agent)

        return conversation

    @staticmethod
    @require_valid_user
    def get_conversation_sessions(user: KhojUser):
        return Conversation.objects.filter(user=user).prefetch_related("agent").order_by("-updated_at")

    @staticmethod
    @arequire_valid_user
    async def aset_conversation_title(user: KhojUser, conversation_id: str, title: str):
        if conversation_id is not None:
            try:
                UUID(str(conversation_id))
            except ValueError:
                return None
        conversation = await Conversation.objects.filter(user=user, id=conversation_id).afirst()
        if conversation:
            conversation.title = clean_text_for_db(title)
            await conversation.asave(update_fields=["title", "updated_at"])
            return conversation
        return None

    @staticmethod
    def get_conversation_by_id(conversation_id: str):
        if conversation_id is None:
            return None
        try:
            UUID(str(conversation_id))
        except ValueError:
            return None
        return Conversation.objects.filter(id=conversation_id).first()

    @staticmethod
    @arequire_valid_user
    async def acreate_conversation_session(user: KhojUser, title: str = None):
        agent = await AgentAdapters.aget_default_agent()
        return await Conversation.objects.select_related("agent", "agent__chat_model").acreate(
            user=user, agent=agent, title=title
        )

    @staticmethod
    @require_valid_user
    def create_conversation_session(user: KhojUser, title: str = None):
        agent = AgentAdapters.get_default_agent()
        return Conversation.objects.create(user=user, agent=agent, title=title)

    @staticmethod
    @arequire_valid_user
    async def aget_conversation_by_user(
        user: KhojUser,
        conversation_id: str = None,
        title: str = None,
        create_new: bool = False,
    ) -> Optional[Conversation]:
        if create_new:
            return await ConversationAdapters.acreate_conversation_session(user)

        query = Conversation.objects.filter(user=user).prefetch_related("agent", "agent__chat_model")

        if conversation_id is not None:
            try:
                UUID(str(conversation_id))
            except ValueError:
                return None
            return await query.filter(id=conversation_id).afirst()
        elif title:
            return await query.filter(title=title).afirst()

        conversation = await query.order_by("-updated_at").afirst()

        return conversation or await Conversation.objects.prefetch_related("agent", "agent__chat_model").acreate(
            user=user
        )

    @staticmethod
    @require_valid_user
    def has_any_chat_model(user: KhojUser):
        return ChatModel.objects.filter(user=user).exists()

    @staticmethod
    def get_all_chat_models():
        return ChatModel.objects.all()

    @staticmethod
    async def aget_all_chat_models():
        return await sync_to_async(list)(ChatModel.objects.prefetch_related("ai_model_api").all())

    @staticmethod
    async def aget_vision_enabled_config():
        chat_models = await ConversationAdapters.aget_all_chat_models()
        for config in chat_models:
            if config.vision_enabled:
                return config
        return None

    @staticmethod
    def get_ai_model_api():
        return AiModelApi.objects.filter().first()

    @staticmethod
    def has_valid_ai_model_api():
        return AiModelApi.objects.filter().exists()

    @staticmethod
    @arequire_valid_user
    async def aset_user_conversation_processor(user: KhojUser, conversation_processor_config_id: int):
        config = await ChatModel.objects.filter(id=conversation_processor_config_id).afirst()
        if not config:
            return None
        new_config = await UserConversationConfig.objects.aupdate_or_create(user=user, defaults={"setting": config})
        return new_config

    @staticmethod
    def get_chat_model(user: KhojUser):
        config = UserConversationConfig.objects.filter(user=user).first()
        if config and config.setting:
            return config.setting
        return ConversationAdapters.get_default_chat_model(user)

    @staticmethod
    async def aget_chat_model(user: KhojUser):
        config = (
            await UserConversationConfig.objects.filter(user=user)
            .prefetch_related("setting", "setting__ai_model_api")
            .afirst()
        )
        if config and config.setting:
            return config.setting
        return await ConversationAdapters.aget_default_chat_model(user)

    @staticmethod
    def get_chat_model_by_name(chat_model_name: str, ai_model_api_name: str = None):
        if ai_model_api_name:
            return ChatModel.objects.filter(name=chat_model_name, ai_model_api__name=ai_model_api_name).first()
        return ChatModel.objects.filter(name=chat_model_name).first()

    @staticmethod
    async def aget_chat_model_by_name(chat_model_name: str, ai_model_api_name: str = None):
        if ai_model_api_name:
            return await ChatModel.objects.filter(name=chat_model_name, ai_model_api__name=ai_model_api_name).afirst()
        return await ChatModel.objects.filter(name=chat_model_name).prefetch_related("ai_model_api").afirst()

    @staticmethod
    async def aget_chat_model_by_friendly_name(chat_model_name: str, ai_model_api_name: str = None):
        if ai_model_api_name:
            return await ChatModel.objects.filter(
                friendly_name=chat_model_name, ai_model_api__name=ai_model_api_name
            ).afirst()
        return await ChatModel.objects.filter(friendly_name=chat_model_name).prefetch_related("ai_model_api").afirst()

    @staticmethod
    def get_default_chat_model(user: KhojUser = None):
        """Get default conversation config. Prefer chat model by server admin > user > first created chat model"""
        # Get the server chat settings
        server_chat_settings = ServerChatSettings.objects.first()

        if server_chat_settings:
            # If the default model is set, return it
            if server_chat_settings.chat_default:
                return server_chat_settings.chat_default

        # Get the user's chat settings, if the server chat settings are not set
        user_chat_settings = UserConversationConfig.objects.filter(user=user).first() if user else None
        if user_chat_settings is not None and user_chat_settings.setting is not None:
            return user_chat_settings.setting

        # Get the first chat model if even the user chat settings are not set
        return ChatModel.objects.filter().first()

    @staticmethod
    async def aget_default_chat_model(
        user: KhojUser = None, fallback_chat_model: Optional[ChatModel] = None, fast: Optional[bool] = None
    ):
        """
        Get the chat model to use. Prefer chat model by server admin > agent > user > first created chat model

        Fast is a trinary flag to indicate preference for fast, deep or default chat model configured by the server admin.
        If fast is True, prefer fast models over deep models when both are configured.
        If fast is False, prefer deep models over fast models when both are configured.
        If fast is None, do not consider speed preference and use the default model selection logic.

        If fallback_chat_model is provided, it will be used as a fallback if server chat settings are not configured.
        Else if user settings are found use that.
        Otherwise the first chat model will be used.
        """
        # Get the server chat settings
        server_chat_settings: ServerChatSettings = (
            await ServerChatSettings.objects.filter()
            .prefetch_related(
                "chat_default",
                "chat_default__ai_model_api",
            )
            .afirst()
        )

        if server_chat_settings:
            if server_chat_settings.chat_default:
                return server_chat_settings.chat_default

        # Revert to an explicit fallback model if the server chat settings are not set
        if fallback_chat_model:
            # The chat model may not be full loaded from the db, so explicitly load it here
            return await ChatModel.objects.filter(id=fallback_chat_model.id).prefetch_related("ai_model_api").afirst()

        # Get the user's chat settings, if both the server chat settings and the fallback model are not set
        user_chat_settings = (
            (await UserConversationConfig.objects.filter(user=user).prefetch_related("setting__ai_model_api").afirst())
            if user
            else None
        )

        if user_chat_settings is not None and user_chat_settings.setting is not None:
            return user_chat_settings.setting

        # Get the first chat model if even the user chat settings are not set
        return await ChatModel.objects.filter().prefetch_related("ai_model_api").afirst()

    @staticmethod
    def get_advanced_chat_model(user: KhojUser):
        return ConversationAdapters.get_default_chat_model(user)

    @staticmethod
    async def aget_advanced_chat_model(user: KhojUser = None):
        return await ConversationAdapters.aget_default_chat_model(user)

    @staticmethod
    def set_default_chat_model(chat_model: ChatModel):
        server_chat_settings = ServerChatSettings.objects.first()
        if server_chat_settings:
            server_chat_settings.chat_default = chat_model
            server_chat_settings.save()
        else:
            ServerChatSettings.objects.create(chat_default=chat_model)

    @staticmethod
    def get_max_context_size(chat_model: ChatModel, user: KhojUser) -> int | None:
        """Get the max context size for the user based on the chat model."""
        return chat_model.max_prompt_size

    @staticmethod
    async def aget_max_context_size(chat_model: ChatModel, user: KhojUser) -> int | None:
        """Get the max context size for the user based on the chat model."""
        return chat_model.max_prompt_size

    @staticmethod
    async def aget_chat_models_with_fallbacks(slot: ServerChatSettings.ChatModelSlot) -> list[ChatModel]:
        """
        Get chat models for a specific server slot from all ServerChatSettings, ordered by priority.
        Used for fallback logic when a chat model fails.

        Args:
            slot: The chat model slot to get.

        Returns:
            List of ChatModel objects ordered by ServerChatSettings priority (lower first)
        """
        # Map slot enum to field name and prefetch related
        slot_field = slot.value
        prefetch_fields = [slot_field, f"{slot_field}__ai_model_api"]

        # Get all server chat settings ordered by priority
        all_settings = [
            settings
            async for settings in ServerChatSettings.objects.filter()
            .prefetch_related(*prefetch_fields)
            .order_by("priority")
            .aiterator()
        ]

        # Extract the chat model for the requested slot from each settings
        chat_models: list[ChatModel] = []
        seen_model_ids: set[int] = set()
        for settings in all_settings:
            chat_model = getattr(settings, slot_field, None)
            if chat_model and chat_model.id not in seen_model_ids:
                chat_models.append(chat_model)
                seen_model_ids.add(chat_model.id)

        return chat_models

    @staticmethod
    async def aget_chat_model_slot(user: KhojUser = None, fast: Optional[bool] = None):
        return ServerChatSettings.ChatModelSlot.CHAT_DEFAULT

    @staticmethod
    async def aget_server_webscraper():
        server_chat_settings = await ServerChatSettings.objects.filter().prefetch_related("web_scraper").afirst()
        if server_chat_settings is not None and server_chat_settings.web_scraper is not None:
            return server_chat_settings.web_scraper
        return None

    @staticmethod
    async def aget_enabled_webscrapers() -> list[WebScraper]:
        enabled_scrapers: list[WebScraper] = []
        server_webscraper = await ConversationAdapters.aget_server_webscraper()
        if server_webscraper:
            # Only use the webscraper set in the server chat settings
            enabled_scrapers = [server_webscraper]
        if not enabled_scrapers:
            # Use the enabled web scrapers, ordered by priority, until get web page content
            enabled_scrapers = [scraper async for scraper in WebScraper.objects.all().order_by("priority").aiterator()]
        if not enabled_scrapers:
            # Use scrapers enabled via environment variables
            if os.getenv("EXA_API_KEY"):
                api_url = os.getenv("EXA_API_URL", "https://api.exa.ai")
                enabled_scrapers.append(
                    WebScraper(
                        type=WebScraper.WebScraperType.EXA,
                        name=WebScraper.WebScraperType.EXA.capitalize(),
                        api_key=os.getenv("EXA_API_KEY"),
                        api_url=api_url,
                    )
                )
            if os.getenv("OLOSTEP_API_KEY"):
                api_url = os.getenv("OLOSTEP_API_URL", "https://agent.olostep.com/olostep-p2p-incomingAPI")
                enabled_scrapers.append(
                    WebScraper(
                        type=WebScraper.WebScraperType.OLOSTEP,
                        name=WebScraper.WebScraperType.OLOSTEP.capitalize(),
                        api_key=os.getenv("OLOSTEP_API_KEY"),
                        api_url=api_url,
                    )
                )
            if os.getenv("FIRECRAWL_API_KEY"):
                api_url = os.getenv("FIRECRAWL_API_URL", "https://api.firecrawl.dev")
                enabled_scrapers.append(
                    WebScraper(
                        type=WebScraper.WebScraperType.FIRECRAWL,
                        name=WebScraper.WebScraperType.FIRECRAWL.capitalize(),
                        api_key=os.getenv("FIRECRAWL_API_KEY"),
                        api_url=api_url,
                    )
                )
            # Only enable the direct web page scraper by default in self-hosted single user setups.
            # Useful for reading webpages on your intranet.
            if state.anonymous_mode or in_debug_mode():
                enabled_scrapers.append(
                    WebScraper(
                        type=WebScraper.WebScraperType.DIRECT,
                        name=WebScraper.WebScraperType.DIRECT.capitalize(),
                        api_key=None,
                        api_url=None,
                    )
                )

        return enabled_scrapers

    @staticmethod
    @require_valid_user
    @transaction.atomic
    def _save_conversation_atomic(
        user: KhojUser,
        new_messages: List[ChatMessageModel],
        conversation_id: str = None,
        user_message: str = None,
    ):
        slug = user_message.strip()[:200] if user_message else None
        conversations = Conversation.objects.select_for_update(of=("self",)).select_related(
            "agent", "agent__chat_model"
        )
        if conversation_id is not None:
            conversation = conversations.filter(user=user, id=conversation_id).first()
        else:
            conversation = conversations.filter(user=user).order_by("-updated_at").first()

        merged_messages = list(conversation.messages if conversation else [])
        existing_message_indexes = {
            (message.turnId, message.by): index for index, message in enumerate(merged_messages) if message.turnId
        }
        for message in new_messages:
            if not message.turnId:
                merged_messages.append(message)
                continue

            message_key = (message.turnId, message.by)
            existing_index = existing_message_indexes.get(message_key)
            if existing_index is None:
                existing_message_indexes[message_key] = len(merged_messages)
                merged_messages.append(message)
            elif not merged_messages[existing_index].message and message.message:
                # Complete an interrupted assistant placeholder without duplicating
                # the user side of the same turn.
                merged_messages[existing_index] = message

        conversation_log = {"chat": [msg.model_dump() for msg in merged_messages]}
        cleaned_conversation_log = clean_object_for_db(conversation_log)
        if conversation:
            conversation.conversation_log = cleaned_conversation_log
            conversation.slug = slug
            conversation.updated_at = django_timezone.now()
            conversation.save(update_fields=["conversation_log", "slug", "updated_at"])
        else:
            conversation = Conversation.objects.create(user=user, conversation_log=cleaned_conversation_log, slug=slug)
        return conversation

    @staticmethod
    @require_valid_user
    async def save_conversation(
        user: KhojUser,
        new_messages: List[ChatMessageModel],
        conversation_id: str = None,
        user_message: str = None,
    ):
        if conversation_id is not None:
            try:
                UUID(str(conversation_id))
            except ValueError:
                return None
        return await sync_to_async(ConversationAdapters._save_conversation_atomic, thread_sensitive=True)(
            user,
            new_messages,
            conversation_id=conversation_id,
            user_message=user_message,
        )

    @staticmethod
    @require_valid_user
    @transaction.atomic
    def pop_message(
        user: KhojUser,
        conversation_id: str,
        *,
        interrupted: bool = False,
    ) -> Optional[ChatMessageModel]:
        conversation = Conversation.objects.select_for_update().filter(user=user, id=conversation_id).first()
        if not conversation:
            return None
        chat_log = conversation.conversation_log.get("chat", [])
        if not chat_log:
            return None
        last_message = chat_log[-1]
        if interrupted and not (last_message.get("by") == "khoj" and not last_message.get("message")):
            return None
        popped_message = chat_log.pop()
        conversation.conversation_log = clean_object_for_db({"chat": chat_log})
        conversation.save(update_fields=["conversation_log", "updated_at"])
        try:
            return ChatMessageModel.model_validate(popped_message)
        except ValidationError as error:
            logger.warning(f"Popped an invalid message from conversation: {error}")
            return None

    @staticmethod
    async def apop_message(*args, **kwargs) -> Optional[ChatMessageModel]:
        return await sync_to_async(ConversationAdapters.pop_message, thread_sensitive=True)(*args, **kwargs)

    @staticmethod
    def get_conversation_processor_options():
        return ChatModel.objects.all()

    @staticmethod
    def set_user_chat_model(user: KhojUser, chat_model: ChatModel):
        user_conversation_config, _ = UserConversationConfig.objects.get_or_create(user=user)
        user_conversation_config.setting = chat_model
        user_conversation_config.save()

    @staticmethod
    async def aget_user_chat_model(user: KhojUser):
        config = (
            await UserConversationConfig.objects.filter(user=user).prefetch_related("setting__ai_model_api").afirst()
        )
        if not config:
            return None
        return config.setting

    @staticmethod
    async def ais_memory_enabled(user: KhojUser) -> bool:
        """
        Check if memory is enabled for the user based on server config and user preference.

        Logic:
        - If server memory_mode is DISABLED: return False (overrides user preference)
        - If server memory_mode is ENABLED_DEFAULT_OFF: use user preference if set, else False
        - If server memory_mode is ENABLED_DEFAULT_ON: use user preference if set, else True
        - If no server config exists: default to True
        """
        # Get server-level memory configuration
        server_settings = await ServerChatSettings.objects.afirst()
        if server_settings:
            memory_mode = server_settings.memory_mode
            # Disabled mode overrides all user preferences
            if memory_mode == ServerChatSettings.MemoryMode.DISABLED:
                return False

            # Get user preference
            user_config = await UserConversationConfig.objects.filter(user=user).afirst()

            if memory_mode == ServerChatSettings.MemoryMode.ENABLED_DEFAULT_OFF:
                # User must explicitly opt-in; check if user has set a preference
                if user_config is None:
                    return False  # Default off for new users
                return user_config.enable_memory

            # ENABLED_DEFAULT_ON: use user preference if set, else True
            if user_config is None:
                return True  # Default on for new users
            return user_config.enable_memory

        # No server config - default behavior (enabled, default on)
        user_config = await UserConversationConfig.objects.filter(user=user).afirst()
        if user_config is None:
            return True
        return user_config.enable_memory

    @staticmethod
    def is_memory_enabled(user: KhojUser) -> bool:
        """
        Sync version of ais_memory_enabled.
        Check if memory is enabled for the user based on server config and user preference.

        Logic:
        - If server memory_mode is DISABLED: return False (overrides user preference)
        - If server memory_mode is ENABLED_DEFAULT_OFF: use user preference if set, else False
        - If server memory_mode is ENABLED_DEFAULT_ON: use user preference if set, else True
        - If no server config exists: default to True
        """
        # Get server-level memory configuration
        server_settings = ServerChatSettings.objects.first()
        if server_settings:
            memory_mode = server_settings.memory_mode
            # Disabled mode overrides all user preferences
            if memory_mode == ServerChatSettings.MemoryMode.DISABLED:
                return False

            # Get user preference
            user_config = UserConversationConfig.objects.filter(user=user).first()

            if memory_mode == ServerChatSettings.MemoryMode.ENABLED_DEFAULT_OFF:
                # User must explicitly opt-in; check if user has set a preference
                if user_config is None:
                    return False  # Default off for new users
                return user_config.enable_memory

            # ENABLED_DEFAULT_ON: use user preference if set, else True
            if user_config is None:
                return True  # Default on for new users
            return user_config.enable_memory

        # No server config - default behavior (enabled, default on)
        user_config = UserConversationConfig.objects.filter(user=user).first()
        if user_config is None:
            return True
        return user_config.enable_memory

    @staticmethod
    async def aget_valid_chat_model(user: KhojUser, conversation: Conversation):
        agent: Agent = conversation.agent if await AgentAdapters.aget_default_agent() != conversation.agent else None
        if agent and agent.chat_model:
            chat_model = await ChatModel.objects.select_related("ai_model_api").aget(
                pk=conversation.agent.chat_model.pk
            )
        else:
            chat_model = await ConversationAdapters.aget_chat_model(user)

        if chat_model is None:
            chat_model = await ConversationAdapters.aget_default_chat_model()

        if (
            chat_model.model_type
            in [
                ChatModel.ModelType.ANTHROPIC,
                ChatModel.ModelType.OPENAI,
                ChatModel.ModelType.GOOGLE,
            ]
        ) and chat_model.ai_model_api:
            return chat_model

        else:
            raise ValueError("Invalid conversation settings. Configure some chat model on server.")

    @staticmethod
    @require_valid_user
    @transaction.atomic
    def add_files_to_filter(user: KhojUser, conversation_id: str, files: List[str]):
        try:
            conversation_uuid = UUID(str(conversation_id))
        except (AttributeError, TypeError, ValueError):
            return None
        conversation = Conversation.objects.select_for_update().filter(user=user, id=conversation_uuid).first()
        file_list = EntryAdapters.get_all_filenames_by_source(user, "computer")
        if not conversation:
            return None
        conversation.file_filters = [file for file in conversation.file_filters if file in file_list]
        conversation.file_filters.extend(
            filename for filename in files if filename in file_list and filename not in conversation.file_filters
        )
        conversation.save(update_fields=["file_filters", "updated_at"])
        return conversation.file_filters

    @staticmethod
    @require_valid_user
    @transaction.atomic
    def remove_files_from_filter(user: KhojUser, conversation_id: str, files: List[str]):
        try:
            conversation_uuid = UUID(str(conversation_id))
        except (AttributeError, TypeError, ValueError):
            return None
        conversation = Conversation.objects.select_for_update().filter(user=user, id=conversation_uuid).first()
        if not conversation:
            return None
        file_list = EntryAdapters.get_all_filenames_by_source(user, "computer")
        conversation.file_filters = [
            file for file in conversation.file_filters if file in file_list and file not in files
        ]
        conversation.save(update_fields=["file_filters", "updated_at"])
        return conversation.file_filters

    @staticmethod
    @require_valid_user
    def delete_message_by_turn_id(user: KhojUser, conversation_id: str, turn_id: str):
        from khoj.processor.conversation.vault_actions import delete_conversation_turn_and_cancel_batch

        return delete_conversation_turn_and_cancel_batch(
            user=user,
            conversation_id=conversation_id,
            turn_id=turn_id,
        )


class FileObjectAdapters:
    @staticmethod
    def update_raw_text(file_object: FileObject, new_raw_text: str):
        cleaned_raw_text = clean_text_for_db(new_raw_text)
        file_object.raw_text = cleaned_raw_text
        file_object.save()

    @staticmethod
    @require_valid_user
    def create_file_object(user: KhojUser, file_name: str, raw_text: str):
        cleaned_raw_text = clean_text_for_db(raw_text)
        return FileObject.objects.create(user=user, file_name=file_name, raw_text=cleaned_raw_text)

    @staticmethod
    @require_valid_user
    def get_file_object_by_name(user: KhojUser, file_name: str):
        return FileObject.objects.filter(user=user, file_name=file_name).first()

    @staticmethod
    @require_valid_user
    def get_all_file_objects(user: KhojUser):
        return FileObject.objects.filter(user=user).all()

    @staticmethod
    @require_valid_user
    def delete_file_object_by_name(user: KhojUser, file_name: str):
        return FileObject.objects.filter(user=user, file_name=file_name).delete()

    @staticmethod
    @require_valid_user
    def delete_all_file_objects(user: KhojUser):
        return FileObject.objects.filter(user=user).delete()

    @staticmethod
    async def aupdate_raw_text(file_object: FileObject, new_raw_text: str):
        cleaned_raw_text = clean_text_for_db(new_raw_text)
        file_object.raw_text = cleaned_raw_text
        await file_object.asave()

    @staticmethod
    @arequire_valid_user
    async def acreate_file_object(user: KhojUser, file_name: str, raw_text: str):
        cleaned_raw_text = clean_text_for_db(raw_text)
        return await FileObject.objects.acreate(user=user, file_name=file_name, raw_text=cleaned_raw_text)

    @staticmethod
    @arequire_valid_user
    async def aget_file_objects_by_name(user: KhojUser, file_name: str):
        return await sync_to_async(list)(FileObject.objects.filter(user=user, file_name=file_name))

    @staticmethod
    @arequire_valid_user
    async def aget_file_objects_by_path_prefix(user: KhojUser, path_prefix: str):
        """Get file objects from the database by path prefix."""
        return await sync_to_async(list)(FileObject.objects.filter(user=user, file_name__startswith=path_prefix))

    @staticmethod
    @arequire_valid_user
    async def aget_file_objects_by_names(user: KhojUser, file_names: List[str]):
        return await sync_to_async(list)(FileObject.objects.filter(user=user, file_name__in=file_names))

    @staticmethod
    @require_valid_user
    async def aget_all_file_objects(user: KhojUser, start: int = 0, limit: int = 10):
        query = FileObject.objects.filter(user=user).order_by("-updated_at")[start : start + limit]
        return await sync_to_async(list)(query)

    @staticmethod
    @require_valid_user
    async def aget_number_of_pages(user: KhojUser, limit: int = 10):
        count = await FileObject.objects.filter(user=user).acount()
        return math.ceil(count / limit)

    @staticmethod
    @arequire_valid_user
    async def adelete_file_object_by_name(user: KhojUser, file_name: str):
        return await FileObject.objects.filter(user=user, file_name=file_name).adelete()

    @staticmethod
    @arequire_valid_user
    async def adelete_file_objects_by_names(user: KhojUser, file_names: List[str]):
        return await FileObject.objects.filter(user=user, file_name__in=file_names).adelete()

    @staticmethod
    @arequire_valid_user
    async def adelete_all_file_objects(user: KhojUser):
        return await FileObject.objects.filter(user=user).adelete()

    @staticmethod
    @arequire_valid_user
    async def aget_file_objects_by_regex(user: KhojUser, regex_pattern: str, path_prefix: Optional[str] = None):
        """
        Search for a regex pattern in file objects, with an optional path prefix filter.
        Outputs results in grep format.
        """
        query = FileObject.objects.filter(user=user, raw_text__iregex=regex_pattern)
        if path_prefix:
            query = query.filter(file_name__startswith=path_prefix)
        return await sync_to_async(list)(query)


class EntryAdapters:
    word_filter = WordFilter()
    file_filter = FileFilter()
    date_filter = DateFilter()

    @staticmethod
    @require_valid_user
    def does_entry_exist(user: KhojUser, hashed_value: str) -> bool:
        return Entry.objects.filter(user=user, hashed_value=hashed_value).exists()

    @staticmethod
    @require_valid_user
    def delete_entry_by_file(user: KhojUser, file_path: str):
        deleted_count, _ = Entry.objects.filter(user=user, file_path=file_path).delete()
        return deleted_count

    @staticmethod
    @require_valid_user
    def get_filtered_entries(user: KhojUser, file_type: str = None, file_source: str = None):
        queryset = Entry.objects.filter(user=user)

        if file_type is not None:
            queryset = queryset.filter(file_type=file_type)

        if file_source is not None:
            queryset = queryset.filter(file_source=file_source)

        return queryset

    @staticmethod
    @require_valid_user
    def delete_all_entries(user: KhojUser, file_type: str = None, file_source: str = None, batch_size=1000):
        deleted_count = 0
        queryset = EntryAdapters.get_filtered_entries(user, file_type, file_source)
        while queryset.exists():
            batch_ids = list(queryset.values_list("id", flat=True)[:batch_size])
            batch = Entry.objects.filter(id__in=batch_ids, user=user)
            count, _ = batch.delete()
            deleted_count += count
        return deleted_count

    @staticmethod
    @arequire_valid_user
    async def adelete_all_entries(user: KhojUser, file_type: str = None, file_source: str = None, batch_size=1000):
        deleted_count = 0
        queryset = EntryAdapters.get_filtered_entries(user, file_type, file_source)
        while await queryset.aexists():
            batch_ids = await sync_to_async(list)(queryset.values_list("id", flat=True)[:batch_size])
            batch = Entry.objects.filter(id__in=batch_ids, user=user)
            count, _ = await batch.adelete()
            deleted_count += count
        return deleted_count

    @staticmethod
    @require_valid_user
    def get_existing_entry_hashes_by_file(user: KhojUser, file_path: str):
        return Entry.objects.filter(user=user, file_path=file_path).values_list("hashed_value", flat=True)

    @staticmethod
    @require_valid_user
    def delete_entry_by_hash(user: KhojUser, hashed_values: List[str]):
        Entry.objects.filter(user=user, hashed_value__in=hashed_values).delete()

    @staticmethod
    def get_entries_by_date_filter(entry: BaseManager[Entry], start_date: date, end_date: date):
        return entry.filter(
            created_at__date__gte=start_date,
            created_at__date__lte=end_date,
        )

    @staticmethod
    @require_valid_user
    def user_has_entries(user: KhojUser):
        return Entry.objects.filter(user=user).exists()

    @staticmethod
    @arequire_valid_user
    async def auser_has_entries(user: KhojUser):
        return await Entry.objects.filter(user=user).aexists()

    @staticmethod
    @arequire_valid_user
    async def adelete_entry_by_file(user: KhojUser, file_path: str):
        return await Entry.objects.filter(user=user, file_path=file_path).adelete()

    @staticmethod
    @arequire_valid_user
    async def adelete_entries_by_filenames(user: KhojUser, filenames: List[str], batch_size=1000):
        deleted_count = 0
        for i in range(0, len(filenames), batch_size):
            batch = filenames[i : i + batch_size]
            count, _ = await Entry.objects.filter(user=user, file_path__in=batch).adelete()
            deleted_count += count

        return deleted_count

    @staticmethod
    @require_valid_user
    def get_all_filenames_by_source(user: KhojUser, file_source: str):
        return (
            Entry.objects.filter(user=user, file_source=file_source)
            .distinct("file_path")
            .values_list("file_path", flat=True)
        )

    @staticmethod
    @require_valid_user
    def get_all_filenames_by_type(user: KhojUser, file_type: str):
        return (
            Entry.objects.filter(user=user, file_type=file_type)
            .distinct("file_path")
            .values_list("file_path", flat=True)
        )

    @staticmethod
    @require_valid_user
    def get_size_of_indexed_data_in_mb(user: KhojUser):
        total_size = sum(len(entry.compiled.encode("utf-8")) for entry in Entry.objects.filter(user=user).iterator())
        return total_size / 1024 / 1024

    @staticmethod
    def apply_filters(user: KhojUser, query: str, file_type_filter: str = None):
        q_filter_terms = Q()

        word_filters = EntryAdapters.word_filter.get_filter_terms(query)
        file_filters = EntryAdapters.file_filter.get_filter_terms(query)
        date_filters = EntryAdapters.date_filter.get_query_date_range(query)

        if user is None:
            return Entry.objects.none()
        owner_filter = Q(user=user)

        if len(word_filters) == 0 and len(file_filters) == 0 and len(date_filters) == 0:
            return Entry.objects.filter(owner_filter)

        for term in word_filters:
            if term.startswith("+"):
                q_filter_terms &= Q(raw__icontains=term[1:])
            elif term.startswith("-"):
                q_filter_terms &= ~Q(raw__icontains=term[1:])

        q_file_filter_terms = Q()

        if len(file_filters) > 0:
            for term in file_filters:
                if term.startswith("-"):
                    # Convert the glob term to a regex pattern
                    regex_term = re.escape(term[1:]).replace(r"\*", ".*").replace(r"\?", ".")
                    # Exclude all files that match the regex term
                    q_file_filter_terms &= ~Q(file_path__regex=regex_term)
                else:
                    # Convert the glob term to a regex pattern
                    regex_term = re.escape(term).replace(r"\*", ".*").replace(r"\?", ".")
                    # Include any files that match the regex term
                    q_file_filter_terms |= Q(file_path__regex=regex_term)

            q_filter_terms &= q_file_filter_terms

        if len(date_filters) > 0:
            min_date, max_date = date_filters
            if min_date is not None:
                # Convert the min_date timestamp to yyyy-mm-dd format
                formatted_min_date = date.fromtimestamp(min_date).strftime("%Y-%m-%d")
                q_filter_terms &= Q(created_at__date__gte=formatted_min_date)
            if max_date is not None:
                # Convert the max_date timestamp to yyyy-mm-dd format
                formatted_max_date = date.fromtimestamp(max_date).strftime("%Y-%m-%d")
                q_filter_terms &= Q(created_at__date__lte=formatted_max_date)

        relevant_entries = Entry.objects.filter(owner_filter).filter(q_filter_terms)
        if file_type_filter:
            relevant_entries = relevant_entries.filter(file_type=file_type_filter)
        return relevant_entries

    @staticmethod
    @require_valid_user
    def get_unique_file_types(user: KhojUser):
        return Entry.objects.filter(user=user).values_list("file_type", flat=True).distinct()

    @staticmethod
    @require_valid_user
    def get_unique_file_sources(user: KhojUser):
        return Entry.objects.filter(user=user).values_list("file_source", flat=True).distinct().all()


class AutomationAdapters:
    @staticmethod
    def parse_automation_metadata(automation: Job) -> dict[str, str]:
        try:
            raw_metadata = json.loads(automation.name or "")
        except (TypeError, json.JSONDecodeError):
            raw_metadata = {}
        if not isinstance(raw_metadata, dict):
            raw_metadata = {}

        fallback = str(automation.name or automation.id)
        kwargs = automation.kwargs or {}
        return {
            "subject": str(raw_metadata.get("subject") or kwargs.get("subject") or fallback),
            "query_to_run": str(raw_metadata.get("query_to_run") or kwargs.get("query_to_run") or fallback),
            "scheduling_request": str(
                raw_metadata.get("scheduling_request") or kwargs.get("scheduling_request") or fallback
            ),
            "crontime": str(raw_metadata.get("crontime") or ""),
            "conversation_id": str(raw_metadata.get("conversation_id") or kwargs.get("conversation_id") or ""),
        }

    @staticmethod
    def get_automations(user: KhojUser) -> Iterable[Job]:
        all_automations: Iterable[Job] = state.scheduler.get_jobs()
        for automation in all_automations:
            if automation.id.startswith(f"automation_{user.uuid}_"):
                yield automation

    @staticmethod
    def get_automation_metadata(user: KhojUser, automation: Job):
        # Perform validation checks
        # Check if user is allowed to delete this automation id
        if not automation.id.startswith(f"automation_{user.uuid}_"):
            raise ValueError(f"Invalid automation id: {automation.id}")

        automation_metadata = AutomationAdapters.parse_automation_metadata(automation)
        crontime = automation_metadata["crontime"]
        next_run_time = automation.next_run_time
        timezone = next_run_time.strftime("%Z") if next_run_time else ""
        try:
            schedule = (
                f"{cron_descriptor.get_description(crontime)} {timezone}".strip()
                if crontime
                else str(automation.trigger)
            )
        except Exception:
            schedule = str(automation.trigger)
        return {
            "id": automation.id,
            "subject": automation_metadata["subject"],
            "query_to_run": automation_metadata["query_to_run"],
            "scheduling_request": automation_metadata["scheduling_request"],
            "schedule": schedule,
            "crontime": crontime,
            "next": next_run_time.strftime("%Y-%m-%d %I:%M %p %Z") if next_run_time else "",
        }

    @staticmethod
    def get_job_last_run(user: KhojUser, automation: Job):
        # Perform validation checks
        # Check if user is allowed to delete this automation id
        if not automation.id.startswith(f"automation_{user.uuid}_"):
            raise ValueError(f"Invalid automation id: {automation.id}")

        django_job = DjangoJob.objects.filter(id=automation.id).first()
        execution = DjangoJobExecution.objects.filter(job=django_job, status="Executed")

        last_run_time = None

        if execution.exists():
            last_run_time = execution.latest("run_time").run_time

        return last_run_time.strftime("%Y-%m-%d %I:%M %p %Z") if last_run_time else None

    @staticmethod
    def get_automations_metadata(user: KhojUser):
        for automation in AutomationAdapters.get_automations(user):
            yield AutomationAdapters.get_automation_metadata(user, automation)

    @staticmethod
    def get_automation(user: KhojUser, automation_id: str) -> Job:
        # Perform validation checks
        # Check if user is allowed to retrieve this automation id
        if is_none_or_empty(automation_id) or not automation_id.startswith(f"automation_{user.uuid}_"):
            raise ValueError(f"Invalid automation id: {automation_id}")
        # Check if automation with this id exist
        automation: Job = state.scheduler.get_job(job_id=automation_id)
        if not automation:
            raise ValueError(f"Invalid automation id: {automation_id}")

        return automation

    @staticmethod
    async def aget_automation(user: KhojUser, automation_id: str) -> Job:
        # Perform validation checks
        # Check if user is allowed to retrieve this automation id
        if is_none_or_empty(automation_id) or not automation_id.startswith(f"automation_{user.uuid}_"):
            raise ValueError(f"Invalid automation id: {automation_id}")
        # Check if automation with this id exist
        automation: Job = await sync_to_async(state.scheduler.get_job)(job_id=automation_id)
        if not automation:
            raise ValueError(f"Invalid automation id: {automation_id}")

        return automation

    @staticmethod
    def delete_automation(user: KhojUser, automation_id: str):
        # Get valid, user-owned automation
        automation: Job = AutomationAdapters.get_automation(user, automation_id)

        # Collate info about user automation to be deleted
        automation_metadata = AutomationAdapters.get_automation_metadata(user, automation)

        automation.remove()
        return automation_metadata


class McpServerAdapters:
    @staticmethod
    async def aget_all_mcp_servers() -> List[McpServer]:
        """Asynchronously retrieve all McpServer objects from the database."""
        servers: List[McpServer] = []
        try:
            servers = [server async for server in McpServer.objects.all()]
        except Exception as e:
            logger.error(f"Error retrieving MCP servers: {e}", exc_info=True)
        return servers
