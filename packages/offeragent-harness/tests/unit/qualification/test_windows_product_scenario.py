# ruff: noqa: RUF001 -- Chinese prompts are qualification inputs.

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

import pytest

from offeragent_harness.qualification.windows_product_driver import QualificationDriverEventTimeout
from offeragent_harness.qualification.windows_product_scenario import (
    BuiltProductQualificationError,
    BuiltProductQualificationSession,
    InterviewImagePage,
)


class _ScriptedDriver:
    def __init__(
        self,
        responses: list[tuple[str, str | None, dict[str, Any]]],
        events: list[dict[str, Any] | BaseException] | None = None,
    ) -> None:
        self.responses = responses
        self.events = events or []
        self.requests: list[tuple[str, Mapping[str, Any]]] = []

    def request(
        self,
        command: str,
        params: dict[str, Any],
        *,
        timeout: float = 60,
    ) -> dict[str, Any]:
        del timeout
        self.requests.append((command, params))
        expected_command, expected_method, result = self.responses.pop(0)
        assert command == expected_command
        if expected_method is not None:
            assert params["method"] == expected_method
        return result

    def next_event(self, *, timeout: float) -> dict[str, Any]:
        assert timeout > 0
        if not self.events:
            raise AssertionError("test requested an unexpected product event")
        event = self.events.pop(0)
        if isinstance(event, BaseException):
            raise event
        return event


def _model(
    model: str = "gpt-5.5",
    *,
    modalities: list[str] | None = None,
    hosted_search: bool = True,
) -> dict[str, Any]:
    return {
        "model": model,
        "displayName": model,
        "inputModalities": modalities or ["text", "image"],
        "supportsImageDetailOriginal": True,
        "supportsHostedSearch": hosted_search,
        "webSearchToolType": "web_search",
        "contextWindow": 200_000,
        "maxContextWindow": 200_000,
        "effectiveContextWindowPercent": 95,
        "additionalSpeedTiers": [],
        "serviceTiers": [],
        "defaultServiceTier": None,
        "supportsFastMode": False,
        "maxContextTokens": 190_000,
    }


def _prepared_driver(*, catalog_freshness: str = "fresh") -> _ScriptedDriver:
    digest = "sha256:" + "a" * 64
    return _ScriptedDriver(
        [
            ("hello", None, {"driverProtocolVersion": 2, "sourceFreeRuntime": True}),
            ("product/start", None, {"identity": {"workerPid": 101}}),
            (
                "rpc",
                "config/get",
                {"scope": "workspace", "revision": 0, "values": {}, "restartPending": False},
            ),
            (
                "rpc",
                "config/update",
                {
                    "status": "restart_required",
                    "snapshot": {
                        "scope": "workspace",
                        "revision": 1,
                        "values": {"model": {"proxy_url": "http://127.0.0.1:7896"}},
                        "restartPending": True,
                    },
                    "fieldErrors": [],
                },
            ),
            ("product/stop", None, {"stopped": True, "workerPid": 101}),
            ("product/start", None, {"identity": {"workerPid": 202}}),
            (
                "rpc",
                "models/list",
                {
                    "models": [_model()],
                    "configRevision": 1,
                    "catalogFreshness": catalog_freshness,
                    "catalogRevision": digest,
                    "fetchedAt": "2026-07-19T00:00:00Z",
                    "accountBinding": digest,
                    "error": None,
                },
            ),
            (
                "rpc",
                "config/get",
                {
                    "scope": "workspace",
                    "revision": 1,
                    "values": {"model": {"proxy_url": "http://127.0.0.1:7896"}},
                    "restartPending": False,
                },
            ),
            (
                "rpc",
                "config/update",
                {
                    "status": "applied",
                    "snapshot": {
                        "scope": "workspace",
                        "revision": 2,
                        "values": {
                            "model": {
                                "model": "gpt-5.5",
                                "account_binding": digest,
                                "proxy_url": "http://127.0.0.1:7896",
                            },
                            "policy": {
                                "read_only": False,
                                "workspace_trusted": True,
                                "approve_vault_writes": True,
                            },
                        },
                        "restartPending": False,
                    },
                    "fieldErrors": [],
                },
            ),
        ]
    )


