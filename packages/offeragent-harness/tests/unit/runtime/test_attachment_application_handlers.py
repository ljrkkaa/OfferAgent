from __future__ import annotations

import base64
import hashlib
import json
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from offeragent_harness.agent import RunPreparationFailure
from offeragent_harness.agent.context_manager import UserImageProvenance
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
    AttachmentClaim,
    AttachmentClaimReceipt,
    AttachmentError,
    AttachmentUploadRequest,
    ConversationAttachmentStore,
)
from offeragent_harness.runtime.production_worker_composition import _resolved_context_inputs
from offeragent_harness.runtime.session_service import SessionGetCommand
from offeragent_harness.testing import DeterministicIdGenerator, ManualCancellationToken, ManualClock

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAIAAAD91JpzAAAAEElEQVR4nGP8zwACTGCSAQANHQEDgslx/wAAAABJRU5ErkJggg=="
)
PNG_ALT = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGNgYPgPAAEDAQAIicLsAAAAAElFTkSuQmCC"
)


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
    await store.claim_submission(
        "ses_one",
        "turn_one",
        (
            AttachmentClaim(
                artifact.artifact_id,
                0,
                artifact.content_hash,
                artifact.media_type,
                artifact.size_bytes,
            ),
        ),
        token,
    )

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
        turn_id="turn_one",
        attachments=store,
        cancellation=token,
        captured_on=date(2026, 7, 17),
        model_binding=cast(
            Any,
            SimpleNamespace(
                model=SimpleNamespace(
                    input_modalities=("text", "image"),
                    supports_image_detail_original=False,
                )
            ),
        ),
    )

    fragment = inputs.user_input[0]
    assert fragment.model_blocks[0].binary_data == PNG
    assert fragment.image_provenance is UserImageProvenance.CURRENT_SUBMISSION
    assert fragment.artifact_ids == (artifact.artifact_id,)
    assert "read the exact current version before relying" in fragment.text


@pytest.mark.asyncio
async def test_run_context_projects_ordered_interview_submission_manifest_without_image_bytes(
    tmp_path: Path,
) -> None:
    store = ConversationAttachmentStore(
        tmp_path / "attachments",
        workspace_id="ws_one",
        clock=ManualClock(datetime(2026, 7, 18, tzinfo=timezone.utc)),
        ids=DeterministicIdGenerator(),
    )
    token = ManualCancellationToken()
    artifacts = []
    for request_id, file_name, content in (
        ("req_first", "first.png", PNG),
        ("req_second", "second.png", PNG_ALT),
    ):
        begun = await store.begin(
            AttachmentUploadRequest(
                "ses_one",
                request_id,
                file_name,
                "image/png",
                len(content),
                digest(content),
            ),
            token,
        )
        await store.append(begun.upload_id, 0, content, token)
        artifacts.append((await store.commit(begun.upload_id, token)).artifact)

    model_binding = cast(
        Any,
        SimpleNamespace(
            model=SimpleNamespace(
                input_modalities=("text", "image"),
                supports_image_detail_original=True,
            )
        ),
    )

    async def resolve(order: tuple[int, int], turn_id: str) -> object:
        ordered = tuple(artifacts[index] for index in order)
        await store.claim_submission(
            "ses_one",
            turn_id,
            tuple(
                AttachmentClaim(
                    artifact.artifact_id,
                    image_order,
                    artifact.content_hash,
                    artifact.media_type,
                    artifact.size_bytes,
                )
                for image_order, artifact in enumerate(ordered)
            ),
            token,
        )
        return await _resolved_context_inputs(
            (
                {"type": "text", "text": "ingest these pages", "format": "markdown", "references": []},
                *({"type": "image", "artifact": artifact.to_wire(), "altText": artifact.title} for artifact in ordered),
            ),
            session_id="ses_one",
            turn_id=turn_id,
            attachments=store,
            cancellation=token,
            model_binding=model_binding,
            captured_on=date(2026, 7, 18),
        )

    ordered_inputs = cast(Any, await resolve((0, 1), "turn_ordered"))
    reversed_inputs = cast(Any, await resolve((1, 0), "turn_reversed"))
    ordered_fragment = ordered_inputs.user_input[0]
    reversed_fragment = reversed_inputs.user_input[0]
    ordered_manifest = next(
        item for item in json.loads(ordered_fragment.text) if item.get("type") == "runtimeOrderedImageSource"
    )
    reversed_manifest = next(
        item for item in json.loads(reversed_fragment.text) if item.get("type") == "runtimeOrderedImageSource"
    )

    assert ordered_manifest["capturedOn"] == "2026-07-18"
    assert ordered_manifest["imageCount"] == 2
    assert ordered_manifest["orderedImageContentHashes"] == [item.content_hash for item in artifacts]
    assert reversed_manifest["orderedImageContentHashes"] == [item.content_hash for item in reversed(artifacts)]
    assert ordered_manifest["sourceFingerprint"] != reversed_manifest["sourceFingerprint"]
    serialized_manifest = json.dumps(ordered_manifest, sort_keys=True)
    assert base64.b64encode(PNG).decode("ascii") not in serialized_manifest
    assert base64.b64encode(PNG_ALT).decode("ascii") not in serialized_manifest
    assert [block.binary_data for block in ordered_fragment.model_blocks] == [PNG, PNG_ALT]


