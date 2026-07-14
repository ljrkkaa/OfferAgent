from __future__ import annotations

import shutil
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

import pytest

from offeragent_harness.ports import CancellationToken
from offeragent_harness.ports.vault import VaultTransaction
from offeragent_harness.sessions import AgentLineage
from offeragent_harness.shell import PowerShellToolExecutor, powershell_tool_definitions
from offeragent_harness.testing import ManualCancellationToken
from offeragent_harness.tools import ToolCall, ToolDefinition, ToolResult, ToolResultStatus, canonical_json_sha256
from offeragent_harness.tools.results import MAX_TOOL_RESULT_SOURCE_REFERENCES
from offeragent_harness.workspace import (
    CodeToolExecutor,
    VaultFileSystem,
    VaultReadPolicy,
    WorkspacePathPolicy,
    code_tool_definitions,
)


class _Transactions:
    async def execute(self, transaction: VaultTransaction, cancellation: CancellationToken) -> ToolResult:
        del transaction, cancellation
        raise AssertionError("read-only code tools must not execute a Vault transaction")


def _source(root: Path) -> VaultFileSystem:
    return VaultFileSystem(
        workspace_id="ws_test",
        paths=WorkspacePathPolicy(root),
        read_policy=VaultReadPolicy(
            allowed_extensions=None,
            max_file_bytes=1024 * 1024,
            max_return_bytes=1024 * 1024,
            max_list_entries=10_000,
            max_list_scan_entries=100_000,
            allowed_hidden_prefixes=(".claude",),
        ),
        workspace_revision=lambda: 1,
        transaction_executor=_Transactions(),
    )


@pytest.fixture(scope="module")
def ripgrep_executable() -> Path:
    executable = shutil.which("rg.exe")
    if executable is None:
        pytest.skip("ripgrep is unavailable on this test host")
    return Path(executable).resolve(strict=True)


def _call(name: str, arguments: dict[str, object], *, definitions: tuple[ToolDefinition, ...]) -> ToolCall:
    definition = next(item for item in definitions if item.name == name)
    return ToolCall(
        tool_call_id=f"call-{name}",
        run_id="run_test",
        workspace_id="ws_test",
        name=name,
        version=definition.version,
        arguments=arguments,
        args_hash=canonical_json_sha256(arguments),
        idempotency_key=f"idem-{name}",
        deadline=None,
        lineage=AgentLineage.root("run_test"),
        definition_fingerprint=definition.fingerprint,
        result_sensitivity=definition.result_sensitivity,
    )


@pytest.mark.asyncio
async def test_glob_grep_and_read_use_a_verified_workspace_and_ripgrep(
    tmp_path: Path,
    ripgrep_executable: Path,
) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("def target():\n    return 'needle'\n", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("needle\n", encoding="utf-8")
    executor = CodeToolExecutor(
        workspace_id="ws_test",
        source=_source(tmp_path),
        workspace_root=tmp_path,
        ripgrep_path=ripgrep_executable,
    )
    cancellation = ManualCancellationToken()

    glob = await executor.execute(
        _call("glob", {"pattern": "src/**/*.py"}, definitions=code_tool_definitions()), cancellation
    )
    grep = await executor.execute(
        _call("grep", {"pattern": "needle", "glob": "*.py"}, definitions=code_tool_definitions()), cancellation
    )
    read = await executor.execute(
        _call("read", {"path": "src/main.py", "startLine": 2}, definitions=code_tool_definitions()), cancellation
    )

    assert glob.status is ToolResultStatus.SUCCEEDED
    glob_data = _data(glob)
    assert glob_data["pattern"] == "src/**/*.py"
    assert glob_data["path"] == ""
    assert glob_data["files"] == ("src/main.py",)
    assert glob_data["truncated"] is False
    assert len(glob.side_effects) == 1
    assert glob.side_effects[0].metadata["sourceCount"] == 1
    assert grep.status is ToolResultStatus.SUCCEEDED
    grep_data = _data(grep)
    matches = cast(list[dict[str, Any]], grep_data["matches"])
    assert matches[0]["path"] == "src/main.py"
    assert read.status is ToolResultStatus.SUCCEEDED
    read_data = _data(read)
    assert read_data["lines"] == ({"number": 2, "text": "    return 'needle'", "truncated": False},)


def test_root_search_contract_is_declared_in_tool_schema() -> None:
    definitions = {item.name: item for item in code_tool_definitions()}
    for name in ("glob", "grep"):
        assert "omit the optional path argument" in definitions[name].description
        assert "'.' and '..' are not valid workspace paths" in definitions[name].description


@pytest.mark.asyncio
async def test_search_tools_truncate_before_exceeding_tool_result_provenance_limit(
    tmp_path: Path,
    ripgrep_executable: Path,
) -> None:
    for index in range(MAX_TOOL_RESULT_SOURCE_REFERENCES + 1):
        (tmp_path / f"note-{index:03}.md").write_text("needle\n", encoding="utf-8")
    executor = CodeToolExecutor(
        workspace_id="ws_test",
        source=_source(tmp_path),
        workspace_root=tmp_path,
        ripgrep_path=ripgrep_executable,
    )
    cancellation = ManualCancellationToken()

    glob = await executor.execute(_call("glob", {"pattern": "*.md"}, definitions=code_tool_definitions()), cancellation)
    grep = await executor.execute(
        _call("grep", {"pattern": "needle", "glob": "*.md"}, definitions=code_tool_definitions()), cancellation
    )

    assert glob.status is ToolResultStatus.SUCCEEDED
    assert grep.status is ToolResultStatus.SUCCEEDED
    assert _data(glob)["truncated"] is True
    assert _data(grep)["truncated"] is True
    assert len(glob.source_references) == MAX_TOOL_RESULT_SOURCE_REFERENCES
    assert 0 < len(grep.source_references) <= MAX_TOOL_RESULT_SOURCE_REFERENCES
    assert len(glob.side_effects) == len(grep.side_effects) == 1


@pytest.mark.asyncio
async def test_powershell_tool_is_a_real_discovered_local_executor(tmp_path: Path) -> None:
    executable = shutil.which("powershell.exe")
    if executable is None:
        pytest.skip("PowerShell is unavailable on this platform")
    executor = PowerShellToolExecutor(workspace_id="ws_test", workspace_root=tmp_path, executable=executable)
    result = await executor.execute(
        _call("shell.powershell", {"command": "Write-Output offeragent"}, definitions=powershell_tool_definitions()),
        ManualCancellationToken(),
    )

    assert result.status is ToolResultStatus.SUCCEEDED
    data = _data(result)
    assert data["exitCode"] == 0
    assert "offeragent" in data["stdout"]


def _data(result: ToolResult) -> Mapping[str, Any]:
    assert isinstance(result.data, Mapping)
    return cast(Mapping[str, Any], result.data)
