"""Versioned, allow-listed entity codecs for durable SQLite state.

No Python module/class names are persisted and no dynamic imports or pickle are
used.  A registered collection has exactly one stable codec tag/version.  An
unregistered collection may persist plain JSON only; attempting to persist a
dataclass, enum, datetime, or other Python object fails closed.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from offeragent_harness.agent.budget_checkpoint import BudgetCheckpoint
from offeragent_harness.agent.budgets import BudgetDelta, RunBudget
from offeragent_harness.agent.state import (
    PendingWork,
    RunPhase,
    RunState,
    VaultWriteIntentBinding,
    WriteObligation,
    WriteOutcome,
)
from offeragent_harness.models.json_types import freeze_json, thaw_json
from offeragent_harness.permissions import (
    ApprovalBinding,
    ApprovalGrant,
    ApprovalGrantState,
    ApprovalRequest,
    ApprovalResolution,
    ApprovalScope,
    ApprovalState,
    RiskClass,
)
from offeragent_harness.sessions import (
    AgentLineage,
    Run,
    RunKind,
    RunStatus,
    Session,
    SessionStatus,
    TerminationReason,
    Turn,
    TurnStatus,
)
from offeragent_harness.tools import ResultSensitivity, ToolCall, ToolResultStatus

from .serialization import (
    dump_datetime,
    dump_json,
    load_datetime,
    load_json,
    tool_result_from_value,
    tool_result_to_value,
)


class EntityCodecError(RuntimeError):
    pass


class EntityCodecTypeError(EntityCodecError, TypeError):
    pass


class EntityCodecVersionError(EntityCodecError):
    pass


@dataclass(frozen=True)
class EntityCodec:
    collection: str
    tag: str
    schema_version: int
    python_type: type[Any]
    encode_payload: Callable[[Any], Mapping[str, Any]]
    decode_payload: Callable[[Mapping[str, Any]], Any]

    def __post_init__(self) -> None:
        if not self.collection or not self.tag or self.schema_version < 1:
            raise ValueError("entity codec identity/version is invalid")


class EntityCodecRegistry:
    JSON_TAG = "json"
    JSON_SCHEMA_VERSION = 1

    def __init__(self, codecs: Sequence[EntityCodec] = ()) -> None:
        by_collection: dict[str, EntityCodec] = {}
        for codec in codecs:
            if codec.collection in by_collection:
                raise ValueError(f"duplicate entity codec for collection {codec.collection!r}")
            by_collection[codec.collection] = codec
        self._by_collection = by_collection

    @property
    def registered_collections(self) -> frozenset[str]:
        return frozenset(self._by_collection)

    def with_codec(self, codec: EntityCodec) -> EntityCodecRegistry:
        if codec.collection in self._by_collection:
            raise ValueError(f"entity codec already registered for collection {codec.collection!r}")
        return EntityCodecRegistry((*self._by_collection.values(), codec))

    def encode(self, collection: str, value: Any) -> str:
        codec = self._by_collection.get(collection)
        if codec is None:
            try:
                payload = thaw_json(freeze_json(value))
            except (TypeError, ValueError) as error:
                raise EntityCodecTypeError(f"unregistered collection {collection!r} accepts plain JSON only") from error
            envelope = {
                "codec": self.JSON_TAG,
                "schemaVersion": self.JSON_SCHEMA_VERSION,
                "payload": payload,
            }
            return dump_json(envelope)

        if type(value) is not codec.python_type:
            raise EntityCodecTypeError(
                f"collection {collection!r} requires {codec.python_type.__name__}, got {type(value).__name__}"
            )
        payload = codec.encode_payload(value)
        return dump_json(
            {
                "codec": codec.tag,
                "schemaVersion": codec.schema_version,
                "payload": payload,
            }
        )

    def decode(self, collection: str, encoded: str) -> Any:
        envelope = _exact_object(load_json(encoded), {"codec", "schemaVersion", "payload"}, "entity envelope")
        tag = _string(envelope["codec"], "entity envelope codec")
        version = _integer(envelope["schemaVersion"], "entity envelope schemaVersion")
        codec = self._by_collection.get(collection)
        if codec is None:
            if tag != self.JSON_TAG or version != self.JSON_SCHEMA_VERSION:
                raise EntityCodecVersionError(
                    f"unregistered collection {collection!r} has unsupported codec {tag!r} v{version}"
                )
            return thaw_json(freeze_json(envelope["payload"]))

        if tag != codec.tag or version != codec.schema_version:
            raise EntityCodecVersionError(
                f"collection {collection!r} expected codec {codec.tag!r} v{codec.schema_version}, "
                f"found {tag!r} v{version}"
            )
        payload = _object(envelope["payload"], f"{codec.tag} payload")
        decoded = codec.decode_payload(payload)
        if type(decoded) is not codec.python_type:
            raise EntityCodecTypeError(f"codec {codec.tag!r} decoded unexpected type {type(decoded).__name__}")
        return decoded


def _object(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise EntityCodecError(f"{label} must be a JSON object")
    return value


def _exact_object(value: Any, keys: set[str], label: str) -> Mapping[str, Any]:
    result = _object(value, label)
    if set(result) != keys:
        raise EntityCodecError(f"{label} fields are incompatible: {sorted(result)}")
    return result


def _string(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise EntityCodecError(f"{label} must be a string")
    return value


def _optional_string(value: Any, label: str) -> str | None:
    if value is None:
        return None
    return _string(value, label)


def _integer(value: Any, label: str) -> int:
    if type(value) is not int:
        raise EntityCodecError(f"{label} must be an integer")
    return value


def _boolean(value: Any, label: str) -> bool:
    if type(value) is not bool:
        raise EntityCodecError(f"{label} must be a boolean")
    return value


def _number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise EntityCodecError(f"{label} must be a number")
    result = float(value)
    if not math.isfinite(result):
        raise EntityCodecError(f"{label} must be finite")
    return result


_NONNEGATIVE_DECIMAL = re.compile(r"^(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$")


def _decimal_to_string(value: Decimal, label: str) -> str:
    if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
        raise EntityCodecError(f"{label} must be a finite non-negative Decimal")
    normalized = abs(value) if value == 0 else value
    result = format(normalized, "f")
    if _NONNEGATIVE_DECIMAL.fullmatch(result) is None:
        raise EntityCodecError(f"{label} is not canonically serializable")
    return result


def _decimal_from_string(value: Any, label: str) -> Decimal:
    raw = _string(value, label)
    if _NONNEGATIVE_DECIMAL.fullmatch(raw) is None:
        raise EntityCodecError(f"{label} must be a canonical non-negative decimal string")
    result = Decimal(raw)
    if not result.is_finite():
        raise EntityCodecError(f"{label} must be finite")
    return result


def _array(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise EntityCodecError(f"{label} must be an array")
    return value


def _string_tuple(value: Any, label: str) -> tuple[str, ...]:
    return tuple(_string(item, f"{label} item") for item in _array(value, label))


def _lineage_to_payload(lineage: AgentLineage) -> dict[str, Any]:
    return {
        "rootRunId": lineage.root_run_id,
        "runId": lineage.run_id,
        "parentRunId": lineage.parent_run_id,
        "ancestorRunIds": list(lineage.ancestor_run_ids),
        "depth": lineage.depth,
        "agentName": lineage.agent_name,
    }


def _lineage_from_payload(value: Any) -> AgentLineage:
    raw = _exact_object(
        value,
        {"rootRunId", "runId", "parentRunId", "ancestorRunIds", "depth", "agentName"},
        "agent lineage",
    )
    return AgentLineage(
        root_run_id=_string(raw["rootRunId"], "rootRunId"),
        run_id=_string(raw["runId"], "runId"),
        parent_run_id=_optional_string(raw["parentRunId"], "parentRunId"),
        ancestor_run_ids=_string_tuple(raw["ancestorRunIds"], "ancestorRunIds"),
        depth=_integer(raw["depth"], "depth"),
        agent_name=_string(raw["agentName"], "agentName"),
    )


def _session_to_payload(value: Any) -> Mapping[str, Any]:
    session = value
    return {
        "sessionId": session.session_id,
        "workspaceId": session.workspace_id,
        "profileId": session.profile_id,
        "title": session.title,
        "status": session.status.value,
        "createdAt": dump_datetime(session.created_at),
        "updatedAt": dump_datetime(session.updated_at),
        "revision": session.revision,
        "forkedFromSessionId": session.forked_from_session_id,
        "forkedFromTurnId": session.forked_from_turn_id,
    }


def _session_from_payload(value: Mapping[str, Any]) -> Session:
    raw = _exact_object(
        value,
        {
            "sessionId",
            "workspaceId",
            "profileId",
            "title",
            "status",
            "createdAt",
            "updatedAt",
            "revision",
            "forkedFromSessionId",
            "forkedFromTurnId",
        },
        "session",
    )
    return Session(
        session_id=_string(raw["sessionId"], "sessionId"),
        workspace_id=_string(raw["workspaceId"], "workspaceId"),
        profile_id=_string(raw["profileId"], "profileId"),
        title=_string(raw["title"], "title"),
        status=SessionStatus(_string(raw["status"], "session status")),
        created_at=load_datetime(_string(raw["createdAt"], "createdAt")),
        updated_at=load_datetime(_string(raw["updatedAt"], "updatedAt")),
        revision=_integer(raw["revision"], "session revision"),
        forked_from_session_id=_optional_string(raw["forkedFromSessionId"], "forkedFromSessionId"),
        forked_from_turn_id=_optional_string(raw["forkedFromTurnId"], "forkedFromTurnId"),
    )


def _turn_to_payload(value: Any) -> Mapping[str, Any]:
    turn = value
    return {
        "turnId": turn.turn_id,
        "sessionId": turn.session_id,
        "ordinal": turn.ordinal,
        "status": turn.status.value,
        "inputBlocks": thaw_json(turn.input_blocks),
        "createdAt": dump_datetime(turn.created_at),
        "updatedAt": dump_datetime(turn.updated_at),
        "revision": turn.revision,
    }


def _turn_from_payload(value: Mapping[str, Any]) -> Turn:
    raw = _exact_object(
        value,
        {"turnId", "sessionId", "ordinal", "status", "inputBlocks", "createdAt", "updatedAt", "revision"},
        "turn",
    )
    blocks = _array(raw["inputBlocks"], "inputBlocks")
    if any(not isinstance(block, Mapping) for block in blocks):
        raise EntityCodecError("each inputBlocks item must be an object")
    return Turn(
        turn_id=_string(raw["turnId"], "turnId"),
        session_id=_string(raw["sessionId"], "sessionId"),
        ordinal=_integer(raw["ordinal"], "turn ordinal"),
        status=TurnStatus(_string(raw["status"], "turn status")),
        input_blocks=tuple(blocks),
        created_at=load_datetime(_string(raw["createdAt"], "createdAt")),
        updated_at=load_datetime(_string(raw["updatedAt"], "updatedAt")),
        revision=_integer(raw["revision"], "turn revision"),
    )


def _run_to_payload(value: Any) -> Mapping[str, Any]:
    run = value
    return {
        "runId": run.run_id,
        "sessionId": run.session_id,
        "turnId": run.turn_id,
        "workspaceId": run.workspace_id,
        "lineage": _lineage_to_payload(run.lineage),
        "kind": run.kind.value,
        "status": run.status.value,
        "attempt": run.attempt,
        "eventSequence": run.event_sequence,
        "configSnapshot": thaw_json(run.config_snapshot),
        "createdAt": dump_datetime(run.created_at),
        "updatedAt": dump_datetime(run.updated_at),
        "deadlineAt": None if run.deadline_at is None else dump_datetime(run.deadline_at),
        "terminationReason": None if run.termination_reason is None else run.termination_reason.value,
    }


def _run_from_payload(value: Mapping[str, Any]) -> Run:
    raw = _exact_object(
        value,
        {
            "runId",
            "sessionId",
            "turnId",
            "workspaceId",
            "lineage",
            "kind",
            "status",
            "attempt",
            "eventSequence",
            "configSnapshot",
            "createdAt",
            "updatedAt",
            "deadlineAt",
            "terminationReason",
        },
        "run",
    )
    deadline = raw["deadlineAt"]
    termination = raw["terminationReason"]
    config = _object(raw["configSnapshot"], "configSnapshot")
    return Run(
        run_id=_string(raw["runId"], "runId"),
        session_id=_string(raw["sessionId"], "sessionId"),
        turn_id=_string(raw["turnId"], "turnId"),
        workspace_id=_string(raw["workspaceId"], "workspaceId"),
        lineage=_lineage_from_payload(raw["lineage"]),
        kind=RunKind(_string(raw["kind"], "run kind")),
        status=RunStatus(_string(raw["status"], "run status")),
        attempt=_integer(raw["attempt"], "run attempt"),
        event_sequence=_integer(raw["eventSequence"], "eventSequence"),
        config_snapshot=config,
        created_at=load_datetime(_string(raw["createdAt"], "createdAt")),
        updated_at=load_datetime(_string(raw["updatedAt"], "updatedAt")),
        deadline_at=None if deadline is None else load_datetime(_string(deadline, "deadlineAt")),
        termination_reason=(
            None if termination is None else TerminationReason(_string(termination, "terminationReason"))
        ),
    )


def _tool_call_to_payload(call: ToolCall) -> dict[str, Any]:
    return {
        "toolCallId": call.tool_call_id,
        "runId": call.run_id,
        "workspaceId": call.workspace_id,
        "name": call.name,
        "version": call.version,
        "definitionFingerprint": call.definition_fingerprint,
        "resultSensitivity": call.result_sensitivity.value,
        "arguments": thaw_json(call.arguments),
        "argsHash": call.args_hash,
        "idempotencyKey": call.idempotency_key,
        "deadlineAt": None if call.deadline is None else dump_datetime(call.deadline),
        "lineage": _lineage_to_payload(call.lineage),
    }


def _tool_call_from_payload(value: Any) -> ToolCall:
    keys = {
        "toolCallId",
        "runId",
        "workspaceId",
        "name",
        "version",
        "definitionFingerprint",
        "arguments",
        "argsHash",
        "idempotencyKey",
        "deadlineAt",
        "lineage",
        "resultSensitivity",
    }
    raw = _exact_object(value, keys, "pending ToolCall")
    deadline = raw["deadlineAt"]
    return ToolCall(
        tool_call_id=_string(raw["toolCallId"], "toolCallId"),
        run_id=_string(raw["runId"], "runId"),
        workspace_id=_string(raw["workspaceId"], "workspaceId"),
        name=_string(raw["name"], "tool name"),
        version=_string(raw["version"], "tool version"),
        definition_fingerprint=_string(raw["definitionFingerprint"], "definitionFingerprint"),
        arguments=_object(raw["arguments"], "tool arguments"),
        args_hash=_string(raw["argsHash"], "argsHash"),
        idempotency_key=_string(raw["idempotencyKey"], "idempotencyKey"),
        deadline=None if deadline is None else load_datetime(_string(deadline, "deadlineAt")),
        lineage=_lineage_from_payload(raw["lineage"]),
        result_sensitivity=ResultSensitivity(_string(raw["resultSensitivity"], "resultSensitivity")),
    )


def _budget_delta_to_payload(delta: BudgetDelta) -> dict[str, Any]:
    return {
        "modelRounds": delta.model_rounds,
        "toolCalls": delta.tool_calls,
        "inputTokens": delta.input_tokens,
        "outputTokens": delta.output_tokens,
        "cost": _decimal_to_string(delta.cost, "budget delta cost"),
        "artifactBytes": delta.artifact_bytes,
        "subagents": delta.subagents,
    }


def _budget_delta_from_payload(value: Any, label: str) -> BudgetDelta:
    raw = _exact_object(
        value,
        {"modelRounds", "toolCalls", "inputTokens", "outputTokens", "cost", "artifactBytes", "subagents"},
        label,
    )
    return BudgetDelta(
        model_rounds=_integer(raw["modelRounds"], f"{label} modelRounds"),
        tool_calls=_integer(raw["toolCalls"], f"{label} toolCalls"),
        input_tokens=_integer(raw["inputTokens"], f"{label} inputTokens"),
        output_tokens=_integer(raw["outputTokens"], f"{label} outputTokens"),
        cost=_decimal_from_string(raw["cost"], f"{label} cost"),
        artifact_bytes=_integer(raw["artifactBytes"], f"{label} artifactBytes"),
        subagents=_integer(raw["subagents"], f"{label} subagents"),
    )


def _run_budget_to_payload(budget: RunBudget) -> dict[str, Any]:
    return {
        "maxModelRounds": budget.max_model_rounds,
        "maxToolCalls": budget.max_tool_calls,
        "maxParallelReads": budget.max_parallel_reads,
        "maxWallSeconds": float(budget.max_wall_seconds),
        "maxInputTokens": budget.max_input_tokens,
        "maxOutputTokens": budget.max_output_tokens,
        "maxCost": _decimal_to_string(budget.max_cost, "budget maxCost"),
        "maxArtifactBytes": budget.max_artifact_bytes,
        "maxSubagents": budget.max_subagents,
        "maxSubagentDepth": budget.max_subagent_depth,
    }


def _run_budget_from_payload(value: Any) -> RunBudget:
    raw = _exact_object(
        value,
        {
            "maxModelRounds",
            "maxToolCalls",
            "maxParallelReads",
            "maxWallSeconds",
            "maxInputTokens",
            "maxOutputTokens",
            "maxCost",
            "maxArtifactBytes",
            "maxSubagents",
            "maxSubagentDepth",
        },
        "budget limits",
    )
    return RunBudget(
        max_model_rounds=_integer(raw["maxModelRounds"], "maxModelRounds"),
        max_tool_calls=_integer(raw["maxToolCalls"], "maxToolCalls"),
        max_parallel_reads=_integer(raw["maxParallelReads"], "maxParallelReads"),
        max_wall_seconds=_number(raw["maxWallSeconds"], "maxWallSeconds"),
        max_input_tokens=_integer(raw["maxInputTokens"], "maxInputTokens"),
        max_output_tokens=_integer(raw["maxOutputTokens"], "maxOutputTokens"),
        max_cost=_decimal_from_string(raw["maxCost"], "maxCost"),
        max_artifact_bytes=_integer(raw["maxArtifactBytes"], "maxArtifactBytes"),
        max_subagents=_integer(raw["maxSubagents"], "maxSubagents"),
        max_subagent_depth=_integer(raw["maxSubagentDepth"], "maxSubagentDepth"),
    )


def _budget_checkpoint_to_payload(checkpoint: BudgetCheckpoint | None) -> Mapping[str, Any] | None:
    if checkpoint is None:
        return None
    return {
        "limits": _run_budget_to_payload(checkpoint.budget),
        "startedAt": dump_datetime(checkpoint.started_at),
        "used": _budget_delta_to_payload(checkpoint.used),
        "reserved": _budget_delta_to_payload(checkpoint.reserved),
        "capturedAt": dump_datetime(checkpoint.captured_at),
        "elapsedSeconds": checkpoint.elapsed_seconds,
    }


def _budget_checkpoint_from_payload(value: Any) -> BudgetCheckpoint | None:
    if value is None:
        return None
    raw = _exact_object(
        value,
        {"limits", "startedAt", "used", "reserved", "capturedAt", "elapsedSeconds"},
        "budget checkpoint",
    )
    return BudgetCheckpoint(
        budget=_run_budget_from_payload(raw["limits"]),
        started_at=load_datetime(_string(raw["startedAt"], "budget startedAt")),
        used=_budget_delta_from_payload(raw["used"], "used budget"),
        reserved=_budget_delta_from_payload(raw["reserved"], "reserved budget"),
        captured_at=load_datetime(_string(raw["capturedAt"], "budget capturedAt")),
        elapsed_seconds=_number(raw["elapsedSeconds"], "budget elapsedSeconds"),
    )


def _run_state_to_payload(value: Any) -> Mapping[str, Any]:
    state = value
    return {
        "workspaceId": state.workspace_id,
        "sessionId": state.session_id,
        "turnId": state.turn_id,
        "runId": state.run_id,
        "lineage": _lineage_to_payload(state.lineage),
        "phase": state.phase.value,
        "revision": state.revision,
        "modelRounds": state.model_rounds,
        "toolCalls": state.tool_calls,
        "pending": {
            "toolCallIds": sorted(state.pending.tool_call_ids),
            "toolCalls": [_tool_call_to_payload(call) for call in state.pending.tool_calls],
            "approvalIds": sorted(state.pending.approval_ids),
            "clientInvocationIds": sorted(state.pending.client_invocation_ids),
            "childRunIds": sorted(state.pending.child_run_ids),
        },
        "writeObligation": {
            "required": state.write_obligation.required,
            "reasons": list(state.write_obligation.reasons),
            "intent": (
                None
                if state.write_obligation.intent is None
                else {
                    "requestHash": state.write_obligation.intent.request_hash,
                    "intentHash": state.write_obligation.intent.intent_hash,
                    "targetPaths": list(state.write_obligation.intent.target_paths),
                }
            ),
            "outcomes": [
                {
                    "toolCallId": outcome.tool_call_id,
                    "status": outcome.status.value,
                    "summary": outcome.summary,
                    "coveredPaths": list(outcome.covered_paths),
                }
                for outcome in state.write_obligation.outcomes
            ],
        },
        "toolResults": [tool_result_to_value(result) for result in state.tool_results],
        "toolResultSensitivities": {
            tool_call_id: sensitivity.value
            for tool_call_id, sensitivity in sorted(state.tool_result_sensitivities.items())
        },
        "assistantText": state.assistant_text,
        "budgetCheckpoint": _budget_checkpoint_to_payload(state.budget_checkpoint),
    }


def _run_state_from_payload(value: Mapping[str, Any]) -> RunState:
    keys = {
        "workspaceId",
        "sessionId",
        "turnId",
        "runId",
        "lineage",
        "phase",
        "revision",
        "modelRounds",
        "toolCalls",
        "pending",
        "writeObligation",
        "toolResults",
        "toolResultSensitivities",
        "assistantText",
        "budgetCheckpoint",
    }
    raw = _exact_object(value, keys, "run state")
    pending_raw = _exact_object(
        raw["pending"],
        {"toolCallIds", "toolCalls", "approvalIds", "clientInvocationIds", "childRunIds"},
        "pending work",
    )
    obligation_value = raw["writeObligation"]
    if not isinstance(obligation_value, Mapping):
        raise TypeError("write obligation must be an object")
    obligation_keys = set(obligation_value)
    obligation_raw: dict[str, Any]
    if obligation_keys == {"required", "reasons", "outcomes"}:
        obligation_raw = dict(obligation_value)
        obligation_raw["intent"] = None
    else:
        obligation_raw = dict(
            _exact_object(
                obligation_value,
                {"required", "reasons", "intent", "outcomes"},
                "write obligation",
            )
        )
    intent_value = obligation_raw["intent"]
    intent: VaultWriteIntentBinding | None = None
    if intent_value is not None:
        intent_raw = _exact_object(
            intent_value,
            {"requestHash", "intentHash", "targetPaths"},
            "Vault write intent",
        )
        intent = VaultWriteIntentBinding(
            request_hash=_string(intent_raw["requestHash"], "write intent requestHash"),
            intent_hash=_string(intent_raw["intentHash"], "write intent intentHash"),
            target_paths=_string_tuple(intent_raw["targetPaths"], "write intent targetPaths"),
        )
    outcomes: list[WriteOutcome] = []
    for item in _array(obligation_raw["outcomes"], "write outcomes"):
        if not isinstance(item, Mapping):
            raise TypeError("write outcome must be an object")
        outcome: dict[str, Any]
        if set(item) == {"toolCallId", "status", "summary"}:
            outcome = dict(item)
            outcome["coveredPaths"] = []
        else:
            outcome = dict(
                _exact_object(
                    item,
                    {"toolCallId", "status", "summary", "coveredPaths"},
                    "write outcome",
                )
            )
        outcomes.append(
            WriteOutcome(
                tool_call_id=_string(outcome["toolCallId"], "write outcome toolCallId"),
                status=ToolResultStatus(_string(outcome["status"], "write outcome status")),
                summary=_string(outcome["summary"], "write outcome summary"),
                covered_paths=_string_tuple(outcome["coveredPaths"], "write outcome coveredPaths"),
            )
        )
    return RunState(
        workspace_id=_string(raw["workspaceId"], "workspaceId"),
        session_id=_string(raw["sessionId"], "sessionId"),
        turn_id=_string(raw["turnId"], "turnId"),
        run_id=_string(raw["runId"], "runId"),
        lineage=_lineage_from_payload(raw["lineage"]),
        phase=RunPhase(_string(raw["phase"], "run phase")),
        revision=_integer(raw["revision"], "state revision"),
        model_rounds=_integer(raw["modelRounds"], "modelRounds"),
        tool_calls=_integer(raw["toolCalls"], "toolCalls"),
        pending=PendingWork(
            tool_call_ids=frozenset(_string_tuple(pending_raw["toolCallIds"], "toolCallIds")),
            tool_calls=tuple(_tool_call_from_payload(item) for item in _array(pending_raw["toolCalls"], "toolCalls")),
            approval_ids=frozenset(_string_tuple(pending_raw["approvalIds"], "approvalIds")),
            client_invocation_ids=frozenset(_string_tuple(pending_raw["clientInvocationIds"], "clientInvocationIds")),
            child_run_ids=frozenset(_string_tuple(pending_raw["childRunIds"], "childRunIds")),
        ),
        write_obligation=WriteObligation(
            required=_boolean(obligation_raw["required"], "write obligation required"),
            reasons=_string_tuple(obligation_raw["reasons"], "write obligation reasons"),
            outcomes=tuple(outcomes),
            intent=intent,
        ),
        tool_results=tuple(tool_result_from_value(item) for item in _array(raw["toolResults"], "toolResults")),
        assistant_text=_string(raw["assistantText"], "assistantText"),
        budget_checkpoint=_budget_checkpoint_from_payload(raw["budgetCheckpoint"]),
        tool_result_sensitivities={
            tool_call_id: ResultSensitivity(_string(sensitivity, "tool result sensitivity"))
            for tool_call_id, sensitivity in _object(raw["toolResultSensitivities"], "toolResultSensitivities").items()
        },
    )


def _approval_binding_to_payload(binding: ApprovalBinding) -> dict[str, Any]:
    return {
        "toolName": binding.tool_name,
        "toolVersion": binding.tool_version,
        "definitionFingerprint": binding.definition_fingerprint,
        "argsHash": binding.args_hash,
        "workspaceId": binding.workspace_id,
        "sessionId": binding.session_id,
        "principalId": binding.principal_id,
        "rootRunId": binding.root_run_id,
        "runId": binding.run_id,
        "agentName": binding.agent_name,
        "ancestorRunIds": list(binding.ancestor_run_ids),
        "expectedStateHash": binding.expected_state_hash,
        "expiresAt": dump_datetime(binding.expires_at),
    }


def _approval_binding_from_payload(value: Any) -> ApprovalBinding:
    raw = _exact_object(
        value,
        {
            "toolName",
            "toolVersion",
            "definitionFingerprint",
            "argsHash",
            "workspaceId",
            "sessionId",
            "principalId",
            "rootRunId",
            "runId",
            "agentName",
            "ancestorRunIds",
            "expectedStateHash",
            "expiresAt",
        },
        "approval binding",
    )
    return ApprovalBinding(
        tool_name=_string(raw["toolName"], "toolName"),
        tool_version=_string(raw["toolVersion"], "toolVersion"),
        definition_fingerprint=_string(raw["definitionFingerprint"], "definitionFingerprint"),
        args_hash=_string(raw["argsHash"], "argsHash"),
        workspace_id=_string(raw["workspaceId"], "workspaceId"),
        session_id=_string(raw["sessionId"], "sessionId"),
        principal_id=_string(raw["principalId"], "principalId"),
        root_run_id=_string(raw["rootRunId"], "rootRunId"),
        run_id=_string(raw["runId"], "runId"),
        agent_name=_string(raw["agentName"], "agentName"),
        ancestor_run_ids=_string_tuple(raw["ancestorRunIds"], "ancestorRunIds"),
        expected_state_hash=_optional_string(raw["expectedStateHash"], "expectedStateHash"),
        expires_at=load_datetime(_string(raw["expiresAt"], "expiresAt")),
    )


def _approval_request_to_payload(request: ApprovalRequest) -> dict[str, Any]:
    return {
        "approvalId": request.approval_id,
        "toolCallId": request.tool_call_id,
        "binding": _approval_binding_to_payload(request.binding),
        "risk": request.risk.value,
        "summary": request.summary,
        "diffArtifactIds": list(request.diff_artifact_ids),
    }


def _approval_request_from_payload(value: Any) -> ApprovalRequest:
    raw = _exact_object(
        value,
        {"approvalId", "toolCallId", "binding", "risk", "summary", "diffArtifactIds"},
        "approval request",
    )
    return ApprovalRequest(
        approval_id=_string(raw["approvalId"], "approvalId"),
        tool_call_id=_string(raw["toolCallId"], "toolCallId"),
        binding=_approval_binding_from_payload(raw["binding"]),
        risk=RiskClass(_string(raw["risk"], "approval risk")),
        summary=_string(raw["summary"], "approval summary"),
        diff_artifact_ids=_string_tuple(raw["diffArtifactIds"], "diffArtifactIds"),
    )


def _approval_resolution_to_payload(resolution: ApprovalResolution) -> dict[str, Any]:
    return {
        "approvalId": resolution.approval_id,
        "state": resolution.state.value,
        "scope": resolution.scope.value,
        "resolvedAt": dump_datetime(resolution.resolved_at),
        "resolverId": resolution.resolver_id,
        "includeDescendants": resolution.include_descendants,
        "reason": resolution.reason,
    }


def _approval_resolution_from_payload(value: Any) -> ApprovalResolution:
    raw = _exact_object(
        value,
        {"approvalId", "state", "scope", "resolvedAt", "resolverId", "includeDescendants", "reason"},
        "approval resolution",
    )
    return ApprovalResolution(
        approval_id=_string(raw["approvalId"], "approvalId"),
        state=ApprovalState(_string(raw["state"], "approval state")),
        scope=ApprovalScope(_string(raw["scope"], "approval scope")),
        resolved_at=load_datetime(_string(raw["resolvedAt"], "resolvedAt")),
        resolver_id=_string(raw["resolverId"], "resolverId"),
        include_descendants=_boolean(raw["includeDescendants"], "includeDescendants"),
        reason=_optional_string(raw["reason"], "approval reason"),
    )


def _approval_grant_to_payload(grant: Any) -> Mapping[str, Any]:
    return {
        "grantId": grant.grant_id,
        "approvalId": grant.approval_id,
        "scope": grant.scope.value,
        "binding": _approval_binding_to_payload(grant.binding),
        "includeDescendants": grant.include_descendants,
        "createdAt": dump_datetime(grant.created_at),
        "expiresAt": None if grant.expires_at is None else dump_datetime(grant.expires_at),
        "state": grant.state.value,
        "revision": grant.revision,
        "revokedAt": None if grant.revoked_at is None else dump_datetime(grant.revoked_at),
        "revokedBy": grant.revoked_by,
        "revocationReason": grant.revocation_reason,
    }


def _approval_grant_from_payload(value: Mapping[str, Any]) -> ApprovalGrant:
    raw = _exact_object(
        value,
        {
            "grantId",
            "approvalId",
            "scope",
            "binding",
            "includeDescendants",
            "createdAt",
            "expiresAt",
            "state",
            "revision",
            "revokedAt",
            "revokedBy",
            "revocationReason",
        },
        "approval grant",
    )
    expires_at = raw["expiresAt"]
    revoked_at = raw["revokedAt"]
    return ApprovalGrant(
        grant_id=_string(raw["grantId"], "grantId"),
        approval_id=_string(raw["approvalId"], "approvalId"),
        scope=ApprovalScope(_string(raw["scope"], "approval grant scope")),
        binding=_approval_binding_from_payload(raw["binding"]),
        include_descendants=_boolean(raw["includeDescendants"], "includeDescendants"),
        created_at=load_datetime(_string(raw["createdAt"], "createdAt")),
        expires_at=None if expires_at is None else load_datetime(_string(expires_at, "expiresAt")),
        state=ApprovalGrantState(_string(raw["state"], "approval grant state")),
        revision=_integer(raw["revision"], "approval grant revision"),
        revoked_at=None if revoked_at is None else load_datetime(_string(revoked_at, "revokedAt")),
        revoked_by=_optional_string(raw["revokedBy"], "revokedBy"),
        revocation_reason=_optional_string(raw["revocationReason"], "revocationReason"),
    )


def approval_record_codec(record_type: type[Any]) -> EntityCodec:
    def encode(value: Any) -> Mapping[str, Any]:
        return {
            "request": _approval_request_to_payload(value.request),
            "state": value.state.value,
            "revision": value.revision,
            "resolution": (None if value.resolution is None else _approval_resolution_to_payload(value.resolution)),
        }

    def decode(value: Mapping[str, Any]) -> Any:
        raw = _exact_object(value, {"request", "state", "revision", "resolution"}, "approval record")
        resolution_raw = raw["resolution"]
        return record_type(
            request=_approval_request_from_payload(raw["request"]),
            state=ApprovalState(_string(raw["state"], "approval record state")),
            revision=_integer(raw["revision"], "approval record revision"),
            resolution=(None if resolution_raw is None else _approval_resolution_from_payload(resolution_raw)),
        )

    return EntityCodec(
        collection="approvals",
        tag="offeragent.approval_record",
        schema_version=3,
        python_type=record_type,
        encode_payload=encode,
        decode_payload=decode,
    )


def core_entity_codec_registry() -> EntityCodecRegistry:
    return EntityCodecRegistry(
        (
            EntityCodec("sessions", "offeragent.session", 1, Session, _session_to_payload, _session_from_payload),
            EntityCodec("turns", "offeragent.turn", 1, Turn, _turn_to_payload, _turn_from_payload),
            EntityCodec("runs", "offeragent.run", 1, Run, _run_to_payload, _run_from_payload),
            EntityCodec(
                "approval_grants",
                "offeragent.approval_grant",
                2,
                ApprovalGrant,
                _approval_grant_to_payload,
                _approval_grant_from_payload,
            ),
            EntityCodec(
                "run_states",
                "offeragent.run_state",
                4,
                RunState,
                _run_state_to_payload,
                _run_state_from_payload,
            ),
        )
    )


__all__ = [
    "EntityCodec",
    "EntityCodecError",
    "EntityCodecRegistry",
    "EntityCodecTypeError",
    "EntityCodecVersionError",
    "approval_record_codec",
    "core_entity_codec_registry",
]
