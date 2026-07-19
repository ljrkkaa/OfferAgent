"""Model-backed Agent loop step with a Harness-owned strict output contract."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import timedelta
from decimal import Decimal
from types import MappingProxyType
from typing import Any, cast

from jsonschema import Draft202012Validator

from offeragent_harness.models import (
    ModelCitation,
    ModelContentBlock,
    ModelContinuation,
    ModelError,
    ModelEventKind,
    ModelFinishReason,
    ModelHostedSearchPhase,
    ModelHostedTool,
    ModelMessage,
    ModelOutputMode,
    ModelPurpose,
    ModelRequest,
    ModelRole,
    ModelUsage,
    TraceContext,
)
from offeragent_harness.models.json_types import FrozenJsonObject, freeze_json, thaw_json
from offeragent_harness.permissions import RiskClass
from offeragent_harness.ports import CancellationToken, Clock, IdGenerator, ModelGateway
from offeragent_harness.tools import (
    CanonicalJsonError,
    SideEffectClass,
    ToolCall,
    ToolDefinition,
    canonical_json_bytes,
    canonical_json_sha256,
)

from .budgets import BudgetDelta, BudgetExceeded, BudgetLedger
from .context_manager import ContextFragment, ContextManager, ContextProjection, ContextWindow
from .planner import PlanningAttempt, PlanningAttemptOutcome, PlanningStep
from .state import RunState


class ModelPlannerError(RuntimeError):
    _planning_attempts: tuple[PlanningAttempt, ...] = ()

    @property
    def planning_attempts(self) -> tuple[PlanningAttempt, ...]:
        return self._planning_attempts

    def with_planning_attempts(self, attempts: tuple[PlanningAttempt, ...]) -> ModelPlannerError:
        self._planning_attempts = attempts
        return self


class AgentStepCatalogError(ModelPlannerError):
    pass


class ModelStreamProtocolError(ModelPlannerError):
    def __init__(self, request_id: str, message: str, *, usage: ModelUsage | None = None) -> None:
        super().__init__(f"model stream {request_id}: {message}")
        self.request_id = request_id
        self.usage = usage


class ModelProviderFailure(ModelPlannerError):
    def __init__(self, request_id: str, error: ModelError, *, usage: ModelUsage | None = None) -> None:
        super().__init__(f"model provider failure {error.code}: {error.message}")
        self.request_id = request_id
        self.error = error
        self.usage = usage


class ModelInvalidOutput(ModelPlannerError):
    def __init__(
        self,
        request_id: str,
        violations: Sequence[str],
        *,
        raw_output: FrozenJsonObject | None,
        usage: ModelUsage,
        repairable: bool = True,
    ) -> None:
        normalized = tuple(violations)
        if not normalized:
            raise ValueError("invalid model output requires at least one violation")
        super().__init__(f"model output {request_id} violated the AgentStep schema: {'; '.join(normalized)}")
        self.request_id = request_id
        self.violations = normalized
        self.raw_output = raw_output
        self.usage = usage
        self.repairable = repairable


class SchemaRepairUnavailable(ModelPlannerError):
    def __init__(self, invalid_output: ModelInvalidOutput, cause: BudgetExceeded) -> None:
        super().__init__(f"schema repair budget unavailable: {cause}")
        self.invalid_output = invalid_output
        self.__cause__ = cause


class SchemaRepairFailed(ModelPlannerError):
    def __init__(self, first: ModelInvalidOutput, second: ModelInvalidOutput) -> None:
        super().__init__("the single permitted AgentStep schema repair also failed")
        self.first = first
        self.second = second


@dataclass(frozen=True, slots=True)
class PlannerModelConfig:
    model: str
    max_output_tokens: int
    model_instructions: str | None = field(default=None, repr=False)
    use_responses_lite: bool = False
    reasoning_effort: str | None = None
    temperature: float | None = 0
    seed: int | None = None
    hosted_tools: tuple[ModelHostedTool, ...] = ()

    def __post_init__(self) -> None:
        if not self.model:
            raise ValueError("planner model must not be empty")
        if self.model_instructions is not None and (
            not self.model_instructions or len(self.model_instructions.encode("utf-8")) > 512 * 1024
        ):
            raise ValueError("planner model instructions exceed their safety limit")
        if not isinstance(self.use_responses_lite, bool):
            raise TypeError("planner use_responses_lite must be a bool")
        if self.max_output_tokens < 1:
            raise ValueError("planner max_output_tokens must be positive")
        if self.temperature is not None and not 0 <= self.temperature <= 2:
            raise ValueError("planner temperature must be between 0 and 2")
        tools = tuple(self.hosted_tools)
        if len(tools) != len(set(tools)) or any(not isinstance(tool, ModelHostedTool) for tool in tools):
            raise ValueError("planner hosted tools must be unique typed values")
        object.__setattr__(self, "hosted_tools", tools)


@dataclass(frozen=True, slots=True)
class StructuredModelResponse:
    request_id: str
    output: FrozenJsonObject
    usage: ModelUsage
    citations: tuple[ModelCitation, ...] = ()
    continuation: ModelContinuation | None = None


@dataclass(frozen=True, slots=True)
class RunCallFence:
    """A catalog-owned dynamic prohibition activated by one prior ToolResult."""

    activation: str
    tool_name: str
    tool_version: str
    argument_name: str
    argument_value: str
    violation: str

    def __post_init__(self) -> None:
        values = (
            self.activation,
            self.tool_name,
            self.tool_version,
            self.argument_name,
            self.argument_value,
            self.violation,
        )
        if any(not value or "\x00" in value or len(value) > 512 for value in values):
            raise ValueError("Run call fence fields must be non-empty bounded strings")

    def matches(self, call: Mapping[str, Any]) -> bool:
        arguments = call.get("arguments")
        return (
            call.get("name") == self.tool_name
            and call.get("version") == self.tool_version
            and isinstance(arguments, Mapping)
            and arguments.get(self.argument_name) == self.argument_value
        )

    def identity(self) -> dict[str, str]:
        return {
            "activation": self.activation,
            "toolName": self.tool_name,
            "toolVersion": self.tool_version,
            "argumentName": self.argument_name,
            "argumentValue": self.argument_value,
            "violation": self.violation,
        }


class AgentStepCatalog:
    """Exact execution authority plus a bounded model-facing AgentStep codec."""

    def __init__(
        self,
        definitions: Sequence[ToolDefinition],
        *,
        max_calls: int,
        run_call_fences: Sequence[RunCallFence] = (),
    ) -> None:
        if max_calls < 1:
            raise ValueError("max_calls must be positive")
        ordered = tuple(sorted(definitions, key=lambda item: (item.name, item.version)))
        keys = [(definition.name, definition.version) for definition in ordered]
        if len(keys) != len(set(keys)):
            raise AgentStepCatalogError("tool catalog contains duplicate name/version entries")
        self._definitions = ordered
        self._by_key = MappingProxyType(dict(zip(keys, ordered, strict=True)))
        fences = tuple(
            sorted(
                run_call_fences,
                key=lambda item: (
                    item.activation,
                    item.tool_name,
                    item.tool_version,
                    item.argument_name,
                    item.argument_value,
                ),
            )
        )
        fence_identities = tuple(tuple(item.identity().items()) for item in fences)
        if len(fence_identities) != len(set(fence_identities)):
            raise AgentStepCatalogError("Run call fences contain duplicate identities")
        if any((fence.tool_name, fence.tool_version) not in self._by_key for fence in fences):
            raise AgentStepCatalogError("Run call fence references an unknown tool/version")
        self._run_call_fences = fences
        self._input_validators = MappingProxyType(
            {
                key: Draft202012Validator(thaw_json(definition.input_schema))
                for key, definition in zip(keys, ordered, strict=True)
            }
        )
        self._max_calls = max_calls
        self._fingerprint = canonical_json_sha256(
            {
                "maxCalls": max_calls,
                "tools": [
                    {
                        "name": definition.name,
                        "version": definition.version,
                        "description": definition.description,
                        "inputSchema": thaw_json(definition.input_schema),
                        "resultSensitivity": definition.result_sensitivity.value,
                    }
                    for definition in ordered
                ],
                "runCallFences": [fence.identity() for fence in fences],
            }
        )
        schema = self._build_schema()
        Draft202012Validator.check_schema(schema)
        frozen = freeze_json(schema)
        if not isinstance(frozen, FrozenJsonObject):
            raise AgentStepCatalogError("generated AgentStep schema must be an object")
        self._schema = frozen
        self._validator = Draft202012Validator(thaw_json(frozen))
        model_schema = self._build_model_schema()
        Draft202012Validator.check_schema(model_schema)
        frozen_model_schema = freeze_json(model_schema)
        if not isinstance(frozen_model_schema, FrozenJsonObject):
            raise AgentStepCatalogError("generated model AgentStep schema must be an object")
        self._model_schema = frozen_model_schema
        self._model_validator = Draft202012Validator(thaw_json(frozen_model_schema))
        directory = {
            "tools": [
                {
                    "name": definition.name,
                    "version": definition.version,
                    "description": definition.description,
                    "inputSchema": thaw_json(definition.input_schema),
                }
                for definition in ordered
            ]
        }
        encoded_directory = canonical_json_bytes(directory).decode("utf-8")
        encoded_fences = canonical_json_bytes({"runCallFences": [fence.identity() for fence in fences]}).decode("utf-8")
        self._model_instruction = (
            "OfferAgent 的模型输出使用紧凑 AgentStep 投影。每个 calls 项必须从下列工具目录选择精确的 "
            "name/version, 并把符合该工具 inputSchema 的单个 JSON 对象编码为 argumentsJson 字符串; "
            "不要把参数对象放在其他字段中。Harness 会在执行前重新解析并按完整目录严格校验。工具目录: "
            f"{encoded_directory}。run_snapshot.activeContexts 激活后的调用栅栏: {encoded_fences}"
        )

    @property
    def definitions(self) -> tuple[ToolDefinition, ...]:
        return self._definitions

    @property
    def schema(self) -> FrozenJsonObject:
        return self._schema

    @property
    def model_schema(self) -> FrozenJsonObject:
        """Bounded schema sent to the model; never an execution authority."""

        return self._model_schema

    @property
    def model_instruction(self) -> str:
        return self._model_instruction

    @property
    def fingerprint(self) -> str:
        return self._fingerprint

    def definition(self, name: str, version: str) -> ToolDefinition:
        try:
            return self._by_key[(name, version)]
        except KeyError:
            raise ModelInvalidOutput(
                "catalog-validation",
                (f"unknown tool/version {name}@{version}",),
                raw_output=None,
                usage=_zero_usage(),
                repairable=False,
            ) from None

    def violations(
        self,
        value: Mapping[str, Any],
        *,
        context_activations: frozenset[str] = frozenset(),
    ) -> tuple[str, ...]:
        errors = sorted(self._validator.iter_errors(value), key=lambda item: tuple(str(part) for part in item.path))
        structural = tuple(f"{_json_path(error.absolute_path)}: {error.message}" for error in errors)
        if structural:
            return structural
        calls = value.get("calls")
        if not isinstance(calls, list):
            raise AssertionError("the validated AgentStep calls field is not an array")
        names = tuple(call.get("name") for call in calls if isinstance(call, Mapping))
        if "skill" in names and any(name != "skill" for name in names):
            return (
                "$.calls: a planning step that invokes a Skill may contain only skill calls; "
                "plan other tools after the Skill body is available",
            )
        if names.count("skill") > 1:
            return (
                "$.calls: a planning step may invoke exactly one Skill so its body is available before "
                "selecting another Skill",
            )
        definitions = tuple(
            self._by_key[(cast(str, call["name"]), cast(str, call["version"]))]
            for call in calls
            if isinstance(call, Mapping)
        )
        if len(definitions) > 1:
            parallel_reads = all(_parallel_safe_read(definition) for definition in definitions)
            serial_idempotent_effects = all(_serial_batch_safe(definition) for definition in definitions)
            if not parallel_reads and not serial_idempotent_effects:
                return (
                    "$.calls: a multi-call AgentStep must contain either independent concurrency-safe reads or "
                    "idempotent effectful calls; never mix reads with effects or batch a non-idempotent tool",
                )
        for call in calls:
            if not isinstance(call, Mapping) or call.get("name") != "skill":
                continue
            arguments = call.get("arguments")
            if isinstance(arguments, Mapping) and f"skill:{arguments.get('name')}" in context_activations:
                return ("$.calls: an already activated Skill cannot be invoked again in the same Run",)
        fenced = tuple(
            f"$.calls: {fence.violation}"
            for fence in self._run_call_fences
            if fence.activation in context_activations and any(fence.matches(call) for call in calls)
        )
        if fenced:
            return fenced
        return ()

    def decode_model_step(
        self,
        value: Mapping[str, Any],
        *,
        context_activations: frozenset[str] = frozenset(),
    ) -> tuple[dict[str, Any] | None, tuple[str, ...]]:
        """Decode the compact model projection and reapply exact local authority."""

        errors = sorted(
            self._model_validator.iter_errors(value),
            key=lambda item: tuple(str(part) for part in item.path),
        )
        structural = tuple(f"{_json_path(error.absolute_path)}: {error.message}" for error in errors)
        if structural:
            return None, structural
        raw_calls = value.get("calls")
        if not isinstance(raw_calls, list):
            raise AssertionError("the validated model AgentStep calls field is not an array")
        calls: list[dict[str, Any]] = []
        decoded_violations: list[str] = []
        for index, raw_call in enumerate(raw_calls):
            if not isinstance(raw_call, Mapping):
                raise AssertionError("the validated model AgentStep call is not an object")
            name = cast(str, raw_call["name"])
            version = cast(str, raw_call["version"])
            key = (name, version)
            validator = self._input_validators.get(key)
            if validator is None:
                decoded_violations.append(f"$.calls.{index}: unknown tool/version {name}@{version}")
                continue
            arguments, decode_error = _decode_arguments_json(cast(str, raw_call["argumentsJson"]))
            if decode_error is not None:
                decoded_violations.append(f"$.calls.{index}.argumentsJson: {decode_error}")
                continue
            assert arguments is not None
            input_errors = sorted(
                validator.iter_errors(arguments),
                key=lambda item: tuple(str(part) for part in item.path),
            )
            decoded_violations.extend(
                f"{_json_path(('calls', index, 'arguments', *error.absolute_path))}: {error.message}"
                for error in input_errors
            )
            calls.append(
                {
                    "name": name,
                    "version": version,
                    "arguments": arguments,
                    "reason": cast(str, raw_call["reason"]),
                }
            )
        if decoded_violations:
            return None, tuple(decoded_violations)
        normalized = {
            "requiresWriteOutcome": value["requiresWriteOutcome"],
            "calls": calls,
            "finalResponse": value["finalResponse"],
        }
        exact = self.violations(normalized, context_activations=context_activations)
        return (normalized if not exact else None), exact

    def _build_schema(self) -> dict[str, Any]:
        definitions: dict[str, Any] = {}
        variants: list[dict[str, Any]] = []
        for index, definition in enumerate(self._definitions):
            schema_key = f"tool_{index:04d}"
            input_schema = cast(dict[str, Any], thaw_json(definition.input_schema))
            # Give each embedded schema its own resource root.  Existing local
            # #/$defs references then remain local instead of resolving against
            # the surrounding AgentStep document.
            input_schema = dict(input_schema)
            input_schema["$id"] = f"urn:offeragent:tool-input:{self._fingerprint[7:]}:{schema_key}"
            definitions[schema_key] = input_schema
            variants.append(
                {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["name", "version", "arguments", "reason"],
                    "properties": {
                        "name": {"const": definition.name},
                        "version": {"const": definition.version},
                        "arguments": {"$ref": f"#/$defs/{schema_key}"},
                        "reason": {"type": "string", "minLength": 1, "maxLength": 4096},
                    },
                    "description": definition.description,
                }
            )
        call_items: bool | dict[str, Any]
        call_items = {"oneOf": variants} if variants else False
        step_constraints = [
            {
                "if": {"properties": {"calls": {"maxItems": 0}}, "required": ["calls"]},
                "then": {"properties": {"finalResponse": {"type": "string", "minLength": 1}}},
                "else": {"properties": {"finalResponse": {"type": "null"}}},
            }
        ]
        return {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": f"urn:offeragent:agent-step:{self._fingerprint[7:]}",
            "type": "object",
            "additionalProperties": False,
            "required": ["requiresWriteOutcome", "calls", "finalResponse"],
            "properties": {
                "requiresWriteOutcome": {"type": "boolean"},
                "calls": {
                    "type": "array",
                    "maxItems": self._max_calls,
                    "items": call_items,
                },
                "finalResponse": {
                    "oneOf": [
                        {"type": "null"},
                        {"type": "string", "minLength": 1, "maxLength": 4096},
                    ]
                },
            },
            "allOf": step_constraints,
            "$defs": definitions,
        }

    def _build_model_schema(self) -> dict[str, Any]:
        call_items: bool | dict[str, Any]
        if not self._definitions:
            call_items = False
        else:
            call_items = {
                "type": "object",
                "additionalProperties": False,
                "required": ["name", "version", "argumentsJson", "reason"],
                "properties": {
                    "name": {
                        "type": "string",
                        "enum": sorted({definition.name for definition in self._definitions}),
                    },
                    "version": {"type": "string", "minLength": 1, "maxLength": 128},
                    "argumentsJson": {"type": "string", "minLength": 2, "maxLength": 131_072},
                    "reason": {"type": "string", "minLength": 1, "maxLength": 4096},
                },
            }
        return {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": f"urn:offeragent:model-agent-step:{self._fingerprint[7:]}",
            "type": "object",
            "additionalProperties": False,
            "required": ["requiresWriteOutcome", "calls", "finalResponse"],
            "properties": {
                "requiresWriteOutcome": {"type": "boolean"},
                "calls": {
                    "type": "array",
                    "maxItems": self._max_calls,
                    "items": call_items,
                },
                "finalResponse": {
                    "oneOf": [
                        {"type": "null"},
                        {"type": "string", "minLength": 1, "maxLength": 4096},
                    ]
                },
            },
            "allOf": [
                {
                    "if": {"properties": {"calls": {"maxItems": 0}}, "required": ["calls"]},
                    "then": {"properties": {"finalResponse": {"type": "string", "minLength": 1}}},
                    "else": {"properties": {"finalResponse": {"type": "null"}}},
                }
            ],
        }


class ModelPlanner:
    """Concrete Planner; the model can propose intent but never safety identity."""

    def __init__(
        self,
        *,
        gateway: ModelGateway,
        context_manager: ContextManager,
        catalog: AgentStepCatalog,
        config: PlannerModelConfig,
        clock: Clock,
        ids: IdGenerator,
        budget: BudgetLedger,
    ) -> None:
        self._gateway = gateway
        self._context_manager = context_manager.with_system_rule(catalog.model_instruction)
        self._catalog = catalog
        self._config = config
        self._clock = clock
        self._ids = ids
        self._budget = budget
        self._hook_context_hints: tuple[str, ...] = ()

    def set_hook_context_hints(self, hints: Sequence[str]) -> None:
        self._hook_context_hints = _validated_hook_hints(hints)

    def add_memory_context(self, fragments: Sequence[ContextFragment]) -> None:
        self._context_manager = self._context_manager.with_memories(fragments)

    def add_conversation_context(self, fragments: Sequence[ContextFragment]) -> None:
        self._context_manager = self._context_manager.with_conversation(fragments)

    def add_skill_context(self, fragments: Sequence[ContextFragment]) -> None:
        self._context_manager = self._context_manager.with_skills(fragments)

    def create_request(
        self,
        state: RunState,
        *,
        repair: ModelInvalidOutput | None = None,
        projection: ContextProjection | None = None,
        retry_of_request_id: str | None = None,
    ) -> ModelRequest:
        window = self._build_window(state, projection=projection)
        window.ensure_model_ready()
        messages = window.messages
        metadata: dict[str, Any] = {
            "workspaceId": state.workspace_id,
            "sessionId": state.session_id,
            "turnId": state.turn_id,
            "runId": state.run_id,
            "toolCatalogHash": self._catalog.fingerprint,
            "schemaRepairAttempt": 0,
            "projection": window.projection.value,
            "projectionHash": window.projection_hash,
            "contextOverflowRetry": retry_of_request_id is not None,
            "omittedContextIds": [item.context_id for item in window.omitted],
        }
        if retry_of_request_id is not None:
            metadata["retryOfRequestId"] = retry_of_request_id
        if repair is not None:
            repair_instruction = ModelMessage(
                ModelRole.SYSTEM,
                (
                    ModelContentBlock.text(
                        "上一次输出未通过 AgentStep contract。严格按照 violations 修复并重新输出一个完整 "
                        "AgentStep; 不得添加 Schema 之外字段, 也不得自报 callId、hash、deadline 或权限。"
                    ),
                ),
                name="offeragent-schema-repair",
            )
            invalid_data = thaw_json(repair.raw_output) if repair.raw_output is not None else None
            prior_output = ModelMessage(
                ModelRole.ASSISTANT,
                (
                    ModelContentBlock(
                        kind="invalid_structured_output",
                        data={
                            "requestId": repair.request_id,
                            "violations": list(repair.violations),
                            "output": invalid_data,
                        },
                    ),
                ),
                name="offeragent-invalid-agent-step",
            )
            system_count = next(
                (index for index, message in enumerate(messages) if message.role is not ModelRole.SYSTEM),
                len(messages),
            )
            messages = (*messages[:system_count], repair_instruction, *messages[system_count:], prior_output)
            metadata["schemaRepairAttempt"] = 1
            metadata["repairOfRequestId"] = repair.request_id
        return ModelRequest(
            request_id=self._ids.new_id("model-request"),
            model=self._config.model,
            purpose=ModelPurpose.PLANNING,
            messages=messages,
            output_mode=ModelOutputMode.JSON,
            # ModelRequest re-validates the schema before freezing it.  Give it
            # a plain recursive JSON tree rather than the catalog's immutable
            # Mapping wrappers so jsonschema's meta-schema type checks see
            # ordinary JSON objects at every level.
            output_schema=cast(dict[str, Any], thaw_json(self._catalog.model_schema)),
            max_output_tokens=self._config.max_output_tokens,
            reasoning_effort=self._config.reasoning_effort,
            temperature=self._config.temperature,
            seed=self._config.seed,
            trace_context=TraceContext(self._ids.new_id("trace")),
            model_instructions=self._config.model_instructions,
            use_responses_lite=self._config.use_responses_lite,
            metadata=metadata,
            hosted_tools=self._config.hosted_tools,
        )

    def _build_window(self, state: RunState, *, projection: ContextProjection | None) -> ContextWindow:
        manager = self._context_manager.with_hook_hints(self._hook_context_hints)
        if projection is not None:
            return manager.build(state, purpose=ModelPurpose.PLANNING, projection=projection)
        normal = manager.build(state, purpose=ModelPurpose.PLANNING, projection=ContextProjection.NORMAL)
        if not normal.compaction_required:
            return normal
        return manager.build(
            state,
            purpose=ModelPurpose.PLANNING,
            projection=ContextProjection.OVERFLOW_REFERENCES,
        )

    async def plan(self, state: RunState, cancellation: CancellationToken) -> PlanningStep:
        try:
            first_step, first_attempts = await self._execute_with_overflow_retry(
                state,
                repair=None,
                start_index=0,
                allow_overflow_retry=True,
                cancellation=cancellation,
            )
            return replace(first_step, attempts=first_attempts)
        except ModelInvalidOutput as first_invalid:
            first_attempts = first_invalid.planning_attempts
            if not first_invalid.repairable:
                raise
            try:
                await self._budget.consume(BudgetDelta(model_rounds=1))
            except BudgetExceeded as exhausted:
                unavailable = SchemaRepairUnavailable(first_invalid, exhausted)
                unavailable.with_planning_attempts(first_attempts)
                raise unavailable from exhausted
            try:
                repair_step, repair_attempts = await self._execute_with_overflow_retry(
                    state,
                    repair=first_invalid,
                    start_index=len(first_attempts),
                    allow_overflow_retry=not any(
                        attempt.error_code == "context_overflow" for attempt in first_attempts
                    ),
                    cancellation=cancellation,
                )
                return replace(repair_step, attempts=(*first_attempts, *repair_attempts))
            except ModelInvalidOutput as second_invalid:
                failed = SchemaRepairFailed(first_invalid, second_invalid)
                repair_attempts = second_invalid.planning_attempts
                combined = (
                    repair_attempts
                    if repair_attempts[: len(first_attempts)] == first_attempts
                    else (*first_attempts, *repair_attempts)
                )
                failed.with_planning_attempts(combined)
                raise failed from second_invalid
            except ModelPlannerError as repair_failure:
                if repair_failure.planning_attempts[: len(first_attempts)] != first_attempts:
                    repair_failure.with_planning_attempts((*first_attempts, *repair_failure.planning_attempts))
                raise

    async def _execute_with_overflow_retry(
        self,
        state: RunState,
        *,
        repair: ModelInvalidOutput | None,
        start_index: int,
        allow_overflow_retry: bool,
        cancellation: CancellationToken,
    ) -> tuple[PlanningStep, tuple[PlanningAttempt, ...]]:
        request = self.create_request(state, repair=repair)
        try:
            step, attempt = await self._execute_planning_attempt(
                state,
                request,
                repair_index=start_index,
                cancellation=cancellation,
            )
            return step, (attempt,)
        except ModelProviderFailure as overflow:
            attempts = overflow.planning_attempts
            metadata = thaw_json(request.metadata)
            if (
                overflow.error.code != "context_overflow"
                or metadata.get("projection") != ContextProjection.NORMAL.value
                or not allow_overflow_retry
            ):
                raise
            try:
                await self._budget.consume(BudgetDelta(model_rounds=1))
            except BudgetExceeded as exhausted:
                raise overflow from exhausted
            retry = self.create_request(
                state,
                repair=repair,
                projection=ContextProjection.OVERFLOW_REFERENCES,
                retry_of_request_id=request.request_id,
            )
            try:
                retry_step, retry_attempt = await self._execute_planning_attempt(
                    state,
                    retry,
                    repair_index=start_index + 1,
                    cancellation=cancellation,
                )
            except ModelPlannerError as retry_failure:
                retry_failure.with_planning_attempts((*attempts, *retry_failure.planning_attempts))
                raise
            return retry_step, (*attempts, retry_attempt)

    async def _execute_planning_attempt(
        self,
        state: RunState,
        request: ModelRequest,
        *,
        repair_index: int,
        cancellation: CancellationToken,
    ) -> tuple[PlanningStep, PlanningAttempt]:
        try:
            response = await self._invoke_and_charge(request, cancellation)
            step = self._to_planning_step(state, response)
        except ModelPlannerError as error:
            attempt = _failed_attempt(error, request, repair_index)
            error.with_planning_attempts((attempt,))
            raise
        return (
            step,
            PlanningAttempt(
                request_id=request.request_id,
                repair_index=repair_index,
                outcome=PlanningAttemptOutcome.SUCCEEDED,
                usage=response.usage,
                **_planning_attempt_metadata(request),
            ),
        )

    async def _invoke_and_charge(
        self,
        request: ModelRequest,
        cancellation: CancellationToken,
    ) -> StructuredModelResponse:
        try:
            response = await collect_structured_response(self._gateway, request, cancellation)
        except (ModelStreamProtocolError, ModelProviderFailure, ModelInvalidOutput) as error:
            if error.usage is not None:
                await self._charge_usage(error.usage)
            raise
        await self._charge_usage(response.usage)
        return response

    async def _charge_usage(self, usage: ModelUsage) -> None:
        await self._budget.consume(
            BudgetDelta(
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cost=usage.cost or Decimal("0"),
            )
        )

    def _to_planning_step(self, state: RunState, response: StructuredModelResponse) -> PlanningStep:
        projected = cast(dict[str, Any], thaw_json(response.output))
        activations = frozenset(
            activation for result in state.tool_results for activation in result.context_activations
        )
        raw, violations = self._catalog.decode_model_step(projected, context_activations=activations)
        if violations:
            raise ModelInvalidOutput(
                response.request_id,
                violations,
                raw_output=response.output,
                usage=response.usage,
            )
        assert raw is not None
        raw_calls = cast(list[dict[str, Any]], raw["calls"])
        calls: list[ToolCall] = []
        now = self._clock.utcnow()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("Clock.utcnow() must return a timezone-aware datetime")
        for raw_call in raw_calls:
            name = cast(str, raw_call["name"])
            version = cast(str, raw_call["version"])
            definition = self._catalog.definition(name, version)
            arguments = cast(dict[str, Any], raw_call["arguments"])
            # The model output schema deliberately contains none of these fields.
            calls.append(
                ToolCall(
                    tool_call_id=self._ids.new_id("call"),
                    run_id=state.run_id,
                    workspace_id=state.workspace_id,
                    name=name,
                    version=version,
                    arguments=arguments,
                    args_hash=canonical_json_sha256(arguments),
                    idempotency_key=self._ids.new_id("idempotency"),
                    deadline=now + timedelta(milliseconds=definition.timeout_ms),
                    lineage=state.lineage,
                    definition_fingerprint=definition.fingerprint,
                    result_sensitivity=definition.result_sensitivity,
                )
            )
        return PlanningStep(
            calls=tuple(calls),
            requires_write_outcome=cast(bool, raw["requiresWriteOutcome"]),
            final_response=cast(str | None, raw["finalResponse"]),
            citations=response.citations,
            agent_step_id=(self._ids.new_id("agent-step") if response.continuation is not None else None),
            continuation=response.continuation,
        )


def _decode_arguments_json(value: str) -> tuple[dict[str, Any] | None, str | None]:
    try:
        decoded = json.loads(
            value,
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except (json.JSONDecodeError, UnicodeError, ValueError, RecursionError) as error:
        return None, f"must contain strict JSON ({error})"
    if not isinstance(decoded, dict):
        return None, "must decode to a JSON object"
    try:
        canonical_json_bytes(decoded)
    except CanonicalJsonError as error:
        return None, f"must contain canonical-hash-compatible JSON ({error})"
    return decoded, None


def _unique_json_object(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate object key {key!r}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant {value!r}")


def _validated_hook_hints(hints: Sequence[str]) -> tuple[str, ...]:
    values = tuple(dict.fromkeys(hints))
    if len(values) > 16 or any(not isinstance(value, str) or not value or len(value) > 4096 for value in values):
        raise ValueError("Hook context hints exceed their count or size limit")
    return values


def _failed_attempt(error: ModelPlannerError, request: ModelRequest, repair_index: int) -> PlanningAttempt:
    request_id = request.request_id
    metadata = _planning_attempt_metadata(request)
    if isinstance(error, ModelInvalidOutput):
        return PlanningAttempt(
            request_id=request_id,
            repair_index=repair_index,
            outcome=PlanningAttemptOutcome.INVALID,
            usage=error.usage,
            error_code="invalid_agent_step",
            violations=error.violations,
            **metadata,
        )
    if isinstance(error, ModelProviderFailure):
        return PlanningAttempt(
            request_id=request_id,
            repair_index=repair_index,
            outcome=PlanningAttemptOutcome.FAILED,
            usage=error.usage,
            error_code=error.error.code,
            **metadata,
        )
    if isinstance(error, ModelStreamProtocolError):
        return PlanningAttempt(
            request_id=request_id,
            repair_index=repair_index,
            outcome=PlanningAttemptOutcome.FAILED,
            usage=error.usage,
            error_code="model_stream_protocol",
            **metadata,
        )
    return PlanningAttempt(
        request_id=request_id,
        repair_index=repair_index,
        outcome=PlanningAttemptOutcome.FAILED,
        usage=None,
        error_code=type(error).__name__,
        **metadata,
    )


def _planning_attempt_metadata(request: ModelRequest) -> dict[str, Any]:
    metadata = thaw_json(request.metadata)
    projection = metadata.get("projection")
    projection_hash = metadata.get("projectionHash")
    retry_of_request_id = metadata.get("retryOfRequestId")
    omitted = metadata.get("omittedContextIds", [])
    return {
        "retry_of_request_id": retry_of_request_id if isinstance(retry_of_request_id, str) else None,
        "projection": projection if isinstance(projection, str) else None,
        "projection_hash": projection_hash if isinstance(projection_hash, str) else None,
        "omitted_context_ids": (
            tuple(item for item in omitted if isinstance(item, str)) if isinstance(omitted, list) else ()
        ),
    }


async def collect_structured_response(
    gateway: ModelGateway,
    request: ModelRequest,
    cancellation: CancellationToken,
) -> StructuredModelResponse:
    """Consume one strict JSON model stream without interpreting its schema."""

    expected_sequence = 1
    started = False
    completed = False
    output: FrozenJsonObject | None = None
    usage: ModelUsage | None = None
    search_phases: dict[str, ModelHostedSearchPhase] = {}
    citations: list[ModelCitation] = []
    continuation: ModelContinuation | None = None
    async for event in gateway.stream(request, cancellation):
        cancellation.checkpoint()
        if completed:
            raise ModelStreamProtocolError(request.request_id, "received an event after completed", usage=usage)
        if event.request_id != request.request_id:
            raise ModelStreamProtocolError(
                request.request_id,
                f"event request_id mismatch: {event.request_id}",
                usage=usage,
            )
        if event.sequence != expected_sequence:
            raise ModelStreamProtocolError(
                request.request_id,
                f"expected sequence {expected_sequence}, received {event.sequence}",
                usage=usage,
            )
        expected_sequence += 1
        if not started and event.kind is not ModelEventKind.STARTED:
            raise ModelStreamProtocolError(request.request_id, "first event must be started", usage=usage)
        if event.kind is ModelEventKind.STARTED:
            if started:
                raise ModelStreamProtocolError(request.request_id, "duplicate started event", usage=usage)
            started = True
        elif event.kind is ModelEventKind.STRUCTURED_OUTPUT:
            if output is not None:
                raise ModelStreamProtocolError(request.request_id, "duplicate structured output", usage=usage)
            if not isinstance(event.data, Mapping):
                raise ModelStreamProtocolError(request.request_id, "structured output must be an object", usage=usage)
            frozen = freeze_json(event.data)
            if not isinstance(frozen, FrozenJsonObject):
                raise ModelStreamProtocolError(request.request_id, "structured output must be an object", usage=usage)
            output = frozen
        elif event.kind is ModelEventKind.USAGE:
            assert event.usage is not None
            validate_usage_progression(usage, event.usage, request.request_id)
            usage = event.usage
        elif event.kind is ModelEventKind.REASONING_SUMMARY:
            if not event.text:
                raise ModelStreamProtocolError(request.request_id, "empty reasoning summary", usage=usage)
        elif event.kind is ModelEventKind.HOSTED_SEARCH:
            if ModelHostedTool.WEB_SEARCH not in request.hosted_tools:
                raise ModelStreamProtocolError(request.request_id, "undeclared hosted search event", usage=usage)
            assert event.hosted_search is not None
            call_id = event.hosted_search.call_id
            phase = event.hosted_search.phase
            previous = search_phases.get(call_id)
            if phase is ModelHostedSearchPhase.STARTED:
                if previous is not None or len(search_phases) >= 16:
                    raise ModelStreamProtocolError(request.request_id, "invalid hosted search start", usage=usage)
            else:
                allowed = {
                    ModelHostedSearchPhase.STARTED: {
                        ModelHostedSearchPhase.IN_PROGRESS,
                        ModelHostedSearchPhase.SEARCHING,
                        ModelHostedSearchPhase.COMPLETED,
                    },
                    ModelHostedSearchPhase.IN_PROGRESS: {
                        ModelHostedSearchPhase.SEARCHING,
                        ModelHostedSearchPhase.COMPLETED,
                    },
                    ModelHostedSearchPhase.SEARCHING: {ModelHostedSearchPhase.COMPLETED},
                }
                if previous is None or phase not in allowed.get(previous, set()):
                    raise ModelStreamProtocolError(request.request_id, "invalid hosted search phase", usage=usage)
            search_phases[call_id] = phase
        elif event.kind is ModelEventKind.CITATION:
            if ModelHostedTool.WEB_SEARCH not in request.hosted_tools:
                raise ModelStreamProtocolError(request.request_id, "undeclared hosted citation event", usage=usage)
            if not any(phase is ModelHostedSearchPhase.COMPLETED for phase in search_phases.values()):
                raise ModelStreamProtocolError(request.request_id, "citation arrived before hosted search", usage=usage)
            assert event.citation is not None
            if event.citation.request_id != request.request_id or event.citation.model != request.model:
                raise ModelStreamProtocolError(request.request_id, "hosted citation identity mismatch", usage=usage)
            if event.citation not in citations:
                if len(citations) >= 256:
                    raise ModelStreamProtocolError(
                        request.request_id, "hosted citations exceed their limit", usage=usage
                    )
                citations.append(event.citation)
        elif event.kind is ModelEventKind.COMPLETED:
            assert event.finish_reason is not None
            if event.usage is not None:
                validate_usage_progression(usage, event.usage, request.request_id)
                usage = event.usage
            if event.finish_reason is not ModelFinishReason.STOP:
                if usage is None:
                    raise ModelStreamProtocolError(request.request_id, "completed without usage")
                raw = output
                raise ModelInvalidOutput(
                    request.request_id,
                    (f"model finish reason was {event.finish_reason.value}, expected stop",),
                    raw_output=raw,
                    usage=usage,
                )
            if event.continuation is not None:
                if continuation is not None:
                    raise ModelStreamProtocolError(request.request_id, "duplicate model continuation", usage=usage)
                continuation = event.continuation
            if any(phase is not ModelHostedSearchPhase.COMPLETED for phase in search_phases.values()):
                raise ModelStreamProtocolError(
                    request.request_id, "completed with unfinished hosted search", usage=usage
                )
            completed = True
        elif event.kind is ModelEventKind.ERROR:
            assert event.error is not None
            raise ModelProviderFailure(request.request_id, event.error, usage=usage)
        elif event.kind is ModelEventKind.CANCELLED:
            raise ModelProviderFailure(
                request.request_id,
                ModelError("provider_cancelled", "model provider cancelled the request", False, True),
                usage=usage,
            )
        elif event.kind is ModelEventKind.TEXT_DELTA:
            raise ModelStreamProtocolError(request.request_id, "text delta is invalid in JSON output mode", usage=usage)
        cancellation.checkpoint()
    if not started:
        raise ModelStreamProtocolError(request.request_id, "stream ended before started", usage=usage)
    if not completed:
        raise ModelStreamProtocolError(request.request_id, "stream ended before completed", usage=usage)
    if usage is None:
        raise ModelStreamProtocolError(request.request_id, "completed stream omitted usage")
    if output is None:
        raise ModelInvalidOutput(
            request.request_id,
            ("completed stream omitted structured output",),
            raw_output=None,
            usage=usage,
        )
    return StructuredModelResponse(request.request_id, output, usage, tuple(citations), continuation)


def validate_usage_progression(previous: ModelUsage | None, current: ModelUsage, request_id: str) -> None:
    if previous is None:
        return
    numeric = (
        ("input_tokens", previous.input_tokens, current.input_tokens),
        ("output_tokens", previous.output_tokens, current.output_tokens),
        ("cached_input_tokens", previous.cached_input_tokens, current.cached_input_tokens),
        ("reasoning_tokens", previous.reasoning_tokens, current.reasoning_tokens),
    )
    if any(after < before for _, before, after in numeric):
        raise ModelStreamProtocolError(request_id, "usage snapshots must be monotonic", usage=previous)
    if previous.currency != current.currency:
        raise ModelStreamProtocolError(request_id, "usage currency changed within one request", usage=previous)
    if previous.cost is not None and current.cost is not None and current.cost < previous.cost:
        raise ModelStreamProtocolError(request_id, "usage cost must be monotonic", usage=previous)


def _json_path(path: Sequence[object]) -> str:
    parts = [str(part) for part in path]
    return "$" if not parts else "$." + ".".join(parts)


def _zero_usage() -> ModelUsage:
    return ModelUsage(0, 0, 0, 0)


def _parallel_safe_read(definition: ToolDefinition) -> bool:
    return (
        definition.risk is RiskClass.READ
        and definition.side_effect_class in {SideEffectClass.NONE, SideEffectClass.READ}
        and definition.concurrency_safe
    )


def _serial_batch_safe(definition: ToolDefinition) -> bool:
    return definition.idempotent and definition.side_effect_class not in {
        SideEffectClass.NONE,
        SideEffectClass.READ,
        SideEffectClass.UNKNOWN,
    }


__all__ = [
    "AgentStepCatalog",
    "AgentStepCatalogError",
    "ModelInvalidOutput",
    "ModelPlanner",
    "ModelPlannerError",
    "ModelProviderFailure",
    "ModelStreamProtocolError",
    "PlannerModelConfig",
    "RunCallFence",
    "SchemaRepairFailed",
    "SchemaRepairUnavailable",
    "StructuredModelResponse",
    "collect_structured_response",
    "validate_usage_progression",
]
