from __future__ import annotations

import asyncio
import hashlib
import os
import threading
from pathlib import Path
from typing import Any

import pytest

import offeragent_harness.workspace.filesystem as workspace_filesystem_module
from offeragent_harness.ports import CancellationToken, OperationCancelled
from offeragent_harness.ports.vault import VaultTransaction
from offeragent_harness.runtime import CancellationReason, CancellationScope
from offeragent_harness.runtime.cancellation import CancellationCode
from offeragent_harness.tools import ToolResult, ToolResultStatus
from offeragent_harness.workspace import (
    VaultFileSystem,
    VaultFilesystemError,
    VaultFilesystemErrorCode,
    VaultReadPolicy,
    WorkspacePathPolicy,
)


class TransactionExecutor:
    def __init__(self) -> None:
        self.transactions: list[VaultTransaction] = []

    async def execute(self, transaction: VaultTransaction, cancellation: CancellationToken) -> ToolResult:
        self.transactions.append(transaction)
        return ToolResult(
            tool_call_id=transaction.transaction_id,
            status=ToolResultStatus.SUCCEEDED,
            data={"workspaceRevision": 8},
            user_visible_summary="transaction committed",
            artifact_ids=(),
            source_refs=(),
            side_effects=(),
            retryable=False,
            before_state=None,
            after_state={"workspaceRevision": 8},
            error=None,
        )


class CheckpointCancellation:
    def __init__(self, cancel_on: int) -> None:
        self._cancel_on = cancel_on
        self._checks = 0
        self._cancelled = False
        self._reason = CancellationReason.now(CancellationCode.USER, "test block cancellation")

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    @property
    def reason(self) -> CancellationReason | None:
        return self._reason if self._cancelled else None

    @property
    def checks(self) -> int:
        return self._checks

    async def wait(self) -> CancellationReason:
        return self._reason

    def checkpoint(self) -> None:
        self._checks += 1
        if self._checks >= self._cancel_on:
            self._cancelled = True
            raise OperationCancelled(self._reason)


def filesystem(
    root: Path,
    *,
    max_file_bytes: int = 1024,
    max_return_bytes: int = 1024,
    max_list_entries: int = 20,
    max_list_scan_entries: int = 100,
    hidden: tuple[str, ...] = (),
    allow_hardlinks: bool = False,
) -> tuple[VaultFileSystem, TransactionExecutor]:
    executor = TransactionExecutor()
    return (
        VaultFileSystem(
            workspace_id="ws_1",
            paths=WorkspacePathPolicy(root),
            read_policy=VaultReadPolicy(
                allowed_extensions=frozenset({".md", ".txt"}),
                max_file_bytes=max_file_bytes,
                max_return_bytes=max_return_bytes,
                max_list_entries=max_list_entries,
                max_list_scan_entries=max_list_scan_entries,
                allowed_hidden_prefixes=hidden,
                allow_hardlinks=allow_hardlinks,
            ),
            workspace_revision=lambda: 7,
            transaction_executor=executor,
        ),
        executor,
    )


@pytest.mark.asyncio
async def test_chinese_space_path_read_is_hashed_revisioned_and_bounded(tmp_path: Path) -> None:
    notes = tmp_path / "中文 笔记"
    notes.mkdir()
    content = "OfferAgent 本地化".encode()
    (notes / "面试.md").write_bytes(content)
    workspace, _ = filesystem(tmp_path, max_return_bytes=8)

    result = await workspace.read("中文 笔记/面试.md", CancellationScope())

    assert result.content == content[:8]
    assert result.truncated
    assert result.entry.content_hash == f"sha256:{hashlib.sha256(content).hexdigest()}"
    assert result.entry.workspace_revision == 7
    assert result.entry.relative_path == "中文 笔记/面试.md"


@pytest.mark.asyncio
async def test_stat_and_list_omit_unauthorized_or_unsupported_children(tmp_path: Path) -> None:
    (tmp_path / "visible.md").write_text("ok", encoding="utf-8")
    (tmp_path / "binary.exe").write_bytes(b"MZ")
    hidden = tmp_path / ".obsidian"
    hidden.mkdir()
    (hidden / "data.json").write_text("secret", encoding="utf-8")
    workspace, _ = filesystem(tmp_path)

    entries = await workspace.list("", CancellationScope())

    assert [entry.relative_path for entry in entries] == ["visible.md"]
    assert (await workspace.stat("visible.md", CancellationScope())) is not None
    with pytest.raises(VaultFilesystemError) as captured:
        await workspace.read(".obsidian/data.json", CancellationScope())
    assert captured.value.code is VaultFilesystemErrorCode.HIDDEN


