from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from offeragent_harness.runtime import conversation_attachments as attachment_module
from offeragent_harness.runtime.conversation_attachments import (
    AttachmentClaim,
    AttachmentError,
    AttachmentLimits,
    AttachmentUploadRequest,
    ConversationAttachmentStore,
)
from offeragent_harness.testing import DeterministicIdGenerator, ManualCancellationToken, ManualClock

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAIAAAD91JpzAAAAEElEQVR4nGP8zwACTGCSAQANHQEDgslx/wAAAABJRU5ErkJggg=="
)
JPEG = base64.b64decode(
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0"
    "Hyc5PTgyPC4zNDL/2wBDAQkJCQwLDBgNDRgyIRwhMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIy"
    "MjL/wAARCAACAAIDASIAAhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQID"
    "AAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlq"
    "c3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3"
    "+Pn6/8QAHwEAAwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3AAECAxEEBSExBhJBUQdhcRMiMoEI"
    "FEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOEhYaHiImK"
    "kpOUlZaXmJmaoqOkpaanqKmqsrO0tba3uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwDi"
    "6KKK+ZP3E//Z"
)
WEBP = base64.b64decode("UklGRjwAAABXRUJQVlA4IDAAAADQAQCdASoCAAIAAUAmJaACdLoB+AADsAD+8ut//NgVzXPv9//S4P0uD9Lg/9KQAAA=")
STATIC_GIF = base64.b64decode("R0lGODdhAgACAIEAAP8AAAAAAAAAAAAAACwAAAAAAgACAAAIBgABCAQQEAA7")
ANIMATED_GIF = base64.b64decode(
    "R0lGODlhAgACAIEAAP8AAAAAAAAAAAAAACH/C05FVFNDQVBFMi4wAwEAAAAh+QQACgAAACwAAAAAAgACAAAIBgABCAQQEAAh+QQBCgAB"
    "ACwAAAAAAgACAIEA/wAAAAAAAAAAAAAIBgABCAQQEAA7"
)


def digest(value: bytes) -> str:
    return f"sha256:{hashlib.sha256(value).hexdigest()}"


def request(
    payload: bytes = PNG,
    *,
    session_id: str = "ses_one",
    client_request_id: str = "req_upload",
    file_name: str = "interview.png",
    media_type: str = "image/png",
) -> AttachmentUploadRequest:
    return AttachmentUploadRequest(
        session_id=session_id,
        client_request_id=client_request_id,
        file_name=file_name,
        media_type=media_type,
        byte_length=len(payload),
        content_hash=digest(payload),
    )


def test_decode_attestation_cache_is_bounded_lru(tmp_path: Path) -> None:
    store = ConversationAttachmentStore(
        tmp_path / "attachments",
        workspace_id="ws_one",
        clock=ManualClock(datetime(2026, 7, 17, tzinfo=timezone.utc)),
        ids=DeterministicIdGenerator(),
    )
    maximum = attachment_module._MAX_DECODE_ATTESTATIONS

    for index in range(maximum + 3):
        store._remember_verified(
            cast(
                attachment_module._StoredAttachment,
                SimpleNamespace(
                    artifact_id=f"art_{index:04d}",
                    content_hash=f"sha256:{index:064x}",
                ),
            ),
            1,
            1,
        )

    assert len(store._verified_files) == maximum
    assert "art_0000" not in store._verified_files
    assert f"art_{maximum + 2:04d}" in store._verified_files


