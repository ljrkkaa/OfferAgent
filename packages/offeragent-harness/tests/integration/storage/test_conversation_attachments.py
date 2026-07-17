from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from offeragent_harness.runtime.conversation_attachments import (
    AttachmentClaim,
    AttachmentError,
    AttachmentLimits,
    AttachmentUploadRequest,
    ConversationAttachmentStore,
)
from offeragent_harness.testing import DeterministicIdGenerator, ManualCancellationToken, ManualClock

PNG = b"\x89PNG\r\n\x1a\n" + b"offeragent-image"
JPEG = b"\xff\xd8\xff\xe0" + b"offeragent-jpeg" + b"\xff\xd9"
GIF_IMAGE = b"\x2c" + b"\x00\x00\x00\x00\x01\x00\x01\x00\x00" + b"\x02\x03\x02\x2c\x01\x00"
STATIC_GIF = b"GIF89a" + b"\x01\x00\x01\x00\x80\x00\x00" + b"\x00\x00\x00\xff\xff\xff" + GIF_IMAGE + b"\x3b"
ANIMATED_GIF = STATIC_GIF[:-1] + GIF_IMAGE + b"\x3b"


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
async def test_gif_validation_reads_frame_blocks_instead_of_compressed_payload_bytes(tmp_path: Path) -> None:
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
    with pytest.raises(AttachmentError, match="Animated GIF"):
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
        ((PNG, "image/png", "first.png"), (JPEG, "image/jpeg", "second.jpg"))
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
    assert [item.order for item in claimed] == [0, 1]
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
