from __future__ import annotations

import json
import multiprocessing
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from pydantic import TypeAdapter

from offeragent_harness.protocol import WorkspaceInstanceId
from offeragent_harness.workspace import WorkspaceRegistry, WorkspaceRegistryCorrupt, identify_workspace_root
from offeragent_harness.workspace.identity import WorkspaceRegistryConflict


class AdvancingClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        current = self.value
        self.value += timedelta(seconds=1)
        return current


def _register_in_process(
    registry_path: str,
    vault_path: str,
    ready: Any,
    start: Any,
    results: Any,
) -> None:
    ready.put(True)
    start.wait()
    record = WorkspaceRegistry(Path(registry_path)).register(Path(vault_path))
    results.put(record.workspace_instance_id)


def test_registry_reuses_instance_for_same_canonical_root(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    registry = WorkspaceRegistry(
        tmp_path / "state" / "workspace-registry.json",
        now=AdvancingClock(),
        new_uuid=lambda: uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
    )

    first = registry.register(vault, portable_workspace_id="portable-one")
    second = registry.register(vault / ".")

    assert second.workspace_instance_id == first.workspace_instance_id
    assert second.created_at == first.created_at
    assert second.last_seen_at > first.last_seen_at
    assert second.portable_workspace_id == "portable-one"
    assert registry.lookup(vault) == second
    assert registry.list() == (second,)
    assert not (vault / ".offeragent").exists()
    assert first.workspace_instance_id == "wsi_aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    assert TypeAdapter(WorkspaceInstanceId).validate_python(first.workspace_instance_id) == first.workspace_instance_id


def test_different_roots_receive_isolated_random_instances(tmp_path: Path) -> None:
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()
    values = iter(
        (
            uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
            uuid.UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"),
        )
    )
    registry = WorkspaceRegistry(tmp_path / "registry.json", new_uuid=lambda: next(values))

    first = registry.register(first_root)
    second = registry.register(second_root)

    assert first.workspace_instance_id != second.workspace_instance_id
    assert first.root_identity.identity_hash != second.root_identity.identity_hash


def test_registry_rejects_generated_instance_id_collision(tmp_path: Path) -> None:
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()
    registry = WorkspaceRegistry(
        tmp_path / "registry.json",
        new_uuid=lambda: uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
    )
    registry.register(first_root)

    with pytest.raises(WorkspaceRegistryConflict):
        registry.register(second_root)

    assert len(registry.list()) == 1


def test_registry_rejects_duplicate_persisted_instance_ids(tmp_path: Path) -> None:
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()
    values = iter(
        (
            uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
            uuid.UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"),
        )
    )
    path = tmp_path / "registry.json"
    registry = WorkspaceRegistry(path, new_uuid=lambda: next(values))
    registry.register(first_root)
    registry.register(second_root)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["workspaces"][1]["workspace_instance_id"] = payload["workspaces"][0]["workspace_instance_id"]
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(WorkspaceRegistryCorrupt):
        registry.list()


def test_registry_serializes_concurrent_process_registration(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    registry_path = tmp_path / "registry.json"
    context = multiprocessing.get_context("spawn")
    ready = context.Queue()
    start = context.Event()
    results = context.Queue()
    processes = [
        context.Process(
            target=_register_in_process,
            args=(str(registry_path), str(vault), ready, start, results),
        )
        for _ in range(4)
    ]
    try:
        for process in processes:
            process.start()
        for _ in processes:
            assert ready.get(timeout=15) is True
        start.set()
        instance_ids = [results.get(timeout=15) for _ in processes]
        for process in processes:
            process.join(timeout=15)
            assert process.exitcode == 0
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
        ready.close()
        results.close()

    assert len(set(instance_ids)) == 1
    assert WorkspaceRegistry(registry_path).list()[0].workspace_instance_id == instance_ids[0]


def test_corrupt_registry_fails_closed_without_overwrite(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    path = tmp_path / "registry.json"
    original = b'{"version": 1, "workspaces": "not-a-list"}\n'
    path.write_bytes(original)
    registry = WorkspaceRegistry(path)

    with pytest.raises(WorkspaceRegistryCorrupt):
        registry.register(vault)

    assert path.read_bytes() == original


def test_tampered_root_identity_hash_fails_closed(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    path = tmp_path / "registry.json"
    registry = WorkspaceRegistry(path)
    registry.register(vault)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["workspaces"][0]["root_identity"]["identity_hash"] = "sha256:" + ("0" * 64)
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(WorkspaceRegistryCorrupt):
        registry.list()


def test_registry_payload_is_versioned_and_contains_no_vault_content(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "private-note.md").write_text("do not copy", encoding="utf-8")
    path = tmp_path / "registry.json"
    registry = WorkspaceRegistry(path)

    record = registry.register(vault)
    payload = json.loads(path.read_text(encoding="utf-8"))

    assert payload["version"] == 1
    assert payload["workspaces"][0]["workspace_instance_id"] == record.workspace_instance_id
    assert "do not copy" not in path.read_text(encoding="utf-8")
    assert identify_workspace_root(vault).identity_hash.startswith("sha256:")