@pytest.mark.asyncio
async def test_chunked_upload_is_exactly_replayable_and_survives_restart(tmp_path: Path) -> None:
    clock = ManualClock(datetime(2026, 7, 17, tzinfo=timezone.utc))
    ids = DeterministicIdGenerator()
    store = ConversationAttachmentStore(tmp_path / "attachments", workspace_id="ws_one", clock=clock, ids=ids)
    token = ManualCancellationToken()

    begun = await store.begin(request(), token)
    replayed_begin = await store.begin(request(), token)
    assert replayed_begin == begun.__class__(
        upload_id=begun.upload_id,
        artifact_id=begun.artifact_id,
        next_offset=0,
        duplicate=True,
    )

    first = await store.append(begun.upload_id, 0, PNG[:8], token)
    duplicate = await store.append(begun.upload_id, 0, PNG[:8], token)
    assert first.next_offset == duplicate.next_offset == 8
    assert first.duplicate is False and duplicate.duplicate is True
    with pytest.raises(AttachmentError, match="offset"):
        await store.append(begun.upload_id, 7, b"x", token)
    with pytest.raises(AttachmentError, match="Conversation"):
        await store.append(begun.upload_id, 8, PNG[8:], token, session_id="ses_other")
    await store.append(begun.upload_id, 8, PNG[8:], token)

    committed = await store.commit(begun.upload_id, token)
    assert committed.artifact.artifact_id == begun.artifact_id
    assert committed.artifact.content_hash == digest(PNG)
    assert committed.artifact.media_type == "image/png"
    assert committed.artifact.size_bytes == len(PNG)
    assert (await store.commit(begun.upload_id, token)).duplicate is True

    reopened = ConversationAttachmentStore(tmp_path / "attachments", workspace_id="ws_one", clock=clock, ids=ids)
    with pytest.raises(AttachmentError, match="Conversation"):
        await reopened.read_for_conversation("ses_other", begun.artifact_id, 0, 7, token)
    read = await reopened.read(begun.artifact_id, offset=3, max_bytes=7, cancellation=token)
    assert read.content == PNG[3:10]
    assert read.next_offset == 10
    assert read.complete is False
    materialized = await reopened.read_all_for_conversation("ses_one", begun.artifact_id, token)
    assert materialized.offset == 0
    assert materialized.next_offset == len(PNG)
    assert materialized.content == PNG
    assert materialized.complete is True


