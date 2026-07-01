import json
import logging
import random
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from asgiref.sync import sync_to_async
from django.core.exceptions import ValidationError
from fastapi import APIRouter, Request
from fastapi.responses import Response
from pydantic import BaseModel
from starlette.authentication import has_required_scope, requires

from khoj.database.adapters import AgentAdapters, ConversationAdapters
from khoj.database.models import Agent, Conversation, KhojUser, PriceTier
from khoj.processor.conversation.codex.auth import get_codex_model
from khoj.processor.conversation.codex.utils import use_codex_runtime
from khoj.routers.helpers import CommonQueryParams, acheck_if_safe_prompt
from khoj.utils.helpers import (
    ConversationCommand,
    command_descriptions_for_agent,
    is_code_sandbox_enabled,
    is_operator_enabled,
    is_web_search_enabled,
    mode_descriptions_for_agent,
)

# Initialize Router
logger = logging.getLogger(__name__)


api_agents = APIRouter()


def _recent_conversation_cutoff():
    return datetime.now(timezone.utc) - timedelta(weeks=2)


class ModifyAgentBody(BaseModel):
    name: str
    persona: str
    privacy_level: str
    icon: str
    color: str
    chat_model: str
    files: Optional[List[str]] = []
    input_tools: Optional[List[str]] = []
    output_modes: Optional[List[str]] = []
    slug: Optional[str] = None
    is_hidden: Optional[bool] = False


class ModifyHiddenAgentBody(BaseModel):
    slug: Optional[str] = None
    persona: Optional[str] = None
    chat_model: Optional[str] = None
    input_tools: Optional[List[str]] = []
    output_modes: Optional[List[str]] = []


def _validate_agent_choices(body: BaseModel) -> Optional[Response]:
    checks = {
        "privacy_level": {choice.value for choice in Agent.PrivacyLevel},
        "icon": {choice.value for choice in Agent.StyleIconTypes},
        "color": {choice.value for choice in Agent.StyleColorTypes},
        "input_tools": {choice.value for choice in Agent.InputToolOptions},
        "output_modes": {choice.value for choice in Agent.OutputModeOptions},
    }
    for field, allowed in checks.items():
        value = getattr(body, field, None)
        if value is None:
            continue
        invalid = [item for item in value if item not in allowed] if isinstance(value, list) else []
        if invalid:
            return Response(
                content=json.dumps({"error": f"Invalid {field}: {invalid[0]}"}),
                media_type="application/json",
                status_code=400,
            )
        if not isinstance(value, list) and value not in allowed:
            return Response(
                content=json.dumps({"error": f"Invalid {field}: {value}"}),
                media_type="application/json",
                status_code=400,
            )
    return None


async def _resolve_agent_chat_model(
    request: Request, chat_model_name: Optional[str]
) -> tuple[Optional[str], Optional[Response]]:
    if not chat_model_name:
        if use_codex_runtime():
            return None, Response(
                content=json.dumps({"error": "Agent editing requires a configured database chat model."}),
                media_type="application/json",
                status_code=400,
            )
        return None, None

    chat_model = await ConversationAdapters.aget_chat_model_by_friendly_name(chat_model_name)
    if not chat_model:
        return None, Response(
            content=json.dumps({"error": f"Unknown chat model: {chat_model_name}"}),
            media_type="application/json",
            status_code=400,
        )

    if has_required_scope(request, ["premium"]) or chat_model.price_tier == PriceTier.FREE:
        return chat_model.name, None
    return None, Response(
        content=json.dumps({"error": f"Chat model {chat_model_name} is not available for this account."}),
        media_type="application/json",
        status_code=403,
    )


async def _agent_chat_model_name(agent: Agent, user: Optional[KhojUser]) -> Optional[str]:
    chat_model = await AgentAdapters.aget_agent_chat_model(agent, user)
    if chat_model:
        return chat_model.friendly_name
    if use_codex_runtime():
        return get_codex_model()
    return None


