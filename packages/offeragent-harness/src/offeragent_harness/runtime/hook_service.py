"""Layered, audited and process-supervised lifecycle Hook runtime."""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import json
from collections.abc import Coroutine, Mapping, Sequence
from dataclasses import replace
from datetime import timedelta
from typing import Any

from offeragent_harness.error_codes import ResourceConflictCause
from offeragent_harness.hooks import (
    HookAuditRecord,
    HookDecision,
    HookDefinition,
    HookEvent,
    HookFailureMode,
    HookImplementation,
    HookInvocation,
    HookLayer,
    HookOutcome,
    HookOutput,
    merge_argument_patch,
    resolve_hook_plan,
)
from offeragent_harness.ports import (
    CancellationToken,
    Clock,
    EntityRevisionConflict,
    EventSink,
    HookHandler,
    HookLayerSource,
    IdGenerator,
    NewEvent,
    ProcessArtifactBudget,
    ProcessOwnerKind,
    ProcessStdinMode,
    ProcessSupervisor,
    StoredEvent,
    SupervisedProcessRequest,
    UnitOfWorkFactory,
)
from offeragent_harness.tools.canonical import canonical_json_bytes, canonical_json_sha256

_RECEIPTS = "hook_invocation_receipts"
_AUDITS = "hook_audits"
_RECEIPT_SCHEMA = 1
_SAFE_ENVIRONMENT = frozenset({"LANG", "LC_ALL", "TZ", "TEMP", "TMP", "SystemRoot"})
_FAIL_CLOSED_EVENTS = frozenset(
    {
        HookEvent.SESSION_START,
        HookEvent.PRE_TOOL_USE,
        HookEvent.APPROVAL_REQUIRED,
    }
)
_active_chains: contextvars.ContextVar[frozenset[str]] = contextvars.ContextVar(
    "offeragent_hook_active_chains",
    default=frozenset(),
)


class HookServiceError(RuntimeError):
    pass


class HookConfigurationError(HookServiceError):
    pass


class HookInvocationConflict(HookServiceError, ResourceConflictCause):
    pass


