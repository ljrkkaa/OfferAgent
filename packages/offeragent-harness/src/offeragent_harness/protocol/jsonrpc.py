"""Strict JSON-RPC 2.0 envelopes and method-aware validation."""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum, IntEnum
from threading import RLock
from typing import Annotated, Literal

from pydantic import Field, JsonValue, StringConstraints, ValidationError
from typing_extensions import TypeAliasType

from ._base import JsonObject, WireModel, validate_wire
from .errors import ErrorCode, ErrorEnvelope, ProtocolViolation, protocol_error
from .events import EventEnvelope
from .messages import CommandSpec, command_spec, validate_command_params, validate_command_result

RpcStringId = TypeAliasType(
    "RpcStringId",
    Annotated[str, StringConstraints(min_length=1, max_length=128, strict=True)],
)
RpcId = TypeAliasType("RpcId", int | RpcStringId)
RpcResponseId = TypeAliasType("RpcResponseId", RpcId | None)


class JsonRpcErrorCode(IntEnum):
    PARSE_ERROR = -32700
    INVALID_REQUEST = -32600
    METHOD_NOT_FOUND = -32601
    INVALID_PARAMS = -32602
    INTERNAL_ERROR = -32603
    SERVER_ERROR = -32000


class JsonRpcError(WireModel):
    code: JsonRpcErrorCode
    message: str = Field(min_length=1, max_length=4096)
    data: ErrorEnvelope


class JsonRpcRequest(WireModel):
    jsonrpc: Literal["2.0"]
    id: RpcId
    method: str = Field(
        min_length=1,
        max_length=256,
        pattern=r"^[A-Za-z][A-Za-z0-9_.-]*(?:/[A-Za-z][A-Za-z0-9_.-]*)*$",
    )
    params: JsonObject = Field(default_factory=dict)


class JsonRpcNotification(WireModel):
    jsonrpc: Literal["2.0"]
    method: str = Field(
        min_length=1,
        max_length=256,
        pattern=r"^[A-Za-z][A-Za-z0-9_.-]*(?:/[A-Za-z][A-Za-z0-9_.-]*)*$",
    )
    params: JsonObject = Field(default_factory=dict)


class RpcCancelParams(WireModel):
    request_id: RpcId


class RpcCancelNotification(WireModel):
    jsonrpc: Literal["2.0"]
    method: Literal["rpc/cancel"]
    params: RpcCancelParams


class EventNotification(WireModel):
    jsonrpc: Literal["2.0"]
    method: Literal["event"]
    params: EventEnvelope


class JsonRpcSuccessResponse(WireModel):
    jsonrpc: Literal["2.0"]
    id: RpcResponseId
    result: JsonValue


class JsonRpcErrorResponse(WireModel):
    jsonrpc: Literal["2.0"]
    id: RpcResponseId
    error: JsonRpcError


JsonRpcMessage = TypeAliasType(
    "JsonRpcMessage",
    JsonRpcRequest
    | JsonRpcNotification
    | RpcCancelNotification
    | EventNotification
    | JsonRpcSuccessResponse
    | JsonRpcErrorResponse,
)


@dataclass(frozen=True)
class ValidatedRequest:
    envelope: JsonRpcRequest
    spec: CommandSpec
    params: WireModel


@dataclass(frozen=True)
class ValidatedResponse:
    envelope: JsonRpcSuccessResponse
    result: WireModel


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number is forbidden: {value}")


def _object_without_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON member: {key}")
        result[key] = value
    return result


def decode_json_document(payload: bytes) -> dict[str, object]:
    """Decode one UTF-8 JSON object while rejecting duplicate members and NaN."""

    try:
        text = payload.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise protocol_error(
            ErrorCode.PROTOCOL_INVALID_UTF8,
            "协议消息不是合法的 UTF-8。",
            details={"byteOffset": error.start},
        ) from None
    try:
        value = json.loads(
            text,
            object_pairs_hook=_object_without_duplicates,
            parse_constant=_reject_constant,
        )
    except (json.JSONDecodeError, ValueError) as error:
        details: JsonObject = {}
        if isinstance(error, json.JSONDecodeError):
            details = {"line": error.lineno, "column": error.colno, "characterOffset": error.pos}
        raise protocol_error(
            ErrorCode.PROTOCOL_INVALID_JSON,
            "协议消息不是合法的 JSON。",
            details=details,
        ) from None
    if not isinstance(value, dict):
        raise protocol_error(
            ErrorCode.PROTOCOL_INVALID_REQUEST,
            "JSON-RPC 消息顶层必须是对象。",
        )
    return value


def _validation_details(error: ValidationError) -> JsonObject:
    return {
        "violations": [
            {
                "path": ".".join(str(part) for part in item["loc"]),
                "type": item["type"],
                "message": item["msg"],
            }
            for item in error.errors(include_input=False, include_url=False)
        ]
    }