@pytest.mark.asyncio
async def test_range_reads_decode_an_unchanged_committed_image_only_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = ManualClock(datetime(2026, 7, 17, tzinfo=timezone.utc))
    ids = DeterministicIdGenerator()
    store = ConversationAttachmentStore(tmp_path / "attachments", workspace_id="ws_one", clock=clock, ids=ids)
    token = ManualCancellationToken()
    begun = await store.begin(request(), token)
    await store.append(begun.upload_id, 0, PNG, token)
    await store.commit(begun.upload_id, token)

    reopened = ConversationAttachmentStore(tmp_path / "attachments", workspace_id="ws_one", clock=clock, ids=ids)
    original = attachment_module._verify_decodable_static_image
    decode_calls = 0

    def count_decode(content: bytes, media_type: str) -> tuple[int, int]:
        nonlocal decode_calls
        decode_calls += 1
        return original(content, media_type)

    monkeypatch.setattr(attachment_module, "_verify_decodable_static_image", count_decode)

    first = await reopened.read_for_conversation("ses_one", begun.artifact_id, 0, 8, token)
    second = await reopened.read_for_conversation("ses_one", begun.artifact_id, first.next_offset, 8, token)

    assert first.content + second.content == PNG[:16]
    assert decode_calls == 1
    assert len(reopened._verified_files) == 1

    object_path = reopened.root / "objects" / begun.artifact_id
    original_stat = object_path.stat()
    corrupted = bytearray(PNG)
    corrupted[-10] ^= 0x01
    object_path.write_bytes(corrupted)
    os.utime(object_path, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
    with pytest.raises(AttachmentError) as corrupt:
        await reopened.read_for_conversation("ses_one", begun.artifact_id, 0, 8, token)
    assert corrupt.value.code == "attachment_corrupt"

    await reopened.abort(begun.upload_id, token, session_id="ses_one")
    assert reopened._verified_files == {}

    second_upload = await reopened.begin(
        request(session_id="ses_two", client_request_id="req_second"),
        token,
    )
    await reopened.append(second_upload.upload_id, 0, PNG, token)
    await reopened.commit(second_upload.upload_id, token)
    await reopened.read_for_conversation("ses_two", second_upload.artifact_id, 0, 8, token)
    assert len(reopened._verified_files) == 1

    await reopened.delete_conversation("ses_two", token)
    assert reopened._verified_files == {}


@pytest.mark.asyncio
async def test_claim_and_materialization_rehash_but_do_not_repeat_expensive_decode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = attachment_module._verify_decodable_static_image
    decode_calls = 0

    def count_decode(content: bytes, media_type: str) -> tuple[int, int]:
        nonlocal decode_calls
        decode_calls += 1
        return original(content, media_type)

    monkeypatch.setattr(attachment_module, "_verify_decodable_static_image", count_decode)
    store = ConversationAttachmentStore(
        tmp_path / "attachments",
        workspace_id="ws_one",
        clock=ManualClock(datetime(2026, 7, 17, tzinfo=timezone.utc)),
        ids=DeterministicIdGenerator(),
    )
    token = ManualCancellationToken()
    begun = await store.begin(request(), token)
    await store.append(begun.upload_id, 0, PNG, token)
    artifact = (await store.commit(begun.upload_id, token)).artifact
    claim = AttachmentClaim(
        artifact_id=artifact.artifact_id,
        order=0,
        content_hash=artifact.content_hash,
        media_type=artifact.media_type,
        byte_length=artifact.size_bytes,
    )

    await store.claim_submission("ses_one", "turn_one", (claim,), token)
    first = await store.read_all_for_conversation("ses_one", artifact.artifact_id, token)
    await store.claim_submission("ses_one", "turn_one", (claim,), token)
    second = await store.read_all_for_conversation("ses_one", artifact.artifact_id, token)

    assert first.content == second.content == PNG
    assert decode_calls == 1


@pytest.mark.asyncio
async def test_upload_limits_signatures_abort_and_expiry_fail_safely(tmp_path: Path) -> None:
    clock = ManualClock(datetime(2026, 7, 17, tzinfo=timezone.utc))
    limits = AttachmentLimits(
        max_image_bytes=len(PNG),
        max_submission_bytes=len(PNG),
        max_conversation_bytes=len(PNG),
        max_total_bytes=len(PNG) * 2,
        staging_ttl=timedelta(minutes=1),
    )
    store = ConversationAttachmentStore(
        tmp_path / "attachments",
        workspace_id="ws_one",
        clock=clock,
        ids=DeterministicIdGenerator(),
        limits=limits,
    )
    token = ManualCancellationToken()

    with pytest.raises(AttachmentError, match=r"10 MiB|image limit"):
        await store.begin(request(PNG + b"x", client_request_id="req_large"), token)

    wrong = await store.begin(request(client_request_id="req_wrong"), token)
    await store.append(wrong.upload_id, 0, b"not-an-image".ljust(len(PNG), b"!"), token)
    with pytest.raises(AttachmentError, match="signature"):
        await store.commit(wrong.upload_id, token)
    await store.abort(wrong.upload_id, token)

    wrong_hash_request = AttachmentUploadRequest(
        session_id="ses_one",
        client_request_id="req_wrong_hash",
        file_name="wrong-hash.png",
        media_type="image/png",
        byte_length=len(PNG),
        content_hash="sha256:" + "0" * 64,
    )
    wrong_hash = await store.begin(wrong_hash_request, token)
    await store.append(wrong_hash.upload_id, 0, PNG, token)
    with pytest.raises(AttachmentError) as caught:
        await store.commit(wrong_hash.upload_id, token)
    assert caught.value.code == "invalid_image"
    await store.abort(wrong_hash.upload_id, token)

    malformed_payload = b"\x89PNG\r\n\x1a\n" + b"x" * (len(PNG) - 8)
    malformed = await store.begin(
        request(malformed_payload, client_request_id="req_malformed", file_name="malformed.png"),
        token,
    )
    await store.append(malformed.upload_id, 0, malformed_payload, token)
    with pytest.raises(AttachmentError, match=r"format|decode"):
        await store.commit(malformed.upload_id, token)
    await store.abort(malformed.upload_id, token)

    with pytest.raises(AttachmentError, match="unavailable"):
        await store.commit(wrong.upload_id, token)

    expired = await store.begin(request(client_request_id="req_expired"), token)
    await store.append(expired.upload_id, 0, PNG[:8], token)
    clock.advance(timedelta(minutes=2))
    report = await store.recover((), token)
    assert expired.artifact_id in report.removed_artifact_ids

    first = await store.begin(request(session_id="ses_a", client_request_id="req_a"), token)
    with pytest.raises(AttachmentError, match=r"delete this Conversation|new Conversation"):
        await store.begin(request(session_id="ses_a", client_request_id="req_a2"), token)
    second = await store.begin(request(session_id="ses_b", client_request_id="req_b"), token)
    assert first.artifact_id != second.artifact_id
    with pytest.raises(AttachmentError, match=r"clean up|delete"):
        await store.begin(request(session_id="ses_c", client_request_id="req_c"), token)


@pytest.mark.asyncio
async def test_image_decoder_accepts_static_gif_and_rejects_animation(tmp_path: Path) -> None:
    store = ConversationAttachmentStore(
        tmp_path / "attachments",
        workspace_id="ws_one",
        clock=ManualClock(datetime(2026, 7, 17, tzinfo=timezone.utc)),
        ids=DeterministicIdGenerator(),
    )
    token = ManualCancellationToken()
    static = await store.begin(
        request(STATIC_GIF, client_request_id="req_static", file_name="static.gif", media_type="image/gif"),
        token,
    )
    await store.append(static.upload_id, 0, STATIC_GIF, token)
    assert (await store.commit(static.upload_id, token)).artifact.media_type == "image/gif"

    animated = await store.begin(
        request(ANIMATED_GIF, client_request_id="req_animated", file_name="animated.gif", media_type="image/gif"),
        token,
    )
    await store.append(animated.upload_id, 0, ANIMATED_GIF, token)
    with pytest.raises(AttachmentError, match="Animated image"):
        await store.commit(animated.upload_id, token)


@pytest.mark.asyncio
async def test_claims_preserve_order_support_reuse_and_delete_only_with_the_conversation(tmp_path: Path) -> None:
    store = ConversationAttachmentStore(
        tmp_path / "attachments",
        workspace_id="ws_one",
        clock=ManualClock(datetime(2026, 7, 17, tzinfo=timezone.utc)),
        ids=DeterministicIdGenerator(),
    )
    token = ManualCancellationToken()
    committed = []
    for index, (payload, media_type, name) in enumerate(
        (
            (PNG, "image/png", "first.png"),
            (JPEG, "image/jpeg", "second.jpg"),
            (WEBP, "image/webp", "third.webp"),
        )
    ):
        begun = await store.begin(
            request(
                payload,
                client_request_id=f"req_{index}",
                file_name=name,
                media_type=media_type,
            ),
            token,
        )
        await store.append(begun.upload_id, 0, payload, token)
        committed.append(await store.commit(begun.upload_id, token))

    claims = tuple(
        AttachmentClaim(
            artifact_id=item.artifact.artifact_id,
            order=index,
            content_hash=item.artifact.content_hash,
            media_type=item.artifact.media_type,
            byte_length=item.artifact.size_bytes,
        )
        for index, item in enumerate(committed)
    )
    first_claim = await store.claim_submission_with_receipt("ses_one", "turn_one", claims, token)
    assert first_claim.created is True
    claimed = first_claim.attachments
    assert [item.order for item in claimed] == [0, 1, 2]
    duplicate_claim = await store.claim_submission_with_receipt("ses_one", "turn_one", claims, token)
    assert duplicate_claim.attachments == claimed
    assert duplicate_claim.created is False
    assert await store.claim_submission("ses_one", "turn_one", claims, token) == claimed
    reused = await store.claim_submission("ses_one", "turn_two", claims, token)
    assert [item.artifact_id for item in reused] == [item.artifact_id for item in claimed]

    with pytest.raises(AttachmentError, match="order"):
        await store.claim_submission("ses_one", "turn_bad", tuple(reversed(claims)), token)
    with pytest.raises(AttachmentError, match="another Conversation"):
        await store.claim_submission("ses_other", "turn_other", claims, token)
    with pytest.raises(AttachmentError, match="claimed"):
        await store.abort(committed[0].upload_id, token)

    await store.recover((), token, existing_turn_ids=("turn_one",))
    await store.release_turn_claim("turn_one", token)
    await store.abort(committed[0].upload_id, token)

    # Archive has no attachment mutation: the exact bytes remain readable.
    assert (await store.read(claimed[1].artifact_id, 0, 1024, token)).content == JPEG
    await store.delete_conversation("ses_one", token)
    with pytest.raises(AttachmentError, match="unavailable"):
        await store.read(claimed[1].artifact_id, 0, 1024, token)


@pytest.mark.asyncio
async def test_claimed_submission_materializes_atomically_in_turn_order_across_restart(tmp_path: Path) -> None:
    root = tmp_path / "attachments"
    clock = ManualClock(datetime(2026, 7, 17, tzinfo=timezone.utc))
    token = ManualCancellationToken()
    store = ConversationAttachmentStore(
        root,
        workspace_id="ws_one",
        clock=clock,
        ids=DeterministicIdGenerator(),
    )
    committed = []
    for index, (payload, media_type, name) in enumerate(
        ((PNG, "image/png", "first.png"), (JPEG, "image/jpeg", "second.jpg"))
    ):
        begun = await store.begin(
            request(
                payload,
                client_request_id=f"req_materialize_{index}",
                file_name=name,
                media_type=media_type,
            ),
            token,
        )
        await store.append(begun.upload_id, 0, payload, token)
        committed.append(await store.commit(begun.upload_id, token))
    claims = tuple(
        AttachmentClaim(
            artifact_id=item.artifact.artifact_id,
            order=index,
            content_hash=item.artifact.content_hash,
            media_type=item.artifact.media_type,
            byte_length=item.artifact.size_bytes,
        )
        for index, item in enumerate(committed)
    )
    await store.claim_submission("ses_one", "turn_images", claims, token)

    reopened = ConversationAttachmentStore(
        root,
        workspace_id="ws_one",
        clock=clock,
        ids=DeterministicIdGenerator(start=100),
    )
    materialized = await reopened.materialize_claimed_submission(
        "ses_one",
        "turn_images",
        claims,
        token,
    )

    assert [item.attachment.order for item in materialized] == [0, 1]
    assert [item.attachment.artifact_id for item in materialized] == [claim.artifact_id for claim in claims]
    assert [item.content for item in materialized] == [PNG, JPEG]
    assert [(item.width, item.height) for item in materialized] == [(2, 2), (2, 2)]
    assert all("content=" not in repr(item) for item in materialized)
    inspected = await reopened.inspect_claimed_submission("ses_one", "turn_images", claims, token)
    assert [(item.attachment.order, item.width, item.height) for item in inspected] == [
        (0, 2, 2),
        (1, 2, 2),
    ]
    assert all(not hasattr(item, "content") for item in inspected)

    assert (
        await reopened.materialize_claimed_submission(
            "ses_one",
            "turn_without_images",
            (),
            token,
        )
        == ()
    )
    with pytest.raises(AttachmentError, match="claim"):
        await reopened.materialize_claimed_submission(
            "ses_one",
            "turn_images",
            (),
            token,
        )

    (root / "objects" / claims[1].artifact_id).write_bytes(b"x" * len(JPEG))
    with pytest.raises(AttachmentError) as corrupt_batch:
        await reopened.materialize_claimed_submission(
            "ses_one",
            "turn_images",
            claims,
            token,
        )
    assert corrupt_batch.value.item_order == 1

    with pytest.raises(AttachmentError, match="claim"):
        await reopened.materialize_claimed_submission(
            "ses_one",
            "turn_images",
            tuple(reversed(claims)),
            token,
        )
    with pytest.raises(AttachmentError, match="Conversation"):
        await reopened.materialize_claimed_submission(
            "ses_other",
            "turn_images",
            claims,
            token,
        )

    await reopened.delete_conversation("ses_one", token)
    with pytest.raises(AttachmentError, match=r"claim|unavailable"):
        await reopened.materialize_claimed_submission(
            "ses_one",
            "turn_images",
            claims,
            token,
        )


@pytest.mark.asyncio
async def test_claimed_batch_blocks_cross_instance_claim_release_until_materialization_returns(tmp_path: Path) -> None:
    root = tmp_path / "attachments"
    clock = ManualClock(datetime(2026, 7, 17, tzinfo=timezone.utc))
    token = ManualCancellationToken()
    first = ConversationAttachmentStore(
        root,
        workspace_id="ws_one",
        clock=clock,
        ids=DeterministicIdGenerator(),
    )
    begun = await first.begin(request(PNG, client_request_id="req_atomic_batch"), token)
    await first.append(begun.upload_id, 0, PNG, token)
    artifact = (await first.commit(begun.upload_id, token)).artifact
    claims = (
        AttachmentClaim(
            artifact.artifact_id,
            0,
            artifact.content_hash,
            artifact.media_type,
            artifact.size_bytes,
        ),
    )
    await first.claim_submission("ses_one", "turn_atomic", claims, token)
    second = ConversationAttachmentStore(
        root,
        workspace_id="ws_one",
        clock=clock,
        ids=DeterministicIdGenerator(start=100),
    )
    started = threading.Event()
    release_reader = threading.Event()
    original_verify = first._verify_ready_with_dimensions

    def blocking_verify(
        record: attachment_module._StoredAttachment,
    ) -> tuple[bytes, int, int]:
        started.set()
        assert release_reader.wait(timeout=2)
        return original_verify(record)

    first._verify_ready_with_dimensions = blocking_verify  # type: ignore[method-assign]
    materializing = asyncio.create_task(first.materialize_claimed_submission("ses_one", "turn_atomic", claims, token))
    assert await asyncio.to_thread(started.wait, 1)
    releasing = asyncio.create_task(second.release_turn_claim("turn_atomic", token))
    try:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(releasing), timeout=0.1)
    finally:
        release_reader.set()

    assert [item.content for item in await materializing] == [PNG]
    await releasing


