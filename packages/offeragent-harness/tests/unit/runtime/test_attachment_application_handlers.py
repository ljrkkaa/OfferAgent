from __future__ import annotations

import base64
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from offeragent_harness.ports import ApplicationCommandContext
from offeragent_harness.protocol.messages import (
    AttachmentAbortParams,
    AttachmentAbortResult,
    AttachmentBeginParams,
    AttachmentBeginResult,
    AttachmentChunkParams,
    AttachmentCommitParams,
    AttachmentReadParams,
    AttachmentReadResult,
    TurnStartParams,
    validate_command_params,
)
from offeragent_harness.runtime.application_domain_handlers import (
    DomainCommandIdentity,
    _attachment_handlers,
    _turn_handlers,
)
from offeragent_harness.runtime.conversation_attachments import (
    AttachmentClaimReceipt,
    AttachmentUploadRequest,
    ConversationAttachmentStore,
)
from offeragent_harness.runtime.production_worker_composition import _resolved_context_inputs
from offeragent_harness.runtime.session_service import SessionGetCommand
from offeragent_harness.testing import DeterministicIdGenerator, ManualCancellationToken, ManualClock

PNG = b"\x89PNG\r\n\x1a\n" + b"offeragent-image"


def digest(value: bytes) -> str:
    return f"sha256:{hashlib.sha256(value).hexdigest()}"


class Sessions:
    async def get(self, command: SessionGetCommand) -> object:
        if command.session_id != "ses_one":
            raise FileNotFoundError("missing")
        return object()


@pytest.mark.asyncio
async def test_attachment_handlers_keep_bytes_inside_bounded_python_commands(tmp_path: Path) -> None:
    store = ConversationAttachmentStore(
        tmp_path / "attachments",
        workspace_id="ws_one",
        clock=ManualClock(datetime(2026, 7, 17, tzinfo=timezone.utc)),
        ids=DeterministicIdGenerator(),
    )
    handlers = _attachment_handlers(
        workspace_id="ws_one",
        harness=SimpleNamespace(sessions=Sessions()),  # type: ignore[arg-type]
        attachments=store,
    )
    token = ManualCancellationToken()
    context = ApplicationCommandContext(transport="stdio")
    begun = cast(
        AttachmentBeginResult,
        await handlers["attachments/begin"](
            AttachmentBeginParams(
                session_id="ses_one",
                client_request_id="req_upload",
                file_name="evidence.png",
                media_type="image/png",
                byte_length=len(PNG),
                content_hash=digest(PNG),
            ),
            token,
            context,
        ),
    )
    upload_id = begun.upload_id
    artifact_id = begun.artifact_id

    for offset, content in ((0, PNG[:8]), (8, PNG[8:])):
        await handlers["attachments/chunk"](
            AttachmentChunkParams(
                session_id="ses_one",
                upload_id=upload_id,
                offset=offset,
                content_base64=base64.b64encode(content).decode("ascii"),
                content_hash=digest(content),
            ),
            token,
            context,
        )
    await handlers["attachments/commit"](
        AttachmentCommitParams(session_id="ses_one", upload_id=upload_id),
        token,
        context,
    )
    read = cast(
        AttachmentReadResult,
        await handlers["attachments/read"](
            AttachmentReadParams(session_id="ses_one", artifact_id=artifact_id),
            token,
            context,
        ),
    )
    assert base64.b64decode(read.content_base64, validate=True) == PNG

    with pytest.raises(ValueError, match="hash"):
        await handlers["attachments/chunk"](
            AttachmentChunkParams(
                session_id="ses_one",
                upload_id=upload_id,
                offset=0,
                content_base64=base64.b64encode(PNG[:8]).decode("ascii"),
                content_hash="sha256:" + "0" * 64,
            ),
            token,
            context,
        )
    aborted = cast(
        AttachmentAbortResult,
        await handlers["attachments/abort"](
            AttachmentAbortParams(session_id="ses_one", upload_id=upload_id),
            token,
            context,
        ),
    )
    assert aborted.aborted is True