@api_agents.get("", response_class=Response)
async def all_agents(
    request: Request,
    common: CommonQueryParams,
) -> Response:
    user: KhojUser = request.user.object if request.user.is_authenticated else None
    agents = await AgentAdapters.aget_all_accessible_agents(user)
    default_agent = await AgentAdapters.aget_default_agent()
    default_agent_packet = None
    agents_packet = list()
    for agent in agents:
        files = agent.fileobject_set.all()
        file_names = [file.file_name for file in files]
        agent_packet = {
            "slug": agent.slug,
            "name": agent.name,
            "persona": agent.personality,
            "creator": agent.creator.username if agent.creator else None,
            "managed_by_admin": agent.managed_by_admin,
            "color": agent.style_color,
            "icon": agent.style_icon,
            "privacy_level": agent.privacy_level,
            "chat_model": await _agent_chat_model_name(agent, user),
            "files": file_names,
            "input_tools": agent.input_tools,
            "output_modes": agent.output_modes,
        }
        if default_agent and agent.slug == default_agent.slug:
            default_agent_packet = agent_packet
        else:
            agents_packet.append(agent_packet)

    # Load recent conversation sessions
    min_date = datetime.min.replace(tzinfo=timezone.utc)
    two_weeks_ago = _recent_conversation_cutoff()
    conversations = []
    if user:
        conversations = await sync_to_async(list[Conversation])(
            ConversationAdapters.get_conversation_sessions(user, request.user.client_app)
            .filter(updated_at__gte=two_weeks_ago)
            .order_by("-updated_at")[:50]
        )
    conversation_times = {conv.agent.slug: conv.updated_at for conv in conversations if conv.agent}

    # Put default agent first, then sort by mru and finally shuffle unused randomly
    random.shuffle(agents_packet)
    agents_packet.sort(key=lambda x: conversation_times.get(x["slug"]) or min_date, reverse=True)
    if default_agent_packet:
        agents_packet.insert(0, default_agent_packet)

    return Response(content=json.dumps(agents_packet), media_type="application/json", status_code=200)


@api_agents.get("/conversation", response_class=Response)
@requires(["authenticated"])
async def get_agent_by_conversation(
    request: Request,
    common: CommonQueryParams,
    conversation_id: str,
) -> Response:
    user: KhojUser = request.user.object if request.user.is_authenticated else None
    is_subscribed = has_required_scope(request, ["premium"])
    conversation = await ConversationAdapters.aget_conversation_by_user(user=user, conversation_id=conversation_id)

    if not conversation:
        return Response(
            content=json.dumps({"error": f"Conversation with id {conversation_id} not found for user {user}."}),
            media_type="application/json",
            status_code=404,
        )
    if conversation.agent:
        agent = await AgentAdapters.aget_agent_by_slug(conversation.agent.slug, user)
    else:
        agent = await AgentAdapters.aget_default_agent()

    if agent is None:
        if use_codex_runtime():
            agents_packet = {
                "slug": AgentAdapters.DEFAULT_AGENT_SLUG,
                "name": AgentAdapters.DEFAULT_AGENT_NAME,
                "persona": "",
                "creator": None,
                "managed_by_admin": True,
                "color": Agent.StyleColorTypes.ORANGE,
                "icon": Agent.StyleIconTypes.LIGHTBULB,
                "privacy_level": Agent.PrivacyLevel.PUBLIC,
                "chat_model": get_codex_model(),
                "has_files": False,
                "input_tools": [],
                "output_modes": [],
                "is_creator": False,
                "is_hidden": False,
            }
            return Response(content=json.dumps(agents_packet), media_type="application/json", status_code=200)
        return Response(
            content=json.dumps({"error": f"Agent for conversation id {conversation_id} not found for user {user}."}),
            media_type="application/json",
            status_code=404,
        )

    chat_model = await AgentAdapters.aget_agent_chat_model(agent, user)
    if is_subscribed or chat_model.price_tier == PriceTier.FREE:
        agent_chat_model = chat_model.friendly_name
    else:
        agent_chat_model = None

    has_files = await agent.fileobject_set.aexists()

    agents_packet = {
        "slug": agent.slug,
        "name": agent.name,
        "persona": agent.personality,
        "creator": agent.creator.username if agent.creator else None,
        "managed_by_admin": agent.managed_by_admin,
        "color": agent.style_color,
        "icon": agent.style_icon,
        "privacy_level": agent.privacy_level,
        "chat_model": agent_chat_model,
        "has_files": has_files,
        "input_tools": agent.input_tools,
        "output_modes": agent.output_modes,
        "is_creator": agent.creator == user,
        "is_hidden": agent.is_hidden,
    }

    return Response(content=json.dumps(agents_packet), media_type="application/json", status_code=200)