@pytest.mark.asyncio
async def test_run_context_rejects_images_without_verified_model_binding_before_attachment_io() -> None:
    class Attachments:
        calls = 0

        async def read_all_for_conversation(self, *args: object) -> object:
            del args
            self.calls += 1
            raise AssertionError("missing model binding must fail before attachment I/O")

    attachments = Attachments()
    with pytest.raises(RunPreparationFailure) as caught:
        await _resolved_context_inputs(
            (
                {
                    "type": "image",
                    "artifact": {
                        "artifactId": "art_one",
                        "contentHash": digest(PNG),
                        "mediaType": "image/png",
                        "sizeBytes": len(PNG),
                        "sensitivity": "private",
                        "state": "complete",
                    },
                },
            ),
            session_id="ses_one",
            turn_id="turn_one",
            attachments=cast(Any, attachments),
            cancellation=ManualCancellationToken(),
            captured_on=date(2026, 7, 17),
            model_binding=None,
        )

    assert caught.value.error_code.value == "provider.image_unsupported"
    assert attachments.calls == 0


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


@pytest.mark.parametrize(
    ("sensitivity", "state"),
    (("secret", "complete"), ("private", "unverified")),
)
@pytest.mark.asyncio
async def test_turn_start_rejects_non_private_or_incomplete_image_metadata_before_claim(
    sensitivity: str,
    state: str,
) -> None:
    class Harness:
        calls = 0

        async def start_turn(self, command: object) -> object:
            del command
            self.calls += 1
            raise AssertionError("invalid image metadata must not start a Run")

    class Attachments:
        calls = 0

        async def claim_submission_with_receipt(self, *args: object) -> object:
            del args
            self.calls += 1
            raise AssertionError("invalid image metadata must not be claimed")

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

    harness = Harness()
    attachments = Attachments()
    handlers = _turn_handlers(
        identity=DomainCommandIdentity("ws_one", "profile_one", "managed", "actor_one"),
        harness=harness,  # type: ignore[arg-type]
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
                        "sensitivity": sensitivity,
                        "state": state,
                    },
                }
            ],
            "runConfig": {"provider": "codex-subscription-experimental", "model": "gpt-test"},
        },
    )
    assert isinstance(params, TurnStartParams)

    with pytest.raises(AttachmentError) as caught:
        await handlers["turn/start"](
            params,
            ManualCancellationToken(),
            ApplicationCommandContext(transport="stdio"),
        )

    assert caught.value.code == "metadata_conflict"
    assert attachments.calls == 0
    assert harness.calls == 0
