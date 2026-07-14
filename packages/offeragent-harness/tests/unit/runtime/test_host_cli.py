from __future__ import annotations

import asyncio
import os
import struct
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from offeragent_harness.runtime import host_cli
from offeragent_harness.runtime.host_cli import (
    HostAttachOutcome,
    HostCliApplication,
    HostCliError,
    parse_attach_arguments,
    parse_self_test_attach_arguments,
    parse_self_test_stop_arguments,
    parse_stop_arguments,
    run_attach_contract,
    run_self_test_attach_contract,
    run_stop_contract,
)
from offeragent_harness.runtime.named_pipe import DiscoveryMaterial, serialize_discovery_material
from offeragent_harness.runtime.process_lock import ProcessAlreadyRunning

_NOW = datetime.now(timezone.utc)


def _material() -> DiscoveryMaterial:
    return DiscoveryMaterial(
        pipe_name=f"\\\\.\\pipe\\OfferAgent.{('a' * 64)}",
        bootstrap_nonce=b"b" * 32,
        issued_at=_NOW,
        expires_at=_NOW + timedelta(hours=1),
    )


class _Engine:
    def __init__(self, *, contender: bool = False) -> None:
        self.contender = contender
        self.started = 0
        self.attached: list[tuple[Path, str]] = []
        self.shutdowns = 0

    async def start(self) -> None:
        self.started += 1
        if self.contender:
            raise ProcessAlreadyRunning("Local\\OfferAgent.Host.test")

    async def attach(self, vault_root: Path, *, client_id: str) -> DiscoveryMaterial:
        self.attached.append((vault_root, client_id))
        return _material()

    async def shutdown(self) -> None:
        self.shutdowns += 1


class _Control:
    def __init__(self) -> None:
        self.starts = 0
        self.serves = 0
        self.forwards: list[tuple[Path, str]] = []
        self.stops = 0
        self.closes = 0

    async def start(self) -> None:
        self.starts += 1

    async def serve_forever(self) -> None:
        self.serves += 1

    async def forward(self, vault_root: Path, *, client_id: str) -> bytes:
        self.forwards.append((vault_root, client_id))
        return serialize_discovery_material(_material())

    async def stop_all(self) -> bytes:
        self.stops += 1
        return host_cli._canonical_stop_response()

    async def close(self) -> None:
        self.closes += 1


class _ControlStore:
    def remove(self) -> None:
        return None


class _PacketStream:
    def __init__(self, payload: bytes, events: list[str]) -> None:
        self._input = bytearray(struct.pack(">I", len(payload)) + payload)
        self.events = events
        self.writes: list[bytes] = []
        self.closed = False

    async def read(self, maximum: int) -> bytes:
        result = bytes(self._input[:maximum])
        del self._input[:maximum]
        return result

    async def write(self, payload: bytes) -> None:
        self.events.append("write")
        self.writes.append(bytes(payload))

    def cancel_pending_io(self) -> None:
        return None

    async def close(self) -> None:
        self.events.append("close")
        self.closed = True


def test_cli_accepts_only_exact_private_fd_contract() -> None:
    assert parse_attach_arguments(["attach", "--discovery-fd", "3", "--vault-root-fd", "4"]) == (3, 4)
    with pytest.raises(HostCliError, match="fixed"):
        parse_attach_arguments(["attach", "--vault-root", "E:\\secret-vault"])
    assert parse_stop_arguments(["stop-all", "--result-fd", "3"]) == 3
    with pytest.raises(HostCliError, match="fixed"):
        parse_stop_arguments(["stop-all", "--result-fd", "1"])
    nonce = "a" * 32
    assert parse_self_test_attach_arguments(
        [
            "self-test-attach",
            "--nonce",
            nonce,
            "--discovery-fd",
            "3",
            "--vault-root-fd",
            "4",
        ]
    ) == (nonce, 3, 4)
    assert parse_self_test_stop_arguments(["self-test-stop-all", "--nonce", nonce, "--result-fd", "3"]) == (nonce, 3)
    with pytest.raises(HostCliError, match="fd3/fd4"):
        parse_self_test_attach_arguments(
            [
                "self-test-attach",
                "--nonce",
                nonce,
                "--discovery-fd",
                "1",
                "--vault-root-fd",
                "4",
            ]
        )
    with pytest.raises(HostCliError, match="nonce/fd3"):
        parse_self_test_stop_arguments(["self-test-stop-all", "--nonce", "predictable", "--result-fd", "3"])


