from __future__ import annotations

import asyncio
import hashlib
import os
import sqlite3
import subprocess
import sys
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO, cast

import pytest

from offeragent_harness.adapters.local_artifacts import (
    ArtifactConflict,
    ArtifactCorrupt,
    ArtifactSecurityError,
    ArtifactTooLarge,
    LocalArtifactStore,
)
from offeragent_harness.ports import ArtifactMetadata, ArtifactState, Sensitivity
from offeragent_harness.testing import FakeRunCancelled, ManualCancellationToken

_PROCESS_PUT_SCRIPT = """
import asyncio
import hashlib
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from offeragent_harness.adapters.local_artifacts import LocalArtifactStore
from offeragent_harness.ports import ArtifactMetadata, ArtifactState, Sensitivity

root = Path(sys.argv[1])
barrier = Path(sys.argv[2])
content = bytes.fromhex(sys.argv[3])
(barrier / ("ready-" + str(os.getpid()))).touch()
deadline = time.monotonic() + 20
while len(list(barrier.glob("ready-*"))) < 2:
    if time.monotonic() >= deadline:
        raise TimeoutError("process barrier timed out")
    time.sleep(0.01)
expected = ArtifactMetadata(
    artifact_id="process-artifact",
    workspace_id="ws_1",
    owner_run_id="run_1",
    mime_type="text/plain",
    byte_length=len(content),
    sha256="sha256:" + hashlib.sha256(content).hexdigest(),
    sensitivity=Sensitivity.WORKSPACE,
    state=ArtifactState.COMPLETE,
    created_at=datetime(2026, 7, 12, tzinfo=timezone.utc),
    attributes={"source": "test"},
)
asyncio.run(LocalArtifactStore(root, workspace_id="ws_1").put(expected, content, idempotency_key="process-key"))
"""


def metadata(content: bytes, *, artifact_id: str = "artifact_1", workspace_id: str = "ws_1") -> ArtifactMetadata:
    return ArtifactMetadata(
        artifact_id=artifact_id,
        workspace_id=workspace_id,
        owner_run_id="run_1",
        mime_type="text/plain",
        byte_length=len(content),
        sha256=f"sha256:{hashlib.sha256(content).hexdigest()}",
        sensitivity=Sensitivity.WORKSPACE,
        state=ArtifactState.COMPLETE,
        created_at=datetime(2026, 7, 12, tzinfo=timezone.utc),
        attributes={"source": "test"},
    )


async def read_all(store: LocalArtifactStore, artifact_id: str, **kwargs: int) -> bytes:
    return b"".join([chunk async for chunk in store.read(artifact_id, **kwargs)])


def object_path(root: Path, expected: ArtifactMetadata) -> Path:
    digest = expected.sha256.removeprefix("sha256:")
    return root / "objects" / digest[:2] / digest


def metadata_path(root: Path, artifact_id: str) -> Path:
    digest = hashlib.sha256(artifact_id.encode()).hexdigest()
    return root / "metadata" / f"{digest}.json"


class CancelAfterCheckpoints(ManualCancellationToken):
    def __init__(self, cancel_at: int) -> None:
        super().__init__()
        self.cancel_at = cancel_at
        self.checkpoints = 0

    def checkpoint(self) -> None:
        self.checkpoints += 1
        if self.checkpoints == self.cancel_at:
            self.cancel()
        super().checkpoint()


class SameHandleMutatingReader:
    """Mutate an already-read byte so only the before/after fstat can catch it."""

    def __init__(self, stream: BinaryIO) -> None:
        self.stream = stream
        self.mutated = False

    def fileno(self) -> int:
        return self.stream.fileno()

    def seek(self, offset: int, whence: int = 0) -> int:
        return self.stream.seek(offset, whence)

    def read(self, size: int = -1) -> bytes:
        chunk = self.stream.read(size)
        if chunk and not self.mutated:
            position = self.stream.tell()
            self.stream.seek(0)
            self.stream.write(b"Z" if chunk[:1] != b"Z" else b"Y")
            self.stream.flush()
            os.fsync(self.stream.fileno())
            self.stream.seek(position)
            self.mutated = True
        return chunk


