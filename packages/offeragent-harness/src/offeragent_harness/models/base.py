"""Provider-neutral model request and streaming event types."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Any
from urllib.parse import urlsplit

from jsonschema import Draft202012Validator

from .json_types import FrozenJsonObject, JsonValue, freeze_json


class ModelRole(str, Enum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class ModelPurpose(str, Enum):
    PLANNING = "planning"
    RESPONDING = "responding"
    COMPACTION = "compaction"
    GROUNDING = "grounding"


class ModelOutputMode(str, Enum):
    TEXT = "text"
    JSON = "json"


class ModelEventKind(str, Enum):
    STARTED = "started"
    TEXT_DELTA = "text_delta"
    REASONING_SUMMARY = "reasoning_summary"
    STRUCTURED_OUTPUT = "structured_output"
    HOSTED_SEARCH = "hosted_search"
    CITATION = "citation"
    USAGE = "usage"
    COMPLETED = "completed"
    ERROR = "error"
    CANCELLED = "cancelled"


class ModelFinishReason(str, Enum):
    STOP = "stop"
    LENGTH = "length"
    CONTENT_FILTER = "content_filter"
    CANCELLED = "cancelled"
    ERROR = "error"


class ModelHostedTool(str, Enum):
    WEB_SEARCH = "web_search"


class ModelHostedSearchPhase(str, Enum):
    STARTED = "started"
    IN_PROGRESS = "in_progress"
    SEARCHING = "searching"
    COMPLETED = "completed"


@dataclass(frozen=True)
class ModelHostedSearch:
    call_id: str
    phase: ModelHostedSearchPhase

    def __post_init__(self) -> None:
        _validate_bounded_text(self.call_id, "hosted search call_id", maximum=256)


@dataclass(frozen=True)
class ModelCitation:
    """One provider-attested URL annotation in provider output coordinates."""

    provider_id: str
    model: str
    request_id: str
    url: str
    title: str
    start_index: int
    end_index: int

    def __post_init__(self) -> None:
        _validate_bounded_text(self.provider_id, "citation provider_id", maximum=128)
        _validate_bounded_text(self.model, "citation model", maximum=256)
        _validate_bounded_text(self.request_id, "citation request_id", maximum=256)
        _validate_public_url(self.url)
        _validate_bounded_text(self.title, "citation title", maximum=512, utf8_bytes=True)
        if (
            type(self.start_index) is not int
            or type(self.end_index) is not int
            or self.start_index < 0
            or self.end_index <= self.start_index
            or self.end_index > 16 * 1024 * 1024
        ):
            raise ValueError("citation indices must be a bounded non-empty ordered range")


@dataclass(frozen=True)
class TraceContext:
    trace_id: str
    parent_span_id: str | None = None

    def __post_init__(self) -> None:
        if not self.trace_id:
            raise ValueError("trace_id must not be empty")


@dataclass(frozen=True)
class ModelContentBlock:
    """A provider-neutral input block.

    ``kind`` is deliberately extensible (for example ``text``, ``image_ref`` or
    ``artifact_ref``); provider adapters decide which supported blocks to encode.
    """

    kind: str
    data: Mapping[str, Any]
    binary_data: bytes | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if not self.kind:
            raise ValueError("content block kind must not be empty")
        frozen = freeze_json(self.data)
        if not isinstance(frozen, FrozenJsonObject):
            raise TypeError("content block data must be a JSON object")
        object.__setattr__(self, "data", frozen)
        if self.binary_data is not None:
            content = bytes(self.binary_data)
            if self.kind != "image" or not content or len(content) > 10 * 1024 * 1024:
                raise ValueError("binary model content must be a bounded non-empty image")
            if not isinstance(frozen.get("mediaType"), str) or not str(frozen["mediaType"]).startswith("image/"):
                raise ValueError("binary image content requires image mediaType metadata")
            object.__setattr__(self, "binary_data", content)

    @classmethod
    def text(cls, text: str) -> ModelContentBlock:
        return cls(kind="text", data={"text": text})


@dataclass(frozen=True)
class ModelMessage:
    role: ModelRole
    content: tuple[ModelContentBlock, ...]
    name: str | None = None

    def __post_init__(self) -> None:
        if not self.content:
            raise ValueError("model messages require at least one content block")


@dataclass(frozen=True)
class ModelRequest:
    """The only request shape visible to a model provider.

    It intentionally has no cwd, tool executor, session store, approval, Vault or
    subagent handle. Structured Agent steps are requested through ``output_schema``.
    """

    request_id: str
    model: str
    purpose: ModelPurpose
    messages: tuple[ModelMessage, ...]
    output_mode: ModelOutputMode
    output_schema: Mapping[str, Any] | None
    max_output_tokens: int | None
    reasoning_effort: str | None
    temperature: float | None
    seed: int | None
    trace_context: TraceContext
    model_instructions: str | None = field(default=None, repr=False)
    use_responses_lite: bool = False
    metadata: Mapping[str, Any] = field(default_factory=dict)
    hosted_tools: tuple[ModelHostedTool, ...] = ()

    def __post_init__(self) -> None:
        if not self.request_id or not self.model:
            raise ValueError("request_id and model must not be empty")
        if not self.messages:
            raise ValueError("model request requires at least one message")
        if self.model_instructions is not None and (
            not self.model_instructions or len(self.model_instructions.encode("utf-8")) > 512 * 1024
        ):
            raise ValueError("model_instructions must be non-empty and bounded")
        if not isinstance(self.use_responses_lite, bool):
            raise TypeError("use_responses_lite must be a bool")
        if self.max_output_tokens is not None and self.max_output_tokens <= 0:
            raise ValueError("max_output_tokens must be positive")
        if self.temperature is not None and not 0 <= self.temperature <= 2:
            raise ValueError("temperature must be between 0 and 2")
        if self.output_mode is ModelOutputMode.JSON and self.output_schema is None:
            raise ValueError("JSON output mode requires output_schema")
        if self.output_schema is not None:
            Draft202012Validator.check_schema(dict(self.output_schema))
            frozen_schema = freeze_json(self.output_schema)
            if not isinstance(frozen_schema, FrozenJsonObject):
                raise TypeError("output_schema must be a JSON object")
            object.__setattr__(self, "output_schema", frozen_schema)
        frozen_metadata = freeze_json(self.metadata)
        if not isinstance(frozen_metadata, FrozenJsonObject):
            raise TypeError("metadata must be a JSON object")
        object.__setattr__(self, "metadata", frozen_metadata)
        tools = tuple(self.hosted_tools)
        if (
            len(tools) > 4
            or len(tools) != len(set(tools))
            or any(not isinstance(tool, ModelHostedTool) for tool in tools)
        ):
            raise ValueError("hosted model tools must be a small unique typed tuple")
        object.__setattr__(self, "hosted_tools", tools)


@dataclass(frozen=True)
class ModelUsage:
    input_tokens: int
    output_tokens: int
    cached_input_tokens: int
    reasoning_tokens: int
    cost: Decimal | None = None
    currency: str | None = None

    def __post_init__(self) -> None:
        values = (self.input_tokens, self.output_tokens, self.cached_input_tokens, self.reasoning_tokens)
        if any(value < 0 for value in values):
            raise ValueError("token usage cannot be negative")
        if self.cached_input_tokens > self.input_tokens:
            raise ValueError("cached_input_tokens cannot exceed input_tokens")
        if self.reasoning_tokens > self.output_tokens:
            raise ValueError("reasoning_tokens cannot exceed output_tokens")
        if self.cost is not None and self.cost < 0:
            raise ValueError("model cost cannot be negative")
        if (self.cost is None) != (self.currency is None):
            raise ValueError("cost and currency must be provided together")

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass(frozen=True)
class ModelError:
    code: str
    message: str
    retryable: bool
    cancelled: bool
    details: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.code or not self.message:
            raise ValueError("model errors require code and message")
        frozen = freeze_json(self.details)
        if not isinstance(frozen, FrozenJsonObject):
            raise TypeError("error details must be a JSON object")
        object.__setattr__(self, "details", frozen)


@dataclass(frozen=True)
class ModelEvent:
    request_id: str
    sequence: int
    kind: ModelEventKind
    text: str | None = None
    data: JsonValue | None = None
    usage: ModelUsage | None = None
    finish_reason: ModelFinishReason | None = None
    error: ModelError | None = None
    hosted_search: ModelHostedSearch | None = None
    citation: ModelCitation | None = None

    def __post_init__(self) -> None:
        if not self.request_id:
            raise ValueError("request_id must not be empty")
        if self.sequence < 1:
            raise ValueError("model event sequence starts at 1")
        if self.data is not None:
            object.__setattr__(self, "data", freeze_json(self.data))

        required: dict[ModelEventKind, tuple[str, ...]] = {
            ModelEventKind.TEXT_DELTA: ("text",),
            ModelEventKind.REASONING_SUMMARY: ("text",),
            ModelEventKind.STRUCTURED_OUTPUT: ("data",),
            ModelEventKind.HOSTED_SEARCH: ("hosted_search",),
            ModelEventKind.CITATION: ("citation",),
            ModelEventKind.USAGE: ("usage",),
            ModelEventKind.COMPLETED: ("finish_reason",),
            ModelEventKind.ERROR: ("error",),
            ModelEventKind.CANCELLED: ("finish_reason",),
        }
        fields = {
            "text": self.text,
            "data": self.data,
            "usage": self.usage,
            "finish_reason": self.finish_reason,
            "error": self.error,
            "hosted_search": self.hosted_search,
            "citation": self.citation,
        }
        missing = [name for name in required.get(self.kind, ()) if fields[name] is None]
        if missing:
            raise ValueError(f"{self.kind.value} event missing fields: {', '.join(missing)}")
        allowed = set(required.get(self.kind, ()))
        if self.kind is ModelEventKind.COMPLETED:
            allowed.add("usage")
        unexpected = [name for name, value in fields.items() if value is not None and name not in allowed]
        if unexpected:
            raise ValueError(f"{self.kind.value} event has unexpected fields: {', '.join(unexpected)}")
        if self.kind is ModelEventKind.CANCELLED and self.finish_reason is not ModelFinishReason.CANCELLED:
            raise ValueError("cancelled events require the cancelled finish reason")


def _validate_bounded_text(value: str, label: str, *, maximum: int, utf8_bytes: bool = False) -> None:
    length = (
        len(value.encode("utf-8"))
        if isinstance(value, str) and utf8_bytes
        else len(value)
        if isinstance(value, str)
        else 0
    )
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or length > maximum
        or any(ord(character) < 32 for character in value)
    ):
        raise ValueError(f"{label} must be bounded non-empty text")


def _validate_public_url(value: str) -> None:
    if not isinstance(value, str) or not value or value != value.strip() or len(value) > 2_048:
        raise ValueError("citation URL must be a bounded public HTTP(S) URL")
    if any(character.isspace() or ord(character) < 32 for character in value):
        raise ValueError("citation URL must be a bounded public HTTP(S) URL")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as error:
        raise ValueError("citation URL must be a bounded public HTTP(S) URL") from error
    if (
        parsed.scheme.casefold() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or (port is not None and not 1 <= port <= 65_535)
    ):
        raise ValueError("citation URL must be a bounded public HTTP(S) URL")


__all__ = [
    "ModelCitation",
    "ModelContentBlock",
    "ModelError",
    "ModelEvent",
    "ModelEventKind",
    "ModelFinishReason",
    "ModelHostedSearch",
    "ModelHostedSearchPhase",
    "ModelHostedTool",
    "ModelMessage",
    "ModelOutputMode",
    "ModelPurpose",
    "ModelRequest",
    "ModelRole",
    "ModelUsage",
    "TraceContext",
]
