from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

import pytest

from offeragent_harness.models import thaw_json
from offeragent_harness.permissions import (
    CapabilityScope,
    PermissionMode,
    PolicyContext,
    PolicyDisposition,
    RiskClass,
)
from offeragent_harness.permissions.audit import NullPolicyAuditSink
from offeragent_harness.permissions.evaluator import RuleBasedPolicyEvaluator
from offeragent_harness.ports import (
    ProcessOutputEncoding,
    SupervisedProcessRequest,
    SupervisedProcessResult,
)
from offeragent_harness.sessions import AgentLineage
from offeragent_harness.shell import ShellCommandProfile, ShellToolExecutor
from offeragent_harness.testing import ManualCancellationToken, ManualClock
from offeragent_harness.tools import SideEffectClass, ToolCall, ToolResultStatus, canonical_json_sha256
from offeragent_harness.tools.recovery_contract import is_safe_crash_replay

NOW = datetime(2026, 7, 13, tzinfo=timezone.utc)


class ProcessFake:
    def __init__(self, result: SupervisedProcessResult) -> None:
        self.result = result
        self.requests: list[SupervisedProcessRequest] = []

    async def execute(self, request: SupervisedProcessRequest, cancellation: Any) -> SupervisedProcessResult:
        cancellation.checkpoint()
        self.requests.append(request)
        return self.result

    async def shutdown(self) -> None:
        return None


class ArtifactReservationFake:
    async def commit(self) -> None:
        return None

    async def release(self) -> None:
        return None


class ArtifactBudgetFake:
    async def reserve_artifact_bytes(self, byte_length: int) -> ArtifactReservationFake:
        assert byte_length >= 0
        return ArtifactReservationFake()


def _profile(*, risk: RiskClass = RiskClass.READ) -> ShellCommandProfile:
    return ShellCommandProfile(
        profile_id="git_status",
        description="读取固定 Git 工作树状态。",
        executable_id="git-status",
        executable_profile_fingerprint="sha256:" + "a" * 64,
        fixed_arguments=("status", "--porcelain=v1"),
        risk=risk,
        side_effect_class=SideEffectClass.READ if risk is RiskClass.READ else SideEffectClass.EXECUTE,
        minimum_variable_arguments=0,
        maximum_variable_arguments=0,
        timeout_ms=2_000,
        inline_output_limit_bytes=1_024,
        artifact_output_limit_bytes=4_096,
        concurrency_safe=risk is RiskClass.READ,
        idempotent=risk is RiskClass.READ,
        retryable=risk is RiskClass.READ,
    )


def _call(profile: ShellCommandProfile) -> ToolCall:
    arguments = {
        "argv": ["status", "--porcelain=v1"],
        "cwd": {"rootId": "vault", "path": ""},
        "environment": {"profileId": "minimal"},
        "timeoutMs": 1_000,
    }
    definition = profile.definition
    return ToolCall(
        tool_call_id="call-shell-1",
        run_id="run-1",
        workspace_id="workspace-1",
        name=definition.name,
        version=definition.version,
        arguments=arguments,
        args_hash=canonical_json_sha256(arguments),
        idempotency_key="shell-idem-1",
        deadline=None,
        lineage=AgentLineage.root("run-1"),
        definition_fingerprint=definition.fingerprint,
        result_sensitivity=definition.result_sensitivity,
    )


@pytest.mark.asyncio
async def test_shell_tool_exposes_only_bound_structured_capabilities_and_sanitizes_output() -> None:
    profile = _profile()
    processes = ProcessFake(
        SupervisedProcessResult(
            0,
            b"\x1b[31mclean\x1b[0m\x00",
            b"\xff",
            stdout_encoding=ProcessOutputEncoding.UTF8,
            stderr_encoding=ProcessOutputEncoding.BINARY,
            stdout_artifact_id="artifact-stdout",
            stdout_total_bytes=100,
        )
    )
    budget = ArtifactBudgetFake()
    executor = ShellToolExecutor(
        (profile,),
        processes=processes,
        clock=ManualClock(NOW),
        artifact_budget=budget,
    )
    definition = executor.definitions[0]

    assert set(definition.input_schema["properties"]) == {"argv", "cwd", "environment", "timeoutMs"}
    assert "command" not in definition.input_schema["properties"]
    assert "executable" not in definition.input_schema["properties"]
    result = await executor.execute(_call(profile), ManualCancellationToken())

    assert result.status is ToolResultStatus.SUCCEEDED
    assert result.artifact_ids == ("artifact-stdout",)
    data = thaw_json(result.data)
    assert isinstance(data, Mapping)
    stdout = data["stdout"]
    stderr = data["stderr"]
    assert isinstance(stdout, Mapping) and stdout["preview"] == "clean�"
    assert isinstance(stderr, Mapping) and stderr["encoding"] == "base64"
    request = processes.requests[0]
    assert request.arguments == ("status", "--porcelain=v1")
    assert request.stdin == b""
    assert request.environment == {}
    assert request.allow_artifact_spill
    assert request.executable_profile_fingerprint == profile.executable_profile_fingerprint
    assert request.artifact_budget is budget


