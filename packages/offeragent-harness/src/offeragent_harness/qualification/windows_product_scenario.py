"""Python-owned orchestration for sealed built-product qualification scenarios."""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol, cast
from urllib.parse import urlsplit

from offeragent_harness.qualification.windows_product_driver import QualificationDriverEventTimeout

_PROVIDER_PROTOCOL_REASON = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_BLOCKER_DIAGNOSTIC = re.compile(r"^[a-z][a-z0-9_.:-]{0,127}$")
_TOOL_RESULT_STATUSES = frozenset(
    {
        "succeeded",
        "failed",
        "denied",
        "cancelled",
        "timed_out",
        "conflict",
        "partial",
        "unknown_outcome",
    }
)


class BuiltProductQualificationError(RuntimeError):
    """The sealed product did not produce evidence required by the qualification."""


class QualificationDriverPort(Protocol):
    """Only the bounded control surface exposed by the separately sealed driver."""

    def request(
        self,
        command: str,
        params: dict[str, Any],
        *,
        timeout: float = 60,
    ) -> dict[str, Any]: ...

    def next_event(self, *, timeout: float) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class SelectedModelEvidence:
    model: str
    input_modalities: tuple[str, ...]
    supports_hosted_search: bool
    catalog_revision: str
    account_binding: str
    worker_pids: tuple[int, int]


@dataclass(frozen=True, slots=True)
class CompletedRunEvidence:
    session_id: str
    turn_id: str
    run_id: str
    events: tuple[Mapping[str, Any], ...]


@dataclass(frozen=True, slots=True)
class InterviewImagePage:
    index: int
    file_name: str
    media_type: str
    content: bytes

    def __post_init__(self) -> None:
        if (
            self.index < 1
            or not self.file_name
            or any(marker in self.file_name for marker in ("/", "\\", "\x00", "\r", "\n"))
            or self.media_type not in {"image/png", "image/jpeg", "image/gif", "image/webp"}
            or not self.content
            or len(self.content) > 10 * 1_024 * 1_024
        ):
            raise ValueError("qualification interview image is invalid")

    @property
    def content_hash(self) -> str:
        return f"sha256:{hashlib.sha256(self.content).hexdigest()}"


@dataclass(frozen=True, slots=True)
class ReviewEvidence:
    review_id: str
    review_hash: str
    batch_id: str
    paths: tuple[str, ...]
    after_content_hashes: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class ApprovalEvidence:
    approval_id: str
    tool_call_id: str
    args_hash: str


@dataclass(frozen=True, slots=True)
class InterviewSubmissionEvidence:
    run: CompletedRunEvidence
    ordered_image_content_hashes: tuple[str, ...]
    approval: ApprovalEvidence
    review: ReviewEvidence


@dataclass(frozen=True, slots=True)
class RestartReplayEvidence:
    worker_pids: tuple[int, int]
    events: tuple[Mapping[str, Any], ...]


@dataclass(frozen=True, slots=True)
class HostedCitationEvidence:
    url: str
    title: str
    provider_id: str
    model: str
    model_request_id: str


@dataclass(frozen=True, slots=True)
class HostedSearchEvidence:
    supported: bool
    run: CompletedRunEvidence
    citations: tuple[HostedCitationEvidence, ...]
    attempt_count: int


@dataclass(frozen=True, slots=True)
class ResearchBrowserEvidence:
    adapter: str
    page_port: str
    actions: tuple[str, ...]
    source_url: str
    source_content_hash: str
    network_requests: int
    side_effects: int
    untrusted: bool


