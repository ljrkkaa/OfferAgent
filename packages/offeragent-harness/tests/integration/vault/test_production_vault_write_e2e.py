from __future__ import annotations

import asyncio
import ctypes
import difflib
import hashlib
import msvcrt
import os
import shutil
import sqlite3
from collections import deque
from collections.abc import AsyncIterator, Mapping, Sequence
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, cast

import pytest

from offeragent_harness.config import HarnessConfig
from offeragent_harness.foundation import vault_write_intent_hash
from offeragent_harness.models import (
    ModelEvent,
    ModelEventKind,
    ModelFinishReason,
    ModelPurpose,
    ModelRequest,
    ModelUsage,
    thaw_json,
)
from offeragent_harness.permissions import ApprovalState
from offeragent_harness.ports import CancellationToken
from offeragent_harness.ports.worker_runtime import WorkerBootstrap
from offeragent_harness.protocol.capabilities import CapabilityName, CapabilitySet
from offeragent_harness.protocol.messages import (
    ApprovalResolveResult,
    ArtifactEncoding,
    ArtifactReadResult,
    ClientToolCommitObserveParams,
    ClientToolCommitObserveResult,
    ClientToolCommitPathState,
    ClientToolPathState,
    ClientToolPreviewParams,
    ClientToolPreviewResult,
    InitializeResult,
    SessionCreateResult,
    TurnStartResult,
)
from offeragent_harness.protocol.schemas import PROTOCOL_VERSION, schema_hash
from offeragent_harness.runtime.approval_manager import ApprovalRecord
from offeragent_harness.runtime.loopback_gateway import LoopbackAsset
from offeragent_harness.runtime.named_pipe import (
    ConnectionRole,
    DiscoveryMaterialStore,
    DuplexJsonRpcConnection,
    authenticate_client_stream,
)
from offeragent_harness.runtime.production_worker_composition import (
    ProductionWorkerApplication,
    ProductionWorkerCompositionRoot,
    ProductionWorkerOverrides,
)
from offeragent_harness.runtime.windows_named_pipe import (
    DpapiCurrentUserProtector,
    connect_windows_named_pipe,
)
from offeragent_harness.runtime.worker_entrypoint import WorkerEntrypoint
from offeragent_harness.sessions import Run, RunStatus
from offeragent_harness.tools import ToolResultStatus, canonical_json_sha256
from offeragent_harness.vault import ABSENT_HASH, content_hash
from offeragent_harness.workspace import identify_workspace_root
from offeragent_harness.workspace.portable_config import ensure_portable_workspace_config
from offeragent_harness.workspace.runtime_identity import workspace_database_identity


def _ripgrep_executable() -> Path:
    executable = shutil.which("rg.exe")
    if executable is None:
        pytest.fail("ripgrep is required for the production Worker integration fixture")
    return Path(executable).resolve(strict=True)


def _powershell_executable() -> Path:
    executable = shutil.which("powershell.exe")
    if executable is None:
        pytest.fail("PowerShell is required for the production Worker integration fixture")
    return Path(executable).resolve(strict=True)


pytestmark = pytest.mark.skipif(os.name != "nt", reason="production Vault write E2E requires Win32 Named Pipes")


def _read_like_obsidian(path: Path) -> bytes:
    """Open with libuv-style sharing while the Worker retains CAS rollback handles."""

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.CreateFileW(
        str(path),
        0x80000000,  # GENERIC_READ
        0x1 | 0x2 | 0x4,  # FILE_SHARE_READ | WRITE | DELETE
        None,
        3,  # OPEN_EXISTING
        0x08000000,  # FILE_FLAG_SEQUENTIAL_SCAN
        None,
    )
    numeric = int(handle or 0)
    if not numeric or numeric == ctypes.c_void_p(-1).value:
        code = ctypes.get_last_error()
        raise OSError(code, ctypes.FormatError(code), str(path))
    try:
        descriptor = msvcrt.open_osfhandle(numeric, os.O_RDONLY)
    except BaseException:
        kernel32.CloseHandle(wintypes.HANDLE(numeric))
        raise
    with os.fdopen(descriptor, "rb") as stream:
        return stream.read()


