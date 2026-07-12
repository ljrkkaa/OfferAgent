import json
import logging
from typing import Any, Awaitable, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from khoj.processor.conversation.utils import ResponseWithThought

logger = logging.getLogger(__name__)

CLARIFICATION_QUESTION = "我还不能确定你希望我直接处理还是进行深度研究。请说明期望的结果。"

ROUTER_SYSTEM_PROMPT = """
You are OfferAgent's semantic intent router. Return only one JSON object and never answer the user.

Choose exactly one route from the meaning and conversation context:
- default: normal conversation and all ordinary tools, including web, vault search, and vault writes.
- research: an explicitly deep, multi-step research workflow is required.

Do not route ordinary web or knowledge-base work away from default; the main tool planner owns it.
Set requires_vault_write=true only when the user explicitly asks to persist, create, append, or modify a note or
file inside the personal knowledge-base vault through reviewed VaultActions. It is false for a downloadable file,
plan, or draft the user did not ask to save in the vault.
Any request with requires_vault_write=true must use route=default because the main tool planner owns reviewed writes.
Do not use keywords, string matching, or guesses. If the intended outcome or specialized route is unclear,
set needs_clarification=true, route=default, and ask one short concrete question.

Return exactly:
{
  "route": "default|research",
  "intent": "short_snake_case",
  "requires_vault_write": false,
  "needs_clarification": false,
  "question": ""
}
""".strip()


class RouteDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    route: Literal["default", "research"]
    intent: str = Field(pattern=r"^[a-z][a-z0-9_]{0,79}$")
    requires_vault_write: bool
    needs_clarification: bool
    question: str = Field(max_length=300)

    @model_validator(mode="after")
    def validate_clarification(self):
        if self.requires_vault_write and self.route != "default":
            raise ValueError("requires_vault_write=true requires route=default")
        if self.needs_clarification:
            if self.route != "default" or self.requires_vault_write or not self.question.strip():
                raise ValueError(
                    "clarification requires route=default, requires_vault_write=false, and a non-empty question"
                )
        elif self.question.strip():
            raise ValueError("question must be empty when clarification is not required")
        return self


def clarification_decision(question: str = CLARIFICATION_QUESTION) -> RouteDecision:
    return RouteDecision(
        route="default",
        intent="clarify_request",
        requires_vault_write=False,
        needs_clarification=True,
        question=question,
    )


def parse_route_decision(value: Any) -> RouteDecision:
    try:
        payload = json.loads(value) if isinstance(value, str) else value
        return RouteDecision.model_validate(payload)
    except (json.JSONDecodeError, TypeError, ValueError, ValidationError):
        return clarification_decision()


async def route_offeragent_intent(
    query: str,
    chat_history: list,
    *,
    send_message: Callable[..., Awaitable[ResponseWithThought]],
) -> RouteDecision:
    try:
        response = await send_message(
            query=f"User request:\n{query}",
            chat_history=chat_history,
            system_message=ROUTER_SYSTEM_PROMPT,
            response_type="json_object",
            response_schema=RouteDecision,
            fast_model=True,
            deepthought=False,
        )
    except Exception:
        logger.warning("OfferAgent semantic intent router failed", exc_info=True)
        return clarification_decision("意图识别暂时失败。请明确说明你希望得到的结果，我再继续。")

    return parse_route_decision(getattr(response, "text", "") or "")
