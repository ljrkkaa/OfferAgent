"""Qualify live Codex text, vision, tools, replay, and cleanup through sealed outputs."""

# ruff: noqa: RUF001 -- Chinese prompts are qualification inputs.

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from types import TracebackType
from typing import Any

from offeragent_harness.qualification.synthetic_interview import (
    SyntheticInterviewFixtureGenerator,
    SyntheticInterviewSemanticError,
    validate_synthetic_interview_vault,
)
from offeragent_harness.qualification.windows_product_artifact import verify_paired_windows_artifacts
from offeragent_harness.qualification.windows_product_driver import QualificationDriverClient
from offeragent_harness.qualification.windows_product_scenario import (
    BuiltProductQualificationError,
    BuiltProductQualificationSession,
    InterviewImagePage,
)
from offeragent_harness.runtime.codex_credentials import default_codex_auth_path
from offeragent_harness.workspace.portable_config import ensure_portable_workspace_config


class LiveBuiltProductQualificationError(RuntimeError):
    """The live sealed product failed one end-to-end acceptance invariant."""


@dataclass(frozen=True, slots=True)
class _FileSnapshot:
    device: int
    inode: int
    size: int
    modified_ns: int
    changed_ns: int
    sha256: str

    @classmethod
    def capture(cls, path: Path) -> _FileSnapshot:
        before = path.stat()
        payload = path.read_bytes()
        after = path.stat()
        before_identity = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        )
        after_identity = (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        )
        if before_identity != after_identity:
            raise LiveBuiltProductQualificationError("Codex auth file changed while it was snapshotted")
        return cls(*before_identity, hashlib.sha256(payload).hexdigest())


