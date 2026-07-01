import inspect
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Optional


@dataclass(frozen=True)
class QQBotMessage:
    text: str
    user_id: str
    chat_id: str
    group_id: Optional[str] = None
    client: str = "qqbot"


@dataclass(frozen=True)
class QQBotResponse:
    text: str
    chat_id: str
    group_id: Optional[str] = None


def normalize_inbound(payload: dict[str, Any]) -> QQBotMessage:
    data = payload.get("d") if isinstance(payload.get("d"), dict) else payload
    author = data.get("author") if isinstance(data.get("author"), dict) else {}
    user_id = str(author.get("id") or data.get("user_id") or data.get("openid") or "")
    chat_id = str(data.get("channel_id") or data.get("chat_id") or data.get("group_id") or "")
    group_id = data.get("guild_id") or data.get("group_id")
    text = str(data.get("content") or data.get("message") or data.get("text") or "").strip()
    return QQBotMessage(text=text, user_id=user_id, chat_id=chat_id, group_id=str(group_id) if group_id else None)


def is_allowed(message: QQBotMessage, allowlist: Iterable[str]) -> bool:
    allowed = {str(item) for item in allowlist if str(item)}
    if not allowed:
        return False
    return any(value in allowed for value in (message.user_id, message.chat_id, message.group_id))


def split_response(text: str, limit: int = 900) -> list[str]:
    text = text.strip()
    if not text:
        return []
    limit = max(1, limit)
    chunks: list[str] = []
    current = ""
    for paragraph in text.split("\n\n"):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        if len(paragraph) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.extend(paragraph[index : index + limit] for index in range(0, len(paragraph), limit))
            continue
        candidate = f"{current}\n\n{paragraph}".strip() if current else paragraph
        if len(candidate) <= limit:
            current = candidate
        else:
            chunks.append(current)
            current = paragraph
    if current:
        chunks.append(current)
    return chunks


async def handle_message(
    payload: dict[str, Any],
    chat_client: Callable[..., Any],
    allowlist: Iterable[str],
    *,
    split_limit: int = 900,
) -> list[QQBotResponse]:
    message = normalize_inbound(payload)
    if not message.text or not is_allowed(message, allowlist):
        return []

    response = chat_client(
        text=message.text,
        client=message.client,
        user_id=message.user_id,
        chat_id=message.chat_id,
        group_id=message.group_id,
    )
    if inspect.isawaitable(response):
        response = await response
    if isinstance(response, dict):
        response = response.get("response") or response.get("text") or ""
    return [
        QQBotResponse(text=chunk, chat_id=message.chat_id, group_id=message.group_id)
        for chunk in split_response(str(response), split_limit)
    ]