@pytest.mark.asyncio
async def test_hidden_prefix_requires_explicit_authorization_and_extension(tmp_path: Path) -> None:
    skills = tmp_path / ".claude" / "skills"
    skills.mkdir(parents=True)
    (skills / "SKILL.md").write_text("instructions", encoding="utf-8")
    workspace, _ = filesystem(tmp_path, hidden=(".claude/skills",))

    result = await workspace.read(".claude/skills/SKILL.md", CancellationScope())

    assert result.content == b"instructions"


@pytest.mark.asyncio
async def test_file_and_directory_limits_fail_closed(tmp_path: Path) -> None:
    (tmp_path / "large.md").write_bytes(b"12345")
    workspace, _ = filesystem(tmp_path, max_file_bytes=4, max_return_bytes=4, max_list_entries=1)

    with pytest.raises(VaultFilesystemError) as large:
        await workspace.read("large.md", CancellationScope())
    assert large.value.code is VaultFilesystemErrorCode.TOO_LARGE
    (tmp_path / "second.md").write_text("2", encoding="utf-8")
    (tmp_path / "third.md").write_text("3", encoding="utf-8")
    with pytest.raises(VaultFilesystemError) as listing:
        await workspace.list("", CancellationScope())
    assert listing.value.code is VaultFilesystemErrorCode.LIST_LIMIT


@pytest.mark.asyncio
async def test_list_scan_budget_is_separate_from_authorized_result_limit(tmp_path: Path) -> None:
    for index in range(8):
        (tmp_path / f"ignored-{index}.exe").write_bytes(b"MZ")
    (tmp_path / "visible.md").write_text("ok", encoding="utf-8")
    workspace, _ = filesystem(tmp_path, max_list_entries=1, max_list_scan_entries=20)

    entries = await workspace.list("", CancellationScope())

    assert [entry.relative_path for entry in entries] == ["visible.md"]


@pytest.mark.asyncio
async def test_list_stops_at_incremental_scan_budget(tmp_path: Path) -> None:
    for index in range(20):
        (tmp_path / f"ignored-{index:02}.exe").write_bytes(b"MZ")
    workspace, _ = filesystem(tmp_path, max_list_entries=2, max_list_scan_entries=3)

    with pytest.raises(VaultFilesystemError) as captured:
        await workspace.list("", CancellationScope())

    assert captured.value.code is VaultFilesystemErrorCode.LIST_SCAN_LIMIT