def qualify_live_built_windows_product(
    plugin_artifact: Path,
    qualification_artifact: Path,
    *,
    source_root_guard: Path,
    node_executable: Path,
    proxy_url: str,
    model: str,
    run_timeout: float = 300,
    temporary_parent: Path | None = None,
) -> dict[str, Any]:
    """Run the complete acceptance only through the paired sealed product artifacts."""

    if run_timeout <= 0:
        raise ValueError("qualification Run timeout must be positive")
    paired = verify_paired_windows_artifacts(plugin_artifact, qualification_artifact)
    guard = _absolute_directory(source_root_guard, "source root guard")
    parent = Path(tempfile.gettempdir()) if temporary_parent is None else temporary_parent
    parent = _absolute_directory(parent, "qualification temporary parent")
    auth_path = default_codex_auth_path()
    auth_before = _FileSnapshot.capture(auth_path)
    baseline_processes = _product_process_ids()
    ownership_token = secrets.token_hex(32)
    root = Path(tempfile.mkdtemp(prefix="offeragent-live-built-qualification-", dir=parent))
    marker = root / ".offeragent-qualification-owner.json"
    marker_payload = _canonical_json({"schemaVersion": 1, "token": ownership_token})
    marker.write_bytes(marker_payload)
    removed = False
    report: dict[str, Any] | None = None
    primary_error: BaseException | None = None
    primary_cause: BaseException | None = None
    try:
        vault = root / "Vault"
        local_app_data = root / "LocalAppData"
        fixture_root = root / "fixture"
        shutil.copytree(paired.plugin_root / "migration" / "target-vault", vault)
        local_app_data.mkdir()
        workspace_id = ensure_portable_workspace_config(vault).portable_workspace_id
        _initialize_vault_git(vault)
        fixture = SyntheticInterviewFixtureGenerator().generate(fixture_root)
        pages = tuple(
            InterviewImagePage(page.index, page.path.name, page.media_type, page.path.read_bytes())
            for page in fixture.pages
        )
        start_params = _product_start_params(paired.plugin_root, paired, vault, local_app_data, workspace_id)
        before_vault = _vault_markdown_snapshot(vault)
        with QualificationDriverClient(
            executable=node_executable,
            driver=paired.driver,
            working_directory=paired.qualification_root,
            source_root_guard=guard,
        ) as driver:
            scenario = BuiltProductQualificationSession(driver, start_params)
            _progress("research_browser")
            browser = scenario.qualify_research_browser()
            _progress("catalog_selection")
            selected = scenario.prepare_live_model(
                proxy_url=proxy_url,
                model=model,
                require_image=True,
            )
            _progress("text_preflight")
            text = scenario.run_text_preflight(
                "这是 sealed OfferAgent 文本预检。不要调用写工具；请只用一句中文确认文本模型路径可用。",
                timeout=run_timeout,
            )
            _progress("vision_submission")
            primary = scenario.run_interview_submission(
                pages,
                (
                    "请先读取 Agent Contract，把这三张按 1→2→3 排序的虚构中文面经视为一个不可分割的 "
                    "Interview Submission。确认每页可读后，用 interview_catalog.search 去重，精确读取所需索引或候选，"
                    "最后最多调用一次 vault.changes.apply 原子入库；不要保留候选人个人信息，不要生成标准答案。"
                ),
                timeout=run_timeout,
            )
            after_primary = _vault_markdown_snapshot(vault)
            changed_paths = _changed_paths(before_vault, after_primary)
            if not changed_paths or set(changed_paths) != set(primary.review.paths):
                raise LiveBuiltProductQualificationError(
                    "primary Vault changes differ from the exactly reviewed target set"
                )
            try:
                validate_synthetic_interview_vault(
                    after_primary,
                    changed_paths,
                    reviewed_content_hashes=dict(primary.review.after_content_hashes),
                )
            except SyntheticInterviewSemanticError as error:
                raise LiveBuiltProductQualificationError(str(error)) from error
            checkpoint_refs = _checkpoint_refs(vault)
            if len(checkpoint_refs) != 1:
                raise LiveBuiltProductQualificationError("primary Interview Submission did not create one checkpoint")
            _progress("restart_replay")
            replay = scenario.restart_and_replay(primary.run.run_id, timeout=run_timeout)
            after_replay = _vault_markdown_snapshot(vault)
            if after_replay != after_primary or _checkpoint_refs(vault) != checkpoint_refs:
                raise LiveBuiltProductQualificationError("restart replay repeated or changed a persisted side effect")
            _progress("duplicate_source")
            duplicate = scenario.run_duplicate_source(
                pages,
                (
                    "这是与前一 Run 字节和顺序完全相同的来源。请通过 Interview Catalog 的来源身份去重；"
                    "不得新建 Experience、重复增加 Question frequency 或提出新的写入。"
                ),
                timeout=run_timeout,
            )
            after_duplicate = _vault_markdown_snapshot(vault)
            if after_duplicate != after_primary or _checkpoint_refs(vault) != checkpoint_refs:
                raise LiveBuiltProductQualificationError("duplicate source changed Vault content or checkpoints")
            _progress("hosted_search")
            search = scenario.run_hosted_search(
                (
                    "这是 Hosted Web Search 资格验证。必须实际调用已提供的 Hosted Web Search，禁止仅凭已有知识回答。"
                    "请查找 OpenAI 官方 Responses Web Search 文档当前列出的一个 Web Search 工具类型，"
                    "用一句中文回答并保留 Provider 返回的内联来源引用；若无法实际搜索，请明确说明无法完成。"
                ),
                timeout=run_timeout,
            )
        report = {
            "schemaVersion": 1,
            "status": "passed",
            "artifact": {
                "sourceCommit": paired.source_commit,
                "sourceTreeSha256": paired.source_tree_sha256,
                "runtimeManifestSha256": paired.runtime_manifest_sha256,
                "sourceFreeRuntime": True,
                "transport": "stdio",
            },
            "catalog": {
                "freshness": "fresh",
                "revision": selected.catalog_revision,
                "accountBinding": selected.account_binding,
                "model": selected.model,
                "inputModalities": list(selected.input_modalities),
                "supportsHostedSearch": selected.supports_hosted_search,
            },
            "textRun": _run_report(text),
            "visionRun": {
                **_run_report(primary.run),
                "fixtureSha256": fixture.fixture_sha256,
                "orderedImageContentHashes": list(primary.ordered_image_content_hashes),
                "review": {
                    "batchId": primary.review.batch_id,
                    "reviewHash": primary.review.review_hash,
                    "paths": list(primary.review.paths),
                    "resolutionCount": 1,
                },
                "approval": {
                    "approvalId": primary.approval.approval_id,
                    "toolCallId": primary.approval.tool_call_id,
                    "argsHash": primary.approval.args_hash,
                    "decision": "allow_once",
                    "scope": "once",
                    "resolutionCount": 1,
                },
            },
            "vault": {
                "beforeSha256": _snapshot_hash(before_vault),
                "afterPrimarySha256": _snapshot_hash(after_primary),
                "afterReplaySha256": _snapshot_hash(after_replay),
                "afterDuplicateSha256": _snapshot_hash(after_duplicate),
                "changedPaths": changed_paths,
                "semanticFactsVerified": ["company", "role", "rounds", "Q1", "cross-page Q2", "Q3", "Q4"],
                "checkpointRefs": checkpoint_refs,
                "conditionalApply": True,
                "duplicateSourceNoChange": True,
            },
            "restartReplay": {
                "workerPids": list(replay.worker_pids),
                "eventCount": len(replay.events),
                "lastEventType": replay.events[-1]["type"],
                "repeatedReviewCount": 0,
            },
            "duplicateRun": _run_report(duplicate),
            "hostedSearch": {
                "supported": search.supported,
                "attemptCount": search.attempt_count,
                "run": _run_report(search.run),
                "citations": [asdict(citation) for citation in search.citations],
            },
            "researchBrowser": {
                **asdict(browser),
                "electronBrowserWindowEndToEnd": False,
                "attributionScope": "production adapter with scripted qualification PagePort",
            },
            "safety": {
                "authFilesSnapshotted": ["Codex broker-selected auth.json"],
                "authUnchanged": True,
                "externalVaultAccepted": False,
                "ownershipMarkerSha256": f"sha256:{hashlib.sha256(marker_payload).hexdigest()}",
                "realVaultUnopened": True,
                "temporaryRootName": root.name,
                "temporaryVaultWorkspaceId": workspace_id,
            },
        }
    except LiveBuiltProductQualificationError as error:
        primary_error = error
    except (BuiltProductQualificationError, OSError, subprocess.SubprocessError) as error:
        primary_error = LiveBuiltProductQualificationError(str(error))
        primary_cause = error
    except BaseException as error:
        primary_error = error

    audit_errors: list[str] = []
    auth_unchanged = False
    try:
        auth_unchanged = auth_before == _FileSnapshot.capture(auth_path)
        if not auth_unchanged:
            audit_errors.append("Codex broker-selected auth file changed")
    except BaseException as error:
        audit_errors.append(f"Codex auth safety audit failed: {type(error).__name__}")
    try:
        _remove_owned_root(root, marker_payload)
        removed = not root.exists()
    except BaseException as error:
        audit_errors.append(f"qualification owned-root cleanup failed: {type(error).__name__}")
    leaked: list[str] = []
    final_processes: dict[str, set[int]] = {}
    try:
        final_processes = _product_process_ids()
        leaked = _new_product_processes(baseline_processes, final_processes)
        if leaked:
            audit_errors.append(f"qualification leaked product processes: {leaked}")
    except BaseException as error:
        audit_errors.append(f"qualification process safety audit failed: {type(error).__name__}")
    if not removed:
        audit_errors.append("qualification temporary root was not removed")
    if audit_errors:
        audit_summary = "; ".join(audit_errors)
        if primary_error is not None:
            raise LiveBuiltProductQualificationError(f"{primary_error}; safety audit: {audit_summary}") from (
                primary_cause or primary_error
            )
        raise LiveBuiltProductQualificationError(f"qualification safety audit failed: {audit_summary}")
    if primary_error is not None:
        if primary_cause is not None:
            raise primary_error from primary_cause
        raise primary_error
    assert report is not None
    report["safety"]["authUnchanged"] = auth_unchanged
    report["safety"]["leakedProcesses"] = leaked
    report["safety"]["processBaseline"] = _process_snapshot_report(baseline_processes)
    report["safety"]["processFinal"] = _process_snapshot_report(final_processes)
    report["safety"]["temporaryRootRemoved"] = True
    return report


