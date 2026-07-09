import json
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from khoj.processor.conversation.utils import ResponseWithThought, load_complex_json
from khoj.processor.conversation.vault_policy import compact_policy_for_prompt

logger = logging.getLogger(__name__)

ROUTER_SYSTEM_PROMPT = """
You are OfferAgent's routing layer.

Classify the user's request and return only a JSON object. Do not answer the user.
Use semantic intent, not keyword matching. The route decision should say which subsystem
should handle the request and whether a local vault action may be needed.

Routes:
- default: normal assistant conversation or broad multi-tool agent work.
- general: direct answer without local vault evidence.
- notes: read, create, append, or edit the user's knowledge-base files.
- online: current web information is required.
- research: deeper multi-step research is required.
- code: code execution or sandboxed computation is required.
- image: image generation or image understanding is requested.

For vault file operations, use the supplied vault policy to infer paths, source priorities,
existence policies, and confirmation rules. If the policy does not identify a safe target,
set needs_confirmation=true and ask a short clarification question.

Return this JSON shape:
{
  "route": "default|general|notes|online|research|code|image",
  "intent": "short_snake_case",
  "confidence": 0.0,
  "needs_confirmation": false,
  "question": "",
  "target": {"path": "", "exists_policy": "create_only|append|replace|none"},
  "required_sources": [],
  "rationale": "brief"
}
""".strip()


@dataclass(frozen=True)
class RouteDecision:
    route: str = "default"
    intent: str = "conversation"
    confidence: float = 0.0
    needs_confirmation: bool = False
    question: str = ""
    target: dict[str, Any] = field(default_factory=dict)
    required_sources: list[str] = field(default_factory=list)
    rationale: str = ""

    @property
    def command(self) -> str:
        return self.route.strip().lower()


def _coerce_float(value: Any, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return max(0.0, min(1.0, parsed))


def parse_route_decision(value: Any) -> RouteDecision:
    if isinstance(value, str):
        value = load_complex_json(value)
    if not isinstance(value, dict):
        return RouteDecision(rationale="router returned a non-object payload")

    route = str(value.get("route") or "default").strip().lower()
    if route not in {"default", "general", "notes", "online", "research", "code", "image"}:
        route = "default"

    target = value.get("target") if isinstance(value.get("target"), dict) else {}
    required_sources = value.get("required_sources")
    if not isinstance(required_sources, list):
        required_sources = []

    return RouteDecision(
        route=route,
        intent=str(value.get("intent") or "conversation").strip()[:80],
        confidence=_coerce_float(value.get("confidence")),
        needs_confirmation=bool(value.get("needs_confirmation")),
        question=str(value.get("question") or "").strip(),
        target={str(k): v for k, v in target.items()},
        required_sources=[str(item) for item in required_sources if isinstance(item, str)],
        rationale=str(value.get("rationale") or "").strip()[:500],
    )


async def route_offeragent_intent(
    query: str,
    chat_history: list,
    *,
    send_message: Callable[..., Awaitable[ResponseWithThought]],
    vault_policy: dict[str, Any],
    client_app: Any = None,
    client_capabilities: Optional[dict[str, Any]] = None,
) -> RouteDecision:
    capabilities = client_capabilities or {}
    prompt = (
        f"Client: {client_app}\n"
        f"Client capabilities: {json.dumps(capabilities, ensure_ascii=False, default=str)}\n\n"
        f"Vault policy:\n{compact_policy_for_prompt(vault_policy)}\n\n"
        f"User request:\n{query}\n"
    )
    try:
        response = await send_message(
            query=prompt,
            chat_history=chat_history,
            system_message=ROUTER_SYSTEM_PROMPT,
            response_type="json_object",
            fast_model=True,
            deepthought=False,
        )
    except Exception as exc:
        logger.warning("OfferAgent intent router failed; falling back to default route: %s", exc, exc_info=True)
        return RouteDecision(rationale=f"router_error: {exc}")

    return parse_route_decision(getattr(response, "text", "") or "")