@dataclass(frozen=True, slots=True)
class _PlannedTurn:
    operations: tuple[Mapping[str, Any], ...]
    requires_write_outcome: bool


class _VaultWriteFakeModel:
    """Emit one Harness-owned public ToolPlan, then a terminal planning step."""

    def __init__(self) -> None:
        self.requests: list[ModelRequest] = []
        self._queued: deque[_PlannedTurn] = deque()
        self._active: _PlannedTurn | None = None
        self._planning_step = 0

    def enqueue(
        self,
        operations: Sequence[Mapping[str, Any]],
        *,
        requires_write_outcome: bool,
    ) -> None:
        if not operations:
            raise ValueError("a planned production turn requires at least one operation")
        self._queued.append(
            _PlannedTurn(
                tuple(dict(operation) for operation in operations),
                requires_write_outcome,
            )
        )

    async def stream(
        self,
        request: ModelRequest,
        cancellation: CancellationToken,
    ) -> AsyncIterator[ModelEvent]:
        self.requests.append(request)
        cancellation.checkpoint()
        yield ModelEvent(request.request_id, 1, ModelEventKind.STARTED)
        if request.purpose is ModelPurpose.PLANNING:
            if self._active is None:
                if not self._queued:
                    raise AssertionError("production test model received an unplanned turn")
                self._active = self._queued.popleft()
                self._planning_step = 0
            active = self._active
            if self._planning_step == 0:
                output = {
                    "requiresWriteOutcome": active.requires_write_outcome,
                    "calls": [
                        {
                            "name": "vault.transaction",
                            "version": "1",
                            "arguments": {"operations": [dict(item) for item in active.operations]},
                            "reason": "执行用户明确要求的单文件 Vault 修改",
                        }
                    ],
                    "stopReason": None,
                }
                self._planning_step = 1
            else:
                output = {
                    "requiresWriteOutcome": active.requires_write_outcome,
                    "calls": [],
                    "stopReason": "已依据真实工具结果结束本轮",
                }
                self._active = None
                self._planning_step = 0
            yield ModelEvent(
                request.request_id,
                2,
                ModelEventKind.STRUCTURED_OUTPUT,
                data=cast(Mapping[str, Any], output),
            )
            sequence = 3
        elif request.purpose is ModelPurpose.COMPOSING:
            yield ModelEvent(request.request_id, 2, ModelEventKind.TEXT_DELTA, text="本地 Vault 事务结果已核验。")
            sequence = 3
        elif request.purpose is ModelPurpose.MEMORY:
            yield ModelEvent(
                request.request_id,
                2,
                ModelEventKind.STRUCTURED_OUTPUT,
                data={"proposals": []},
            )
            sequence = 3
        elif request.purpose is ModelPurpose.GROUNDING:
            sequence = 2
        else:
            raise AssertionError(f"unexpected model purpose: {request.purpose}")
        yield ModelEvent(
            request.request_id,
            sequence,
            ModelEventKind.USAGE,
            usage=ModelUsage(8, 4, 0, 0),
        )
        yield ModelEvent(
            request.request_id,
            sequence + 1,
            ModelEventKind.COMPLETED,
            finish_reason=ModelFinishReason.STOP,
        )


