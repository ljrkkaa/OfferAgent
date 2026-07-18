from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import shutil
from collections.abc import AsyncIterator, Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType
from typing import Any, cast

from offeragent_harness.adapters.sqlite_stores import SqliteUnitOfWorkFactory
from offeragent_harness.agent import BudgetCheckpoint, BudgetDelta, RunBudget
from offeragent_harness.agent.state import RunPhase, RunState
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
from offeragent_harness.providers import (
    CODEX_SUBSCRIPTION_PROVIDER_ID,
    CodexCatalogHttpRequest,
    CodexCatalogHttpResponse,
    ModelCredentialLease,
    ModelCredentialSourceError,
)
from offeragent_harness.runtime.plugin_tools import plugin_tool_definitions
from offeragent_harness.runtime.production_worker_composition import (
    ProductionWorkerApplication,
    ProductionWorkerCompositionRoot,
    ProductionWorkerOverrides,
)
from offeragent_harness.runtime.worker_entrypoint import WorkerEntrypoint
from offeragent_harness.sessions import (
    AgentLineage,
    Run,
    RunKind,
    RunStatus,
    Session,
    SessionStatus,
    Turn,
    TurnStatus,
)
from offeragent_harness.testing import ManualCancellationToken
from offeragent_harness.tools import (
    ToolCall,
    canonical_json_sha256,
    invocation_journal_scope,
    invocation_request_fingerprint,
)
from offeragent_harness.vault import content_hash
from offeragent_harness.workspace import identify_workspace_root
from offeragent_harness.workspace.portable_config import read_portable_workspace_config
from offeragent_harness.workspace.runtime_identity import workspace_database_identity

CRASH_EXIT = 73
NO_CRASH_EXIT = 74
WORKSPACE_INSTANCE_ID = "wsi_5814036c-4192-49ea-9e75-b458bd7a53aa"
BEFORE_CONTENT = b"BEFORE_PAYLOAD\n"
AFTER_CONTENT = b"BEFORE_PAYLOAD\nAFTER_PAYLOAD\n"
MODEL_ID = "gpt-crash-recovery"
ACCOUNT_FINGERPRINT = "account-crash-recovery"
ACCOUNT_BINDING = "sha256:" + hashlib.sha256(ACCOUNT_FINGERPRINT.encode()).hexdigest()
NOW = datetime(2026, 7, 18, 12, 0, tzinfo=timezone.utc)
PLUGIN_RECOVERY_TOKEN = "a" * 64
PLUGIN_RECOVERY_BATCH_ID = "batch_production_plugin_recovery"


class _CodexCredentials:
    @contextmanager
    def lease(self) -> Iterator[ModelCredentialLease]:
        token = bytearray(b"fixture-access-token")
        view = memoryview(token)
        try:
            yield ModelCredentialLease(
                material=view,
                headers=MappingProxyType(
                    {
                        "ChatGPT-Account-ID": "fixture-account-id",
                        "originator": "codex_cli_rs",
                        "User-Agent": "codex_cli_rs/test (OfferAgent)",
                    }
                ),
                credential_fingerprint="credential-crash-recovery",
                account_fingerprint=ACCOUNT_FINGERPRINT,
            )
        finally:
            view.release()
            token[:] = b"\0" * len(token)


class _MissingCodexCredentials:
    def lease(self) -> Any:
        raise ModelCredentialSourceError("auth_required")