class HookExecutionError(HookServiceError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class StaticHookLayerSource:
    """Immutable source suitable for a captured Run configuration snapshot."""

    def __init__(self, layers: Sequence[HookLayer]) -> None:
        self._layers = tuple(layers)

    async def layers_for(self, invocation: HookInvocation) -> Sequence[HookLayer]:
        del invocation
        return self._layers


class HookHandlerRegistry:
    def __init__(self, handlers: Mapping[str, HookHandler] | None = None) -> None:
        self._handlers = dict(handlers or {})
        if any(not key for key in self._handlers):
            raise ValueError("Hook handler registry IDs must not be empty")

    def resolve(self, handler_id: str) -> HookHandler:
        try:
            return self._handlers[handler_id]
        except KeyError as error:
            raise HookConfigurationError("hook_handler_unavailable") from error


class HookService:
    """Run one idempotent Hook chain and persist only redacted audit facts."""

    def __init__(
        self,
        *,
        layers: HookLayerSource,
        handlers: HookHandlerRegistry,
        process_supervisor: ProcessSupervisor,
        unit_of_work: UnitOfWorkFactory,
        event_sink: EventSink,
        clock: Clock,
        ids: IdGenerator,
        environment: Mapping[str, str] | None = None,
        environment_allowlist: frozenset[str] = _SAFE_ENVIRONMENT,
        artifact_budget: ProcessArtifactBudget | None = None,
    ) -> None:
        if not environment_allowlist <= _SAFE_ENVIRONMENT:
            raise ValueError("Hook environment allowlist contains a sensitive or unsupported variable")
        self._layers = layers
        self._handlers = handlers
        self._process_supervisor = process_supervisor
        self._unit_of_work = unit_of_work
        self._event_sink = event_sink
        self._clock = clock
        self._ids = ids
        self._environment_allowlist = environment_allowlist
        self._environment = {
            key: value
            for key, value in dict(environment or {}).items()
            if key in environment_allowlist and isinstance(value, str)
        }
        self._artifact_budget = artifact_budget
        self._locks: dict[str, asyncio.Lock] = {}
        self._locks_guard = asyncio.Lock()
        self._completed_cache: dict[str, tuple[str, HookOutcome, tuple[StoredEvent, ...]]] = {}
        self.delivery_failures: list[str] = []

    async def invoke(self, invocation: HookInvocation, cancellation: CancellationToken) -> HookOutcome:
        cancellation.checkpoint()
        lock = await self._lock_for(invocation.invocation_id)
        async with lock:
            cached = self._completed_cache.get(invocation.invocation_id)
            if cached is not None:
                request_hash, outcome, events = cached
                if request_hash != invocation.request_hash:
                    raise HookInvocationConflict("Hook invocation ID is bound to another request")
                await self._deliver(events)
                return replace(outcome, replayed=True)

            replay = await self._claim_or_replay(invocation)
            if replay is not None:
                outcome, events = replay
                await self._deliver(events)
                return outcome

            deferred: BaseException | None = None
            chain_token: contextvars.Token[frozenset[str]] | None = None
            try:
                active = _active_chains.get()
                if invocation.chain_id in active:
                    outcome = self._failure_outcome(invocation, "hook_recursion_blocked")
                else:
                    chain_token = _active_chains.set(active | {invocation.chain_id})
                    outcome = await self._execute_chain(invocation, cancellation)
            except BaseException as error:
                code = error.code if isinstance(error, HookExecutionError) else f"hook_{type(error).__name__}"
                outcome = self._failure_outcome(invocation, code)
                if not isinstance(error, Exception):
                    deferred = error
            finally:
                if chain_token is not None:
                    _active_chains.reset(chain_token)

            stored = await self._complete(invocation, outcome)
            self._completed_cache[invocation.invocation_id] = (invocation.request_hash, outcome, stored)
            await self._deliver(stored)
            if deferred is not None:
                raise deferred
            return outcome

    async def _execute_chain(
        self,
        invocation: HookInvocation,
        cancellation: CancellationToken,
    ) -> HookOutcome:
        try:
            plan = resolve_hook_plan(await self._layers.layers_for(invocation), invocation)
        except Exception as error:
            raise HookExecutionError("hook_layer_resolution_failed") from error
        if plan.managed_denied:
            return HookOutcome(
                HookDecision.DENY,
                ("managed.deny",),
                (),
                (),
                ("hook_managed_event_denied",),
            )

        decision = HookDecision.CONTINUE
        tags: set[str] = set()
        hints: list[str] = []
        applied: list[str] = []
        warnings: list[str] = []
        current_arguments = None if invocation.tool is None else invocation.tool.arguments
        mutated = False
        for definition in plan.hooks:
            cancellation.checkpoint()
            if definition.implementation is HookImplementation.COMMAND and not invocation.context.workspace_trusted:
                warnings.append("hook_command_workspace_untrusted")
                continue
            try:
                output = await self._invoke_one(definition, invocation, cancellation)
                self._validate_output_for_event(invocation.event, output)
            except BaseException as error:
                if not isinstance(error, Exception):
                    raise
                code = error.code if isinstance(error, HookExecutionError) else f"hook_{type(error).__name__}"
                if self._failure_mode(invocation.event) is HookFailureMode.FAIL_CLOSED:
                    decision = HookDecision.DENY
                    warnings.append(code)
                else:
                    warnings.append(code)
                continue
            applied.append(definition.hook_id)
            tags.update(output.audit_tags)
            for hint in output.context_hints:
                if hint not in hints:
                    hints.append(hint)
            if output.argument_patch is not None:
                assert current_arguments is not None
                current_arguments = merge_argument_patch(current_arguments, output.argument_patch)
                mutated = True
            decision = _stronger_decision(decision, output.decision)

        mutated_hash = canonical_json_sha256(current_arguments) if mutated and current_arguments is not None else None
        return HookOutcome(
            decision,
            tuple(sorted(tags)),
            tuple(hints),
            tuple(applied),
            tuple(dict.fromkeys(warnings)),
            current_arguments if mutated else None,
            mutated_hash,
        )

    async def _invoke_one(
        self,
        definition: HookDefinition,
        invocation: HookInvocation,
        cancellation: CancellationToken,
    ) -> HookOutput:
        if definition.implementation is HookImplementation.BUILTIN:
            assert definition.handler_id is not None
            handler = self._handlers.resolve(definition.handler_id)
            operation = handler.invoke(definition, invocation, cancellation)
        else:
            operation = self._invoke_command(definition, invocation, cancellation)
        output = await self._with_deadline(operation, definition.timeout_ms, cancellation)
        if len(_output_bytes(output)) > definition.output_limit_bytes:
            raise HookExecutionError("hook_output_limit_exceeded")
        return output

    async def _invoke_command(
        self,
        definition: HookDefinition,
        invocation: HookInvocation,
        cancellation: CancellationToken,
    ) -> HookOutput:
        command = definition.command
        assert command is not None
        body = _command_input(invocation)
        environment = {
            name: self._environment[name]
            for name in sorted(command.allowed_environment & self._environment_allowlist)
            if name in self._environment
        }
        result = await self._process_supervisor.execute(
            SupervisedProcessRequest(
                process_id=self._ids.new_id("hook-process"),
                executable_id=command.executable_id,
                arguments=command.arguments,
                stdin=body,
                environment=environment,
                deadline=self._clock.utcnow() + timedelta(milliseconds=definition.timeout_ms),
                stdout_limit_bytes=definition.output_limit_bytes,
                stderr_limit_bytes=min(definition.output_limit_bytes, 64 * 1024),
                allow_network=False,
                owner_kind=ProcessOwnerKind.HOOK,
                owner_run_id=invocation.run_id or f"session:{invocation.context.session_id}",
                workspace_id=invocation.context.workspace_id,
                cwd_root_id=command.cwd_root_id,
                cwd=command.cwd,
                environment_profile_id=command.environment_profile_id,
                stdin_mode=ProcessStdinMode.FIXED_PAYLOAD,
                artifact_limit_bytes=command.artifact_output_limit_bytes,
                allow_artifact_spill=self._artifact_budget is not None,
                executable_profile_fingerprint=command.executable_profile_fingerprint,
                artifact_budget=self._artifact_budget,
            ),
            cancellation,
        )
        if result.timed_out:
            raise HookExecutionError("hook_timeout")
        if (
            result.output_truncated
            or result.stdout_artifact_id is not None
            or result.stderr_artifact_id is not None
            or len(result.stdout) > definition.output_limit_bytes
            or len(result.stderr) > min(definition.output_limit_bytes, 64 * 1024)
        ):
            raise HookExecutionError("hook_output_limit_exceeded")
        if result.exit_code != 0:
            raise HookExecutionError("hook_process_failed")
        return _parse_command_output(result.stdout)

    async def _with_deadline(
        self,
        operation: Coroutine[Any, Any, HookOutput],
        timeout_ms: int,
        cancellation: CancellationToken,
    ) -> HookOutput:
        cancellation.checkpoint()
        task = asyncio.create_task(operation)
        timeout = asyncio.create_task(
            self._clock.sleep_until(self._clock.utcnow() + timedelta(milliseconds=timeout_ms))
        )
        cancelled = asyncio.create_task(cancellation.wait())
        try:
            done, _ = await asyncio.wait((task, timeout, cancelled), return_when=asyncio.FIRST_COMPLETED)
            if cancelled in done:
                cancellation.checkpoint()
            if timeout in done and task not in done:
                raise HookExecutionError("hook_timeout")
            return await task
        finally:
            for pending in (task, timeout, cancelled):
                if not pending.done():
                    pending.cancel()
            await asyncio.gather(task, timeout, cancelled, return_exceptions=True)

    @staticmethod
    def _validate_output_for_event(event: HookEvent, output: HookOutput) -> None:
        if output.argument_patch is not None and event is not HookEvent.PRE_TOOL_USE:
            raise HookExecutionError("hook_argument_mutation_not_allowed")

    @staticmethod
    def _failure_mode(event: HookEvent) -> HookFailureMode:
        return HookFailureMode.FAIL_CLOSED if event in _FAIL_CLOSED_EVENTS else HookFailureMode.WARN

    def _failure_outcome(self, invocation: HookInvocation, code: str) -> HookOutcome:
        decision = (
            HookDecision.DENY
            if self._failure_mode(invocation.event) is HookFailureMode.FAIL_CLOSED
            else HookDecision.CONTINUE
        )
        return HookOutcome(decision, (), (), (), (code,))

    async def _claim_or_replay(
        self,
        invocation: HookInvocation,
    ) -> tuple[HookOutcome, tuple[StoredEvent, ...]] | None:
        receipt_id = invocation.invocation_id
        try:
            async with self._unit_of_work.begin() as uow:
                raw = await uow.entities.get(_RECEIPTS, receipt_id)
                if raw is not None:
                    return await self._decode_replay(uow, invocation, raw)
                await uow.entities.put(
                    _RECEIPTS,
                    receipt_id,
                    {
                        "schemaVersion": _RECEIPT_SCHEMA,
                        "state": "started",
                        "requestHash": invocation.request_hash,
                        "event": invocation.event.value,
                        "startedAt": self._clock.utcnow().isoformat(),
                    },
                    expected_revision=0,
                )
                await uow.commit()
                return None
        except EntityRevisionConflict:
            async with self._unit_of_work.begin() as uow:
                raw = await uow.entities.get(_RECEIPTS, receipt_id)
                if raw is None:
                    raise HookServiceError("Hook invocation claim disappeared") from None
                return await self._decode_replay(uow, invocation, raw)
        except Exception as error:
            async with self._unit_of_work.begin() as uow:
                raw = await uow.entities.get(_RECEIPTS, receipt_id)
                if raw is None:
                    raise HookServiceError("Hook invocation claim disappeared") from error
                receipt = _mapping(raw, "Hook receipt")
                if receipt.get("requestHash") != invocation.request_hash:
                    raise HookInvocationConflict("Hook invocation receipt identity mismatch") from error
                if receipt.get("state") == "started":
                    return None
                return await self._decode_replay(uow, invocation, receipt)

    async def _decode_replay(
        self,
        uow: Any,
        invocation: HookInvocation,
        raw: Any,
    ) -> tuple[HookOutcome, tuple[StoredEvent, ...]]:
        receipt = _mapping(raw, "Hook receipt")
        if receipt.get("schemaVersion") != _RECEIPT_SCHEMA or receipt.get("requestHash") != invocation.request_hash:
            raise HookInvocationConflict("Hook invocation receipt identity mismatch")
        if receipt.get("event") != invocation.event.value:
            raise HookInvocationConflict("Hook invocation event mismatch")
        if receipt.get("state") == "started":
            return self._failure_outcome(invocation, "hook_previous_outcome_unknown"), ()
        if receipt.get("state") != "completed":
            raise HookServiceError("Hook receipt state is corrupt")
        outcome = _outcome_from_receipt(receipt, invocation)
        sequence = _integer(receipt.get("eventSequence"), "Hook eventSequence")
        stream_id = _text(receipt.get("streamId"), "Hook streamId")
        events = await uow.events.read(stream_id, after_sequence=sequence - 1, limit=1)
        if len(events) != 1 or events[0].sequence != sequence:
            raise HookServiceError("Hook receipt event is missing")
        return outcome, events

    async def _complete(self, invocation: HookInvocation, outcome: HookOutcome) -> tuple[StoredEvent, ...]:
        stream_id = f"hooks:{invocation.context.workspace_id}:{invocation.context.session_id}"
        audit = HookAuditRecord(
            audit_id=self._ids.new_id("hook-audit"),
            invocation_id=invocation.invocation_id,
            event=invocation.event,
            request_hash=invocation.request_hash,
            decision=outcome.decision,
            applied_hook_ids=outcome.applied_hook_ids,
            audit_tags=outcome.audit_tags,
            warning_codes=outcome.warning_codes,
            original_args_hash=None if invocation.tool is None else invocation.tool.args_hash,
            mutated_args_hash=outcome.mutated_args_hash,
            workspace_id_hash=_identity_hash(invocation.context.workspace_id),
            run_id_hash=None if invocation.run_id is None else _identity_hash(invocation.run_id),
        )
        stored: tuple[StoredEvent, ...] = ()
        try:
            async with self._unit_of_work.begin() as uow:
                raw = _mapping(await uow.entities.get(_RECEIPTS, invocation.invocation_id), "Hook receipt")
                if raw.get("requestHash") != invocation.request_hash or raw.get("state") != "started":
                    replay = await self._decode_replay(uow, invocation, raw)
                    return replay[1]
                expected_sequence = await uow.events.latest_sequence(stream_id)
                event = NewEvent(
                    event_id=self._ids.new_id("event"),
                    event_type="hook.evaluated",
                    payload=audit.payload(),
                    occurred_at=self._clock.utcnow(),
                    terminal=False,
                    idempotency_key=invocation.invocation_id,
                )
                stored = await uow.events.append(stream_id, expected_sequence, (event,))
                await uow.entities.put(
                    _AUDITS,
                    audit.audit_id,
                    audit.payload(),
                    expected_revision=0,
                )
                await uow.entities.put(
                    _RECEIPTS,
                    invocation.invocation_id,
                    _receipt_payload(invocation, outcome, stream_id, stored[0].sequence),
                    expected_revision=1,
                )
                await uow.commit()
        except BaseException:
            async with self._unit_of_work.begin() as uow:
                recovered_raw = await uow.entities.get(_RECEIPTS, invocation.invocation_id)
                if recovered_raw is None:
                    raise
                replay_outcome, recovered = await self._decode_replay(uow, invocation, recovered_raw)
                if replay_outcome.decision is not outcome.decision:
                    raise HookServiceError("Recovered Hook outcome does not match committed outcome") from None
                return recovered
        return stored

    async def _deliver(self, events: Sequence[StoredEvent]) -> None:
        if not events:
            return
        try:
            await self._event_sink.publish(events)
        except Exception as error:
            self.delivery_failures.append(type(error).__name__)

    async def _lock_for(self, invocation_id: str) -> asyncio.Lock:
        async with self._locks_guard:
            return self._locks.setdefault(invocation_id, asyncio.Lock())


def _command_input(invocation: HookInvocation) -> bytes:
    tool = invocation.tool
    return canonical_json_bytes(
        {
            "protocolVersion": 1,
            "invocationId": invocation.invocation_id,
            "chainId": invocation.chain_id,
            "event": invocation.event.value,
            "workspaceTrusted": invocation.context.workspace_trusted,
            "workspaceId": invocation.context.workspace_id,
            "sessionId": invocation.context.session_id,
            "runId": invocation.run_id,
            "facts": invocation.facts,
            "tool": (
                None
                if tool is None
                else {
                    "toolCallId": tool.tool_call_id,
                    "name": tool.name,
                    "version": tool.version,
                    "definitionFingerprint": tool.definition_fingerprint,
                    "arguments": tool.arguments,
                    "argsHash": tool.args_hash,
                    "idempotencyKey": tool.idempotency_key,
                }
            ),
        }
    )


def _output_bytes(output: HookOutput) -> bytes:
    return canonical_json_bytes(
        {
            "decision": output.decision.value,
            "auditTags": list(output.audit_tags),
            "argumentPatch": output.argument_patch,
            "contextHints": list(output.context_hints),
        }
    )


def _parse_command_output(data: bytes) -> HookOutput:
    try:
        raw = json.loads(data.decode("utf-8"), object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise HookExecutionError("hook_output_invalid_json") from error
    if not isinstance(raw, dict):
        raise HookExecutionError("hook_output_not_object")
    allowed = {"decision", "auditTags", "argumentPatch", "contextHints"}
    if set(raw) - allowed:
        raise HookExecutionError("hook_output_unknown_field")
    decision_raw = raw.get("decision", "continue")
    tags = raw.get("auditTags", [])
    patch = raw.get("argumentPatch")
    hints = raw.get("contextHints", [])
    if not isinstance(decision_raw, str) or not isinstance(tags, list) or not isinstance(hints, list):
        raise HookExecutionError("hook_output_wrong_type")
    if any(not isinstance(item, str) for item in (*tags, *hints)):
        raise HookExecutionError("hook_output_wrong_type")
    if patch is not None and not isinstance(patch, dict):
        raise HookExecutionError("hook_output_wrong_type")
    try:
        return HookOutput(HookDecision(decision_raw), tuple(tags), patch, tuple(hints))
    except (TypeError, ValueError) as error:
        raise HookExecutionError("hook_output_schema_invalid") from error


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for key, value in pairs:
        if key in output:
            raise ValueError(f"duplicate JSON key: {key}")
        output[key] = value
    return output


def _stronger_decision(left: HookDecision, right: HookDecision) -> HookDecision:
    rank = {HookDecision.CONTINUE: 0, HookDecision.ASK: 1, HookDecision.DENY: 2}
    return left if rank[left] >= rank[right] else right


def _receipt_payload(
    invocation: HookInvocation,
    outcome: HookOutcome,
    stream_id: str,
    event_sequence: int,
) -> dict[str, Any]:
    return {
        "schemaVersion": _RECEIPT_SCHEMA,
        "state": "completed",
        "requestHash": invocation.request_hash,
        "event": invocation.event.value,
        "decision": outcome.decision.value,
        "auditTags": list(outcome.audit_tags),
        "appliedHookIds": list(outcome.applied_hook_ids),
        "warningCodes": list(outcome.warning_codes),
        "hasContextHints": bool(outcome.context_hints),
        "mutatedArgsHash": outcome.mutated_args_hash,
        "streamId": stream_id,
        "eventSequence": event_sequence,
    }


def _outcome_from_receipt(receipt: Mapping[str, Any], invocation: HookInvocation) -> HookOutcome:
    try:
        decision = HookDecision(receipt["decision"])
        tags = _string_tuple(receipt.get("auditTags"), "Hook auditTags")
        applied = _string_tuple(receipt.get("appliedHookIds"), "Hook appliedHookIds")
        warnings = list(_string_tuple(receipt.get("warningCodes"), "Hook warningCodes"))
        has_hints = receipt.get("hasContextHints")
        mutated_hash = receipt.get("mutatedArgsHash")
        if not isinstance(has_hints, bool) or (mutated_hash is not None and not isinstance(mutated_hash, str)):
            raise TypeError("Hook receipt output markers are invalid")
    except (KeyError, TypeError, ValueError) as error:
        raise HookServiceError("Hook receipt outcome is corrupt") from error
    if has_hints or mutated_hash is not None:
        warnings.append("hook_sensitive_output_not_replayed")
        if HookService._failure_mode(invocation.event) is HookFailureMode.FAIL_CLOSED:
            decision = HookDecision.DENY
    return HookOutcome(decision, tags, (), applied, tuple(dict.fromkeys(warnings)), replayed=True)


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise HookServiceError(f"{label} is not an object")
    return value


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise HookServiceError(f"{label} is invalid")
    return value


def _integer(value: Any, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise HookServiceError(f"{label} is invalid")
    return value


def _string_tuple(value: Any, label: str) -> tuple[str, ...]:
    if not isinstance(value, (tuple, list)) or any(not isinstance(item, str) for item in value):
        raise TypeError(f"{label} is invalid")
    return tuple(value)


def _identity_hash(value: str) -> str:
    return f"sha256:{hashlib.sha256(value.encode('utf-8')).hexdigest()}"


__all__ = [
    "HookConfigurationError",
    "HookExecutionError",
    "HookHandlerRegistry",
    "HookInvocationConflict",
    "HookService",
    "HookServiceError",
    "StaticHookLayerSource",
]