def test_prepares_fresh_account_bound_image_model_after_real_restart() -> None:
    driver = _prepared_driver()
    session = BuiltProductQualificationSession(driver, {"vaultRoot": "C:/sealed/Vault"})

    evidence = session.prepare_live_model(
        proxy_url="http://127.0.0.1:7896",
        model="gpt-5.5",
        require_image=True,
    )

    assert evidence.worker_pids == (101, 202)
    assert evidence.model == "gpt-5.5"
    assert evidence.input_modalities == ("text", "image")
    assert evidence.supports_hosted_search is True
    assert evidence.catalog_revision == "sha256:" + "a" * 64
    selection = driver.requests[-1][1]["params"]["patch"]
    assert selection == {
        "model": {
            "model": "gpt-5.5",
            "account_binding": "sha256:" + "a" * 64,
        },
        "policy": {
            "read_only": False,
            "workspace_trusted": True,
            "approve_vault_writes": True,
        },
    }
    assert not driver.responses


def test_refuses_to_qualify_a_stale_live_catalog() -> None:
    driver = _prepared_driver(catalog_freshness="stale")
    session = BuiltProductQualificationSession(driver, {"vaultRoot": "C:/sealed/Vault"})

    with pytest.raises(BuiltProductQualificationError, match="fresh complete catalog"):
        session.prepare_live_model(
            proxy_url="http://127.0.0.1:7896",
            model="gpt-5.5",
            require_image=True,
        )


def test_runs_text_preflight_with_persisted_model_and_waits_for_exact_terminal_run() -> None:
    driver = _prepared_driver()
    driver.responses.extend(
        [
            (
                "rpc",
                "session/create",
                {"session": {"sessionId": "ses_text"}, "created": True},
            ),
            (
                "rpc",
                "turn/start",
                {
                    "sessionId": "ses_text",
                    "turnId": "turn_text",
                    "runId": "run_text",
                    "accepted": True,
                    "duplicate": False,
                },
            ),
        ]
    )
    driver.events.extend(
        [
            {
                "event": "runtime.event",
                "value": {"eventId": "evt_other", "runId": "run_other", "type": "turn.completed"},
            },
            {"event": "runtime.event", "value": {"eventId": "evt_1", "runId": "run_text", "type": "turn.started"}},
            {"event": "runtime.event", "value": {"eventId": "evt_2", "runId": "run_text", "type": "turn.completed"}},
        ]
    )
    session = BuiltProductQualificationSession(driver, {"vaultRoot": "C:/sealed/Vault"})
    session.prepare_live_model(
        proxy_url="http://127.0.0.1:7896",
        model="gpt-5.5",
        require_image=True,
    )

    result = session.run_text_preflight("Return one short sentence.", timeout=5)

    assert result.session_id == "ses_text"
    assert result.run_id == "run_text"
    assert [event["type"] for event in result.events] == ["turn.started", "turn.completed"]
    turn_params = driver.requests[-1][1]["params"]
    assert turn_params["runConfig"] == {
        "model": "gpt-5.5",
        "reasoningEffort": "medium",
        "permissionMode": "read-only",
    }
    assert turn_params["input"] == [
        {"type": "text", "text": "Return one short sentence.", "format": "markdown", "references": []}
    ]
    assert not driver.responses


