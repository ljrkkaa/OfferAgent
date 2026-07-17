from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, cast

from offeragent_harness.config import HarnessConfig
from offeragent_harness.models import (
    ModelEvent,
    ModelEventKind,
    ModelFinishReason,
    ModelPurpose,
    ModelRequest,
    ModelRole,
    ModelUsage,
)
from offeragent_harness.permissions import ApprovalRequest
from offeragent_harness.ports import ApplicationCommandContext, CancellationToken
from offeragent_harness.ports.worker_runtime import WorkerApplication, WorkerBootstrap, WorkerCompositionRoot
from offeragent_harness.protocol._base import WireModel
from offeragent_harness.runtime.production_worker_composition import (
    ProductionWorkerApplication,
    ProductionWorkerCompositionRoot,
    ProductionWorkerOverrides,
)
from offeragent_harness.runtime.worker_entrypoint import WorkerEntrypoint
from offeragent_harness.testing import ManualCancellationToken
from offeragent_harness.vault import VaultCasBarrier, content_hash
from offeragent_harness.workspace import identify_workspace_root
from offeragent_harness.workspace.runtime_identity import workspace_database_identity

CRASH_EXIT = 73
NO_CRASH_EXIT = 74
WORKSPACE_INSTANCE_ID = "wsi_5814036c-4192-49ea-9e75-b458bd7a53aa"
BEFORE_CONTENT = b"BEFORE_PAYLOAD\n"
AFTER_CONTENT = b"BEFORE_PAYLOAD\nAFTER_PAYLOAD\n"


def _ripgrep_executable() -> Path:
    executable = shutil.which("rg.exe")
    if executable is None:
        raise RuntimeError("ripgrep is required for the production Worker integration fixture")
    return Path(executable).resolve(strict=True)


def _powershell_executable() -> Path:
    executable = shutil.which("powershell.exe")
    if executable is None:
        raise RuntimeError("PowerShell is required for the production Worker integration fixture")
    return Path(executable).resolve(strict=True)


class _CrashRecoveryModel:
    def __init__(self) -> None:
        self.planning_requests = 0
        self.planning_requests_with_tool_result = 0

    async def stream(
        self,
        request: ModelRequest,
        cancellation: CancellationToken,
    ) -> AsyncIterator[ModelEvent]:
        cancellation.checkpoint()
        yield ModelEvent(request.request_id, 1, ModelEventKind.STARTED)
        sequence = 2
        if request.purpose is ModelPurpose.PLANNING:
            self.planning_requests += 1
            has_tool_result = any(message.role is ModelRole.TOOL for message in request.messages)
            if has_tool_result:
                self.planning_requests_with_tool_result += 1
                output: dict[str, Any] = {
                    "requiresWriteOutcome": True,
                    "calls": [],
                    "finalResponse": "已依据持久化的工具结果完成恢复。",
                }
            else:
                arguments = {
                    "operations": [
                        {
                            "op": "append",
                            "path": "note.md",
                            "content": "AFTER_PAYLOAD\n",
                            "expectedHash": content_hash(BEFORE_CONTENT),
                        }
                    ]
                }
                output = {
                    "requiresWriteOutcome": True,
                    "calls": [
                        {
                            "name": "vault.transaction",
                            "version": "1",
                            "arguments": arguments,
                            "reason": "执行用户明确要求的单文件追加。",
                        }
                    ],
                    "finalResponse": None,
                }
            yield ModelEvent(request.request_id, sequence, ModelEventKind.STRUCTURED_OUTPUT, data=output)
            sequence += 1
        elif request.purpose is not ModelPurpose.GROUNDING:
            raise AssertionError(f"unexpected model purpose: {request.purpose}")
        yield ModelEvent(request.request_id, sequence, ModelEventKind.USAGE, usage=ModelUsage(8, 4, 0, 0))
        yield ModelEvent(
            request.request_id,
            sequence + 1,
            ModelEventKind.COMPLETED,
            finish_reason=ModelFinishReason.STOP,
        )


class _CrashAndRecoveryBarrier:
    def __init__(self, crash_stage: str | None) -> None:
        self._crash_stage = crash_stage
        self.application: ProductionWorkerApplication | None = None
        self.recovery_observations: list[dict[str, object]] = []

    def __call__(self, stage: str, relative_path: str) -> None:
        if stage == self._crash_stage:
            os._exit(CRASH_EXIT)
        if not stage.startswith("recovery_"):
            return
        application = self.application
        if application is None:
            raise RuntimeError("production application was not bound before Vault recovery")
        self.recovery_observations.append(
            {
                "stage": stage,
                "path": relative_path,
                "applicationReady": application.ready,
                "harnessReady": application.harness_application.ready,
            }
        )


class _ObservedCompositionRoot(WorkerCompositionRoot):
    def __init__(self, delegate: ProductionWorkerCompositionRoot, barrier: _CrashAndRecoveryBarrier) -> None:
        self._delegate = delegate
        self._barrier = barrier

    def build(self, bootstrap: WorkerBootstrap) -> WorkerApplication:
        application = self._delegate.build(bootstrap)
        if not isinstance(application, ProductionWorkerApplication):
            raise TypeError("production composition did not build ProductionWorkerApplication")
        self._barrier.application = application
        return application