def test_vault_root_is_strict_utf8_local_absolute_directory(tmp_path: Path) -> None:
    read_fd, write_fd = os.pipe()
    os.write(write_fd, str(tmp_path).encode("utf-8"))
    os.close(write_fd)

    assert host_cli._read_vault_root(read_fd) == tmp_path.resolve()


@pytest.mark.parametrize("payload", [b"..", b"\\\\server\\vault", b"C:\\vault\n", b"\xff"])
def test_vault_root_fd_rejects_relative_remote_newline_and_invalid_utf8(payload: bytes) -> None:
    read_fd, write_fd = os.pipe()
    os.write(write_fd, payload)
    os.close(write_fd)

    with pytest.raises(HostCliError):
        host_cli._read_vault_root(read_fd)


@pytest.mark.asyncio
async def test_owner_returns_discovery_then_remains_the_host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    canonical_tmp = tmp_path
    engine = _Engine()
    control = _Control()
    application = HostCliApplication(engine=engine, control=control)
    writes: list[tuple[int, bytes]] = []
    monkeypatch.setattr(host_cli, "_read_vault_root", lambda descriptor: canonical_tmp)
    monkeypatch.setattr(host_cli, "_write_discovery", lambda descriptor, payload: writes.append((descriptor, payload)))

    await run_attach_contract(
        application,
        discovery_fd=3,
        vault_root_fd=4,
        stay_resident=True,
    )

    assert engine.started == 1
    assert engine.attached == [(canonical_tmp, f"obsidian-pid-{os.getppid()}")]
    assert control.starts == 1
    assert control.serves == 1
    assert writes == [(3, serialize_discovery_material(_material()))]
    assert engine.shutdowns == 1


@pytest.mark.asyncio
async def test_contender_forwards_to_owner_and_exits_without_second_runtime(tmp_path: Path) -> None:
    engine = _Engine(contender=True)
    control = _Control()
    application = HostCliApplication(engine=engine, control=control)

    outcome = await application.attach(tmp_path, client_id="obsidian-pid-42")

    assert outcome == HostAttachOutcome(serialize_discovery_material(_material()), False)
    assert engine.attached == []
    assert control.starts == 0
    assert control.forwards == [(tmp_path, "obsidian-pid-42")]


@pytest.mark.asyncio
async def test_control_self_test_starts_real_owner_boundary_without_attaching_a_vault() -> None:
    engine = _Engine()
    control = _Control()
    application = HostCliApplication(engine=engine, control=control)

    await application.self_test_control_start_stop()

    assert engine.started == 1
    assert engine.attached == []
    assert engine.shutdowns == 1
    assert control.starts == 1
    assert control.closes == 1


@pytest.mark.asyncio
async def test_contract_rejects_any_fd_substitution_before_reading() -> None:
    application = HostCliApplication(engine=_Engine(), control=_Control())
    with pytest.raises(HostCliError, match="fd3/fd4"):
        await run_attach_contract(application, discovery_fd=1, vault_root_fd=2, stay_resident=False)