@pytest.mark.asyncio
async def test_repeatedly_cancelled_claimed_batch_waits_for_worker_and_preserves_cancellation(tmp_path: Path) -> None:
    root = tmp_path / "attachments"
    clock = ManualClock(datetime(2026, 7, 17, tzinfo=timezone.utc))
    token = ManualCancellationToken()
    store = ConversationAttachmentStore(
        root,
        workspace_id="ws_one",
        clock=clock,
        ids=DeterministicIdGenerator(),
    )
    begun = await store.begin(request(PNG, client_request_id="req_cancelled_batch"), token)
    await store.append(begun.upload_id, 0, PNG, token)
    artifact = (await store.commit(begun.upload_id, token)).artifact
    claims = (
        AttachmentClaim(
            artifact.artifact_id,
            0,
            artifact.content_hash,
            artifact.media_type,
            artifact.size_bytes,
        ),
    )
    await store.claim_submission("ses_one", "turn_cancelled", claims, token)
    started = threading.Event()
    release_reader = threading.Event()

    def blocking_verify(
        record: attachment_module._StoredAttachment,
    ) -> tuple[bytes, int, int]:
        del record
        started.set()
        assert release_reader.wait(timeout=2)
        raise AttachmentError("attachment_corrupt", "worker failure must not replace cancellation")

    store._verify_ready_with_dimensions = blocking_verify  # type: ignore[method-assign]
    materializing = asyncio.create_task(
        store.materialize_claimed_submission("ses_one", "turn_cancelled", claims, token)
    )
    assert await asyncio.to_thread(started.wait, 1)
    materializing.cancel()
    await asyncio.sleep(0)
    assert not materializing.done()
    materializing.cancel()
    await asyncio.sleep(0)
    assert not materializing.done()

    release_reader.set()
    with pytest.raises(asyncio.CancelledError):
        await materializing
    await asyncio.wait_for(store.release_turn_claim("turn_cancelled", token), timeout=1)


