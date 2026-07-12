"""32-bit big-endian length-prefixed UTF-8 JSON-RPC framing."""

from __future__ import annotations

import json
import struct

from ._base import WireModel
from .errors import ErrorCode, ProtocolViolation, protocol_error
from .jsonrpc import JsonRpcMessage, parse_jsonrpc_message

LENGTH_PREFIX_BYTES = 4
DEFAULT_MAX_MESSAGE_BYTES = 8 * 1024 * 1024
_LENGTH = struct.Struct(">I")


def _validate_limit(max_message_bytes: int) -> None:
    if isinstance(max_message_bytes, bool) or not isinstance(max_message_bytes, int):
        raise TypeError("max_message_bytes must be an integer")
    if max_message_bytes < 2 or max_message_bytes > 0xFFFFFFFF:
        raise ValueError("max_message_bytes must be between 2 and 2^32-1")


def canonical_json_bytes(value: object) -> bytes:
    if isinstance(value, WireModel):
        value = value.to_wire()
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise protocol_error(
            ErrorCode.PROTOCOL_INVALID_REQUEST,
            "消息不能编码为标准 JSON。",
            details={"reason": str(error)},
        ) from None


def encode_frame(
    message: object,
    *,
    max_message_bytes: int = DEFAULT_MAX_MESSAGE_BYTES,
    validate: bool = True,
) -> bytes:
    """Encode exactly one JSON-RPC message with an unsigned network-order length."""

    _validate_limit(max_message_bytes)
    payload = canonical_json_bytes(message)
    if validate:
        parse_jsonrpc_message(payload)
    if len(payload) > max_message_bytes:
        raise protocol_error(
            ErrorCode.PROTOCOL_MESSAGE_TOO_LARGE,
            "协议消息超过单消息大小上限; 大内容必须转为 Artifact 引用。",
            details={"actualBytes": len(payload), "maximumBytes": max_message_bytes},
        )
    return _LENGTH.pack(len(payload)) + payload


class LengthPrefixedJsonRpcDecoder:
    """Incremental decoder supporting arbitrary split and coalesced pipe reads.

    A malformed frame poisons the decoder because production transports must close
    the connection after a protocol violation.  ``reset`` is only intended for a
    newly authenticated transport stream, not for skipping bad bytes in-place.
    """

    def __init__(self, *, max_message_bytes: int = DEFAULT_MAX_MESSAGE_BYTES) -> None:
        _validate_limit(max_message_bytes)
        self._max_message_bytes = max_message_bytes
        self._buffer = bytearray()
        self._expected_length: int | None = None
        self._poisoned: ProtocolViolation | None = None

    @property
    def buffered_bytes(self) -> int:
        return len(self._buffer)

    @property
    def expected_length(self) -> int | None:
        return self._expected_length

    @property
    def poisoned(self) -> bool:
        return self._poisoned is not None

    def feed(self, chunk: bytes | bytearray | memoryview) -> list[JsonRpcMessage]:
        if self._poisoned is not None:
            raise self._poisoned
        if not isinstance(chunk, (bytes, bytearray, memoryview)):
            raise TypeError("framing input must be bytes-like")
        self._buffer.extend(chunk)
        messages: list[JsonRpcMessage] = []
        try:
            while True:
                if self._expected_length is None:
                    if len(self._buffer) < LENGTH_PREFIX_BYTES:
                        break
                    self._expected_length = _LENGTH.unpack(self._buffer[:LENGTH_PREFIX_BYTES])[0]
                    del self._buffer[:LENGTH_PREFIX_BYTES]
                    if self._expected_length > self._max_message_bytes:
                        raise protocol_error(
                            ErrorCode.PROTOCOL_MESSAGE_TOO_LARGE,
                            "协议消息声明长度超过上限。",
                            details={
                                "declaredBytes": self._expected_length,
                                "maximumBytes": self._max_message_bytes,
                            },
                        )
                    if self._expected_length == 0:
                        raise protocol_error(
                            ErrorCode.PROTOCOL_INVALID_JSON,
                            "零长度 frame 不是合法 JSON-RPC 消息。",
                        )
                if len(self._buffer) < self._expected_length:
                    break
                payload = bytes(self._buffer[: self._expected_length])
                del self._buffer[: self._expected_length]
                self._expected_length = None
                messages.append(parse_jsonrpc_message(payload))
        except ProtocolViolation as error:
            self._buffer.clear()
            self._expected_length = None
            self._poisoned = error
            raise
        return messages

    def end_of_stream(self) -> None:
        """Assert that EOF occurred exactly on a frame boundary."""

        if self._poisoned is not None:
            raise self._poisoned
        if self._buffer or self._expected_length is not None:
            error = protocol_error(
                ErrorCode.PROTOCOL_INVALID_REQUEST,
                "Transport 在完整 frame 到达前关闭。",
                details={
                    "bufferedBytes": len(self._buffer),
                    "expectedBytes": self._expected_length,
                },
            )
            self._poisoned = error
            raise error

    def reset(self) -> None:
        self._buffer.clear()
        self._expected_length = None
        self._poisoned = None


__all__ = [
    "DEFAULT_MAX_MESSAGE_BYTES",
    "LENGTH_PREFIX_BYTES",
    "LengthPrefixedJsonRpcDecoder",
    "canonical_json_bytes",
    "encode_frame",
]