def _product_start_params(
    plugin_root: Path,
    paired: Any,
    vault: Path,
    local_app_data: Path,
    workspace_id: str,
) -> dict[str, Any]:
    runtime_root = plugin_root / "runtime" / "windows-x64" / "local-development"
    manifest = _json_object(runtime_root / "development-runtime-manifest.json", "Runtime manifest")
    protocol = manifest.get("protocol")
    if not isinstance(protocol, dict):
        raise LiveBuiltProductQualificationError("Runtime manifest protocol is invalid")
    protocol_version = protocol.get("minimum")
    schema_hash = protocol.get("schemaHash")
    if (
        not isinstance(protocol_version, str)
        or protocol.get("maximum") != protocol_version
        or not isinstance(schema_hash, str)
    ):
        raise LiveBuiltProductQualificationError("Runtime manifest protocol identity is invalid")
    return {
        "localAppData": str(local_app_data),
        "pluginVersion": paired.plugin_version,
        "protocolVersion": protocol_version,
        "runtimeVersion": paired.runtime_version,
        "schemaHash": schema_hash,
        "vaultRoot": str(vault),
        "workerExecutable": str(runtime_root / "offeragent-worker.exe"),
        "workspaceId": workspace_id,
    }


def _initialize_vault_git(vault: Path) -> None:
    commands = (
        ("init", "--quiet"),
        ("config", "user.name", "OfferAgent Qualification"),
        ("config", "user.email", "offeragent-qualification@localhost.invalid"),
        ("add", "--all"),
        ("commit", "--quiet", "-m", "qualification baseline"),
    )
    for command in commands:
        subprocess.run(
            ["git", *command],
            cwd=vault,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
        )