def _composition(root: Path, barrier: _CrashAndRecoveryBarrier, model: _CrashRecoveryModel) -> WorkerEntrypoint:
    vault = root / "vault"
    runtime_config = HarnessConfig.model_validate(
        {
            "policy": {
                "workspace_trusted": True,
                "read_only": False,
            }
        }
    )
    composition = ProductionWorkerCompositionRoot(
        canonical_root_identity=identify_workspace_root(vault).identity_hash,
        database_identity=workspace_database_identity(WORKSPACE_INSTANCE_ID),
        runtime_version="1.2.3",
        build_commit="abcdef0",
        overrides=ProductionWorkerOverrides(
            model_gateway_factory=lambda _settings: model,
            runtime_config=runtime_config,
            vault_cas_barrier=cast(VaultCasBarrier, barrier),
            ripgrep_path=_ripgrep_executable(),
            powershell_path=_powershell_executable(),
        ),
    )
    return WorkerEntrypoint(_ObservedCompositionRoot(composition, barrier))


async def _dispatch(
    application: ProductionWorkerApplication,
    method: str,
    params: dict[str, object],
    context: ApplicationCommandContext,
) -> dict[str, Any]:
    result = await application.dispatcher.dispatch(
        method,
        params,
        ManualCancellationToken(),
        context=context,
    )
    if isinstance(result, WireModel):
        return result.to_wire()
    if not isinstance(result, dict):
        raise TypeError(f"unexpected {method} result: {type(result).__name__}")
    return cast(dict[str, Any], result)


async def _crash(root: Path, stage: str) -> int:
    barrier = _CrashAndRecoveryBarrier(stage)
    model = _CrashRecoveryModel()
    entrypoint = _composition(root, barrier, model)
    application = cast(
        ProductionWorkerApplication,
        await entrypoint.start(WorkerBootstrap(WORKSPACE_INSTANCE_ID, root / "vault", root / "state")),
    )
    context = ApplicationCommandContext(
        transport="loopback-http",
        client_id="web-production-crash",
        peer="127.0.0.1",
    )
    try:
        created = await _dispatch(
            application,
            "session/create",
            {"title": "Production crash recovery", "clientRequestId": "req_production_crash_session"},
            context,
        )
        started = await _dispatch(
            application,
            "turn/start",
            {
                "sessionId": created["session"]["sessionId"],
                "turnId": "turn_production_crash",
                "idempotencyKey": "production-crash-turn",
                "input": [{"type": "text", "text": "在 note.md 末尾追加指定内容。"}],
                "runConfig": {"provider": "codex", "model": "fake", "permissionMode": "normal"},
            },
            context,
        )
        session_id = cast(str, created["session"]["sessionId"])
        pending: tuple[ApprovalRequest, ...] = ()
        for _ in range(1_000):
            pending = tuple(
                request
                for request in await application.approvals.list_pending(
                    workspace_id=application.workspace_id,
                    session_id=session_id,
                    principal_id="profile_local",
                )
                if request.binding.tool_name == "vault.transaction"
            )
            if pending:
                break
            run = await application.harness.get_run(cast(str, started["runId"]))
            if run.status.is_terminal:
                raise RuntimeError(f"Run terminated before Vault approval: {run.status.value}")
            await asyncio.sleep(0.01)
        if len(pending) != 1:
            raise RuntimeError("real Tool Kernel did not persist one Vault approval")
        resolved = await _dispatch(
            application,
            "approval/resolve",
            {
                "approvalId": pending[0].approval_id,
                "decision": "allow_once",
                "scope": "once",
                "expectedArgsHash": pending[0].binding.args_hash,
                "comment": "production process-crash recovery integration",
            },
            context,
        )
        if resolved["status"] != "approved" or resolved["resumed"] is not True:
            raise RuntimeError("Vault approval did not resume the real Run")
        await asyncio.sleep(15)
        return NO_CRASH_EXIT
    finally:
        await entrypoint.shutdown()


async def _wait_recovered_runs(application: ProductionWorkerApplication) -> list[str]:
    report = application.harness_application.startup_report
    if report is None:
        raise RuntimeError("production Harness startup report is missing")
    statuses: list[str] = []
    for active in report.active_runs:
        for _ in range(1_000):
            run = await application.harness.get_run(active.run_id)
            if run.status.is_terminal:
                statuses.append(run.status.value)
                break
            await asyncio.sleep(0.01)
        else:
            raise RuntimeError(f"recovered Run did not terminate: {active.run_id}")
    return statuses


async def _recover(root: Path) -> int:
    barrier = _CrashAndRecoveryBarrier(None)
    model = _CrashRecoveryModel()
    entrypoint = _composition(root, barrier, model)
    application = cast(
        ProductionWorkerApplication,
        await entrypoint.start(WorkerBootstrap(WORKSPACE_INSTANCE_ID, root / "vault", root / "state")),
    )
    try:
        statuses = await _wait_recovered_runs(application)
        report = application.harness_application.startup_report
        if report is None:
            raise RuntimeError("production Harness startup report is missing")
        result = {
            "readyBeforeShutdown": application.ready,
            "harnessReadyBeforeShutdown": application.harness_application.ready,
            "recoveryObservations": barrier.recovery_observations,
            "plansScanned": report.plans_scanned,
            "resumedRunCount": len(report.active_runs),
            "terminalizedRunCount": len(report.terminalized_run_ids),
            "recoveredRunStatuses": statuses,
            "modelPlanningRequests": model.planning_requests,
            "modelPlanningRequestsWithToolResult": model.planning_requests_with_tool_result,
        }
    finally:
        await entrypoint.shutdown()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("crash", "recover"))
    parser.add_argument("root", type=Path)
    parser.add_argument("stage", nargs="?", default="none")
    arguments = parser.parse_args()
    if arguments.action == "crash":
        if arguments.stage not in {"published", "manifest_committed"}:
            raise ValueError("crash action requires a supported CAS stage")
        return asyncio.run(_crash(arguments.root, arguments.stage))
    if arguments.stage != "none":
        raise ValueError("recover action does not accept a CAS stage")
    return asyncio.run(_recover(arguments.root))


if __name__ == "__main__":
    raise SystemExit(main())