@api_agents.get("/options", response_class=Response)
async def get_agent_configuration_options(
    request: Request,
    common: CommonQueryParams,
) -> Response:
    agent_input_tools = [key for key, _ in Agent.InputToolOptions.choices]
    agent_output_modes = [key for key, _ in Agent.OutputModeOptions.choices]

    agent_input_tool_with_descriptions: Dict[str, str] = {}
    for key in agent_input_tools:
        conversation_command = ConversationCommand(key)
        if conversation_command == ConversationCommand.Operator and not is_operator_enabled():
            continue
        if (
            conversation_command in [ConversationCommand.Online, ConversationCommand.Webpage]
            and not is_web_search_enabled()
        ):
            continue
        if conversation_command == ConversationCommand.Code and not is_code_sandbox_enabled():
            continue
        agent_input_tool_with_descriptions[key] = command_descriptions_for_agent[conversation_command]

    agent_output_modes_with_descriptions: Dict[str, str] = {}
    for key in agent_output_modes:
        conversation_command = ConversationCommand(key)
        if conversation_command in mode_descriptions_for_agent:
            agent_output_modes_with_descriptions[key] = mode_descriptions_for_agent[conversation_command]

    return Response(
        content=json.dumps(
            {
                "input_tools": agent_input_tool_with_descriptions,
                "output_modes": agent_output_modes_with_descriptions,
            }
        ),
        media_type="application/json",
        status_code=200,
    )


@api_agents.get("/{agent_slug}", response_class=Response)
async def get_agent(
    request: Request,
    common: CommonQueryParams,
    agent_slug: str,
) -> Response:
    user: KhojUser = request.user.object if request.user.is_authenticated else None
    agent = await AgentAdapters.aget_readonly_agent_by_slug(agent_slug, user)

    if not agent:
        return Response(
            content=json.dumps({"error": f"Agent with name {agent_slug} not found."}),
            media_type="application/json",
            status_code=404,
        )

    files = agent.fileobject_set.all()
    file_names = [file.file_name for file in files]

    agents_packet = {
        "slug": agent.slug,
        "name": agent.name,
        "persona": agent.personality,
        "creator": agent.creator.username if agent.creator else None,
        "managed_by_admin": agent.managed_by_admin,
        "color": agent.style_color,
        "icon": agent.style_icon,
        "privacy_level": agent.privacy_level,
        "chat_model": await _agent_chat_model_name(agent, user),
        "files": file_names,
        "input_tools": agent.input_tools,
        "output_modes": agent.output_modes,
    }

    return Response(content=json.dumps(agents_packet), media_type="application/json", status_code=200)


@api_agents.delete("/{agent_slug}", response_class=Response)
@requires(["authenticated"])
async def delete_agent(
    request: Request,
    common: CommonQueryParams,
    agent_slug: str,
) -> Response:
    user: KhojUser = request.user.object

    agent = await AgentAdapters.aget_agent_by_slug(agent_slug, user)

    if not agent or agent.creator_id != user.id:
        return Response(
            content=json.dumps({"error": f"Agent with name {agent_slug} not found."}),
            media_type="application/json",
            status_code=404,
        )

    await AgentAdapters.adelete_agent_by_slug(agent_slug, user)

    return Response(content=json.dumps({"message": "Agent deleted."}), media_type="application/json", status_code=200)