class _ObsidianPreviewDispatcher:
    """Read-only Obsidian-side authority used by the real reverse Pipe."""

    def __init__(self, vault_root: Path) -> None:
        self._vault_root = vault_root
        self.previews: list[dict[str, Any]] = []
        self.commit_observations: list[dict[str, Any]] = []

    def require_ready(self) -> None:
        return None

    async def dispatch(
        self,
        method: str,
        params: Mapping[str, Any],
        cancellation: CancellationToken,
        *,
        context: object | None = None,
    ) -> object:
        del context
        cancellation.checkpoint()
        if method == "client/tool/preview":
            return self._preview(ClientToolPreviewParams.model_validate(params)).to_wire()
        if method == "client/tool/commit-observe":
            return self._observe_commit(ClientToolCommitObserveParams.model_validate(params)).to_wire()
        raise AssertionError(f"unexpected production reverse request: {method}")

    def _preview(self, params: ClientToolPreviewParams) -> ClientToolPreviewResult:
        arguments = cast(dict[str, Any], thaw_json(params.arguments))
        assert params.name == "obsidian.vault.transaction"
        assert set(arguments) == {"transactionId", "operations"}
        operations = cast(list[dict[str, Any]], arguments["operations"])
        assert len(operations) == 1
        operation = operations[0]
        path, before, after = self._planned_contents(operation)
        before_hash = ABSENT_HASH if before is None else content_hash(before)
        after_hash = content_hash(after)
        assert operation["expectedHash"] == before_hash
        diff = "".join(
            difflib.unified_diff(
                (before or b"").decode("utf-8").splitlines(keepends=True),
                after.decode("utf-8").splitlines(keepends=True),
                fromfile=f"a/{path}" if before is not None else "/dev/null",
                tofile=f"b/{path}",
            )
        )
        assert diff
        record = {
            "invocationId": params.invocation_id,
            "toolCallId": params.tool_call_id,
            "operation": dict(operation),
            "beforeHash": before_hash,
            "afterHash": after_hash,
            "diff": diff,
        }
        self.previews.append(record)
        return ClientToolPreviewResult(
            invocation_id=params.invocation_id,
            tool_call_id=params.tool_call_id,
            state_hash=canonical_json_sha256({"path": path, "beforeHash": before_hash}),
            after_state_hash=canonical_json_sha256({"path": path, "afterHash": after_hash}),
            paths=[path],
            diff=diff,
            diff_sha256=content_hash(diff.encode("utf-8")),
            has_unsaved_editors=False,
            has_open_editors=False,
            path_states=[
                ClientToolPathState(
                    path=path,
                    before_hash=before_hash,
                    after_hash=after_hash,
                    unsaved_editor=False,
                    open_editor=False,
                )
            ],
        )

    def _observe_commit(self, params: ClientToolCommitObserveParams) -> ClientToolCommitObserveResult:
        states: list[ClientToolCommitPathState] = []
        for path in params.paths:
            target = self._target(path)
            observed_hash = content_hash(_read_like_obsidian(target)) if target.is_file() else ABSENT_HASH
            states.append(
                ClientToolCommitPathState(
                    path=path,
                    observed_hash=observed_hash,
                    unsaved_editor=False,
                    open_editor=False,
                )
            )
        self.commit_observations.append(
            {
                "invocationId": params.invocation_id,
                "toolCallId": params.tool_call_id,
                "paths": list(params.paths),
                "hashes": [item.observed_hash for item in states],
            }
        )
        return ClientToolCommitObserveResult(
            invocation_id=params.invocation_id,
            tool_call_id=params.tool_call_id,
            paths=list(params.paths),
            has_unsaved_editors=False,
            has_open_editors=False,
            path_states=states,
        )

    def _planned_contents(self, operation: Mapping[str, Any]) -> tuple[str, bytes | None, bytes]:
        path = cast(str, operation["path"])
        target = self._target(path)
        before = target.read_bytes() if target.is_file() else None
        kind = operation["op"]
        if kind == "create":
            assert before is None
            after = cast(str, operation["content"]).encode("utf-8")
        elif kind == "append":
            assert before is not None
            after = before + cast(str, operation["content"]).encode("utf-8")
        elif kind == "replace":
            assert before is not None
            text = before.decode("utf-8")
            find = cast(str, operation["find"])
            assert text.count(find) == 1
            after = text.replace(find, cast(str, operation["replace"]), 1).encode("utf-8")
        elif kind == "patch":
            assert before is not None
            lines = before.decode("utf-8").splitlines(keepends=True)
            edits = cast(list[dict[str, Any]], operation["edits"])
            ranges = sorted(
                (
                    cast(int, edit["startLine"]),
                    cast(int, edit["endLine"]),
                    cast(str, edit["replacement"]),
                )
                for edit in edits
            )
            for start, end, replacement in reversed(ranges):
                assert 1 <= start <= end <= len(lines)
                lines[start - 1 : end] = replacement.splitlines(keepends=True)
            after = "".join(lines).encode("utf-8")
        else:
            raise AssertionError(f"non-public operation reached Obsidian preview: {kind}")
        return path, before, after

    def _target(self, path: str) -> Path:
        relative = PurePosixPath(path)
        assert not relative.is_absolute() and ".." not in relative.parts
        return self._vault_root.joinpath(*relative.parts)