@pytest.mark.parametrize(
    ("max_images", "max_bytes"),
    [(1, len(PNG) * 2), (2, len(PNG))],
)
@pytest.mark.asyncio
async def test_invalid_submission_capacity_rejects_the_whole_ordered_batch(
    tmp_path: Path,
    max_images: int,
    max_bytes: int,
) -> None:
    store = ConversationAttachmentStore(
        tmp_path / f"attachments-{max_images}-{max_bytes}",
        workspace_id="ws_one",
        clock=ManualClock(datetime(2026, 7, 17, tzinfo=timezone.utc)),
        ids=DeterministicIdGenerator(),
        limits=AttachmentLimits(
            max_image_bytes=len(PNG),
            max_submission_images=max_images,
            max_submission_bytes=max_bytes,
            max_conversation_bytes=len(PNG) * 2,
            max_total_bytes=len(PNG) * 2,
        ),
    )
    token = ManualCancellationToken()
    committed = []
    for index in range(2):
        begun = await store.begin(
            request(PNG, client_request_id=f"req_capacity_{index}", file_name=f"page-{index}.png"),
            token,
        )
        await store.append(begun.upload_id, 0, PNG, token)
        committed.append((await store.commit(begun.upload_id, token)).artifact)
    claims = tuple(
        AttachmentClaim(
            artifact_id=artifact.artifact_id,
            order=index,
            content_hash=artifact.content_hash,
            media_type=artifact.media_type,
            byte_length=artifact.size_bytes,
        )
        for index, artifact in enumerate(committed)
    )

    with pytest.raises(AttachmentError) as caught:
        await store.claim_submission("ses_one", "turn_capacity", claims, token)

    assert caught.value.code == "submission_too_large"
    accepted = await store.claim_submission_with_receipt("ses_one", "turn_capacity", claims[:1], token)
    assert accepted.created is True
    assert [item.artifact_id for item in accepted.attachments] == [committed[0].artifact_id]