def create_junction(link: Path, target: Path) -> None:
    result = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(target)],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        pytest.skip("this Windows environment cannot create a test junction")


@pytest.mark.asyncio
async def test_put_reopen_and_range_read_are_content_verified(tmp_path: Path) -> None:
    content = "中文 artifact".encode()
    expected = metadata(content)
    store = LocalArtifactStore(tmp_path / "artifacts", workspace_id="ws_1", chunk_size=3)

    assert await store.put(expected, content, idempotency_key="idem-1") == expected
    reopened = LocalArtifactStore(tmp_path / "artifacts", workspace_id="ws_1", chunk_size=2)

    assert await reopened.metadata("artifact_1") == expected
    assert await read_all(reopened, "artifact_1") == content
    assert await read_all(reopened, "artifact_1", offset=2, limit=5) == content[2:7]


@pytest.mark.asyncio
async def test_range_read_strictly_handles_end_offset_zero_limit_and_past_end(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    content = b"abcdef"
    expected = metadata(content)
    store = LocalArtifactStore(root, workspace_id="ws_1", chunk_size=2)
    await store.put(expected, content, idempotency_key="range-edges")

    assert await read_all(store, expected.artifact_id, offset=len(content)) == b""
    assert await read_all(store, expected.artifact_id, offset=2, limit=0) == b""
    with pytest.raises(ValueError, match="offset exceeds"):
        await read_all(store, expected.artifact_id, offset=len(content) + 1)

    object_path(root, expected).write_bytes(b"tamper")
    with pytest.raises(ArtifactCorrupt, match="integrity"):
        await read_all(store, expected.artifact_id, limit=0)


@pytest.mark.asyncio
async def test_same_idempotency_key_replays_and_conflicting_binding_fails(tmp_path: Path) -> None:
    store = LocalArtifactStore(tmp_path / "artifacts", workspace_id="ws_1")
    content = b"first"
    first = metadata(content)

    assert await store.put(first, content, idempotency_key="same") == first
    assert await store.put(first, content, idempotency_key="same") == first
    with pytest.raises(ArtifactConflict, match="different request"):
        await store.put(metadata(b"second", artifact_id="artifact_2"), b"second", idempotency_key="same")


@pytest.mark.asyncio
async def test_commit_ack_loss_replays_without_removing_or_overwriting_committed_files(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    content = b"commit succeeded before transport ACK was lost"
    expected = metadata(content, artifact_id="ack-loss")
    hook_calls = 0

    def lose_first_ack(_database_path: Path) -> None:
        nonlocal hook_calls
        hook_calls += 1
        if hook_calls == 1:
            raise OSError("simulated ACK loss")

    store = LocalArtifactStore(root, workspace_id="ws_1", _commit_hook=lose_first_ack)
    with pytest.raises(OSError, match="ACK loss"):
        await store.put(expected, content, idempotency_key="ack-key")

    reopened = LocalArtifactStore(root, workspace_id="ws_1")
    assert await reopened.metadata(expected.artifact_id) == expected
    assert await reopened.put(expected, content, idempotency_key="ack-key") == expected
    with pytest.raises(ArtifactConflict, match="different request"):
        await reopened.put(
            metadata(b"different", artifact_id="different-after-ack"),
            b"different",
            idempotency_key="ack-key",
        )
    assert await read_all(reopened, expected.artifact_id) == content


@pytest.mark.asyncio
async def test_orphan_metadata_only_accepts_identical_recovery_and_never_overwrites(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    original = metadata(b"original", artifact_id="orphan-metadata")
    store = LocalArtifactStore(root, workspace_id="ws_1")
    await store.put(original, b"original", idempotency_key="before-crash")

    with sqlite3.connect(root / ".artifact-store.sqlite3") as connection:
        connection.execute("DELETE FROM artifact_idempotency")
        connection.execute("DELETE FROM artifact_entries")

    assert await store.metadata(original.artifact_id) is None
    divergent = metadata(b"divergent", artifact_id=original.artifact_id)
    with pytest.raises(ArtifactCorrupt, match="orphan corruption"):
        await store.put(divergent, b"divergent", idempotency_key="divergent-retry")
    assert await store.metadata(original.artifact_id) is None

    assert await store.put(original, b"original", idempotency_key="identical-retry") == original
    assert await read_all(store, original.artifact_id) == b"original"


@pytest.mark.asyncio
async def test_two_store_instances_atomically_bind_the_same_key(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    first_store = LocalArtifactStore(root, workspace_id="ws_1")
    second_store = LocalArtifactStore(root, workspace_id="ws_1")
    content = b"shared across independent Store instances"
    expected = metadata(content)

    results = await asyncio.gather(
        first_store.put(expected, content, idempotency_key="cross-store"),
        second_store.put(expected, content, idempotency_key="cross-store"),
    )

    assert len(results) == 2
    assert all(result == expected for result in results)
    assert await read_all(first_store, expected.artifact_id) == content


@pytest.mark.asyncio
async def test_two_store_instances_cannot_race_conflicting_key_bindings(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    stores = [LocalArtifactStore(root, workspace_id="ws_1") for _ in range(2)]
    requests = [
        stores[0].put(metadata(b"first", artifact_id="first"), b"first", idempotency_key="winner"),
        stores[1].put(metadata(b"second", artifact_id="second"), b"second", idempotency_key="winner"),
    ]

    results = await asyncio.gather(*requests, return_exceptions=True)

    assert sum(isinstance(result, ArtifactMetadata) for result in results) == 1
    assert sum(isinstance(result, ArtifactConflict) for result in results) == 1


@pytest.mark.skipif(os.name != "nt", reason="production Store is a Windows runtime component")
@pytest.mark.asyncio
async def test_two_processes_share_the_atomic_idempotency_binding(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    barrier = tmp_path / "barrier"
    barrier.mkdir()
    content = b"cross-process content"
    processes: list[asyncio.subprocess.Process] = []
    for _ in range(2):
        processes.append(
            await asyncio.create_subprocess_exec(
                sys.executable,
                "-c",
                _PROCESS_PUT_SCRIPT,
                str(root),
                str(barrier),
                content.hex(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        )

    for process in processes:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=30)
        assert process.returncode == 0, f"child stdout={stdout.decode()!r}, stderr={stderr.decode()!r}"

    store = LocalArtifactStore(root, workspace_id="ws_1")
    assert await store.metadata("process-artifact") == metadata(content, artifact_id="process-artifact")
    assert await read_all(store, "process-artifact") == content


@pytest.mark.asyncio
async def test_rejects_hash_workspace_and_secret_boundary_violations(tmp_path: Path) -> None:
    store = LocalArtifactStore(tmp_path / "artifacts", workspace_id="ws_1")
    content = b"content"
    wrong_hash = replace(metadata(content), sha256="sha256:" + "0" * 64)
    with pytest.raises(ArtifactConflict, match="length/hash"):
        await store.put(wrong_hash, content, idempotency_key="hash")
    with pytest.raises(ArtifactConflict, match="different workspace"):
        await store.put(metadata(content, workspace_id="ws_2"), content, idempotency_key="workspace")
    secret = replace(metadata(content), sensitivity=Sensitivity.SECRET)
    with pytest.raises(ArtifactConflict, match="SecretStore"):
        await store.put(secret, content, idempotency_key="secret")


@pytest.mark.asyncio
async def test_tampered_content_fails_closed_before_streaming(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    store = LocalArtifactStore(root, workspace_id="ws_1")
    content = b"trusted"
    expected = metadata(content)
    await store.put(expected, content, idempotency_key="idem")
    path = object_path(root, expected)
    for tampered in (b"short", b"TRUSTED", b"trusted-and-longer"):
        path.write_bytes(tampered)
        with pytest.raises(ArtifactCorrupt, match="integrity"):
            await read_all(store, expected.artifact_id)


def test_verification_rejects_same_size_mutation_detected_only_by_fstat(tmp_path: Path) -> None:
    content = b"abcdefgh"
    expected = metadata(content, artifact_id="fstat-mutation")
    store = LocalArtifactStore(tmp_path / "unused-root", workspace_id="ws_1", chunk_size=2)
    mutable = tmp_path / "mutable-object"
    mutable.write_bytes(content)

    with mutable.open("r+b", buffering=0) as stream:
        wrapped = SameHandleMutatingReader(stream)
        with pytest.raises(ArtifactCorrupt, match="integrity"):
            store._verify_open_stream(cast(BinaryIO, wrapped), expected, None)

    assert wrapped.mutated


@pytest.mark.asyncio
async def test_overlong_sparse_object_is_rejected_after_only_length_plus_one_probe(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    content = b"trusted"
    expected = metadata(content, artifact_id="sparse-tamper")
    store = LocalArtifactStore(root, workspace_id="ws_1", chunk_size=2)
    await store.put(expected, content, idempotency_key="sparse")

    with object_path(root, expected).open("r+b", buffering=0) as stream:
        stream.truncate(256 * 1024 * 1024)

    with pytest.raises(ArtifactCorrupt, match="integrity"):
        await asyncio.wait_for(read_all(store, expected.artifact_id), timeout=2)


@pytest.mark.asyncio
async def test_oversized_sparse_metadata_file_is_rejected_without_unbounded_read(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    content = b"trusted"
    expected = metadata(content, artifact_id="sparse-metadata")
    store = LocalArtifactStore(root, workspace_id="ws_1")
    await store.put(expected, content, idempotency_key="sparse-metadata")

    with metadata_path(root, expected.artifact_id).open("r+b", buffering=0) as stream:
        stream.truncate(256 * 1024 * 1024)

    with pytest.raises(ArtifactCorrupt, match=r"hard limit|exceeds"):
        await asyncio.wait_for(store.metadata(expected.artifact_id), timeout=2)


@pytest.mark.asyncio
async def test_large_object_verification_observes_cancellation_between_bounded_blocks(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    content = b"x" * (4 * 1024 * 1024)
    expected = metadata(content, artifact_id="cancel-verification")
    store = LocalArtifactStore(root, workspace_id="ws_1", chunk_size=1024)
    await store.put(expected, content, idempotency_key="cancel-verification")
    cancellation = CancelAfterCheckpoints(cancel_at=3)

    with pytest.raises(FakeRunCancelled):
        await asyncio.to_thread(store._verify_object, expected, cancellation)

    assert cancellation.checkpoints == 3


@pytest.mark.asyncio
async def test_idempotent_replay_reverifies_a_tampered_object(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    store = LocalArtifactStore(root, workspace_id="ws_1")
    content = b"trusted"
    expected = metadata(content)
    await store.put(expected, content, idempotency_key="idem")
    digest = expected.sha256.removeprefix("sha256:")
    (root / "objects" / digest[:2] / digest).write_bytes(b"tampered")

    with pytest.raises(ArtifactCorrupt, match="integrity"):
        await store.put(expected, content, idempotency_key="idem")


@pytest.mark.asyncio
async def test_idempotency_collision_and_corrupt_binding_never_replace_original_content(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    content = b"original content"
    expected = metadata(content, artifact_id="idempotency-record")
    store = LocalArtifactStore(root, workspace_id="ws_1")
    await store.put(expected, content, idempotency_key="bound-key")
    key_digest = hashlib.sha256(b"bound-key").hexdigest()

    with sqlite3.connect(root / ".artifact-store.sqlite3") as connection:
        connection.execute(
            "UPDATE artifact_idempotency SET request_digest = ? WHERE key_digest = ?",
            ("0" * 64, key_digest),
        )
    with pytest.raises(ArtifactConflict, match="different request"):
        await store.put(expected, content, idempotency_key="bound-key")
    assert await read_all(store, expected.artifact_id) == content

    with sqlite3.connect(root / ".artifact-store.sqlite3") as connection:
        connection.execute(
            "UPDATE artifact_idempotency SET artifact_id = ? WHERE key_digest = ?",
            ("../escape", key_digest),
        )
    with pytest.raises(ArtifactCorrupt, match="unsafe idempotency artifact ID"):
        await store.put(expected, content, idempotency_key="bound-key")
    assert await read_all(store, expected.artifact_id) == content


@pytest.mark.asyncio
async def test_corrupt_sqlite_metadata_blob_type_fails_closed(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    content = b"trusted"
    expected = metadata(content, artifact_id="corrupt-record")
    store = LocalArtifactStore(root, workspace_id="ws_1")
    await store.put(expected, content, idempotency_key="corrupt-record")

    with sqlite3.connect(root / ".artifact-store.sqlite3") as connection:
        persisted = connection.execute(
            "SELECT metadata_json FROM artifact_entries WHERE artifact_id = ?",
            (expected.artifact_id,),
        ).fetchone()
        assert persisted is not None
        connection.execute(
            "UPDATE artifact_entries SET metadata_json = CAST(? AS TEXT) WHERE artifact_id = ?",
            (bytes(persisted[0]).decode(), expected.artifact_id),
        )

    with pytest.raises(ArtifactCorrupt, match="storage type"):
        await store.metadata(expected.artifact_id)


@pytest.mark.asyncio
async def test_crash_orphan_object_is_reverified_before_recovery_commit(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    store = LocalArtifactStore(root, workspace_id="ws_1")
    content = b"recoverable"
    expected = metadata(content, artifact_id="recovered")
    assert await store.metadata(expected.artifact_id) is None
    digest = expected.sha256.removeprefix("sha256:")
    object_path = root / "objects" / digest[:2] / digest
    object_path.parent.mkdir()
    object_path.write_bytes(b"crash left a corrupt object")

    with pytest.raises(ArtifactCorrupt, match="integrity"):
        await store.put(expected, content, idempotency_key="crash-replay")
    assert await store.metadata(expected.artifact_id) is None

    object_path.write_bytes(content)
    assert await store.put(expected, content, idempotency_key="crash-replay") == expected
    assert await read_all(store, expected.artifact_id) == content


@pytest.mark.asyncio
async def test_artifact_ids_cannot_escape_metadata_directory(tmp_path: Path) -> None:
    store = LocalArtifactStore(tmp_path / "artifacts", workspace_id="ws_1")
    with pytest.raises(ValueError, match="filesystem-safe"):
        await store.metadata("../outside")


@pytest.mark.asyncio
async def test_case_distinct_ids_use_digest_metadata_names_on_windows(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    store = LocalArtifactStore(root, workspace_id="ws_1")
    upper = metadata(b"upper", artifact_id="CaseSensitive")
    lower = metadata(b"lower", artifact_id="casesensitive")

    await store.put(upper, b"upper", idempotency_key="upper")
    await store.put(lower, b"lower", idempotency_key="lower")

    assert await store.metadata("CaseSensitive") == upper
    assert await store.metadata("casesensitive") == lower
    metadata_files = list((root / "metadata").glob("*.json"))
    assert len(metadata_files) == 2
    assert all(len(path.stem) == 64 and path.stem.isalnum() for path in metadata_files)


@pytest.mark.asyncio
async def test_stream_put_is_bounded_cancellable_and_leaves_no_visible_metadata(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    store = LocalArtifactStore(root, workspace_id="ws_1")
    token = ManualCancellationToken()
    expected = metadata(b"ab", artifact_id="cancelled")

    async def cancelled_content() -> AsyncIterator[bytes]:
        yield b"a"
        token.cancel()
        yield b"b"

    with pytest.raises(FakeRunCancelled):
        await store.put_stream(
            expected,
            cancelled_content(),
            idempotency_key="cancelled",
            max_bytes=2,
            cancellation=token,
        )

    assert await store.metadata(expected.artifact_id) is None
    assert not list((root / "metadata").glob("*.json"))
    assert not list((root / ".staging").glob("*.tmp"))

    async def oversized_content() -> AsyncIterator[bytes]:
        yield b"ab"

    with pytest.raises(ArtifactTooLarge, match="max_bytes"):
        await store.put_stream(
            metadata(b"a", artifact_id="oversized"),
            oversized_content(),
            idempotency_key="oversized",
            max_bytes=1,
            cancellation=ManualCancellationToken(),
        )
    assert await store.metadata("oversized") is None


@pytest.mark.asyncio
async def test_stream_put_publishes_only_after_complete_hash_verified_staging(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    store = LocalArtifactStore(root, workspace_id="ws_1", chunk_size=2)
    content = "完整流式制品".encode()
    expected = metadata(content, artifact_id="streamed")

    async def chunks() -> AsyncIterator[bytes]:
        for start in range(0, len(content), 3):
            await asyncio.sleep(0)
            yield content[start : start + 3]

    stored = await store.put_stream(
        expected,
        chunks(),
        idempotency_key="streamed",
        max_bytes=len(content),
        cancellation=ManualCancellationToken(),
    )

    assert stored == expected
    assert await read_all(store, expected.artifact_id) == content


@pytest.mark.asyncio
async def test_failed_stream_does_not_publish_metadata(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    store = LocalArtifactStore(root, workspace_id="ws_1")

    async def failing_content() -> AsyncIterator[bytes]:
        yield b"partial"
        raise RuntimeError("producer failed")

    with pytest.raises(RuntimeError, match="producer failed"):
        await store.put_stream(
            metadata(b"partial-rest", artifact_id="failed"),
            failing_content(),
            idempotency_key="failed",
            max_bytes=1024,
            cancellation=ManualCancellationToken(),
        )

    assert await store.metadata("failed") is None
    assert not list((root / "metadata").glob("*.json"))


@pytest.mark.skipif(os.name != "nt", reason="junction semantics are Windows-specific")
@pytest.mark.asyncio
async def test_root_and_layout_junctions_fail_closed(tmp_path: Path) -> None:
    outside_root = tmp_path / "outside-root"
    outside_root.mkdir()
    root_junction = tmp_path / "root-junction"
    create_junction(root_junction, outside_root)
    try:
        store = LocalArtifactStore(root_junction, workspace_id="ws_1")
        with pytest.raises(ArtifactSecurityError, match="reparse"):
            await store.metadata("artifact_1")
    finally:
        os.rmdir(root_junction)

    root = tmp_path / "artifacts"
    root.mkdir()
    outside_metadata = tmp_path / "outside-metadata"
    outside_metadata.mkdir()
    metadata_junction = root / "metadata"
    create_junction(metadata_junction, outside_metadata)
    try:
        store = LocalArtifactStore(root, workspace_id="ws_1")
        with pytest.raises(ArtifactSecurityError, match="reparse"):
            await store.put(metadata(b"content"), b"content", idempotency_key="junction")
    finally:
        os.rmdir(metadata_junction)


@pytest.mark.asyncio
async def test_store_revalidates_root_identity_on_every_operation(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    store = LocalArtifactStore(root, workspace_id="ws_1")
    assert await store.metadata("artifact_1") is None
    original = tmp_path / "original-artifacts"
    root.rename(original)
    root.mkdir()

    with pytest.raises(ArtifactSecurityError, match="identity changed"):
        await store.metadata("artifact_1")


@pytest.mark.skipif(os.name != "nt", reason="junction semantics are Windows-specific")
@pytest.mark.asyncio
async def test_toctou_parent_junction_swap_is_detected_before_object_bytes_are_read(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    content = b"trusted object"
    expected = metadata(content)
    outside = tmp_path / "outside"
    outside.mkdir()
    swapped: dict[str, Path] = {}

    def swap_parent(_event: str, object_path: Path) -> None:
        if swapped:
            return
        shard = object_path.parent
        backup = shard.with_name(f"{shard.name}-trusted")
        outside_shard = outside / shard.name
        outside_shard.mkdir()
        (outside_shard / object_path.name).write_bytes(b"outside bytes must not be consumed")
        shard.rename(backup)
        create_junction(shard, outside_shard)
        swapped.update(shard=shard, backup=backup)

    store = LocalArtifactStore(root, workspace_id="ws_1", _security_hook=swap_parent)
    await store.put(expected, content, idempotency_key="toctou")
    try:
        with pytest.raises(ArtifactSecurityError, match="reparse"):
            await read_all(store, expected.artifact_id)
    finally:
        if swapped:
            os.rmdir(swapped["shard"])
            swapped["backup"].rename(swapped["shard"])


@pytest.mark.skipif(os.name != "nt", reason="Windows share-mode contract")
@pytest.mark.asyncio
async def test_read_handle_denies_concurrent_write_and_delete_sharing(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    store = LocalArtifactStore(root, workspace_id="ws_1", chunk_size=2)
    content = b"abcdef"
    expected = metadata(content)
    await store.put(expected, content, idempotency_key="sharing")
    digest = expected.sha256.removeprefix("sha256:")
    object_path = root / "objects" / digest[:2] / digest
    iterator = store.read(expected.artifact_id)

    assert await anext(iterator) == b"ab"
    try:
        with pytest.raises(PermissionError):
            object_path.write_bytes(b"overwrite")
        with pytest.raises(PermissionError):
            object_path.unlink()
        with pytest.raises(PermissionError):
            object_path.rename(object_path.with_suffix(".replaced"))
    finally:
        assert b"".join([chunk async for chunk in iterator]) == b"cdef"


@pytest.mark.skipif(os.name == "nt", reason="Windows prevents this mutation through share flags")
@pytest.mark.asyncio
async def test_posix_range_read_detects_object_growth_from_fstat_change(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    content = b"abcdef"
    expected = metadata(content, artifact_id="posix-growth")
    store = LocalArtifactStore(root, workspace_id="ws_1", chunk_size=2)
    await store.put(expected, content, idempotency_key="posix-growth")
    iterator = store.read(expected.artifact_id)

    assert await anext(iterator) == b"ab"
    with object_path(root, expected).open("ab", buffering=0) as stream:
        stream.write(b"growth")

    with pytest.raises(ArtifactCorrupt, match="changed while open"):
        await anext(iterator)


@pytest.mark.skipif(os.name == "nt", reason="Windows prevents this mutation through share flags")
@pytest.mark.asyncio
async def test_posix_range_read_detects_concurrent_path_replacement(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    content = b"abcdef"
    expected = metadata(content, artifact_id="posix-replacement")
    store = LocalArtifactStore(root, workspace_id="ws_1", chunk_size=2)
    await store.put(expected, content, idempotency_key="posix-replacement")
    path = object_path(root, expected)
    iterator = store.read(expected.artifact_id)

    assert await anext(iterator) == b"ab"
    path.rename(path.with_suffix(".original"))
    path.write_bytes(content)

    with pytest.raises((ArtifactCorrupt, ArtifactSecurityError), match="changed"):
        await anext(iterator)