@pytest.mark.asyncio
async def test_run_context_resolves_claimed_image_bytes_and_explicit_pin_guidance(tmp_path: Path) -> None:
    store = ConversationAttachmentStore(
        tmp_path / "attachments",
        workspace_id="ws_one",
        clock=ManualClock(datetime(2026, 7, 17, tzinfo=timezone.utc)),
        ids=DeterministicIdGenerator(),
    )
    token = ManualCancellationToken()
    begun = await store.begin(
        AttachmentUploadRequest("ses_one", "req_upload", "evidence.png", "image/png", len(PNG), digest(PNG)),
        token,
    )
    await store.append(begun.upload_id, 0, PNG, token)
    artifact = (await store.commit(begun.upload_id, token)).artifact

    inputs = await _resolved_context_inputs(
        (
            {"type": "text", "text": "compare this", "format": "markdown", "references": []},
            {"type": "image", "artifact": artifact.to_wire(), "altText": "offer screenshot"},
            {
                "type": "pinnedContext",
                "references": [{"kind": "document", "path": "notes/preferred.md"}],
            },
        ),
        session_id="ses_one",
        attachments=store,
        cancellation=token,
    )

    fragment = inputs.user_input[0]
    assert fragment.model_blocks[0].binary_data == PNG
    assert fragment.artifact_ids == (artifact.artifact_id,)
    assert "read the exact current version before relying" in fragment.text


@pytest.mark.parametrize(("claim_created", "expected_releases"), ((True, 1), (False, 0)))
@pytest.mark.asyncio
async def test_failed_turn_start_releases_only_the_claim_created_by_that_attempt(
    claim_created: bool,
    expected_releases: int,
) -> None:
    class FailingHarness:
        async def start_turn(self, command: object) -> object:
            del command
            raise RuntimeError("injected start failure")

    class Attachments:
        releases = 0

        async def claim_submission_with_receipt(self, *args: object) -> AttachmentClaimReceipt:
            del args
            return AttachmentClaimReceipt((), claim_created)

        async def release_turn_claim(self, turn_id: str, cancellation: object) -> None:
            del turn_id, cancellation
            self.releases += 1

    class Config:
        async def snapshot(self, **keys: str) -> object:
            del keys
            return SimpleNamespace(
                config=SimpleNamespace(
                    model=SimpleNamespace(
                        provider=SimpleNamespace(value="codex-subscription-experimental"),
                        model="gpt-test",
                    ),
                    policy=SimpleNamespace(allow_bypass=False, read_only=False, workspace_trusted=True),
                ),
                fingerprint="sha256:" + "a" * 64,
            )

    class TransportPolicy:
        async def resolve_run_route(self, context: object, permission_mode: object) -> object:
            del context
            return SimpleNamespace(permission_mode=permission_mode)

    attachments = Attachments()
    handlers = _turn_handlers(
        identity=DomainCommandIdentity("ws_one", "profile_one", "managed", "actor_one"),
        harness=FailingHarness(),  # type: ignore[arg-type]
        projections=SimpleNamespace(),
        config=Config(),  # type: ignore[arg-type]
        transport_policy=TransportPolicy(),  # type: ignore[arg-type]
        attachments=attachments,  # type: ignore[arg-type]
    )
    params = validate_command_params(
        "turn/start",
        {
            "sessionId": "ses_one",
            "turnId": "turn_one",
            "idempotencyKey": "turn-one",
            "input": [
                {
                    "type": "image",
                    "artifact": {
                        "artifactId": "art_one",
                        "contentHash": digest(PNG),
                        "mediaType": "image/png",
                        "sizeBytes": len(PNG),
                        "sensitivity": "private",
                    },
                }
            ],
            "runConfig": {"provider": "codex-subscription-experimental", "model": "gpt-test"},
        },
    )
    assert isinstance(params, TurnStartParams)

    with pytest.raises(RuntimeError, match="injected"):
        await handlers["turn/start"](params, ManualCancellationToken(), ApplicationCommandContext(transport="stdio"))
    assert attachments.releases == expected_releases