@api_agents.patch("/hidden", response_class=Response)
@requires(["authenticated"])
async def update_hidden_agent(
    request: Request,
    common: CommonQueryParams,
    body: ModifyHiddenAgentBody,
) -> Response:
    user: KhojUser = request.user.object

    validation_error = _validate_agent_choices(body)
    if validation_error:
        return validation_error

    agent_chat_model, error_response = await _resolve_agent_chat_model(request, body.chat_model)
    if error_response:
        return error_response

    selected_agent = await AgentAdapters.aget_agent_by_slug(body.slug, user)

    if not selected_agent or selected_agent.creator_id != user.id:
        return Response(
            content=json.dumps({"error": f"Agent with name {body.slug} not found."}),
            media_type="application/json",
            status_code=404,
        )

    if not selected_agent.is_hidden:
        return Response(
            content=json.dumps({"error": f"Agent with name {body.slug} is not hidden."}),
            media_type="application/json",
            status_code=400,
        )

    agent = await AgentAdapters.aupdate_hidden_agent(
        user=user,
        slug=body.slug,
        persona=body.persona,
        chat_model=agent_chat_model,
        input_tools=body.input_tools,
        output_modes=body.output_modes,
        existing_agent=selected_agent,
    )

    agents_packet = {
        "slug": agent.slug,
        "name": agent.name,
        "persona": agent.personality,
        "creator": agent.creator.username if agent.creator else None,
        "chat_model": await _agent_chat_model_name(agent, user),
        "input_tools": agent.input_tools,
        "output_modes": agent.output_modes,
    }

    return Response(content=json.dumps(agents_packet), media_type="application/json", status_code=200)


@api_agents.post("/hidden", response_class=Response)
@requires(["authenticated"])
async def create_hidden_agent(
    request: Request,
    common: CommonQueryParams,
    conversation_id: str,
    body: ModifyHiddenAgentBody,
) -> Response:
    user: KhojUser = request.user.object

    validation_error = _validate_agent_choices(body)
    if validation_error:
        return validation_error

    agent_chat_model, error_response = await _resolve_agent_chat_model(request, body.chat_model)
    if error_response:
        return error_response

    conversation = await ConversationAdapters.aget_conversation_by_user(user=user, conversation_id=conversation_id)
    if not conversation:
        return Response(
            content=json.dumps({"error": f"Conversation with id {conversation_id} not found for user {user}."}),
            media_type="application/json",
            status_code=404,
        )

    if conversation.agent:
        # If the conversation is not already associated with an agent (i.e., it's using the default agent ), we can create a new one
        if conversation.agent.slug != AgentAdapters.DEFAULT_AGENT_SLUG:
            return Response(
                content=json.dumps(
                    {"error": f"Conversation with id {conversation_id} already has an agent. Use the PATCH method."}
                ),
                media_type="application/json",
                status_code=400,
            )

    agent = await AgentAdapters.aupdate_hidden_agent(
        user=user,
        slug=body.slug,
        persona=body.persona,
        chat_model=agent_chat_model,
        input_tools=body.input_tools,
        output_modes=body.output_modes,
        existing_agent=None,
    )

    conversation.agent = agent
    await conversation.asave()

    agents_packet = {
        "slug": agent.slug,
        "name": agent.name,
        "persona": agent.personality,
        "creator": agent.creator.username if agent.creator else None,
        "chat_model": await _agent_chat_model_name(agent, user),
        "input_tools": agent.input_tools,
        "output_modes": agent.output_modes,
    }

    return Response(content=json.dumps(agents_packet), media_type="application/json", status_code=200)