def _development_web_assets() -> tuple[LoopbackAsset, ...]:
    web = Path(__file__).resolve().parents[3] / "web"
    assets: list[LoopbackAsset] = []
    for route, path, media_type in (
        ("/", web / "index.html", "text/html; charset=utf-8"),
        ("/assets/app.js", web / "assets" / "app.js", "text/javascript; charset=utf-8"),
        ("/assets/app.css", web / "assets" / "app.css", "text/css; charset=utf-8"),
    ):
        content = path.read_bytes()
        assets.append(LoopbackAsset(route, media_type, content, f"sha256:{hashlib.sha256(content).hexdigest()}"))
    return tuple(assets)


async def _connect_pipe(
    application: ProductionWorkerApplication,
    workspace_id: str,
    reverse_dispatcher: _ObsidianPreviewDispatcher,
) -> tuple[DuplexJsonRpcConnection, InitializeResult]:
    material = DiscoveryMaterialStore(
        application.state_directory / "transport",
        protector=DpapiCurrentUserProtector(),
    ).load(now=application.clock.utcnow())
    stream = await connect_windows_named_pipe(material.pipe_name)
    await authenticate_client_stream(stream, material, now=application.clock.utcnow)
    connection = DuplexJsonRpcConnection(
        stream,
        role=ConnectionRole.CLIENT,
        dispatcher=reverse_dispatcher,
        connection_id="pipe-production-vault-write-e2e",
    )
    await connection.start()
    initialized = await connection.request(
        "initialize",
        {
            "protocolVersion": PROTOCOL_VERSION,
            "clientVersion": "2.0.0",
            "workspaceId": workspace_id,
            "capabilities": CapabilitySet.from_enabled(set(CapabilityName)).to_wire(),
            "requiredCapabilities": ["eventReplay", "multiSession", "loopbackWeb", "clientTools"],
            "schemaHash": schema_hash(),
        },
    )
    assert isinstance(initialized, InitializeResult)
    await connection.wait_ready()
    return connection, initialized