class BuiltProductQualificationSession:
    """Drive production adapters without importing repository product composition."""

    def __init__(
        self,
        driver: QualificationDriverPort,
        product_start_params: Mapping[str, Any],
    ) -> None:
        if not product_start_params:
            raise ValueError("product start parameters must not be empty")
        self._driver = driver
        self._product_start_params = dict(product_start_params)
        self._selection: SelectedModelEvidence | None = None
        self._worker_pid: int | None = None
        self._primary_image_hashes: tuple[str, ...] | None = None

    def prepare_live_model(
        self,
        *,
        proxy_url: str,
        model: str,
        require_image: bool,
    ) -> SelectedModelEvidence:
        """Restart for the explicit proxy, then atomically persist one fresh selection."""

        if not proxy_url or not model:
            raise ValueError("live qualification requires an explicit proxy and model")
        hello = self._driver.request("hello", {})
        if hello.get("driverProtocolVersion") != 2 or hello.get("sourceFreeRuntime") is not True:
            raise BuiltProductQualificationError("sealed qualification driver identity is invalid")

        first_pid = self._start_product()
        initial = self._rpc("config/get", {"scope": "workspace"})
        initial_revision = _integer(initial, "revision", "initial configuration")
        proxy_update = self._rpc(
            "config/update",
            {
                "scope": "workspace",
                "expectedRevision": initial_revision,
                "patch": {"model": {"proxy_url": proxy_url}},
            },
        )
        if proxy_update.get("status") != "restart_required":
            raise BuiltProductQualificationError("model proxy update did not require a product restart")
        stopped = self._driver.request("product/stop", {})
        if stopped.get("stopped") is not True or stopped.get("workerPid") != first_pid:
            raise BuiltProductQualificationError("first product worker did not stop cleanly")
        second_pid = self._start_product()
        if first_pid == second_pid:
            raise BuiltProductQualificationError("product restart reused the previous worker process")

        catalog = self._rpc("models/list", {})
        if (
            catalog.get("catalogFreshness") != "fresh"
            or catalog.get("error") is not None
            or not isinstance(catalog.get("models"), list)
            or not catalog["models"]
        ):
            raise BuiltProductQualificationError("live model selection requires a fresh complete catalog")
        catalog_revision = _sha256(catalog, "catalogRevision", "live model catalog")
        account_binding = _sha256(catalog, "accountBinding", "live model catalog")
        matches = [item for item in catalog["models"] if isinstance(item, Mapping) and item.get("model") == model]
        if len(matches) != 1:
            raise BuiltProductQualificationError("selected model is not uniquely present in the live catalog")
        selected = matches[0]
        raw_modalities = selected.get("inputModalities")
        if (
            not isinstance(raw_modalities, list)
            or not raw_modalities
            or any(not isinstance(item, str) or not item for item in raw_modalities)
        ):
            raise BuiltProductQualificationError("selected model modalities are invalid")
        modalities = tuple(raw_modalities)
        if require_image and "image" not in modalities:
            raise BuiltProductQualificationError("selected live model does not declare image input")
        supports_hosted_search = selected.get("supportsHostedSearch")
        if not isinstance(supports_hosted_search, bool):
            raise BuiltProductQualificationError("selected model hosted-search capability is invalid")

        restarted = self._rpc("config/get", {"scope": "workspace"})
        restarted_revision = _integer(restarted, "revision", "restarted configuration")
        selection_patch = {
            "model": {"model": model, "account_binding": account_binding},
            "policy": {
                "read_only": False,
                "workspace_trusted": True,
                "approve_vault_writes": True,
            },
        }
        update = self._rpc(
            "config/update",
            {
                "scope": "workspace",
                "expectedRevision": restarted_revision,
                "patch": selection_patch,
            },
        )
        if update.get("status") != "applied":
            raise BuiltProductQualificationError("live model selection was not applied atomically")
        snapshot = update.get("snapshot")
        if not isinstance(snapshot, Mapping) or snapshot.get("restartPending") is not False:
            raise BuiltProductQualificationError("live model selection left a pending restart")
        values = snapshot.get("values")
        configured_model = values.get("model") if isinstance(values, Mapping) else None
        if (
            not isinstance(configured_model, Mapping)
            or configured_model.get("model") != model
            or configured_model.get("account_binding") != account_binding
        ):
            raise BuiltProductQualificationError("persisted model selection differs from the live catalog binding")

        evidence = SelectedModelEvidence(
            model=model,
            input_modalities=modalities,
            supports_hosted_search=supports_hosted_search,
            catalog_revision=catalog_revision,
            account_binding=account_binding,
            worker_pids=(first_pid, second_pid),
        )
        self._selection = evidence
        self._worker_pid = second_pid
        return evidence

    def run_text_preflight(self, prompt: str, *, timeout: float) -> CompletedRunEvidence:
        """Complete one network-backed text Run before any image materialization."""

        selection = self._require_selection()
        if not prompt or timeout <= 0:
            raise ValueError("text preflight prompt and timeout must be positive")
        created = self._rpc(
            "session/create",
            {"title": "Sealed product text preflight", "clientRequestId": "req_qualification_text"},
        )
        session = created.get("session")
        session_id = _string(session, "sessionId", "text preflight session")
        started = self._rpc(
            "turn/start",
            {
                "sessionId": session_id,
                "turnId": "turn_qualification_text",
                "idempotencyKey": "qualification-text-v1",
                "input": [
                    {
                        "type": "text",
                        "text": prompt,
                        "format": "markdown",
                        "references": [],
                    }
                ],
                "runConfig": {
                    "model": selection.model,
                    "reasoningEffort": "medium",
                    "permissionMode": "read-only",
                },
            },
        )
        if started.get("accepted") is not True or started.get("duplicate") is not False:
            raise BuiltProductQualificationError("text preflight was not accepted as a fresh Run")
        turn_id = _string(started, "turnId", "text preflight")
        run_id = _string(started, "runId", "text preflight")
        events = self._wait_for_terminal(run_id, timeout=timeout)
        return CompletedRunEvidence(session_id, turn_id, run_id, events)

    def run_interview_submission(
        self,
        pages: tuple[InterviewImagePage, ...],
        prompt: str,
        *,
        timeout: float,
    ) -> InterviewSubmissionEvidence:
        """Upload and submit one ordered three-page source, then resolve one bound review."""

        selection = self._require_selection()
        if "image" not in selection.input_modalities:
            raise BuiltProductQualificationError("selected model does not support image input")
        if (
            len(pages) != 3
            or tuple(page.index for page in pages) != (1, 2, 3)
            or len({page.content_hash for page in pages}) != 3
            or not prompt
            or timeout <= 0
        ):
            raise ValueError("interview qualification requires three distinct ordered pages and a prompt")
        created = self._rpc(
            "session/create",
            {"title": "Sealed product Interview Submission", "clientRequestId": "req_qualification_image"},
        )
        session = created.get("session")
        session_id = _string(session, "sessionId", "image qualification session")
        image_blocks = [self._upload_page(session_id, page) for page in pages]
        started = self._rpc(
            "turn/start",
            {
                "sessionId": session_id,
                "turnId": "turn_qualification_image",
                "idempotencyKey": "qualification-image-v1",
                "input": [
                    {"type": "text", "text": prompt, "format": "markdown", "references": []},
                    *image_blocks,
                ],
                "runConfig": {
                    "model": selection.model,
                    "reasoningEffort": "medium",
                    "permissionMode": "trusted-workspace",
                },
            },
        )
        if started.get("accepted") is not True or started.get("duplicate") is not False:
            raise BuiltProductQualificationError("image qualification was not accepted as a fresh Run")
        turn_id = _string(started, "turnId", "image qualification")
        run_id = _string(started, "runId", "image qualification")
        ordered_hashes = tuple(page.content_hash for page in pages)
        events, approval, review = self._wait_for_interview_terminal(run_id, ordered_hashes, timeout=timeout)
        self._primary_image_hashes = ordered_hashes
        return InterviewSubmissionEvidence(
            run=CompletedRunEvidence(session_id, turn_id, run_id, events),
            ordered_image_content_hashes=ordered_hashes,
            approval=approval,
            review=review,
        )

    def restart_and_replay(self, run_id: str, *, timeout: float) -> RestartReplayEvidence:
        """Restart the sealed Worker and replay one completed Run without side effects."""

        self._require_selection()
        previous_pid = self._worker_pid
        if previous_pid is None or not run_id or timeout <= 0:
            raise ValueError("restart replay requires an active product, Run ID, and positive timeout")
        stopped = self._driver.request("product/stop", {})
        if stopped.get("stopped") is not True or stopped.get("workerPid") != previous_pid:
            raise BuiltProductQualificationError("qualification product did not stop before replay")
        restarted_pid = self._start_product()
        if restarted_pid == previous_pid:
            raise BuiltProductQualificationError("qualification replay restart reused the previous worker")
        self._worker_pid = restarted_pid
        replayed = self._driver.request(
            "events/replay",
            {"runId": run_id, "afterSequence": 0, "limit": 10_000},
        )
        last_sequence = _integer(replayed, "lastSequence", "replayed Run", minimum=1)
        deadline = time.monotonic() + timeout
        seen: set[str] = set()
        events: list[Mapping[str, Any]] = []
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise BuiltProductQualificationError("replayed Run did not reach its declared last sequence")
            notification = self._driver.next_event(timeout=remaining)
            notification_type = notification.get("event")
            if notification_type == "review.proposed":
                raise BuiltProductQualificationError("replayed Run repeated a write review")
            _raise_control_failure(notification)
            if notification_type != "runtime.event":
                continue
            event = notification.get("value")
            if not isinstance(event, Mapping) or event.get("runId") != run_id:
                continue
            event_id = _string(event, "eventId", "replayed runtime event")
            sequence = _integer(event, "sequence", "replayed runtime event", minimum=1)
            if event_id in seen:
                continue
            seen.add(event_id)
            events.append(event)
            if sequence == last_sequence:
                if event.get("type") != "turn.completed":
                    raise BuiltProductQualificationError("replayed Run last sequence is not completed")
                sequences = [_integer(item, "sequence", "replayed runtime event", minimum=1) for item in events]
                if sequences != sorted(sequences):
                    raise BuiltProductQualificationError("replayed Run events are not ordered")
                return RestartReplayEvidence((previous_pid, restarted_pid), tuple(events))
            if sequence > last_sequence:
                raise BuiltProductQualificationError("replayed Run exceeded its declared last sequence")

    def run_duplicate_source(
        self,
        pages: tuple[InterviewImagePage, ...],
        prompt: str,
        *,
        timeout: float,
    ) -> CompletedRunEvidence:
        """Run the same source in a fresh Session and forbid another write review."""

        selection = self._require_selection()
        hashes = tuple(page.content_hash for page in pages)
        if (
            len(pages) != 3
            or tuple(page.index for page in pages) != (1, 2, 3)
            or len(set(hashes)) != 3
            or (self._primary_image_hashes is not None and hashes != self._primary_image_hashes)
            or not prompt
            or timeout <= 0
        ):
            raise ValueError("duplicate qualification source must equal the ordered primary three-page source")
        created = self._rpc(
            "session/create",
            {"title": "Sealed product duplicate source", "clientRequestId": "req_qualification_duplicate"},
        )
        session = created.get("session")
        session_id = _string(session, "sessionId", "duplicate qualification session")
        image_blocks = [self._upload_page(session_id, page) for page in pages]
        started = self._rpc(
            "turn/start",
            {
                "sessionId": session_id,
                "turnId": "turn_qualification_duplicate",
                "idempotencyKey": "qualification-duplicate-v1",
                "input": [
                    {"type": "text", "text": prompt, "format": "markdown", "references": []},
                    *image_blocks,
                ],
                "runConfig": {
                    "model": selection.model,
                    "reasoningEffort": "medium",
                    "permissionMode": "trusted-workspace",
                },
            },
        )
        if started.get("accepted") is not True or started.get("duplicate") is not False:
            raise BuiltProductQualificationError("duplicate source was not accepted as a fresh Run")
        turn_id = _string(started, "turnId", "duplicate qualification")
        run_id = _string(started, "runId", "duplicate qualification")
        events = self._wait_for_terminal(
            run_id,
            timeout=timeout,
            review_error="duplicate source proposed another write review",
        )
        return CompletedRunEvidence(session_id, turn_id, run_id, events)

    def run_hosted_search(
        self,
        prompt: str,
        *,
        timeout: float,
        max_attempts: int = 2,
    ) -> HostedSearchEvidence:
        """Attribute provider-hosted citations to the exact persisted catalog selection."""

        selection = self._require_selection()
        if (
            not prompt
            or timeout <= 0
            or isinstance(max_attempts, bool)
            or not isinstance(max_attempts, int)
            or not 1 <= max_attempts <= 3
        ):
            raise ValueError("hosted-search prompt, timeout, and bounded attempt count must be positive")
        attempt_limit = max_attempts if selection.supports_hosted_search else 1
        for attempt in range(1, attempt_limit + 1):
            run = self._run_hosted_search_attempt(prompt, timeout=timeout, attempt=attempt)
            citations = _hosted_citations(run.events, selection.model)
            if citations:
                if not selection.supports_hosted_search:
                    raise BuiltProductQualificationError("unsupported Hosted Web Search emitted a provider citation")
                return HostedSearchEvidence(True, run, citations, attempt)
            if not selection.supports_hosted_search:
                return HostedSearchEvidence(False, run, (), attempt)
        raise BuiltProductQualificationError(
            f"supported Hosted Web Search returned no provider-attributed citation after {attempt_limit} attempts"
        )

    def _run_hosted_search_attempt(
        self,
        prompt: str,
        *,
        timeout: float,
        attempt: int,
    ) -> CompletedRunEvidence:
        selection = self._require_selection()
        suffix = "" if attempt == 1 else f"-{attempt}"
        created = self._rpc(
            "session/create",
            {
                "title": f"Sealed product hosted search attempt {attempt}",
                "clientRequestId": f"req_qualification_search{suffix}",
            },
        )
        session = created.get("session")
        session_id = _string(session, "sessionId", "hosted-search session")
        started = self._rpc(
            "turn/start",
            {
                "sessionId": session_id,
                "turnId": f"turn_qualification_search{suffix}",
                "idempotencyKey": f"qualification-search-v1{suffix}",
                "input": [
                    {"type": "text", "text": prompt, "format": "markdown", "references": []},
                ],
                "runConfig": {
                    "model": selection.model,
                    "reasoningEffort": "medium",
                    "permissionMode": "read-only",
                },
            },
        )
        if started.get("accepted") is not True or started.get("duplicate") is not False:
            raise BuiltProductQualificationError("hosted-search qualification was not accepted as a fresh Run")
        turn_id = _string(started, "turnId", "hosted-search qualification")
        run_id = _string(started, "runId", "hosted-search qualification")
        events = self._wait_for_terminal(run_id, timeout=timeout)
        return CompletedRunEvidence(session_id, turn_id, run_id, events)

    def qualify_research_browser(self) -> ResearchBrowserEvidence:
        """Exercise the production adapter against its declared qualification PagePort."""

        result = self._driver.request("research-browser/qualify", {})
        expected_actions = ("open", "read", "enumerate", "follow", "back")
        actions = result.get("actions")
        if (
            result.get("adapter") != "ResearchBrowserAdapter"
            or result.get("pagePort") != "qualification-scripted"
            or not isinstance(actions, list)
            or tuple(actions) != expected_actions
            or result.get("networkRequests") != 0
            or result.get("sideEffects") != 0
            or result.get("untrusted") is not True
        ):
            raise BuiltProductQualificationError("Research Browser qualification identity is invalid")
        source = result.get("readSource")
        if not isinstance(source, Mapping) or source.get("type") != "web" or source.get("freshness") != "fresh":
            raise BuiltProductQualificationError("Research Browser read source is invalid")
        url = _string(source, "url", "Research Browser read source")
        if not _public_http_url(url):
            raise BuiltProductQualificationError("Research Browser read source is not public HTTP")
        title = _string(source, "title", "Research Browser read source")
        del title
        content_hash = _sha256(source, "contentHash", "Research Browser read source")
        return ResearchBrowserEvidence(
            adapter="ResearchBrowserAdapter",
            page_port="qualification-scripted",
            actions=expected_actions,
            source_url=url,
            source_content_hash=content_hash,
            network_requests=0,
            side_effects=0,
            untrusted=True,
        )

    def _require_selection(self) -> SelectedModelEvidence:
        if self._selection is None:
            raise BuiltProductQualificationError("live model must be prepared before starting a Run")
        return self._selection

    def _start_product(self) -> int:
        result = self._driver.request("product/start", dict(self._product_start_params))
        identity = result.get("identity")
        return _integer(identity, "workerPid", "started product identity", minimum=1)

    def _rpc(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        return self._driver.request("rpc", {"method": method, "params": params})

    def _upload_page(self, session_id: str, page: InterviewImagePage) -> dict[str, Any]:
        alt_text = f"Interview page {page.index} of 3"
        image = self._driver.request(
            "attachment/upload",
            {
                "sessionId": session_id,
                "fileName": page.file_name,
                "mediaType": page.media_type,
                "contentBase64": base64.b64encode(page.content).decode("ascii"),
                "altText": alt_text,
            },
        )
        artifact = image.get("artifact")
        if (
            set(image) != {"type", "artifact", "altText"}
            or image.get("type") != "image"
            or image.get("altText") != alt_text
            or not isinstance(artifact, Mapping)
            or artifact.get("contentHash") != page.content_hash
            or artifact.get("mediaType") != page.media_type
            or artifact.get("sizeBytes") != len(page.content)
            or artifact.get("state") != "complete"
        ):
            raise BuiltProductQualificationError("sealed product returned an invalid committed image attachment")
        _string(artifact, "artifactId", "committed image attachment")
        return image

    def _wait_for_interview_terminal(
        self,
        run_id: str,
        ordered_hashes: tuple[str, ...],
        *,
        timeout: float,
    ) -> tuple[tuple[Mapping[str, Any], ...], ApprovalEvidence, ReviewEvidence]:
        deadline = time.monotonic() + timeout
        seen: set[str] = set()
        events: list[Mapping[str, Any]] = []
        approval: ApprovalEvidence | None = None
        approval_resolved = False
        review: ReviewEvidence | None = None
        last_sequence = 0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise BuiltProductQualificationError(_timeout_failure_message("image Run", events))
            notification = self._next_event_or_replay(run_id, deadline, last_sequence)
            if notification is None:
                continue
            notification_type = notification.get("event")
            if notification_type == "review.proposed":
                if review is not None:
                    raise BuiltProductQualificationError("image qualification proposed more than one review")
                review = self._accept_interview_review(notification, ordered_hashes)
                continue
            _raise_control_failure(notification)
            if notification_type != "runtime.event":
                continue
            event = notification.get("value")
            if not isinstance(event, Mapping) or event.get("runId") != run_id:
                continue
            sequence = event.get("sequence")
            if isinstance(sequence, int) and not isinstance(sequence, bool):
                last_sequence = max(last_sequence, sequence)
            event_id = event.get("eventId")
            event_type = event.get("type")
            if not isinstance(event_id, str) or not event_id or not isinstance(event_type, str):
                raise BuiltProductQualificationError("sealed product emitted an invalid runtime event")
            if event_id in seen:
                continue
            seen.add(event_id)
            events.append(event)
            if event_type == "approval.required":
                if approval is not None:
                    raise BuiltProductQualificationError("image qualification requested more than one approval")
                approval = self._approve_interview_write(event, run_id, ordered_hashes)
                continue
            if event_type == "approval.resolved":
                if approval is None or approval_resolved:
                    raise BuiltProductQualificationError("image qualification emitted an unmatched approval resolution")
                _validate_interview_approval_resolution(event, approval)
                approval_resolved = True
                continue
            if event_type == "turn.completed":
                if approval is None or not approval_resolved:
                    raise BuiltProductQualificationError(
                        "image qualification completed without one explicit bound approval"
                    )
                if review is None:
                    raise BuiltProductQualificationError("image qualification completed without an explicit review")
                return tuple(events), approval, review
            if event_type in {"turn.cancelled", "turn.failed", "turn.interrupted"}:
                raise BuiltProductQualificationError(_terminal_failure_message(event, events, label="image Run"))

    def _approve_interview_write(
        self,
        event: Mapping[str, Any],
        run_id: str,
        ordered_hashes: tuple[str, ...],
    ) -> ApprovalEvidence:
        payload = event.get("payload")
        approval = payload.get("approval") if isinstance(payload, Mapping) else None
        if not isinstance(approval, Mapping) or approval.get("status") != "pending":
            raise BuiltProductQualificationError("Interview Submission approval is invalid or not pending")
        approval_id = _string(approval, "approvalId", "Interview Submission approval")
        if approval.get("runId") != run_id or approval.get("includeDescendants") is not False:
            raise BuiltProductQualificationError("Interview Submission approval Run or descendant scope differs")
        tool_call = approval.get("toolCall")
        if not isinstance(tool_call, Mapping):
            raise BuiltProductQualificationError("Interview Submission approval tool call is missing")
        arguments = tool_call.get("arguments")
        submission = arguments.get("interviewSubmission") if isinstance(arguments, Mapping) else None
        lineage = tool_call.get("agentLineage")
        if (
            tool_call.get("name") != "vault.changes.apply"
            or tool_call.get("version") != "1"
            or tool_call.get("risk") != "write"
            or not isinstance(arguments, Mapping)
            or arguments.get("changeKind") != "interview_submission"
            or not isinstance(submission, Mapping)
            or submission.get("orderedImageContentHashes") != list(ordered_hashes)
            or not isinstance(lineage, list)
            or lineage != [run_id]
        ):
            raise BuiltProductQualificationError("Interview Submission approval is not the exact bound root write")
        tool_call_id = _string(tool_call, "toolCallId", "Interview Submission approval tool call")
        args_hash = _sha256(tool_call, "argsHash", "Interview Submission approval tool call")
        resolved = self._rpc(
            "approval/resolve",
            {
                "approvalId": approval_id,
                "decision": "allow_once",
                "scope": "once",
                "expectedArgsHash": args_hash,
                "includeDescendants": False,
                "comment": "Sealed qualification: approve the exact bound Interview Submission once.",
            },
        )
        if resolved != {
            "approvalId": approval_id,
            "status": "approved",
            "runId": run_id,
            "resumed": True,
        }:
            raise BuiltProductQualificationError("Interview Submission approval did not resolve exactly once")
        return ApprovalEvidence(approval_id, tool_call_id, args_hash)

    def _accept_interview_review(
        self,
        notification: Mapping[str, Any],
        ordered_hashes: tuple[str, ...],
    ) -> ReviewEvidence:
        review_id = _string(notification, "reviewId", "Interview Submission review")
        proposal = notification.get("proposal")
        evidence = _validate_interview_review(proposal, review_id, ordered_hashes)
        resolved = self._driver.request(
            "review/resolve",
            {"reviewId": review_id, "decision": "accept", "reviewHash": evidence.review_hash},
        )
        if resolved != {"resolved": True}:
            raise BuiltProductQualificationError("Interview Submission review was not resolved exactly once")
        return evidence

    def _wait_for_terminal(
        self,
        run_id: str,
        *,
        timeout: float,
        review_error: str = "unexpected write review was proposed",
    ) -> tuple[Mapping[str, Any], ...]:
        deadline = time.monotonic() + timeout
        seen: set[str] = set()
        events: list[Mapping[str, Any]] = []
        last_sequence = 0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise BuiltProductQualificationError(_timeout_failure_message("Run", events))
            notification = self._next_event_or_replay(run_id, deadline, last_sequence)
            if notification is None:
                continue
            notification_type = notification.get("event")
            if notification_type == "review.proposed":
                raise BuiltProductQualificationError(review_error)
            _raise_control_failure(notification)
            if notification_type != "runtime.event":
                continue
            event = notification.get("value")
            if not isinstance(event, Mapping) or event.get("runId") != run_id:
                continue
            sequence = event.get("sequence")
            if isinstance(sequence, int) and not isinstance(sequence, bool):
                last_sequence = max(last_sequence, sequence)
            event_id = event.get("eventId")
            event_type = event.get("type")
            if not isinstance(event_id, str) or not event_id or not isinstance(event_type, str):
                raise BuiltProductQualificationError("sealed product emitted an invalid runtime event")
            if event_id in seen:
                continue
            seen.add(event_id)
            events.append(event)
            if event_type == "turn.completed":
                return tuple(events)
            if event_type in {"turn.cancelled", "turn.failed", "turn.interrupted"}:
                raise BuiltProductQualificationError(_terminal_failure_message(event, events, label="Run"))

    def _next_event_or_replay(
        self,
        run_id: str,
        deadline: float,
        after_sequence: int,
    ) -> dict[str, Any] | None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        try:
            return self._driver.next_event(timeout=min(5.0, remaining))
        except QualificationDriverEventTimeout:
            replayed = self._driver.request(
                "events/replay",
                {"runId": run_id, "afterSequence": after_sequence, "limit": 1_000},
            )
            last_sequence = _integer(replayed, "lastSequence", "polled Run replay")
            if last_sequence < after_sequence:
                raise BuiltProductQualificationError("polled Run replay cursor moved backwards") from None
            return None


def _integer(
    value: object,
    key: str,
    label: str,
    *,
    minimum: int = 0,
) -> int:
    if not isinstance(value, Mapping):
        raise BuiltProductQualificationError(f"{label} is not an object")
    result = value.get(key)
    if not isinstance(result, int) or isinstance(result, bool) or result < minimum:
        raise BuiltProductQualificationError(f"{label} {key} is invalid")
    return result


def _string(value: object, key: str, label: str) -> str:
    if not isinstance(value, Mapping):
        raise BuiltProductQualificationError(f"{label} is not an object")
    result = value.get(key)
    if not isinstance(result, str) or not result or "\x00" in result:
        raise BuiltProductQualificationError(f"{label} {key} is invalid")
    return result


def _sha256(value: Mapping[str, Any], key: str, label: str) -> str:
    result = _string(value, key, label)
    if len(result) != 71 or not result.startswith("sha256:"):
        raise BuiltProductQualificationError(f"{label} {key} is invalid")
    try:
        bytes.fromhex(result[7:])
    except ValueError as error:
        raise BuiltProductQualificationError(f"{label} {key} is invalid") from error
    return result


def _hosted_citations(
    events: tuple[Mapping[str, Any], ...],
    selected_model: str,
) -> tuple[HostedCitationEvidence, ...]:
    citations: list[HostedCitationEvidence] = []
    identities: set[tuple[str, str, str, str, str]] = set()
    for event in events:
        if event.get("type") != "references.updated":
            continue
        payload = event.get("payload")
        references = payload.get("references") if isinstance(payload, Mapping) else None
        if not isinstance(references, list):
            raise BuiltProductQualificationError("Hosted Web Search reference event is invalid")
        for reference in references:
            if not isinstance(reference, Mapping) or reference.get("type") != "hostedWeb":
                continue
            url = _string(reference, "url", "Hosted Web Search citation")
            title = _string(reference, "title", "Hosted Web Search citation")
            provider_id = _string(reference, "providerId", "Hosted Web Search citation")
            model = _string(reference, "model", "Hosted Web Search citation")
            request_id = _string(reference, "modelRequestId", "Hosted Web Search citation")
            if model != selected_model or not _public_http_url(url):
                raise BuiltProductQualificationError("Hosted Web Search citation identity is invalid")
            identity = (url, title, provider_id, model, request_id)
            if identity in identities:
                continue
            identities.add(identity)
            citations.append(HostedCitationEvidence(url, title, provider_id, model, request_id))
    return tuple(citations)


def _public_http_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        if (
            parsed.scheme not in {"http", "https"}
            or host is None
            or parsed.username is not None
            or parsed.password is not None
            or host.casefold() == "localhost"
            or host.casefold().endswith(".localhost")
        ):
            return False
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            return True
        return not (
            address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_multicast
            or address.is_reserved
            or address.is_unspecified
        )
    except ValueError:
        return False


def _validate_interview_review(
    value: object,
    review_id: str,
    ordered_hashes: tuple[str, ...],
) -> ReviewEvidence:
    if not isinstance(value, Mapping):
        raise BuiltProductQualificationError("Interview Submission review is not an object")
    proposal = dict(value)
    if (
        proposal.get("version") != 1
        or proposal.get("changeKind") != "interview_submission"
        or proposal.get("controlFiles") is not False
        or proposal.get("memoryDelete") is not False
    ):
        raise BuiltProductQualificationError("Interview Submission review classification is invalid")
    batch_id = _string(proposal, "batchId", "Interview Submission review")
    _sha256(proposal, "argsHash", "Interview Submission review")
    review_hash = _sha256(proposal, "reviewHash", "Interview Submission review")
    submission = proposal.get("interviewSubmission")
    if not isinstance(submission, Mapping):
        raise BuiltProductQualificationError("Interview Submission receipt is missing from the review")
    _sha256(submission, "sourceFingerprint", "Interview Submission receipt")
    if submission.get("orderedImageContentHashes") != list(ordered_hashes):
        raise BuiltProductQualificationError("Interview Submission review ordered image binding differs")
    if not isinstance(submission.get("canonicalUrls"), list) or not isinstance(submission.get("reviewItems"), list):
        raise BuiltProductQualificationError("Interview Submission receipt evidence is invalid")

    paths = proposal.get("paths")
    categorized = proposal.get("categorizedTargets")
    targets = proposal.get("reviewTargets")
    bindings = proposal.get("sourceBindings")
    if (
        not isinstance(paths, list)
        or not paths
        or len(paths) != len(set(paths))
        or any(not isinstance(path, str) or not path for path in paths)
        or not isinstance(categorized, list)
        or not isinstance(targets, list)
        or not isinstance(bindings, list)
    ):
        raise BuiltProductQualificationError("Interview Submission review targets or sources are invalid")
    if [item.get("path") for item in categorized if isinstance(item, Mapping)] != paths:
        raise BuiltProductQualificationError("Interview Submission categorized targets differ from reviewed paths")
    if [item.get("path") for item in targets if isinstance(item, Mapping)] != paths:
        raise BuiltProductQualificationError("Interview Submission review targets differ from reviewed paths")
    if len(categorized) != len(paths) or len(targets) != len(paths):
        raise BuiltProductQualificationError("Interview Submission review target count is invalid")
    after_content_hashes: list[tuple[str, str]] = []
    for categorized_target, review_target in zip(categorized, targets, strict=True):
        if not isinstance(categorized_target, Mapping) or not isinstance(review_target, Mapping):
            raise BuiltProductQualificationError("Interview Submission review target is invalid")
        before = review_target.get("beforeContent")
        after = review_target.get("afterContent")
        if before is not None and not isinstance(before, str):
            raise BuiltProductQualificationError("Interview Submission before content is invalid")
        if after is not None and not isinstance(after, str):
            raise BuiltProductQualificationError("Interview Submission after content is invalid")
        before_hash = "absent" if before is None else f"sha256:{hashlib.sha256(before.encode()).hexdigest()}"
        after_hash = "absent" if after is None else f"sha256:{hashlib.sha256(after.encode()).hexdigest()}"
        if (
            review_target.get("beforeContentHash") != before_hash
            or review_target.get("afterContentHash") != after_hash
            or categorized_target.get("expectedContentHash") != before_hash
            or categorized_target.get("expectedModifiedVersion") != review_target.get("beforeModifiedVersion")
        ):
            raise BuiltProductQualificationError("Interview Submission review content identity is invalid")
        after_content_hashes.append((cast(str, review_target["path"]), after_hash))
    binding_paths: set[str] = set()
    for binding in bindings:
        if not isinstance(binding, Mapping):
            raise BuiltProductQualificationError("Interview Submission source binding is invalid")
        path = _string(binding, "path", "Interview Submission source binding")
        _string(binding, "expectedModifiedVersion", "Interview Submission source binding")
        _sha256(binding, "expectedContentHash", "Interview Submission source binding")
        if path in binding_paths:
            raise BuiltProductQualificationError("Interview Submission source bindings are duplicated")
        binding_paths.add(path)

    serialized = json.dumps(proposal, ensure_ascii=False, separators=(",", ":"))
    if "data:image" in serialized or "iVBOR" in serialized:
        raise BuiltProductQualificationError("Interview Submission review contains raw image bytes")
    reviewed = dict(proposal)
    reviewed.pop("reviewHash", None)
    expected_hash = (
        f"sha256:{hashlib.sha256(json.dumps(reviewed, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()}"
    )
    if review_hash != expected_hash:
        raise BuiltProductQualificationError("Interview Submission review hash is invalid")
    return ReviewEvidence(review_id, review_hash, batch_id, tuple(paths), tuple(after_content_hashes))


def _validate_interview_approval_resolution(
    event: Mapping[str, Any],
    approval: ApprovalEvidence,
) -> None:
    payload = event.get("payload")
    if (
        not isinstance(payload, Mapping)
        or payload.get("approvalId") != approval.approval_id
        or payload.get("decision") != "allow_once"
        or payload.get("scope") != "once"
        or payload.get("status") != "approved"
        or payload.get("resolvedBy") != "user"
        or payload.get("includeDescendants") is not False
    ):
        raise BuiltProductQualificationError("Interview Submission approval resolution identity differs")


def _raise_control_failure(notification: Mapping[str, Any]) -> None:
    event = notification.get("event")
    if event in {"adapter.error", "product.disconnected"}:
        message = notification.get("error")
        diagnostic = f"sealed product control plane emitted {event}"
        if isinstance(message, str) and message:
            diagnostic = f"{diagnostic} message={_diagnostic_text(message)}"
        raise BuiltProductQualificationError(diagnostic)


def _event_types(events: list[Mapping[str, Any]]) -> str:
    values = [str(event.get("type", "invalid")) for event in events[-16:]]
    return "[" + ",".join(values) + "]"


def _terminal_failure_message(
    terminal: Mapping[str, Any],
    events: list[Mapping[str, Any]],
    *,
    label: str,
) -> str:
    event_type = terminal.get("type")
    payload = terminal.get("payload")
    error = payload.get("error") if isinstance(payload, Mapping) else None
    fields = [f"sealed product {label} terminated with {event_type}"]
    if isinstance(error, Mapping):
        code = error.get("code")
        retryable = error.get("retryable")
        message = error.get("userVisibleMessage")
        if isinstance(code, str) and code:
            fields.append(f"code={_diagnostic_text(code)}")
        if isinstance(retryable, bool):
            fields.append(f"retryable={str(retryable).lower()}")
        if isinstance(message, str) and message:
            fields.append(f"message={_diagnostic_text(message)}")
        details = error.get("details")
        protocol_reason = details.get("providerProtocolReason") if isinstance(details, Mapping) else None
        if isinstance(protocol_reason, str) and _PROVIDER_PROTOCOL_REASON.fullmatch(protocol_reason):
            fields.append(f"protocolReason={protocol_reason}")
    elif isinstance(payload, Mapping):
        code = payload.get("code")
        reason = payload.get("reason")
        if isinstance(code, str) and code:
            fields.append(f"code={_diagnostic_text(code)}")
        if isinstance(reason, str) and reason:
            fields.append(f"message={_diagnostic_text(reason)}")
    if isinstance(payload, Mapping):
        usage = payload.get("usage")
        if isinstance(usage, Mapping):
            for key in ("inputTokens", "modelCalls", "toolCalls"):
                value = usage.get(key)
                if type(value) is int and value >= 0:
                    fields.append(f"{key}={value}")
    tool_trace = _tool_trace(events)
    if tool_trace:
        fields.append(f"toolTrace={'>'.join(tool_trace)}")
    last_tool = _last_tool_name(events)
    if last_tool is not None:
        fields.append(f"lastTool={last_tool}")
    tool_results = _tool_result_trace(events)
    if tool_results:
        fields.append(f"toolResultTrace={'>'.join(tool_results)}")
    tool_failure_message = _last_tool_failure_message(events)
    if tool_failure_message is not None:
        fields.append(f"toolFailureMessage={tool_failure_message}")
    blocker_trace = _blocker_trace(events)
    if blocker_trace:
        fields.append(f"blockerTrace={'>'.join(blocker_trace)}")
    return " ".join(fields)


def _timeout_failure_message(label: str, events: list[Mapping[str, Any]]) -> str:
    fields = [f"sealed product {label} timed out after events {_event_types(events)}"]
    tool_results = _tool_result_trace(events)
    if tool_results:
        fields.append(f"toolResultTrace={'>'.join(tool_results)}")
    tool_failure_message = _last_tool_failure_message(events)
    if tool_failure_message is not None:
        fields.append(f"toolFailureMessage={tool_failure_message}")
    blocker_trace = _blocker_trace(events)
    if blocker_trace:
        fields.append(f"blockerTrace={'>'.join(blocker_trace)}")
    return " ".join(fields)


def _tool_trace(events: list[Mapping[str, Any]]) -> tuple[str, ...]:
    names: list[str] = []
    for event in events:
        if event.get("type") != "tool.calls.accepted":
            continue
        payload = event.get("payload")
        calls = payload.get("calls") if isinstance(payload, Mapping) else None
        if not isinstance(calls, list):
            continue
        for call in calls:
            name = call.get("name") if isinstance(call, Mapping) else None
            if isinstance(name, str) and name:
                names.append(_diagnostic_text(name))
                if len(names) == 16:
                    return tuple(names)
    return tuple(names)


def _last_tool_name(events: list[Mapping[str, Any]]) -> str | None:
    for event in reversed(events):
        payload = event.get("payload")
        if not isinstance(payload, Mapping):
            continue
        call = payload.get("call")
        if isinstance(call, Mapping):
            name = call.get("name")
            if isinstance(name, str) and name:
                return _diagnostic_text(name)
        calls = payload.get("calls")
        if isinstance(calls, list):
            for candidate in reversed(calls):
                if not isinstance(candidate, Mapping):
                    continue
                name = candidate.get("name")
                if isinstance(name, str) and name:
                    return _diagnostic_text(name)
    return None


def _blocker_trace(events: list[Mapping[str, Any]]) -> tuple[str, ...]:
    blockers: list[str] = []
    for event in events:
        if event.get("type") != "run.continuation_required":
            continue
        payload = event.get("payload")
        values = payload.get("blockers") if isinstance(payload, Mapping) else None
        if not isinstance(values, list):
            continue
        blockers.extend(value for value in values if isinstance(value, str) and _BLOCKER_DIAGNOSTIC.fullmatch(value))
    return tuple(blockers[-16:])


def _tool_result_trace(events: list[Mapping[str, Any]]) -> tuple[str, ...]:
    call_names: dict[str, str] = {}
    results: list[str] = []
    for event in events:
        payload = event.get("payload")
        if not isinstance(payload, Mapping):
            continue
        if event.get("type") == "tool.calls.accepted":
            calls = payload.get("calls")
            if not isinstance(calls, list):
                continue
            for call in calls:
                call_id = call.get("toolCallId") if isinstance(call, Mapping) else None
                name = call.get("name") if isinstance(call, Mapping) else None
                if (
                    isinstance(call_id, str)
                    and call_id
                    and isinstance(name, str)
                    and _BLOCKER_DIAGNOSTIC.fullmatch(name)
                ):
                    call_names.setdefault(call_id, name)
            continue
        if event.get("type") not in {"tool.completed", "tool.failed"}:
            continue
        result = payload.get("result")
        call_id = result.get("toolCallId") if isinstance(result, Mapping) else None
        status = result.get("status") if isinstance(result, Mapping) else None
        name = call_names.get(call_id) if isinstance(call_id, str) else None
        if name is not None and isinstance(status, str) and status in _TOOL_RESULT_STATUSES:
            diagnostic = f"{name}:{status}"
            error = result.get("error") if isinstance(result, Mapping) else None
            details = error.get("details") if isinstance(error, Mapping) else None
            tool_error_code = details.get("toolErrorCode") if isinstance(details, Mapping) else None
            if isinstance(tool_error_code, str) and _BLOCKER_DIAGNOSTIC.fullmatch(tool_error_code):
                diagnostic = f"{diagnostic}:{tool_error_code}"
            results.append(diagnostic)
    return tuple(results[-16:])


def _last_tool_failure_message(events: list[Mapping[str, Any]]) -> str | None:
    call_names: dict[str, str] = {}
    failure: str | None = None
    for event in events:
        payload = event.get("payload")
        if not isinstance(payload, Mapping):
            continue
        if event.get("type") == "tool.calls.accepted":
            calls = payload.get("calls")
            if not isinstance(calls, list):
                continue
            for call in calls:
                call_id = call.get("toolCallId") if isinstance(call, Mapping) else None
                name = call.get("name") if isinstance(call, Mapping) else None
                if (
                    isinstance(call_id, str)
                    and call_id
                    and isinstance(name, str)
                    and _BLOCKER_DIAGNOSTIC.fullmatch(name)
                ):
                    call_names.setdefault(call_id, name)
            continue
        if event.get("type") != "tool.failed":
            continue
        result = payload.get("result")
        call_id = result.get("toolCallId") if isinstance(result, Mapping) else None
        error = result.get("error") if isinstance(result, Mapping) else None
        message = error.get("userVisibleMessage") if isinstance(error, Mapping) else None
        name = call_names.get(call_id) if isinstance(call_id, str) else None
        if name is not None and isinstance(message, str) and message:
            failure = f"{name}:{json.dumps(_diagnostic_text(message), ensure_ascii=False)}"
    return failure


def _diagnostic_text(value: str) -> str:
    return " ".join(value.replace("\x00", "").split())[:512]
