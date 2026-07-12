from __future__ import annotations

import struct

import pytest

from offeragent_harness.protocol.errors import ErrorCode, ProtocolViolation
from offeragent_harness.protocol.framing import LengthPrefixedJsonRpcDecoder, encode_frame
from offeragent_harness.protocol.jsonrpc import JsonRpcMessage, JsonRpcRequest, validate_request
from offeragent_harness.protocol.messages import TurnStartParams
from offeragent_harness.protocol.schemas import build_examples


def _ping_request(number: int = 1) -> dict[str, object]:
    return {
        "jsonrpc": "2.0",
        "id": number,
        "method": "runtime/ping",
        "params": {"nonce": f"req_{number}"},
    }


@pytest.mark.parametrize("split_at", range(1, 32))
def test_decoder_accepts_every_header_and_early_payload_split(split_at: int) -> None:
    frame = encode_frame(_ping_request())
    if split_at >= len(frame):
        pytest.skip("split is beyond this frame")
    decoder = LengthPrefixedJsonRpcDecoder()
    assert decoder.feed(frame[:split_at]) == []
    messages = decoder.feed(frame[split_at:])
    assert len(messages) == 1
    assert isinstance(messages[0], JsonRpcRequest)
    assert messages[0].method == "runtime/ping"
    decoder.end_of_stream()


def test_decoder_accepts_one_byte_fragments_with_multibyte_utf8() -> None:
    frame = encode_frame(build_examples()["turn-start.request.json"])
    decoder = LengthPrefixedJsonRpcDecoder()
    messages: list[JsonRpcMessage] = []
    for byte in frame:
        messages.extend(decoder.feed(bytes([byte])))
    assert len(messages) == 1
    message = messages[0]
    assert isinstance(message, JsonRpcRequest)
    validated = validate_request(message)
    assert isinstance(validated.params, TurnStartParams)
    assert validated.params.input[0].type == "text"


def test_decoder_separates_coalesced_frames_and_preserves_order() -> None:
    decoder = LengthPrefixedJsonRpcDecoder()
    blob = b"".join(encode_frame(_ping_request(number)) for number in range(1, 11))
    messages = decoder.feed(blob)
    requests = [message for message in messages if isinstance(message, JsonRpcRequest)]
    assert len(requests) == len(messages)
    assert [message.id for message in requests] == list(range(1, 11))
    assert decoder.buffered_bytes == 0


def test_decoder_handles_complete_and_partial_stuck_frames_together() -> None:
    first = encode_frame(_ping_request(1))
    second = encode_frame(_ping_request(2))
    decoder = LengthPrefixedJsonRpcDecoder()
    messages = decoder.feed(first + second[:7])
    assert len(messages) == 1
    assert isinstance(messages[0], JsonRpcRequest)
    assert messages[0].id == 1
    final = decoder.feed(second[7:])[-1]
    assert isinstance(final, JsonRpcRequest)
    assert final.id == 2


def test_declared_oversize_is_rejected_before_payload_arrives_and_poisons_stream() -> None:
    decoder = LengthPrefixedJsonRpcDecoder(max_message_bytes=64)
    with pytest.raises(ProtocolViolation) as caught:
        decoder.feed(struct.pack(">I", 65))
    assert caught.value.error.code == ErrorCode.PROTOCOL_MESSAGE_TOO_LARGE
    assert decoder.poisoned is True
    with pytest.raises(ProtocolViolation) as repeated:
        decoder.feed(b"ignored")
    assert repeated.value is caught.value


def test_encoder_applies_utf8_byte_limit_not_character_count() -> None:
    message = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "turn/steer",
        "params": {},
        "note": "中" * 8,
    }
    with pytest.raises(ProtocolViolation) as caught:
        encode_frame(message, max_message_bytes=80, validate=False)
    assert caught.value.error.code == ErrorCode.PROTOCOL_MESSAGE_TOO_LARGE


def test_invalid_utf8_is_rejected_with_distinct_error() -> None:
    payload = b'{"jsonrpc":"2.0","id":1,"method":"runtime/status","params":{}}\xff'
    decoder = LengthPrefixedJsonRpcDecoder()
    with pytest.raises(ProtocolViolation) as caught:
        decoder.feed(struct.pack(">I", len(payload)) + payload)
    assert caught.value.error.code == ErrorCode.PROTOCOL_INVALID_UTF8


def test_invalid_json_and_zero_length_frame_are_rejected() -> None:
    decoder = LengthPrefixedJsonRpcDecoder()
    payload = b'{"jsonrpc":'
    with pytest.raises(ProtocolViolation) as invalid:
        decoder.feed(struct.pack(">I", len(payload)) + payload)
    assert invalid.value.error.code == ErrorCode.PROTOCOL_INVALID_JSON

    zero = LengthPrefixedJsonRpcDecoder()
    with pytest.raises(ProtocolViolation) as empty:
        zero.feed(struct.pack(">I", 0))
    assert empty.value.error.code == ErrorCode.PROTOCOL_INVALID_JSON


def test_eof_rejects_partial_header_and_partial_payload() -> None:
    for chunk in (b"\x00\x00", encode_frame(_ping_request())[:-1]):
        decoder = LengthPrefixedJsonRpcDecoder()
        decoder.feed(chunk)
        with pytest.raises(ProtocolViolation) as caught:
            decoder.end_of_stream()
        assert caught.value.error.code == ErrorCode.PROTOCOL_INVALID_REQUEST


def test_reset_only_reuses_decoder_as_a_new_stream() -> None:
    decoder = LengthPrefixedJsonRpcDecoder(max_message_bytes=128)
    with pytest.raises(ProtocolViolation):
        decoder.feed(struct.pack(">I", 129))
    decoder.reset()
    assert decoder.poisoned is False
    frame = encode_frame(_ping_request(), max_message_bytes=128)
    message = decoder.feed(frame)[0]
    assert isinstance(message, JsonRpcRequest)
    assert message.method == "runtime/ping"