def _checkpoint_refs(vault: Path) -> list[str]:
    completed = subprocess.run(
        ["git", "for-each-ref", "--format=%(refname)", "refs/offeragent/checkpoints"],
        cwd=vault,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    return sorted(line for line in completed.stdout.splitlines() if line)


def _vault_markdown_snapshot(vault: Path) -> dict[str, str]:
    snapshot: dict[str, str] = {}
    for path in sorted(vault.rglob("*.md")):
        relative = path.relative_to(vault)
        if any(part.startswith(".") for part in relative.parts):
            continue
        payload = path.read_bytes()
        snapshot[relative.as_posix()] = f"sha256:{hashlib.sha256(payload).hexdigest()}"
    return snapshot


def _changed_paths(before: dict[str, str], after: dict[str, str]) -> list[str]:
    return sorted(path for path in before.keys() | after.keys() if before.get(path) != after.get(path))


def _snapshot_hash(snapshot: dict[str, str]) -> str:
    return f"sha256:{hashlib.sha256(_canonical_json(snapshot)).hexdigest()}"


def _run_report(run: Any) -> dict[str, Any]:
    return {
        "sessionId": run.session_id,
        "turnId": run.turn_id,
        "runId": run.run_id,
        "eventCount": len(run.events),
        "lastEventType": run.events[-1]["type"],
    }


def _absolute_directory(path: Path, label: str) -> Path:
    candidate = Path(path)
    if not candidate.is_absolute():
        raise LiveBuiltProductQualificationError(f"{label} must be absolute")
    try:
        result = candidate.resolve(strict=True)
    except OSError as error:
        raise LiveBuiltProductQualificationError(f"{label} is unavailable") from error
    if not result.is_dir():
        raise LiveBuiltProductQualificationError(f"{label} is not a directory")
    return result


def _json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8", errors="strict"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise LiveBuiltProductQualificationError(f"{label} is unavailable or malformed") from error
    if not isinstance(value, dict):
        raise LiveBuiltProductQualificationError(f"{label} is not an object")
    return value


def _canonical_json(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _remove_owned_root(root: Path, expected_marker: bytes) -> None:
    marker = root / ".offeragent-qualification-owner.json"
    try:
        info = marker.lstat()
        actual = marker.read_bytes()
    except OSError as error:
        raise LiveBuiltProductQualificationError("qualification temp ownership marker is unavailable") from error
    if marker.is_symlink() or info.st_nlink != 1 or actual != expected_marker:
        raise LiveBuiltProductQualificationError("qualification temp ownership identity differs")

    def clear_read_only(
        function: Callable[[str], object],
        path: str,
        error_info: tuple[type[BaseException], BaseException, TracebackType],
    ) -> None:
        error = error_info[1]
        if not isinstance(error, PermissionError):
            raise error
        os.chmod(path, stat.S_IWRITE)
        function(path)

    shutil.rmtree(root, onerror=clear_read_only)


def _product_process_ids() -> dict[str, set[int]]:
    names = ("offeragent-worker.exe", "offeragent-process-host.exe")
    if os.name != "nt":
        return {name: set() for name in names}
    script = (
        "$items = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | "
        "Where-Object { $_.Name -in @('offeragent-worker.exe','offeragent-process-host.exe') } | "
        "ForEach-Object { [pscustomobject]@{ name=$_.Name; pid=[int]$_.ProcessId } }); "
        "$items | ConvertTo-Json -Compress"
    )
    raw = subprocess.check_output(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        text=True,
        encoding="utf-8",
        timeout=30,
    ).strip()
    result: dict[str, set[int]] = {name: set() for name in names}
    if not raw:
        return result
    value = json.loads(raw)
    records = value if isinstance(value, list) else [value]
    for record in records:
        if not isinstance(record, dict):
            continue
        name = record.get("name")
        process_id = record.get("pid")
        if name in result and isinstance(process_id, int):
            result[name].add(process_id)
    return result


def _new_product_processes(before: dict[str, set[int]], after: dict[str, set[int]]) -> list[str]:
    return sorted(
        f"{name}:{process_id}"
        for name, process_ids in after.items()
        for process_id in process_ids - before.get(name, set())
    )


def _process_snapshot_report(snapshot: dict[str, set[int]]) -> dict[str, list[int]]:
    return {name: sorted(process_ids) for name, process_ids in sorted(snapshot.items())}


def _progress(stage: str) -> None:
    print(f"qualification.stage={stage}", file=sys.stderr, flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="通过 sealed Windows 产品执行真实 OfferAgent 资格验证")
    parser.add_argument("--plugin-artifact", type=Path, required=True)
    parser.add_argument("--qualification-artifact", type=Path, required=True)
    parser.add_argument("--source-root-guard", type=Path, required=True)
    parser.add_argument("--proxy-url", required=True)
    parser.add_argument("--model", default="gpt-5.5")
    parser.add_argument("--node-executable", type=Path)
    parser.add_argument("--run-timeout", type=float, default=300)
    args = parser.parse_args()
    node = args.node_executable
    if node is None:
        discovered = shutil.which("node.exe") or shutil.which("node")
        if discovered is None:
            raise SystemExit("Node.js is unavailable for the qualification adapter")
        node = Path(discovered)
    report = qualify_live_built_windows_product(
        args.plugin_artifact,
        args.qualification_artifact,
        source_root_guard=args.source_root_guard,
        node_executable=node,
        proxy_url=args.proxy_url,
        model=args.model,
        run_timeout=args.run_timeout,
    )
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