def test_text_preflight_recovers_missed_live_delivery_from_durable_replay() -> None:
    driver = _prepared_driver()
    driver.responses.extend(
        [
            (
                "rpc",
                "session/create",
                {"session": {"sessionId": "ses_text"}, "created": True},
            ),
            (
                "rpc",
                "turn/start",
                {
                    "sessionId": "ses_text",
                    "turnId": "turn_text",
                    "runId": "run_text",
                    "accepted": True,
                    "duplicate": False,
                },
            ),
            ("events/replay", None, {"lastSequence": 2}),
        ]
    )
    driver.events.extend(
        [
            QualificationDriverEventTimeout("poll elapsed"),
            {
                "event": "runtime.event",
                "value": {
                    "eventId": "evt_1",
                    "runId": "run_text",
                    "sequence": 1,
                    "type": "turn.started",
                },
            },
            {
                "event": "runtime.event",
                "value": {
                    "eventId": "evt_2",
                    "runId": "run_text",
                    "sequence": 2,
                    "type": "turn.completed",
                },
            },
        ]
    )
    session = BuiltProductQualificationSession(driver, {"vaultRoot": "C:/sealed/Vault"})
    session.prepare_live_model(
        proxy_url="http://127.0.0.1:7896",
        model="gpt-5.5",
        require_image=True,
    )

    result = session.run_text_preflight("Return one short sentence.", timeout=5)

    assert result.events[-1]["type"] == "turn.completed"
    assert driver.requests[-1] == (
        "events/replay",
        {"runId": "run_text", "afterSequence": 0, "limit": 1_000},
    )
    assert not driver.responses


def test_terminal_failure_reports_stable_error_and_last_tool_identity() -> None:
    driver = _prepared_driver()
    driver.responses.extend(
        [
            ("rpc", "session/create", {"session": {"sessionId": "ses_text"}, "created": True}),
            (
                "rpc",
                "turn/start",
                {
                    "sessionId": "ses_text",
                    "turnId": "turn_text",
                    "runId": "run_text",
                    "accepted": True,
                    "duplicate": False,
                },
            ),
        ]
    )
    driver.events.extend(
        [
            {
                "event": "runtime.event",
                "value": {
                    "eventId": "evt_calls",
                    "runId": "run_text",
                    "type": "tool.calls.accepted",
                    "payload": {"calls": [{"name": "vault.changes.apply"}]},
                },
            },
            {
                "event": "runtime.event",
                "value": {
                    "eventId": "evt_continuation",
                    "runId": "run_text",
                    "type": "run.continuation_required",
                    "payload": {"blockers": ["write_outcome_required"]},
                },
            },
            {
                "event": "runtime.event",
                "value": {
                    "eventId": "evt_failed",
                    "runId": "run_text",
                    "type": "turn.failed",
                    "payload": {
                        "error": {
                            "code": "provider_timeout",
                            "retryable": True,
                            "cancelled": False,
                            "userVisibleMessage": "Provider continuation timed out.",
                            "details": {
                                "providerProtocolReason": "unsupported_continuation_item",
                                "secret": "must-not-leak",
                            },
                        },
                        "usage": {"inputTokens": 404_079, "modelCalls": 5, "toolCalls": 4},
                        "partialContent": [],
                    },
                },
            },
        ]
    )
    session = BuiltProductQualificationSession(driver, {"vaultRoot": "C:/sealed/Vault"})
    session.prepare_live_model(
        proxy_url="http://127.0.0.1:7896",
        model="gpt-5.5",
        require_image=True,
    )

    with pytest.raises(
        BuiltProductQualificationError,
        match=(
            r"turn.failed code=provider_timeout retryable=true "
            r"message=Provider continuation timed out\. "
            r"protocolReason=unsupported_continuation_item inputTokens=404079 modelCalls=5 toolCalls=4 "
            r"toolTrace=vault\.changes\.apply lastTool=vault\.changes\.apply "
            r"blockerTrace=write_outcome_required"
        ),
    ) as captured:
        session.run_text_preflight("Return one short sentence.", timeout=5)

    assert "must-not-leak" not in str(captured.value)


def _artifact(index: int, payload: bytes) -> dict[str, Any]:
    return {
        "type": "image",
        "artifact": {
            "artifactId": f"art_page_{index}",
            "contentHash": f"sha256:{hashlib.sha256(payload).hexdigest()}",
            "mediaType": "image/png",
            "sizeBytes": len(payload),
            "sensitivity": "private",
            "state": "complete",
            "title": f"page-{index}.png",
        },
        "altText": f"Interview page {index} of 3",
    }