@pytest.fixture
async def production_vault_application(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[
    tuple[
        ProductionWorkerApplication,
        _VaultWriteFakeModel,
        _ObsidianPreviewDispatcher,
        DuplexJsonRpcConnection,
        str,
        Path,
    ]
]:
    monkeypatch.setattr(
        "offeragent_harness.runtime.loopback_gateway.load_packaged_web_assets",
        _development_web_assets,
    )
    vault = tmp_path / "临时生产写入 Vault"
    vault.mkdir()
    portable = ensure_portable_workspace_config(vault)
    state = tmp_path / "runtime-state"
    workspace_instance_id = "wsi_6d376b25-53ca-4c18-aee5-3076617f4345"
    model = _VaultWriteFakeModel()
    root = ProductionWorkerCompositionRoot(
        canonical_root_identity=identify_workspace_root(vault).identity_hash,
        database_identity=workspace_database_identity(workspace_instance_id),
        runtime_version="1.2.3",
        build_commit="abcdef0",
        overrides=ProductionWorkerOverrides(
            model_gateway_factory=lambda _settings: model,
            start_native_transports=True,
            runtime_config=HarnessConfig.model_validate(
                {"policy": {"workspace_trusted": True, "read_only": False}, "ui": {"loopback_web_enabled": True}}
            ),
            ripgrep_path=_ripgrep_executable(),
            powershell_path=_powershell_executable(),
        ),
    )
    entrypoint = WorkerEntrypoint(root)
    application = await entrypoint.start(WorkerBootstrap(workspace_instance_id, vault, state))
    reverse_dispatcher = _ObsidianPreviewDispatcher(vault)
    pipe: DuplexJsonRpcConnection | None = None
    try:
        assert isinstance(application, ProductionWorkerApplication)
        pipe, initialized = await _connect_pipe(application, portable.portable_workspace_id, reverse_dispatcher)
        assert initialized.worker_pid == os.getpid() == application.worker_pid
        assert initialized.workspace_instance_id == workspace_instance_id
        yield application, model, reverse_dispatcher, pipe, portable.portable_workspace_id, vault
    finally:
        if pipe is not None:
            await pipe.close()
        await entrypoint.shutdown()


async def _wait_pending_approval(
    application: ProductionWorkerApplication,
    run_id: str,
) -> tuple[ApprovalRecord, int]:
    for _ in range(1_000):
        records = await application.unit_of_work.list_entities("approvals", limit=100)
        matching = [
            item
            for item in records
            if isinstance(item.value, ApprovalRecord)
            and item.value.request.binding.run_id == run_id
            and item.value.state is ApprovalState.PENDING
        ]
        if matching:
            assert len(matching) == 1
            record = cast(ApprovalRecord, matching[0].value)
            assert matching[0].revision == record.revision
            return record, matching[0].revision
        await asyncio.sleep(0.01)
    raise AssertionError(f"durable production approval did not become pending: {run_id}")


async def _wait_terminal(application: ProductionWorkerApplication, run_id: str) -> Run:
    for _ in range(1_000):
        run = await application.harness.get_run(run_id)
        if run.status.is_terminal:
            return run
        await asyncio.sleep(0.01)
    raise AssertionError(f"production Run did not become terminal: {run_id}")


def _journal_rows(database_path: Path, run_id: str) -> list[tuple[str, str, str]]:
    with sqlite3.connect(database_path) as connection:
        rows = connection.execute(
            """
            SELECT scope, idempotency_key, state
            FROM invocation_journal
            WHERE scope LIKE ?
            ORDER BY scope, idempotency_key
            """,
            (f"%:{run_id}:{run_id}:vault.transaction:1",),
        ).fetchall()
    return [(cast(str, scope), cast(str, key), cast(str, state)) for scope, key, state in rows]


@pytest.mark.asyncio
async def test_real_production_pipe_approves_and_commits_every_public_single_file_operation(
    production_vault_application: tuple[
        ProductionWorkerApplication,
        _VaultWriteFakeModel,
        _ObsidianPreviewDispatcher,
        DuplexJsonRpcConnection,
        str,
        Path,
    ],
) -> None:
    application, model, reverse, pipe, _workspace_id, vault = production_vault_application
    created = await pipe.request(
        "session/create",
        {"title": "生产 Vault 写入闭环", "clientRequestId": "req_production_vault_write_session"},
    )
    assert isinstance(created, SessionCreateResult)
    session_id = created.session.session_id
    target = vault / "note.md"

    async def execute_operation(
        operation: dict[str, Any],
        *,
        expected_content: bytes,
        diff_fragments: tuple[str, ...],
    ) -> None:
        operation_name = cast(str, operation["op"])
        expected_before_hash = cast(str, operation["expectedHash"])
        model.enqueue((operation,), requires_write_outcome=True)
        preview_count = len(reverse.previews)
        observation_count = len(reverse.commit_observations)
        model_request_count = len(model.requests)
        turn_params = {
            "sessionId": session_id,
            "turnId": f"turn_production_{operation_name}",
            "idempotencyKey": f"turn-idempotency-{operation_name}",
            "writeIntent": {
                "kind": "vault_write_required",
                "targetPaths": ["note.md"],
                "intentHash": vault_write_intent_hash(("note.md",)),
            },
            "input": [{"type": "text", "text": f"请执行 {operation_name} 单文件写入"}],
            "runConfig": {"provider": "codex", "model": "fake", "permissionMode": "normal"},
        }
        started = await pipe.request("turn/start", turn_params)
        assert isinstance(started, TurnStartResult)
        assert started.accepted and not started.duplicate

        pending, pending_revision = await _wait_pending_approval(application, started.run_id)
        assert pending_revision == 1
        assert pending.request.binding.args_hash == canonical_json_sha256({"operations": [operation]})
        assert pending.request.binding.expected_state_hash is not None
        assert pending.request.binding.expected_state_hash.startswith("sha256:")
        assert len(pending.request.diff_artifact_ids) == 1
        assert len(reverse.previews) == preview_count + 1
        first_preview = reverse.previews[-1]
        assert first_preview["beforeHash"] == expected_before_hash
        assert cast(dict[str, Any], first_preview["operation"])["expectedHash"] == expected_before_hash

        artifact = await pipe.request(
            "artifact/read",
            {"artifactId": pending.request.diff_artifact_ids[0], "offset": 0, "maxBytes": 1_048_576},
        )
        assert isinstance(artifact, ArtifactReadResult)
        assert artifact.encoding is ArtifactEncoding.UTF8 and artifact.eof
        for fragment in diff_fragments:
            assert fragment in artifact.content
        assert content_hash(artifact.content.encode("utf-8")) == artifact.artifact.content_hash
        assert artifact.artifact.artifact_id == pending.request.diff_artifact_ids[0]

        resolution = await pipe.request(
            "approval/resolve",
            {
                "approvalId": pending.request.approval_id,
                "decision": "allow_once",
                "scope": "once",
                "expectedArgsHash": pending.request.binding.args_hash,
                "includeDescendants": False,
                "comment": "真实 Pipe 生产事务测试",
            },
        )
        assert isinstance(resolution, ApprovalResolveResult)
        assert resolution.status == "approved" and resolution.resumed
        assert resolution.run_id == started.run_id

        run = await _wait_terminal(application, started.run_id)
        assert run.status is RunStatus.COMPLETED
        state = await application.harness.get_run_state(started.run_id)
        assert len(state.tool_results) == 1
        result = state.tool_results[0]
        assert result.status is ToolResultStatus.SUCCEEDED
        assert result.data is not None and thaw_json(result.data)["paths"] == ["note.md"]
        assert target.read_bytes() == expected_content
        assert len(reverse.previews) == preview_count + 3
        assert len(reverse.commit_observations) == observation_count + 1
        assert reverse.commit_observations[-1]["hashes"] == [content_hash(expected_content)]

        durable = await application.approvals.get(pending.request.approval_id)
        assert durable is not None
        assert durable.state is ApprovalState.APPROVED and durable.revision == 2
        rows = _journal_rows(application.database_path, started.run_id)
        assert len(rows) == 1 and rows[0][2] == "completed"
        journal = await application.unit_of_work.get_journal(rows[0][0], rows[0][1])
        assert journal is not None and journal.result == result
        assert not tuple((application.state_directory / "vault-transactions").glob("*.json"))

        duplicate = await pipe.request("turn/start", turn_params)
        assert isinstance(duplicate, TurnStartResult)
        assert duplicate.run_id == started.run_id and duplicate.duplicate
        await asyncio.sleep(0.02)
        assert target.read_bytes() == expected_content
        assert _journal_rows(application.database_path, started.run_id) == rows
        assert len(reverse.previews) == preview_count + 3
        assert len(reverse.commit_observations) == observation_count + 1
        assert len(model.requests) > model_request_count

    await execute_operation(
        {"op": "create", "path": "note.md", "content": "alpha\nbeta\n", "expectedHash": ABSENT_HASH},
        expected_content=b"alpha\nbeta\n",
        diff_fragments=("--- /dev/null", "+alpha", "+beta"),
    )
    before = target.read_bytes()
    await execute_operation(
        {
            "op": "append",
            "path": "note.md",
            "content": "gamma\n",
            "expectedHash": content_hash(before),
        },
        expected_content=b"alpha\nbeta\ngamma\n",
        diff_fragments=("--- a/note.md", "+gamma"),
    )
    before = target.read_bytes()
    await execute_operation(
        {
            "op": "replace",
            "path": "note.md",
            "find": "beta",
            "replace": "BETA",
            "expectedHash": content_hash(before),
        },
        expected_content=b"alpha\nBETA\ngamma\n",
        diff_fragments=("-beta", "+BETA"),
    )
    before = target.read_bytes()
    await execute_operation(
        {
            "op": "patch",
            "path": "note.md",
            "edits": [{"startLine": 1, "endLine": 1, "replacement": "ALPHA\n"}],
            "expectedHash": content_hash(before),
        },
        expected_content=b"ALPHA\nBETA\ngamma\n",
        diff_fragments=("-alpha", "+ALPHA"),
    )


@pytest.mark.asyncio
async def test_real_production_registry_fails_closed_for_rename_trash_and_multi_operation_plans(
    production_vault_application: tuple[
        ProductionWorkerApplication,
        _VaultWriteFakeModel,
        _ObsidianPreviewDispatcher,
        DuplexJsonRpcConnection,
        str,
        Path,
    ],
) -> None:
    application, model, reverse, pipe, _workspace_id, vault = production_vault_application
    created = await pipe.request(
        "session/create",
        {"title": "生产宽写入失败关闭", "clientRequestId": "req_production_broad_write_session"},
    )
    assert isinstance(created, SessionCreateResult)
    guard = vault / "guard.md"
    guard.write_bytes(b"safe\n")
    guard_hash = content_hash(guard.read_bytes())
    invalid_plans: tuple[tuple[str, tuple[dict[str, Any], ...]], ...] = (
        (
            "rename",
            (
                {
                    "op": "rename",
                    "path": "guard.md",
                    "destination": "renamed.md",
                    "expectedHash": guard_hash,
                    "expectedDestinationHash": ABSENT_HASH,
                },
            ),
        ),
        ("trash", ({"op": "trash", "path": "guard.md", "expectedHash": guard_hash},)),
        (
            "multi",
            (
                {"op": "create", "path": "multi-a.md", "content": "a\n", "expectedHash": ABSENT_HASH},
                {"op": "create", "path": "multi-b.md", "content": "b\n", "expectedHash": ABSENT_HASH},
            ),
        ),
    )

    for label, operations in invalid_plans:
        model.enqueue(operations, requires_write_outcome=False)
        started = await pipe.request(
            "turn/start",
            {
                "sessionId": created.session.session_id,
                "turnId": f"turn_fail_closed_{label}",
                "idempotencyKey": f"turn-fail-closed-{label}",
                "writeIntent": {"kind": "none"},
                "input": [{"type": "text", "text": f"尝试不可公开的 {label} 写入"}],
                "runConfig": {"provider": "codex", "model": "fake", "permissionMode": "normal"},
            },
        )
        assert isinstance(started, TurnStartResult)
        run = await _wait_terminal(application, started.run_id)
        assert run.status is RunStatus.COMPLETED
        state = await application.harness.get_run_state(started.run_id)
        assert state.tool_results == ()
        registry = application.components.registry_for_run(started.run_id)
        assert registry is not None
        transaction = registry.get("vault.transaction", "1")
        schema = cast(dict[str, Any], thaw_json(transaction.input_schema))
        operations_schema = cast(dict[str, Any], cast(dict[str, Any], schema["properties"])["operations"])
        variants = cast(list[dict[str, Any]], cast(dict[str, Any], operations_schema["items"])["oneOf"])
        public_operations = {
            cast(str, cast(dict[str, Any], variant["properties"])["op"]["const"]) for variant in variants
        }
        assert operations_schema["maxItems"] == 1
        assert public_operations == {"create", "append", "replace", "patch"}
        assert _journal_rows(application.database_path, started.run_id) == []
        records = await application.unit_of_work.list_entities("approvals", limit=100)
        assert all(
            not isinstance(item.value, ApprovalRecord) or item.value.request.binding.run_id != started.run_id
            for item in records
        )

    assert reverse.previews == [] and reverse.commit_observations == []
    assert guard.read_bytes() == b"safe\n"
    assert not (vault / "renamed.md").exists()
    assert not (vault / "multi-a.md").exists()
    assert not (vault / "multi-b.md").exists()
    assert not (vault / ".trash" / "offeragent").exists()