@api_agents.post("", response_class=Response)
@requires(["authenticated"])
async def create_agent(
    request: Request,
    common: CommonQueryParams,
    body: ModifyAgentBody,
) -> Response:
    user: KhojUser = request.user.object

    validation_error = _validate_agent_choices(body)
    if validation_error:
        return validation_error

    is_safe_prompt, reason = await acheck_if_safe_prompt(
        body.persona, user, lax=body.privacy_level == Agent.PrivacyLevel.PRIVATE
    )

    if not is_safe_prompt:
        return Response(
            content=json.dumps({"error": f"{reason}"}),
            media_type="application/json",
            status_code=400,
        )

    agent_chat_model, error_response = await _resolve_agent_chat_model(request, body.chat_model)
    if error_response:
        return error_response

    try:
        agent = await AgentAdapters.aupdate_agent(
            user,
            body.name,
            body.persona,
            body.privacy_level,
            body.icon,
            body.color,
            agent_chat_model,
            body.files,
            body.input_tools,
            body.output_modes,
            body.slug,
            body.is_hidden,
        )
    except ValidationError as e:
        return Response(
            content=json.dumps({"error": e.message}),
            media_type="application/json",
            status_code=400,
        )

    agents_packet = {
        "slug": agent.slug,
        "name": agent.name,
        "persona": agent.personality,
        "creator": agent.creator.username if agent.creator else None,
        "managed_by_admin": agent.managed_by_admin,
        "color": agent.style_color,
        "icon": agent.style_icon,
        "privacy_level": agent.privacy_level,
        "chat_model": await _agent_chat_model_name(agent, user),
        "files": body.files,
        "input_tools": agent.input_tools,
        "output_modes": agent.output_modes,
        "is_hidden": agent.is_hidden,
    }

    return Response(content=json.dumps(agents_packet), media_type="application/json", status_code=200)


@api_agents.patch("", response_class=Response)
@requires(["authenticated"])
async def update_agent(
    request: Request,
    common: CommonQueryParams,
    body: ModifyAgentBody,
) -> Response:
    user: KhojUser = request.user.object

    validation_error = _validate_agent_choices(body)
    if validation_error:
        return validation_error

    selected_agent = await AgentAdapters.aget_agent_by_slug(body.slug, user)

    if not selected_agent or selected_agent.creator_id != user.id:
        return Response(
            content=json.dumps({"error": f"Agent with name {body.name} not found."}),
            media_type="application/json",
            status_code=404,
        )

    if selected_agent.personality != body.persona:
        # Check if the new persona is safe
        is_safe_prompt, reason = await acheck_if_safe_prompt(
            body.persona, user, lax=body.privacy_level == Agent.PrivacyLevel.PRIVATE
        )

        if not is_safe_prompt:
            return Response(
                content=json.dumps({"error": f"{reason}"}),
                media_type="application/json",
                status_code=400,
            )

    agent_chat_model, error_response = await _resolve_agent_chat_model(request, body.chat_model)
    if error_response:
        return error_response

    try:
        agent = await AgentAdapters.aupdate_agent(
            user,
            body.name,
            body.persona,
            body.privacy_level,
            body.icon,
            body.color,
            agent_chat_model,
            body.files,
            body.input_tools,
            body.output_modes,
            body.slug,
        )
    except ValidationError as e:
        return Response(
            content=json.dumps({"error": e.message}),
            media_type="application/json",
            status_code=400,
        )

    agents_packet = {
        "slug": agent.slug,
        "name": agent.name,
        "persona": agent.personality,
        "creator": agent.creator.username if agent.creator else None,
        "managed_by_admin": agent.managed_by_admin,
        "color": agent.style_color,
        "icon": agent.style_icon,
        "privacy_level": agent.privacy_level,
        "chat_model": await _agent_chat_model_name(agent, user),
        "files": body.files,
        "input_tools": agent.input_tools,
        "output_modes": agent.output_modes,
    }

    return Response(content=json.dumps(agents_packet), media_type="application/json", status_code=200)