@pytest.mark.asyncio
async def test_recovery_finishes_a_conversation_deletion_tombstone(tmp_path: Path) -> None:
    clock = ManualClock(datetime(2026, 7, 17, tzinfo=timezone.utc))
    store = ConversationAttachmentStore(
        tmp_path / "attachments",
        workspace_id="ws_one",
        clock=clock,
        ids=DeterministicIdGenerator(),
    )
    token = ManualCancellationToken()
    begun = await store.begin(request(), token)
    await store.append(begun.upload_id, 0, PNG, token)
    await store.commit(begun.upload_id, token)
    await store.claim_submission(
        "ses_one",
        "turn_one",
        (AttachmentClaim(begun.artifact_id, 0, digest(PNG), "image/png", len(PNG)),),
        token,
    )

    await store.mark_conversation_deleting("ses_one", token)
    assert any(path.suffix == ".deleting" for path in (tmp_path / "attachments" / "objects").iterdir())

    reopened = ConversationAttachmentStore(
        tmp_path / "attachments",
        workspace_id="ws_one",
        clock=clock,
        ids=DeterministicIdGenerator(start=100),
    )
    report = await reopened.recover(("ses_one",), token)
    assert report.deleted_session_ids == ("ses_one",)
    assert list((tmp_path / "attachments" / "objects").iterdir()) == []