@pytest.mark.asyncio
async def test_self_test_attach_cannot_target_a_non_synthetic_vault(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from offeragent_harness.runtime import windows_process

    nonce = "a" * 32
    sandbox = tmp_path / "sandbox"
    expected = sandbox / "Vault"
    expected.mkdir(parents=True)
    wrong = tmp_path / "real-vault"
    wrong.mkdir()
    engine = _Engine()
    application = HostCliApplication(engine=engine, control=_Control())
    monkeypatch.setattr(host_cli, "_read_vault_root", lambda descriptor: wrong)
    monkeypatch.setattr(windows_process, "self_test_runtime_sandbox_root", lambda value: sandbox)

    with pytest.raises(HostCliError, match="synthetic Vault"):
        await run_self_test_attach_contract(
            application,
            nonce=nonce,
            discovery_fd=3,
            vault_root_fd=4,
            stay_resident=False,
        )

    assert engine.started == 0


@pytest.mark.asyncio
async def test_stop_contract_forwards_authenticated_stop_and_writes_canonical_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control = _Control()
    application = HostCliApplication(engine=_Engine(), control=control, host_stopped=lambda: False)
    writes: list[tuple[int, bytes]] = []
    monkeypatch.setattr(
        host_cli,
        "_write_stop_result",
        lambda descriptor, payload: writes.append((descriptor, payload)),
    )

    await run_stop_contract(application, result_fd=3)

    assert control.stops == 1
    assert writes == [(3, host_cli._canonical_cli_stop_result("stopped"))]


@pytest.mark.asyncio
async def test_stop_contract_is_idempotent_when_no_host_exists(monkeypatch: pytest.MonkeyPatch) -> None:
    control = _Control()
    application = HostCliApplication(engine=_Engine(), control=control, host_stopped=lambda: True)
    writes: list[bytes] = []
    monkeypatch.setattr(host_cli, "_write_stop_result", lambda descriptor, payload: writes.append(payload))

    await run_stop_contract(application, result_fd=3)

    assert control.stops == 0
    assert writes == [host_cli._canonical_cli_stop_result("already_stopped")]


@pytest.mark.asyncio
async def test_stop_contract_rejects_fd_substitution_before_forwarding() -> None:
    control = _Control()
    application = HostCliApplication(engine=_Engine(), control=control)
    with pytest.raises(HostCliError, match="fd3"):
        await run_stop_contract(application, result_fd=4)
    assert control.stops == 0


@pytest.mark.asyncio
async def test_authenticated_broker_exits_accept_loop_only_after_stop_receipt_is_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    engine = _Engine()

    async def shutdown() -> None:
        events.append("shutdown")
        engine.shutdowns += 1

    async def authenticated(*args: object, **kwargs: object) -> None:
        return None

    engine.shutdown = shutdown  # type: ignore[method-assign]
    broker = host_cli.HostControlBroker(engine=engine, material_store=_ControlStore())  # type: ignore[arg-type]
    broker._material = _material()
    stream = _PacketStream(host_cli._canonical_stop_request(), events)
    monkeypatch.setattr(host_cli, "authenticate_server_stream", authenticated)

    await broker._serve_one(stream)

    assert events == ["shutdown", "write", "close"]
    assert engine.shutdowns == 1
    assert stream.closed
    assert broker._exit_after_receipt.is_set()
    response_size = struct.unpack(">I", stream.writes[0][:4])[0]
    assert stream.writes[0][4:] == host_cli._canonical_stop_response()
    assert response_size == len(stream.writes[0][4:])


@pytest.mark.asyncio
async def test_broker_rejects_new_attach_after_stop_gate_closes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stopping = asyncio.Event()
    release = asyncio.Event()
    engine = _Engine()

    async def shutdown() -> None:
        stopping.set()
        await release.wait()

    async def authenticated(*args: object, **kwargs: object) -> None:
        return None

    engine.shutdown = shutdown  # type: ignore[method-assign]
    broker = host_cli.HostControlBroker(engine=engine, material_store=_ControlStore())  # type: ignore[arg-type]
    broker._material = _material()
    monkeypatch.setattr(host_cli, "authenticate_server_stream", authenticated)
    stop_stream = _PacketStream(host_cli._canonical_stop_request(), [])
    stop = asyncio.create_task(broker._serve_one(stop_stream))
    await stopping.wait()
    attach_stream = _PacketStream(host_cli._canonical_control_request(tmp_path, "obsidian-pid-42"), [])

    with pytest.raises(HostCliError, match="no longer accepting"):
        await broker._serve_one(attach_stream)

    release.set()
    await stop
    assert engine.attached == []