@pytest.mark.asyncio
async def test_read_and_stat_reject_actual_hardlinks_by_default(tmp_path: Path) -> None:
    source = tmp_path / "source.md"
    alias = tmp_path / "alias.md"
    source.write_text("same inode", encoding="utf-8")
    try:
        os.link(source, alias)
    except OSError as error:
        pytest.skip(f"hard links are unavailable on this filesystem: {error}")
    workspace, _ = filesystem(tmp_path)

    with pytest.raises(VaultFilesystemError) as reading:
        await workspace.read("source.md", CancellationScope())
    with pytest.raises(VaultFilesystemError) as inspecting:
        await workspace.stat("alias.md", CancellationScope())

    assert reading.value.code is VaultFilesystemErrorCode.HARD_LINK
    assert inspecting.value.code is VaultFilesystemErrorCode.HARD_LINK


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["read", "stat"])
async def test_open_handle_detects_path_exchange_race(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    target = tmp_path / "target.md"
    replacement = tmp_path / "replacement.md"
    target.write_text("authorized", encoding="utf-8")
    replacement.write_text("swapped", encoding="utf-8")
    workspace, _ = filesystem(tmp_path)
    original_open = workspace_filesystem_module._open_readonly_fd
    exchanged = False

    def exchange_then_open(path: Path) -> int:
        nonlocal exchanged
        if path == target and not exchanged:
            os.replace(replacement, target)
            exchanged = True
        return original_open(path)

    monkeypatch.setattr(workspace_filesystem_module, "_open_readonly_fd", exchange_then_open)

    with pytest.raises(VaultFilesystemError) as captured:
        if operation == "read":
            await workspace.read("target.md", CancellationScope())
        else:
            await workspace.stat("target.md", CancellationScope())

    assert exchanged
    assert captured.value.code is VaultFilesystemErrorCode.CHANGED


@pytest.mark.asyncio
async def test_windows_verified_read_handle_denies_new_write_and_replace_handles(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if os.name != "nt":
        pytest.skip("Windows share-mode contract")
    target = tmp_path / "target.md"
    replacement = tmp_path / "replacement.md"
    target.write_bytes(b"authorized")
    replacement.write_bytes(b"replacement")
    workspace, _ = filesystem(tmp_path)
    original_open = workspace_filesystem_module._open_readonly_fd
    opened = threading.Event()
    release = threading.Event()

    def hold_verified_handle(path: Path) -> int:
        descriptor = original_open(path)
        if path == target:
            opened.set()
            if not release.wait(timeout=5):
                os.close(descriptor)
                raise TimeoutError("test did not release verified Vault handle")
        return descriptor

    monkeypatch.setattr(workspace_filesystem_module, "_open_readonly_fd", hold_verified_handle)
    reading = asyncio.create_task(workspace.read("target.md", CancellationScope()))
    assert await asyncio.to_thread(opened.wait, 2)
    try:
        with pytest.raises(OSError):
            await asyncio.to_thread(target.write_bytes, b"mutated")
        with pytest.raises(OSError):
            await asyncio.to_thread(os.replace, replacement, target)
    finally:
        release.set()

    result = await reading
    assert result.content == b"authorized"
    assert target.read_bytes() == b"authorized"


@pytest.mark.asyncio
async def test_list_rejects_directory_mutation_after_scandir_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "first.md").write_text("first", encoding="utf-8")
    workspace, _ = filesystem(tmp_path)
    original_scandir = os.scandir
    mutated = False

    def mutate_after_open(path: os.PathLike[str] | str) -> Any:
        nonlocal mutated
        iterator = original_scandir(path)
        if Path(path) == tmp_path and not mutated:
            (tmp_path / "late.md").write_text("late", encoding="utf-8")
            mutated = True
        return iterator

    monkeypatch.setattr(os, "scandir", mutate_after_open)

    with pytest.raises(VaultFilesystemError) as captured:
        await workspace.list("", CancellationScope())

    assert mutated
    assert captured.value.code is VaultFilesystemErrorCode.CHANGED


@pytest.mark.asyncio
async def test_cumulative_read_enforces_exact_max_plus_one(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "boundary.md"
    target.write_bytes(b"12345")
    workspace, _ = filesystem(tmp_path, max_file_bytes=4, max_return_bytes=4)
    real_fstat = os.fstat

    def underreported_fstat(descriptor: int) -> os.stat_result:
        info = real_fstat(descriptor)
        values = list(info)
        values[6] = 4
        return os.stat_result(values)

    monkeypatch.setattr(os, "fstat", underreported_fstat)

    with pytest.raises(VaultFilesystemError) as captured:
        await workspace.read("boundary.md", CancellationScope())

    assert captured.value.code is VaultFilesystemErrorCode.TOO_LARGE


@pytest.mark.asyncio
async def test_read_checks_cancellation_between_blocks(tmp_path: Path) -> None:
    target = tmp_path / "blocks.md"
    target.write_bytes(b"x" * (3 * 64 * 1024))
    workspace, _ = filesystem(
        tmp_path,
        max_file_bytes=4 * 64 * 1024,
        max_return_bytes=4 * 64 * 1024,
    )
    cancellation = CheckpointCancellation(cancel_on=4)

    with pytest.raises(OperationCancelled):
        await workspace.read("blocks.md", cancellation)

    assert cancellation.checks == 4


@pytest.mark.asyncio
async def test_transaction_is_workspace_bound_and_only_delegated(tmp_path: Path) -> None:
    workspace, executor = filesystem(tmp_path)
    transaction = VaultTransaction("call_1", "ws_1", ({"op": "create"},), 7, "idem", "approval_1")

    result = await workspace.execute_transaction(transaction, CancellationScope())

    assert result.status is ToolResultStatus.SUCCEEDED
    assert executor.transactions == [transaction]
    foreign = VaultTransaction("call_2", "ws_2", ({"op": "create"},), 7, "idem-2", "approval_2")
    with pytest.raises(VaultFilesystemError):
        await workspace.execute_transaction(foreign, CancellationScope())