def _review(page_hashes: list[str]) -> dict[str, Any]:
    experience_content = "---\ntype: interview-experience\n---\n"
    question_content = "---\ntype: interview-question\n---\n"
    reviewed: dict[str, Any] = {
        "version": 1,
        "batchId": "batch_interview_1",
        "argsHash": "sha256:" + "b" * 64,
        "changeKind": "interview_submission",
        "task": "Ingest one ordered Interview Submission.",
        "paths": ["Interview Experience/fake.md", "Interview Questions/question.md"],
        "categorizedTargets": [
            {
                "path": "Interview Experience/fake.md",
                "operation": "create",
                "category": "experience",
                "expectedModifiedVersion": "missing",
                "expectedContentHash": "absent",
            },
            {
                "path": "Interview Questions/question.md",
                "operation": "create",
                "category": "question",
                "expectedModifiedVersion": "missing",
                "expectedContentHash": "absent",
            },
        ],
        "sourceBindings": [
            {
                "path": "Interview Experience/README.md",
                "expectedModifiedVersion": "mtime:1:size:1",
                "expectedContentHash": "sha256:" + "c" * 64,
            }
        ],
        "interviewSubmission": {
            "sourceFingerprint": "sha256:" + "d" * 64,
            "orderedImageContentHashes": page_hashes,
            "canonicalUrls": [],
            "reviewItems": [],
        },
        "reviewTargets": [
            {
                "operation": "create",
                "path": "Interview Experience/fake.md",
                "beforeContent": None,
                "afterContent": experience_content,
                "beforeContentHash": "absent",
                "afterContentHash": f"sha256:{hashlib.sha256(experience_content.encode()).hexdigest()}",
                "beforeModifiedVersion": "missing",
            },
            {
                "operation": "create",
                "path": "Interview Questions/question.md",
                "beforeContent": None,
                "afterContent": question_content,
                "beforeContentHash": "absent",
                "afterContentHash": f"sha256:{hashlib.sha256(question_content.encode()).hexdigest()}",
                "beforeModifiedVersion": "missing",
            },
        ],
        "controlFiles": False,
        "memoryDelete": False,
    }
    digest = hashlib.sha256(json.dumps(reviewed, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    return {**reviewed, "reviewHash": f"sha256:{digest}"}


def test_uploads_three_ordered_user_images_and_accepts_one_exact_review() -> None:
    driver = _prepared_driver()
    pages = (b"first-page", b"second-page", b"third-page")
    page_hashes = [f"sha256:{hashlib.sha256(page).hexdigest()}" for page in pages]
    proposal = _review(page_hashes)
    approval_id = "apr_01J00000000000000000000000"
    approval_args_hash = "sha256:" + "e" * 64
    approval_tool_call_id = "call_01J00000000000000000000000"
    approval_tool_call = {
        "toolCallId": approval_tool_call_id,
        "name": "vault.changes.apply",
        "version": "1",
        "arguments": {
            "changeKind": "interview_submission",
            "interviewSubmission": {"orderedImageContentHashes": page_hashes},
        },
        "argsHash": approval_args_hash,
        "idempotencyKey": "qualification-image-apply",
        "risk": "write",
        "reason": "Apply the reviewed Interview Submission",
        "agentLineage": ["run_image"],
    }
    driver.responses.extend(
        [
            ("rpc", "session/create", {"session": {"sessionId": "ses_image"}, "created": True}),
            *(("attachment/upload", None, _artifact(index, page)) for index, page in enumerate(pages, 1)),
            (
                "rpc",
                "turn/start",
                {
                    "sessionId": "ses_image",
                    "turnId": "turn_image",
                    "runId": "run_image",
                    "accepted": True,
                    "duplicate": False,
                },
            ),
            (
                "rpc",
                "approval/resolve",
                {
                    "approvalId": approval_id,
                    "status": "approved",
                    "runId": "run_image",
                    "resumed": True,
                },
            ),
            ("review/resolve", None, {"resolved": True}),
        ]
    )
    driver.events.extend(
        [
            {"event": "runtime.event", "value": {"eventId": "evt_img_1", "runId": "run_image", "type": "turn.started"}},
            {
                "event": "runtime.event",
                "value": {
                    "eventId": "evt_img_approval_required",
                    "runId": "run_image",
                    "type": "approval.required",
                    "payload": {
                        "approval": {
                            "approvalId": approval_id,
                            "status": "pending",
                            "toolCall": approval_tool_call,
                            "workspaceId": "ws_qualification",
                            "runId": "run_image",
                            "expiresAt": "2026-07-19T00:05:00Z",
                            "includeDescendants": False,
                        },
                        "explanation": "Review and apply the Interview Submission",
                        "diffArtifactIds": [],
                    },
                },
            },
            {
                "event": "runtime.event",
                "value": {
                    "eventId": "evt_img_approval_resolved",
                    "runId": "run_image",
                    "type": "approval.resolved",
                    "payload": {
                        "approvalId": approval_id,
                        "decision": "allow_once",
                        "scope": "once",
                        "resolvedAt": "2026-07-19T00:00:01Z",
                        "resolvedBy": "user",
                        "status": "approved",
                        "resolverId": "qualification",
                        "includeDescendants": False,
                        "reason": None,
                    },
                },
            },
            {"event": "review.proposed", "reviewId": "review_1", "proposal": proposal},
            {
                "event": "runtime.event",
                "value": {"eventId": "evt_img_2", "runId": "run_image", "type": "turn.completed"},
            },
        ]
    )
    session = BuiltProductQualificationSession(driver, {"vaultRoot": "C:/sealed/Vault"})
    session.prepare_live_model(
        proxy_url="http://127.0.0.1:7896",
        model="gpt-5.5",
        require_image=True,
    )

    result = session.run_interview_submission(
        tuple(InterviewImagePage(index, f"page-{index}.png", "image/png", page) for index, page in enumerate(pages, 1)),
        "请将这三页虚构面经作为一个 Interview Submission 原子入库。",
        timeout=5,
    )

    assert result.run.run_id == "run_image"
    assert result.ordered_image_content_hashes == tuple(page_hashes)
    assert result.approval.approval_id == approval_id
    assert result.approval.tool_call_id == approval_tool_call_id
    assert result.approval.args_hash == approval_args_hash
    assert result.review.review_id == "review_1"
    assert result.review.paths == tuple(proposal["paths"])
    approval_resolve = next(
        request for request in driver.requests if request[0] == "rpc" and request[1].get("method") == "approval/resolve"
    )
    assert approval_resolve == (
        "rpc",
        {
            "method": "approval/resolve",
            "params": {
                "approvalId": approval_id,
                "decision": "allow_once",
                "scope": "once",
                "expectedArgsHash": approval_args_hash,
                "includeDescendants": False,
                "comment": "Sealed qualification: approve the exact bound Interview Submission once.",
            },
        },
    )
    resolve = driver.requests[-1]
    assert resolve == (
        "review/resolve",
        {"reviewId": "review_1", "decision": "accept", "reviewHash": proposal["reviewHash"]},
    )
    turn_input = next(
        request[1]["params"]["input"]
        for request in driver.requests
        if request[0] == "rpc" and request[1].get("method") == "turn/start"
    )
    assert [block["type"] for block in turn_input] == ["text", "image", "image", "image"]
    assert turn_input[1:] == [_artifact(index, page) for index, page in enumerate(pages, 1)]
    assert not driver.responses


def test_refuses_review_when_ordered_image_binding_differs() -> None:
    driver = _prepared_driver()
    pages = (b"first-page", b"second-page", b"third-page")
    page_hashes = [f"sha256:{hashlib.sha256(page).hexdigest()}" for page in pages]
    proposal = _review(list(reversed(page_hashes)))
    driver.responses.extend(
        [
            ("rpc", "session/create", {"session": {"sessionId": "ses_image"}, "created": True}),
            *(("attachment/upload", None, _artifact(index, page)) for index, page in enumerate(pages, 1)),
            (
                "rpc",
                "turn/start",
                {
                    "sessionId": "ses_image",
                    "turnId": "turn_image",
                    "runId": "run_image",
                    "accepted": True,
                    "duplicate": False,
                },
            ),
        ]
    )
    driver.events.extend([{"event": "review.proposed", "reviewId": "review_1", "proposal": proposal}])
    session = BuiltProductQualificationSession(driver, {"vaultRoot": "C:/sealed/Vault"})
    session.prepare_live_model(
        proxy_url="http://127.0.0.1:7896",
        model="gpt-5.5",
        require_image=True,
    )

    with pytest.raises(BuiltProductQualificationError, match="ordered image binding"):
        session.run_interview_submission(
            tuple(
                InterviewImagePage(index, f"page-{index}.png", "image/png", page) for index, page in enumerate(pages, 1)
            ),
            "请原子入库。",
            timeout=5,
        )

    assert all(command != "review/resolve" for command, _ in driver.requests)


def test_refuses_to_approve_an_unrelated_image_run_write() -> None:
    driver = _prepared_driver()
    pages = (b"first-page", b"second-page", b"third-page")
    page_hashes = [f"sha256:{hashlib.sha256(page).hexdigest()}" for page in pages]
    driver.responses.extend(
        [
            ("rpc", "session/create", {"session": {"sessionId": "ses_image"}, "created": True}),
            *(("attachment/upload", None, _artifact(index, page)) for index, page in enumerate(pages, 1)),
            (
                "rpc",
                "turn/start",
                {
                    "sessionId": "ses_image",
                    "turnId": "turn_image",
                    "runId": "run_image",
                    "accepted": True,
                    "duplicate": False,
                },
            ),
        ]
    )
    driver.events.append(
        {
            "event": "runtime.event",
            "value": {
                "eventId": "evt_wrong_approval",
                "runId": "run_image",
                "type": "approval.required",
                "payload": {
                    "approval": {
                        "approvalId": "apr_01J00000000000000000000000",
                        "status": "pending",
                        "toolCall": {
                            "toolCallId": "call_01J00000000000000000000000",
                            "name": "shell.exec",
                            "version": "1",
                            "arguments": {
                                "changeKind": "interview_submission",
                                "interviewSubmission": {"orderedImageContentHashes": page_hashes},
                            },
                            "argsHash": "sha256:" + "e" * 64,
                            "idempotencyKey": "unrelated-write",
                            "risk": "write",
                            "reason": "Unrelated write",
                            "agentLineage": ["run_image"],
                        },
                        "workspaceId": "ws_qualification",
                        "runId": "run_image",
                        "expiresAt": "2026-07-19T00:05:00Z",
                        "includeDescendants": False,
                    },
                    "explanation": "Unrelated write",
                    "diffArtifactIds": [],
                },
            },
        }
    )
    session = BuiltProductQualificationSession(driver, {"vaultRoot": "C:/sealed/Vault"})
    session.prepare_live_model(
        proxy_url="http://127.0.0.1:7896",
        model="gpt-5.5",
        require_image=True,
    )

    with pytest.raises(BuiltProductQualificationError, match="not the exact bound root write"):
        session.run_interview_submission(
            tuple(
                InterviewImagePage(index, f"page-{index}.png", "image/png", page) for index, page in enumerate(pages, 1)
            ),
            "请原子入库。",
            timeout=5,
        )

    assert all(command != "rpc" or params.get("method") != "approval/resolve" for command, params in driver.requests)


def test_restarts_and_replays_primary_run_without_repeated_review() -> None:
    driver = _prepared_driver()
    session = BuiltProductQualificationSession(driver, {"vaultRoot": "C:/sealed/Vault"})
    session.prepare_live_model(
        proxy_url="http://127.0.0.1:7896",
        model="gpt-5.5",
        require_image=True,
    )
    original_events = (
        {"eventId": "evt_img_1", "runId": "run_image", "sequence": 1, "type": "turn.started"},
        {"eventId": "evt_img_2", "runId": "run_image", "sequence": 2, "type": "turn.completed"},
    )
    driver.responses.extend(
        [
            ("product/stop", None, {"stopped": True, "workerPid": 202}),
            ("product/start", None, {"identity": {"workerPid": 303}}),
            ("events/replay", None, {"lastSequence": 2}),
        ]
    )
    driver.events.extend([{"event": "runtime.event", "value": event} for event in original_events])

    replay = session.restart_and_replay("run_image", timeout=5)

    assert replay.worker_pids == (202, 303)
    assert replay.events == original_events
    assert driver.requests[-1] == (
        "events/replay",
        {"runId": "run_image", "afterSequence": 0, "limit": 10_000},
    )
    assert not driver.responses


def test_duplicate_source_completes_without_another_write_review() -> None:
    driver = _prepared_driver()
    pages = (b"first-page", b"second-page", b"third-page")
    driver.responses.extend(
        [
            ("rpc", "session/create", {"session": {"sessionId": "ses_duplicate"}, "created": True}),
            *(("attachment/upload", None, _artifact(index, page)) for index, page in enumerate(pages, 1)),
            (
                "rpc",
                "turn/start",
                {
                    "sessionId": "ses_duplicate",
                    "turnId": "turn_duplicate",
                    "runId": "run_duplicate",
                    "accepted": True,
                    "duplicate": False,
                },
            ),
        ]
    )
    driver.events.extend(
        [
            {
                "event": "runtime.event",
                "value": {"eventId": "evt_dup_1", "runId": "run_duplicate", "type": "tool.started"},
            },
            {
                "event": "runtime.event",
                "value": {"eventId": "evt_dup_2", "runId": "run_duplicate", "type": "turn.completed"},
            },
        ]
    )
    session = BuiltProductQualificationSession(driver, {"vaultRoot": "C:/sealed/Vault"})
    session.prepare_live_model(
        proxy_url="http://127.0.0.1:7896",
        model="gpt-5.5",
        require_image=True,
    )

    duplicate = session.run_duplicate_source(
        tuple(InterviewImagePage(index, f"page-{index}.png", "image/png", page) for index, page in enumerate(pages, 1)),
        "这是与上一 Run 完全相同的来源；请按 Catalog 身份去重，不要重复写入或增加频次。",
        timeout=5,
    )

    assert duplicate.run_id == "run_duplicate"
    assert duplicate.events[-1]["type"] == "turn.completed"
    assert all(command != "review/resolve" for command, _ in driver.requests)
    assert not driver.responses


def test_duplicate_source_fails_closed_if_it_proposes_another_review() -> None:
    driver = _prepared_driver()
    pages = (b"first-page", b"second-page", b"third-page")
    driver.responses.extend(
        [
            ("rpc", "session/create", {"session": {"sessionId": "ses_duplicate"}, "created": True}),
            *(("attachment/upload", None, _artifact(index, page)) for index, page in enumerate(pages, 1)),
            (
                "rpc",
                "turn/start",
                {
                    "sessionId": "ses_duplicate",
                    "turnId": "turn_duplicate",
                    "runId": "run_duplicate",
                    "accepted": True,
                    "duplicate": False,
                },
            ),
        ]
    )
    driver.events.append({"event": "review.proposed", "reviewId": "review_again", "proposal": {}})
    session = BuiltProductQualificationSession(driver, {"vaultRoot": "C:/sealed/Vault"})
    session.prepare_live_model(
        proxy_url="http://127.0.0.1:7896",
        model="gpt-5.5",
        require_image=True,
    )

    with pytest.raises(BuiltProductQualificationError, match="duplicate source proposed another write review"):
        session.run_duplicate_source(
            tuple(
                InterviewImagePage(index, f"page-{index}.png", "image/png", page) for index, page in enumerate(pages, 1)
            ),
            "请去重。",
            timeout=5,
        )


def test_hosted_search_is_attributed_to_selected_model_and_provider_request() -> None:
    driver = _prepared_driver()
    driver.responses.extend(
        [
            ("rpc", "session/create", {"session": {"sessionId": "ses_search"}, "created": True}),
            (
                "rpc",
                "turn/start",
                {
                    "sessionId": "ses_search",
                    "turnId": "turn_search",
                    "runId": "run_search",
                    "accepted": True,
                    "duplicate": False,
                },
            ),
        ]
    )
    citation = {
        "type": "hostedWeb",
        "url": "https://example.com/interview-trends",
        "title": "Interview trends",
        "providerId": "codex_subscription",
        "model": "gpt-5.5",
        "modelRequestId": "resp_search_1",
        "freshness": "fresh",
    }
    driver.events.extend(
        [
            {
                "event": "runtime.event",
                "value": {
                    "eventId": "evt_search_1",
                    "runId": "run_search",
                    "type": "references.updated",
                    "payload": {"references": [citation], "replace": False},
                },
            },
            {
                "event": "runtime.event",
                "value": {"eventId": "evt_search_2", "runId": "run_search", "type": "turn.completed"},
            },
        ]
    )
    session = BuiltProductQualificationSession(driver, {"vaultRoot": "C:/sealed/Vault"})
    session.prepare_live_model(
        proxy_url="http://127.0.0.1:7896",
        model="gpt-5.5",
        require_image=True,
    )

    result = session.run_hosted_search(
        "请使用 Hosted Web Search 查找一条公开的面试趋势，并引用来源。",
        timeout=5,
    )

    assert result.supported is True
    assert len(result.citations) == 1
    assert result.citations[0].url == citation["url"]
    assert result.citations[0].provider_id == "codex_subscription"
    assert result.citations[0].model == "gpt-5.5"
    assert result.citations[0].model_request_id == "resp_search_1"
    assert not driver.responses


def test_supported_hosted_search_fails_without_provider_attributed_citation() -> None:
    driver = _prepared_driver()
    driver.responses.extend(
        [
            ("rpc", "session/create", {"session": {"sessionId": "ses_search"}, "created": True}),
            (
                "rpc",
                "turn/start",
                {
                    "sessionId": "ses_search",
                    "turnId": "turn_search",
                    "runId": "run_search",
                    "accepted": True,
                    "duplicate": False,
                },
            ),
        ]
    )
    driver.events.append(
        {
            "event": "runtime.event",
            "value": {"eventId": "evt_search_2", "runId": "run_search", "type": "turn.completed"},
        }
    )
    session = BuiltProductQualificationSession(driver, {"vaultRoot": "C:/sealed/Vault"})
    session.prepare_live_model(
        proxy_url="http://127.0.0.1:7896",
        model="gpt-5.5",
        require_image=True,
    )

    with pytest.raises(BuiltProductQualificationError, match="provider-attributed citation"):
        session.run_hosted_search("请搜索并引用来源。", timeout=5)


def test_research_browser_adapter_has_independent_read_only_attribution() -> None:
    digest = "sha256:" + "a" * 64
    driver = _ScriptedDriver(
        [
            (
                "research-browser/qualify",
                None,
                {
                    "actions": ["open", "read", "enumerate", "follow", "back"],
                    "adapter": "ResearchBrowserAdapter",
                    "networkRequests": 0,
                    "pagePort": "qualification-scripted",
                    "readSource": {
                        "type": "web",
                        "url": "https://example.com/offeragent/qualification",
                        "contentHash": digest,
                        "title": "OfferAgent qualification research",
                        "freshness": "fresh",
                    },
                    "sideEffects": 0,
                    "untrusted": True,
                },
            )
        ]
    )
    session = BuiltProductQualificationSession(driver, {"vaultRoot": "C:/sealed/Vault"})

    evidence = session.qualify_research_browser()

    assert evidence.adapter == "ResearchBrowserAdapter"
    assert evidence.page_port == "qualification-scripted"
    assert evidence.actions == ("open", "read", "enumerate", "follow", "back")
    assert evidence.source_url == "https://example.com/offeragent/qualification"
    assert evidence.source_content_hash == digest
    assert evidence.network_requests == 0
    assert evidence.side_effects == 0
    assert evidence.untrusted is True
    assert not driver.responses