@pytest.mark.asyncio
async def test_nonzero_exit_is_typed_tool_failure_not_runtime_exception() -> None:
    profile = _profile()
    executor = ShellToolExecutor(
        (profile,),
        processes=ProcessFake(SupervisedProcessResult(7, b"", b"failed")),
        clock=ManualClock(NOW),
        artifact_budget=ArtifactBudgetFake(),
    )

    result = await executor.execute(_call(profile), ManualCancellationToken())

    assert result.status is ToolResultStatus.FAILED
    assert result.error is not None and result.error.code == "shell_nonzero_exit"
    data = thaw_json(result.data)
    assert isinstance(data, Mapping) and data["exitCode"] == 7


def test_profile_schema_rejects_model_controlled_metacharacters() -> None:
    profile = ShellCommandProfile(
        profile_id="git_show",
        description="读取固定对象。",
        executable_id="git-show",
        executable_profile_fingerprint="sha256:" + "b" * 64,
        fixed_arguments=("show",),
        risk=RiskClass.READ,
        side_effect_class=SideEffectClass.READ,
        maximum_variable_arguments=1,
    )
    validator = __import__("jsonschema").Draft202012Validator(profile.input_schema)
    unsafe = {
        "argv": ["show", "HEAD;calc.exe"],
        "cwd": {"rootId": "vault", "path": ""},
        "environment": {"profileId": "minimal"},
        "timeoutMs": 1_000,
    }
    assert not validator.is_valid(unsafe)


@pytest.mark.asyncio
async def test_even_read_only_process_profiles_are_denied_in_untrusted_workspace() -> None:
    profile = _profile()
    definition = profile.definition
    call = _call(profile)
    scope = CapabilityScope(
        allowed_tools=frozenset({definition.name}),
        denied_tools=frozenset(),
        allowed_risks=frozenset(RiskClass),
        root_capabilities=definition.required_capabilities,
        allow_network=False,
        allow_secret_handles=False,
    )
    context = PolicyContext(
        workspace_id="workspace-1",
        session_id="session-1",
        principal_id="principal-1",
        run_id="run-1",
        permission_mode=PermissionMode.NORMAL,
        effective_scope=scope,
        workspace_trusted=False,
        now=NOW,
    )
    evaluator = RuleBasedPolicyEvaluator(audit_sink=NullPolicyAuditSink())

    decision = await evaluator.evaluate(definition, call, context)

    assert decision.disposition is PolicyDisposition.DENY
    assert decision.reason_code == "workspace_untrusted"


@pytest.mark.asyncio
async def test_execute_profile_requires_hash_bound_approval() -> None:
    profile = _profile(risk=RiskClass.EXECUTE)
    definition = profile.definition
    call = _call(profile)
    context = PolicyContext(
        workspace_id="workspace-1",
        session_id="session-1",
        principal_id="principal-1",
        run_id="run-1",
        permission_mode=PermissionMode.NORMAL,
        effective_scope=CapabilityScope(
            allowed_tools=frozenset({definition.name}),
            denied_tools=frozenset(),
            allowed_risks=frozenset(RiskClass),
            root_capabilities=definition.required_capabilities,
            allow_network=False,
            allow_secret_handles=False,
        ),
        workspace_trusted=True,
        now=NOW,
    )
    decision = await RuleBasedPolicyEvaluator(audit_sink=NullPolicyAuditSink()).evaluate(definition, call, context)

    assert decision.disposition is PolicyDisposition.ASK
    assert decision.approval_binding is not None
    assert decision.approval_binding.args_hash == call.args_hash
    assert decision.approval_binding.definition_fingerprint == definition.fingerprint
    assert not definition.idempotent
    assert not definition.retryable
    assert not is_safe_crash_replay(definition)