def parse_jsonrpc_message(value: object) -> JsonRpcMessage:
    """Parse a JSON-RPC envelope without dispatching application behavior."""

    if isinstance(value, bytes):
        raw = decode_json_document(value)
    elif isinstance(value, bytearray):
        raw = decode_json_document(bytes(value))
    elif isinstance(value, str):
        raw = decode_json_document(value.encode("utf-8"))
    elif isinstance(value, dict):
        raw = value
    else:
        raise protocol_error(
            ErrorCode.PROTOCOL_INVALID_REQUEST,
            "JSON-RPC 消息必须是 JSON 对象。",
        )

    try:
        if "method" in raw:
            if raw.get("method") == "event" and "id" not in raw:
                return validate_wire(EventNotification, raw)
            if raw.get("method") == "rpc/cancel" and "id" not in raw:
                return validate_wire(RpcCancelNotification, raw)
            if "id" in raw:
                return validate_wire(JsonRpcRequest, raw)
            notification = validate_wire(JsonRpcNotification, raw)
            raise protocol_error(
                ErrorCode.PROTOCOL_INVALID_REQUEST,
                "OfferAgent Command 必须携带 request id 并接收明确 receipt。",
                details={"method": notification.method},
            )
        if "result" in raw and "error" not in raw:
            return validate_wire(JsonRpcSuccessResponse, raw)
        if "error" in raw and "result" not in raw:
            return validate_wire(JsonRpcErrorResponse, raw)
    except ValidationError as error:
        raise protocol_error(
            ErrorCode.PROTOCOL_INVALID_REQUEST,
            "JSON-RPC 消息不符合协议 Schema。",
            details=_validation_details(error),
        ) from None
    raise protocol_error(
        ErrorCode.PROTOCOL_INVALID_REQUEST,
        "JSON-RPC 消息必须且只能包含 request、notification、result 或 error 之一。",
    )


def validate_request(request: JsonRpcRequest) -> ValidatedRequest:
    spec = command_spec(request.method)
    params = validate_command_params(request.method, request.params)
    return ValidatedRequest(envelope=request, spec=spec, params=params)


def validate_response(method: str, response: JsonRpcSuccessResponse) -> ValidatedResponse:
    result = validate_command_result(method, response.result)
    return ValidatedResponse(envelope=response, result=result)


def jsonrpc_error_code(error: ErrorEnvelope) -> JsonRpcErrorCode:
    mapping = {
        ErrorCode.PROTOCOL_INVALID_JSON: JsonRpcErrorCode.PARSE_ERROR,
        ErrorCode.PROTOCOL_INVALID_REQUEST: JsonRpcErrorCode.INVALID_REQUEST,
        ErrorCode.PROTOCOL_METHOD_NOT_FOUND: JsonRpcErrorCode.METHOD_NOT_FOUND,
        ErrorCode.PROTOCOL_INVALID_PARAMS: JsonRpcErrorCode.INVALID_PARAMS,
        ErrorCode.PROTOCOL_INTERNAL_ERROR: JsonRpcErrorCode.INTERNAL_ERROR,
    }
    return mapping.get(error.code, JsonRpcErrorCode.SERVER_ERROR)


def make_error_response(
    request_id: RpcResponseId,
    violation: ProtocolViolation,
) -> JsonRpcErrorResponse:
    return JsonRpcErrorResponse(
        jsonrpc="2.0",
        id=request_id,
        error=JsonRpcError(
            code=jsonrpc_error_code(violation.error),
            message=violation.error.user_visible_message,
            data=violation.error,
        ),
    )


class RequestDirection(str, Enum):
    LOCAL = "local"
    REMOTE = "remote"


class BidirectionalRequestIds:
    """Thread-safe, independent pending-ID sets for a fully duplex connection."""

    def __init__(self) -> None:
        self._pending: dict[RequestDirection, set[int | str]] = {
            RequestDirection.LOCAL: set(),
            RequestDirection.REMOTE: set(),
        }
        self._lock = RLock()

    def register(self, direction: RequestDirection, request_id: int | str) -> None:
        if isinstance(request_id, bool) or not isinstance(request_id, (int, str)):
            raise protocol_error(
                ErrorCode.PROTOCOL_INVALID_REQUEST,
                "JSON-RPC request id 必须是字符串或整数。",
            )
        with self._lock:
            if request_id in self._pending[direction]:
                raise protocol_error(
                    ErrorCode.PROTOCOL_DUPLICATE_REQUEST_ID,
                    "同一方向存在重复的未完成 JSON-RPC request id。",
                    details={"direction": direction.value, "requestId": request_id},
                )
            self._pending[direction].add(request_id)

    def complete(self, direction: RequestDirection, request_id: int | str) -> bool:
        with self._lock:
            if request_id not in self._pending[direction]:
                return False
            self._pending[direction].remove(request_id)
            return True

    def contains(self, direction: RequestDirection, request_id: int | str) -> bool:
        with self._lock:
            return request_id in self._pending[direction]

    def clear(self) -> None:
        with self._lock:
            for pending in self._pending.values():
                pending.clear()


__all__ = [
    "BidirectionalRequestIds",
    "EventNotification",
    "JsonRpcError",
    "JsonRpcErrorCode",
    "JsonRpcErrorResponse",
    "JsonRpcMessage",
    "JsonRpcNotification",
    "JsonRpcRequest",
    "JsonRpcSuccessResponse",
    "RequestDirection",
    "RpcCancelNotification",
    "RpcCancelParams",
    "RpcId",
    "ValidatedRequest",
    "ValidatedResponse",
    "decode_json_document",
    "make_error_response",
    "parse_jsonrpc_message",
    "validate_request",
    "validate_response",
]