class _CodexCatalog:
    def get(self, request: CodexCatalogHttpRequest) -> CodexCatalogHttpResponse:
        del request
        body = {
            "models": [
                {
                    "slug": MODEL_ID,
                    "display_name": "Crash Recovery Model",
                    "description": "Deterministic production recovery fixture",
                    "visibility": "list",
                    "input_modalities": ["text"],
                    "supports_image_detail_original": False,
                    "supports_search_tool": False,
                    "web_search_tool_type": None,
                    "context_window": 128000,
                    "max_context_window": 128000,
                    "effective_context_window_percent": 95,
                    "additional_speed_tiers": [],
                    "service_tiers": [],
                    "default_service_tier": None,
                }
            ]
        }
        return CodexCatalogHttpResponse(
            status=200,
            headers={"Content-Type": "application/json"},
            body=json.dumps(body, separators=(",", ":")).encode(),
        )


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
        self._write_already_resolved = False

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
            tool_result_count = sum(message.role is ModelRole.TOOL for message in request.messages)
            has_tool_result = tool_result_count > 0
            if has_tool_result:
                self.planning_requests_with_tool_result += 1
            if self.planning_requests == 1:
                self._write_already_resolved = tool_result_count >= 2
                output: dict[str, Any] = {
                    "requiresWriteOutcome": False,
                    "calls": [
                        {
                            "name": "agent_contract.read",
                            "version": "1",
                            "arguments": {},
                            "reason": "在根 Agent Run 行动前加载当前 Vault Agent Contract。",
                        }
                    ],
                    "finalResponse": None,
                }
            elif self._write_already_resolved or tool_result_count >= 2:
                output = {
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


def _composition(
    root: Path,
    barrier: _CrashAndRecoveryBarrier,
    model: _CrashRecoveryModel,
    *,
    authenticated: bool = True,
) -> WorkerEntrypoint:
    vault = root / "vault"
    runtime_config = HarnessConfig.model_validate(
        {
            "model": {"model": MODEL_ID, "account_binding": ACCOUNT_BINDING},
            "policy": {
                "workspace_trusted": True,
                "read_only": False,
            },
        }
    )
    composition = ProductionWorkerCompositionRoot(
        canonical_root_identity=identify_workspace_root(vault).identity_hash,
        database_identity=workspace_database_identity(WORKSPACE_INSTANCE_ID),
        runtime_version="1.2.3",
        build_commit="abcdef0",
        overrides=ProductionWorkerOverrides(
            model_gateway_factory=lambda _settings: model,
            codex_credential_source=_CodexCredentials() if authenticated else _MissingCodexCredentials(),
            codex_catalog_http=_CodexCatalog(),
            runtime_config=runtime_config,
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


async def _complete_pending_agent_contract(
    application: ProductionWorkerApplication,
    run_id: str,
) -> bool:
    events = await application.harness.replay_events(run_id)
    completed_ids: set[str] = set()
    for event in events:
        if event.event_type not in {"tool.completed", "tool.failed"}:
            continue
        payload = cast(Mapping[str, Any], event.payload["payload"])
        result = cast(Mapping[str, Any], payload["result"])
        completed_ids.add(cast(str, result["toolCallId"]))
    for event in reversed(events):
        if event.event_type != "tool.started":
            continue
        payload = cast(Mapping[str, Any], event.payload["payload"])
        call = cast(Mapping[str, Any], payload["call"])
        tool_call_id = cast(str, call["toolCallId"])
        if call["name"] != "agent_contract.read" or tool_call_id in completed_ids:
            continue
        content = (application.vault_root / "agent.md").read_text(encoding="utf-8")
        digest = content_hash(content.encode("utf-8"))
        await _dispatch(
            application,
            "plugin-tools/complete",
            {
                "workspaceId": call["workspaceId"],
                "runId": call["runId"],
                "definitionFingerprint": call["definitionFingerprint"],
                "argsHash": call["argsHash"],
                "idempotencyKey": call["idempotencyKey"],
                "result": {
                    "toolCallId": tool_call_id,
                    "status": "succeeded",
                    "summary": "Read the Vault Agent Contract.",
                    "data": {"path": "agent.md", "content": content, "contentHash": digest},
                    "sourceRefs": [
                        {
                            "type": "vault",
                            "file": {
                                "workspaceId": call["workspaceId"],
                                "path": "agent.md",
                                "contentHash": digest,
                            },
                            "freshness": "fresh",
                        }
                    ],
                },
            },
            ApplicationCommandContext(transport="stdio", client_id="obsidian-plugin"),
        )
        return True
    return False


async def _wait_for_agent_contract(application: ProductionWorkerApplication, run_id: str) -> None:
    for _ in range(1_000):
        if await _complete_pending_agent_contract(application, run_id):
            return
        run = await application.harness.get_run(run_id)
        if run.status.is_terminal:
            raise RuntimeError(f"Run terminated before Agent Contract read: {run.status.value}")
        await asyncio.sleep(0.01)
    raise RuntimeError("root Agent Run did not request agent_contract.read")


async def _crash(root: Path, stage: str) -> int:
    barrier = _CrashAndRecoveryBarrier(stage)
    model = _CrashRecoveryModel()
    entrypoint = _composition(root, barrier, model)
    application = cast(
        ProductionWorkerApplication,
        await entrypoint.start(WorkerBootstrap(WORKSPACE_INSTANCE_ID, root / "vault", root / "state")),
    )
    context = ApplicationCommandContext(
        transport="stdio",
        client_id="stdio-production-crash",
        peer="parent-process",
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
                "runConfig": {
                    "provider": CODEX_SUBSCRIPTION_PROVIDER_ID,
                    "model": MODEL_ID,
                    "permissionMode": "normal",
                },
            },
            context,
        )
        run_id = cast(str, started["runId"])
        await _wait_for_agent_contract(application, run_id)
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
            run = await application.harness.get_run(run_id)
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
        contract_completed = False
        for _ in range(1_000):
            if not contract_completed:
                contract_completed = await _complete_pending_agent_contract(application, active.run_id)
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
    entrypoint = _composition(root, barrier, model, authenticated=False)
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


async def _seed_plugin_unknown_recovery(
    root: Path,
) -> tuple[SqliteUnitOfWorkFactory, ToolCall, Path, bytes, bytes, bytes, bytes]:
    now = datetime.now(timezone.utc)
    vault = root / "vault"
    workspace_id = read_portable_workspace_config(vault).portable_workspace_id
    state_directory = root / "state"
    state_directory.mkdir(parents=True, exist_ok=True)
    definition = next(item for item in plugin_tool_definitions() if item.name == "vault.changes.apply")
    run_id = "run_production_plugin_recovery"
    session_id = "ses_production_plugin_recovery"
    turn_id = "turn_production_plugin_recovery"
    arguments: dict[str, object] = {
        "batchId": PLUGIN_RECOVERY_BATCH_ID,
        "task": "Recover one production Interview Submission",
        "changeKind": "interview_submission",
        "sourceBindings": [],
        "interviewSubmission": {
            "sourceKind": "public_url",
            "capturedOn": "2026-07-18",
            "canonicalUrls": ["https://example.com/interview/production-recovery"],
            "orderedImageContentHashes": [],
            "sourceFingerprint": None,
            "reviewItems": [
                {
                    "kind": "experience",
                    "path": "experiences/production-recovery.md",
                    "identity": "new",
                    "mutation": "create",
                }
            ],
        },
        "operations": [
            {
                "op": "create",
                "path": "experiences/production-recovery.md",
                "content": "recovered\n",
                "expectedContentHash": "absent",
                "expectedModifiedVersion": "missing",
            }
        ],
    }
    call = ToolCall(
        tool_call_id="call_production_plugin_recovery",
        run_id=run_id,
        workspace_id=workspace_id,
        name=definition.name,
        version=definition.version,
        arguments=arguments,
        args_hash=canonical_json_sha256(arguments),
        idempotency_key="idem-production-plugin-recovery",
        deadline=now + timedelta(minutes=5),
        lineage=AgentLineage.root(run_id),
        definition_fingerprint=definition.fingerprint,
        result_sensitivity=definition.result_sensitivity,
    )
    lineage = AgentLineage.root(run_id)
    run_state = RunState(
        workspace_id=workspace_id,
        session_id=session_id,
        turn_id=turn_id,
        run_id=run_id,
        lineage=lineage,
        phase=RunPhase.EXECUTING_TOOLS,
        revision=7,
        model_rounds=1,
        assistant_text="等待插件写入结果",
        budget_checkpoint=BudgetCheckpoint(
            budget=RunBudget(
                32,
                64,
                4,
                900,
                400_000,
                64_000,
                Decimal("0"),
                64 * 1024 * 1024,
                1,
            ),
            started_at=now,
            used=BudgetDelta(model_rounds=1, tool_calls=1),
            reserved=BudgetDelta(),
            captured_at=now,
            elapsed_seconds=0,
        ),
    ).accept_tool_calls((call,))
    run = Run(
        run_id=run_id,
        session_id=session_id,
        turn_id=turn_id,
        workspace_id=workspace_id,
        lineage=lineage,
        kind=RunKind.ROOT,
        status=RunStatus.EXECUTING_TOOLS,
        attempt=1,
        event_sequence=0,
        config_snapshot={"model": MODEL_ID},
        created_at=now,
        updated_at=now,
        deadline_at=now + timedelta(seconds=900),
    )
    session = Session(
        session_id=session_id,
        workspace_id=workspace_id,
        profile_id="profile_production_plugin_recovery",
        title="Production plugin recovery",
        status=SessionStatus.ACTIVE,
        created_at=now,
        updated_at=now,
        revision=1,
    )
    turn = Turn(
        turn_id=turn_id,
        session_id=session_id,
        ordinal=1,
        status=TurnStatus.RUNNING,
        input_blocks=({"type": "text", "text": "recover plugin write"},),
        created_at=now,
        updated_at=now,
    )
    factory = SqliteUnitOfWorkFactory(state_directory / "state.sqlite")
    async with factory.begin() as unit_of_work:
        await unit_of_work.entities.put("sessions", session_id, session, expected_revision=0)
        await unit_of_work.entities.put("runs", run_id, run, expected_revision=0)
        await unit_of_work.entities.put("run_states", run_id, run_state, expected_revision=0)
        await unit_of_work.entities.put("turns", turn_id, turn, expected_revision=0)
        await unit_of_work.entities.put(
            "active_root_runs",
            session_id,
            {
                "schemaVersion": 1,
                "workspaceId": workspace_id,
                "sessionId": session_id,
                "runId": run_id,
                "acquiredAt": now.isoformat(),
            },
            expected_revision=0,
        )
        scope = invocation_journal_scope(call, definition)
        request_hash = invocation_request_fingerprint(call)
        await unit_of_work.journal.start(scope, call.idempotency_key, request_hash, now)
        await unit_of_work.journal.mark_unknown(scope, call.idempotency_key, request_hash, now)
        await unit_of_work.commit()

    applied_content = b"recovered\n"
    applied_target = vault / "experiences" / "production-recovery.md"
    applied_target.parent.mkdir(parents=True, exist_ok=True)
    applied_target.write_bytes(applied_content)
    journal_directory = vault / ".obsidian" / "offeragent" / "vault-change-journal"
    journal_directory.mkdir(parents=True)
    marker = (
        json.dumps({"schemaVersion": 2, "recoveryToken": PLUGIN_RECOVERY_TOKEN}, separators=(",", ":")).encode() + b"\n"
    )
    record = (
        json.dumps(
            {
                "version": 2,
                "batchId": PLUGIN_RECOVERY_BATCH_ID,
                "toolCallId": call.tool_call_id,
                "workspaceId": call.workspace_id,
                "runId": call.run_id,
                "rootRunId": call.lineage.root_run_id,
                "changeKind": "interview_submission",
                "reviewHash": "sha256:" + "c" * 64,
                "argsHash": call.args_hash,
                "idempotencyKey": call.idempotency_key,
                "state": "applied",
                "checkpointRef": f"refs/offeragent/checkpoints/{PLUGIN_RECOVERY_BATCH_ID}",
                "targets": [
                    {
                        "operation": "create",
                        "path": "experiences/production-recovery.md",
                        "beforeHash": "absent",
                        "afterHash": content_hash(applied_content),
                        "beforeModifiedVersion": "missing",
                        "afterModifiedVersion": f"mtime:1:size:{len(applied_content)}",
                    }
                ],
                "appliedPaths": ["experiences/production-recovery.md"],
                "manualReviewPaths": [],
            },
            separators=(",", ":"),
        ).encode()
        + b"\n"
    )
    seal = (
        json.dumps(
            {
                "schemaVersion": 1,
                "recoveryToken": PLUGIN_RECOVERY_TOKEN,
                "batchId": PLUGIN_RECOVERY_BATCH_ID,
                "contentHash": content_hash(record),
                "byteLength": len(record),
            },
            separators=(",", ":"),
        ).encode()
        + b"\n"
    )
    marker_path = journal_directory / ".recovery-ready.json"
    record_path = journal_directory / f"{PLUGIN_RECOVERY_BATCH_ID}.json"
    seal_directory = journal_directory / ".recovery-seals" / "current"
    seal_path = seal_directory / f"{hashlib.sha256(PLUGIN_RECOVERY_BATCH_ID.encode()).hexdigest()}.json"
    seal_directory.mkdir(parents=True)
    record_path.write_bytes(record)
    seal_path.write_bytes(seal)
    marker_path.write_bytes(marker)
    sentinel = (vault / "note.md").read_bytes()
    return factory, call, journal_directory, marker, record, seal, sentinel


async def _recover_plugin_apply(root: Path) -> int:
    factory, call, journal_directory, marker, record, seal, sentinel = await _seed_plugin_unknown_recovery(root)
    barrier = _CrashAndRecoveryBarrier(None)
    model = _CrashRecoveryModel()
    entrypoint = _composition(root, barrier, model)
    application = cast(
        ProductionWorkerApplication,
        await entrypoint.start(
            WorkerBootstrap(
                WORKSPACE_INSTANCE_ID,
                root / "vault",
                root / "state",
                journal_directory,
                PLUGIN_RECOVERY_TOKEN,
            )
        ),
    )
    try:
        report = application.harness_application.startup_report
        if report is None or len(report.applied_results) != 1:
            raise RuntimeError("production plugin recovery report is missing")
        applied = report.applied_results[0]
        recovered = tuple(item for item in applied.state.tool_results if item.tool_call_id == call.tool_call_id)
        definition = next(item for item in plugin_tool_definitions() if item.name == "vault.changes.apply")
        persisted = await factory.get_journal(
            invocation_journal_scope(call, definition),
            call.idempotency_key,
        )
        if persisted is None or persisted.result is None:
            raise RuntimeError("production plugin recovery journal did not complete")
        if not isinstance(persisted.result.data, Mapping):
            raise RuntimeError("production plugin recovery result data is malformed")
        result = {
            "readyBeforeShutdown": application.ready,
            "plansScanned": report.plans_scanned,
            "journalState": persisted.state.value,
            "resultStatus": persisted.result.status.value,
            "resultBatchId": persisted.result.data["batchId"],
            "pendingToolCallCount": len(applied.state.pending.tool_calls),
            "recoveredToolCallCount": len(recovered),
            "pluginJournalUnchanged": (
                (journal_directory / ".recovery-ready.json").read_bytes() == marker
                and (journal_directory / f"{PLUGIN_RECOVERY_BATCH_ID}.json").read_bytes() == record
                and (
                    journal_directory
                    / ".recovery-seals"
                    / "current"
                    / f"{hashlib.sha256(PLUGIN_RECOVERY_BATCH_ID.encode()).hexdigest()}.json"
                ).read_bytes()
                == seal
            ),
            "vaultSentinelUnchanged": (root / "vault" / "note.md").read_bytes() == sentinel,
        }
    finally:
        await entrypoint.shutdown()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("crash", "recover", "plugin-recover"))
    parser.add_argument("root", type=Path)
    parser.add_argument("stage", nargs="?", default="none")
    arguments = parser.parse_args()
    if arguments.action == "crash":
        if arguments.stage not in {"published", "manifest_committed"}:
            raise ValueError("crash action requires a supported CAS stage")
        return asyncio.run(_crash(arguments.root, arguments.stage))
    if arguments.action == "plugin-recover":
        if arguments.stage != "none":
            raise ValueError("plugin-recover action does not accept a CAS stage")
        return asyncio.run(_recover_plugin_apply(arguments.root))
    if arguments.stage != "none":
        raise ValueError("recover action does not accept a CAS stage")
    return asyncio.run(_recover(arguments.root))


if __name__ == "__main__":
    raise SystemExit(main())
