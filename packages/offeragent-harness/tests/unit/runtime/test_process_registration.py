from __future__ import annotations

import os
from datetime import timedelta
from pathlib import Path

import pytest

from offeragent_harness.adapters.sqlite_stores import SqliteUnitOfWorkFactory
from offeragent_harness.ports import ApplicationCommandContext, ProcessStdinMode
from offeragent_harness.protocol._base import WireModel
from offeragent_harness.protocol.messages import validate_command_params
from offeragent_harness.runtime.process_registration import (
    PROCESS_REGISTRATION_RECEIPT_COLLECTION,
    EnvironmentRegistrationProposal,
    ExecutableRegistrationProposal,
    ProcessFilesystemRegistration,
    ProcessRegistrationConflict,
    ProcessRegistrationError,
    WorkspaceProcessRegistrationService,
    merge_process_registration_snapshot,
)
from offeragent_harness.runtime.process_registration_application_handlers import (
    process_registration_command_handlers,
)
from offeragent_harness.runtime.process_supervisor import (
    ExecutableTrust,
    ProcessEnvironmentProfile,
    ProcessFilesystemAccess,
    ProcessProfileError,
)
from offeragent_harness.testing import ManualCancellationToken, ManualClock


class _Ids:
    def __init__(self) -> None:
        self.value = 0

    def new_id(self, namespace: str) -> str:
        self.value += 1
        return f"{namespace}_{self.value:032x}"


class _Authenticode:
    def __init__(self, verified: bool = False) -> None:
        self.verified = verified

    def verify(self, executable: Path) -> bool:
        assert executable.is_absolute()
        return self.verified


def _service(
    tmp_path: Path,
    *,
    workspace_id: str = "ws_test",
    clock: ManualClock | None = None,
    authenticode: _Authenticode | None = None,
) -> WorkspaceProcessRegistrationService:
    return WorkspaceProcessRegistrationService(
        workspace_id=workspace_id,
        unit_of_work=SqliteUnitOfWorkFactory(tmp_path / "state.sqlite"),
        clock=clock or ManualClock(),
        ids=_Ids(),
        authenticode=authenticode or _Authenticode(),
        builtin_executables=(),
        builtin_environments=(ProcessEnvironmentProfile("minimal", frozenset(), frozenset()),),
        allowed_workspace_root_ids=frozenset({"process-scratch", "vault"}),
    )


def _executable(tmp_path: Path, name: str = "safe-tool.exe") -> Path:
    root = tmp_path / "tool"
    root.mkdir(exist_ok=True)
    target = root / name
    target.write_bytes(b"MZ-safe-test-image")
    return target.resolve(strict=True)


def _proposal(path: Path, **overrides: object) -> ExecutableRegistrationProposal:
    values: dict[str, object] = {
        "executable_id": "user-tool",
        "executable_path": str(path),
        "fixed_arguments": ("serve",),
        "minimum_variable_arguments": 0,
        "maximum_variable_arguments": 2,
        "variable_argument_pattern": r"^[A-Za-z0-9_.-]{1,64}$",
        "environment_profile_ids": frozenset({"minimal"}),
        "allowed_stdin_modes": frozenset({ProcessStdinMode.CLOSED, ProcessStdinMode.DUPLEX}),
        "allowed_cwd_root_ids": frozenset({"process-scratch"}),
        "appcontainer_filesystem": (
            ProcessFilesystemRegistration("process-scratch", "working", ProcessFilesystemAccess.READ_WRITE),
        ),
    }
    values.update(overrides)
    return ExecutableRegistrationProposal(**values)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_application_boundary_is_direct_stdio_only_and_binds_probe_to_connection(tmp_path: Path) -> None:
    handlers = process_registration_command_handlers(_service(tmp_path))
    cancellation = ManualCancellationToken()
    list_params = validate_command_params("process/registrations/list", {})
    with pytest.raises(ValueError, match="direct stdio"):
        ApplicationCommandContext("loopback-http", "browser", "127.0.0.1")

    context = ApplicationCommandContext("stdio", "stdio-connection-1", "parent-process")
    probe = await handlers["process/registrations/probe"](
        validate_command_params(
            "process/registrations/probe",
            {
                "executable": None,
                "environment": {
                    "profileId": "user-env",
                    "allowedNames": ["LANG"],
                    "allowedSecretNames": ["GITHUB_TOKEN"],
                    "expectedRevision": 0,
                    "expectedContentHash": None,
                },
            },
        ),
        cancellation,
        context,
    )
    assert isinstance(probe, WireModel)
    probe_wire = probe.to_wire()
    assert probe_wire["kind"] == "environment"
    confirmed = await handlers["process/registrations/confirm"](
        validate_command_params(
            "process/registrations/confirm",
            {
                "challengeId": probe_wire["challengeId"],
                "expectedProbeContentHash": probe_wire["contentHash"],
                "expectedCatalogRevision": 0,
                "clientRequestId": "req_process_handler_1",
            },
        ),
        cancellation,
        context,
    )
    assert isinstance(confirmed, WireModel)
    assert confirmed.to_wire()["restartRequired"] is True
    listed = await handlers["process/registrations/list"](list_params, cancellation, context)
    assert isinstance(listed, WireModel)
    listed_wire = listed.to_wire()
    assert listed_wire["catalogRevision"] == 1
    assert listed_wire["activeCatalogRevision"] == 0
    assert listed_wire["restartRequired"] is True


@pytest.mark.asyncio
async def test_environment_and_executable_confirm_are_durable_and_replayable(tmp_path: Path) -> None:
    service = _service(tmp_path)
    environment_probe = await service.probe_environment(
        client_id="pipe-1",
        proposal=EnvironmentRegistrationProposal(
            "user-env",
            frozenset({"LANG"}),
            frozenset({"GITHUB_TOKEN"}),
        ),
    )
    environment_result = await service.confirm(
        client_id="pipe-1",
        challenge_id=environment_probe.challenge_id,
        expected_probe_content_hash=environment_probe.content_hash,
        expected_catalog_revision=0,
        client_request_id="req_environment_1",
    )
    assert environment_result.restart_required is True

    executable_probe = await service.probe_executable(
        client_id="pipe-1",
        proposal=_proposal(
            _executable(tmp_path),
            environment_profile_ids=frozenset({"user-env"}),
        ),
    )
    executable_result = await service.confirm(
        client_id="pipe-1",
        challenge_id=executable_probe.challenge_id,
        expected_probe_content_hash=executable_probe.content_hash,
        expected_catalog_revision=1,
        client_request_id="req_executable_1",
    )
    assert executable_result.catalog_revision == 2

    restarted = _service(tmp_path)
    replay = await restarted.confirm(
        client_id="pipe-new-connection",
        challenge_id=executable_probe.challenge_id,
        expected_probe_content_hash=executable_probe.content_hash,
        expected_catalog_revision=1,
        client_request_id="req_executable_1",
    )
    assert replay == executable_result
    snapshot = await restarted.runtime_snapshot()
    assert [item.profile_id for item in snapshot.environment_profiles] == ["user-env"]
    assert [item.executable_id for item in snapshot.executable_profiles] == ["user-tool"]
    assert snapshot.executable_profiles[0].allow_network is False
    merged_executables, merged_environments = merge_process_registration_snapshot(
        (),
        (ProcessEnvironmentProfile("minimal", frozenset(), frozenset()),),
        snapshot,
    )
    assert [item.executable_id for item in merged_executables] == ["user-tool"]
    assert [item.profile_id for item in merged_environments] == ["minimal", "user-env"]
    with pytest.raises(ProcessRegistrationConflict, match="builtin catalog"):
        merge_process_registration_snapshot(merged_executables, (), snapshot)


@pytest.mark.asyncio
async def test_catalogs_and_idempotency_receipts_are_isolated_per_workspace(tmp_path: Path) -> None:
    first = _service(tmp_path, workspace_id="ws_first")
    second = _service(tmp_path, workspace_id="ws_second")

    probe = await first.probe_environment(
        client_id="pipe-first",
        proposal=EnvironmentRegistrationProposal("user-env", frozenset({"LANG"}), frozenset()),
    )
    registered = await first.confirm(
        client_id="pipe-first",
        challenge_id=probe.challenge_id,
        expected_probe_content_hash=probe.content_hash,
        expected_catalog_revision=0,
        client_request_id="req_shared_name",
    )

    assert (await first.catalog()).revision == 1
    assert (await second.catalog()).revision == 0
    assert (await second.runtime_snapshot()).environment_profiles == ()

    second_probe = await second.probe_environment(
        client_id="pipe-second",
        proposal=EnvironmentRegistrationProposal("user-env", frozenset(), frozenset()),
    )
    second_registered = await second.confirm(
        client_id="pipe-second",
        challenge_id=second_probe.challenge_id,
        expected_probe_content_hash=second_probe.content_hash,
        expected_catalog_revision=0,
        client_request_id="req_shared_name",
    )

    assert second_registered.catalog_revision == 1
    assert second_registered.record_content_hash != registered.record_content_hash
    assert (await first.runtime_snapshot()).environment_profiles[0].allowed_names == frozenset({"LANG"})
    assert (await second.runtime_snapshot()).environment_profiles[0].allowed_names == frozenset()


@pytest.mark.asyncio
async def test_probe_is_short_lived_one_time_and_stdio_connection_bound(tmp_path: Path) -> None:
    clock = ManualClock()
    service = _service(tmp_path, clock=clock)
    probe = await service.probe_environment(
        client_id="pipe-owner",
        proposal=EnvironmentRegistrationProposal("user-env", frozenset(), frozenset()),
    )
    with pytest.raises(ProcessRegistrationError, match="another stdio"):
        await service.confirm(
            client_id="pipe-other",
            challenge_id=probe.challenge_id,
            expected_probe_content_hash=probe.content_hash,
            expected_catalog_revision=0,
            client_request_id="req_wrong_pipe",
        )
    await service.confirm(
        client_id="pipe-owner",
        challenge_id=probe.challenge_id,
        expected_probe_content_hash=probe.content_hash,
        expected_catalog_revision=0,
        client_request_id="req_owner_confirm",
    )
    with pytest.raises(ProcessRegistrationError, match="consumed"):
        await service.confirm(
            client_id="pipe-owner",
            challenge_id=probe.challenge_id,
            expected_probe_content_hash=probe.content_hash,
            expected_catalog_revision=0,
            client_request_id="req_owner_second_confirm",
        )
    expiring = await service.probe_environment(
        client_id="pipe-owner",
        proposal=EnvironmentRegistrationProposal("later-env", frozenset(), frozenset()),
    )
    clock.advance(timedelta(minutes=6))
    with pytest.raises(ProcessRegistrationError, match="expired"):
        await service.confirm(
            client_id="pipe-owner",
            challenge_id=expiring.challenge_id,
            expected_probe_content_hash=expiring.content_hash,
            expected_catalog_revision=1,
            client_request_id="req_expired_probe",
        )


@pytest.mark.asyncio
async def test_unsigned_and_signed_executable_trust_is_explicit(tmp_path: Path) -> None:
    path = _executable(tmp_path)
    unsigned = await _service(tmp_path).probe_executable(client_id="pipe-1", proposal=_proposal(path))
    assert unsigned.executable is not None
    assert unsigned.executable.trust is ExecutableTrust.FIXED_HASH
    assert unsigned.executable.authenticode_verified is False

    signed_root = tmp_path / "signed"
    signed_root.mkdir()
    signed = await _service(signed_root, authenticode=_Authenticode(True)).probe_executable(
        client_id="pipe-1",
        proposal=_proposal(_executable(signed_root)),
    )
    assert signed.executable is not None
    assert signed.executable.trust is ExecutableTrust.OS_AUTHENTICODE
    assert signed.executable.authenticode_verified is True


@pytest.mark.asyncio
async def test_binary_drift_makes_registration_unavailable(tmp_path: Path) -> None:
    service = _service(tmp_path)
    path = _executable(tmp_path)
    probe = await service.probe_executable(client_id="pipe-1", proposal=_proposal(path))
    await service.confirm(
        client_id="pipe-1",
        challenge_id=probe.challenge_id,
        expected_probe_content_hash=probe.content_hash,
        expected_catalog_revision=0,
        client_request_id="req_register_drift",
    )
    path.write_bytes(b"MZ-changed")

    snapshot = await service.runtime_snapshot()
    assert snapshot.executable_profiles == ()
    availability = next(item for item in snapshot.availability if item.kind == "executable")
    assert availability.available is False
    assert availability.unavailable_reason == "file_or_profile_drift"


@pytest.mark.asyncio
async def test_confirm_reprobes_executable_and_rejects_post_probe_drift(tmp_path: Path) -> None:
    service = _service(tmp_path)
    path = _executable(tmp_path)
    probe = await service.probe_executable(client_id="pipe-1", proposal=_proposal(path))
    path.write_bytes(b"MZ-replaced-after-probe")

    with pytest.raises(ProcessRegistrationError, match="changed after the confirmation probe"):
        await service.confirm(
            client_id="pipe-1",
            challenge_id=probe.challenge_id,
            expected_probe_content_hash=probe.content_hash,
            expected_catalog_revision=0,
            client_request_id="req_reject_post_probe_drift",
        )

    assert (await service.catalog()).revision == 0


@pytest.mark.asyncio
async def test_authenticode_drift_makes_registration_unavailable(tmp_path: Path) -> None:
    authenticode = _Authenticode(True)
    service = _service(tmp_path, authenticode=authenticode)
    probe = await service.probe_executable(client_id="pipe-1", proposal=_proposal(_executable(tmp_path)))
    await service.confirm(
        client_id="pipe-1",
        challenge_id=probe.challenge_id,
        expected_probe_content_hash=probe.content_hash,
        expected_catalog_revision=0,
        client_request_id="req_register_authenticode_drift",
    )
    authenticode.verified = False

    snapshot = await service.runtime_snapshot()
    assert snapshot.executable_profiles == ()
    availability = next(item for item in snapshot.availability if item.kind == "executable")
    assert availability.available is False
    assert availability.unavailable_reason == "file_or_profile_drift"


@pytest.mark.asyncio
async def test_delete_requires_revision_hash_and_is_idempotent(tmp_path: Path) -> None:
    service = _service(tmp_path)
    probe = await service.probe_environment(
        client_id="pipe-1",
        proposal=EnvironmentRegistrationProposal("user-env", frozenset(), frozenset()),
    )
    registered = await service.confirm(
        client_id="pipe-1",
        challenge_id=probe.challenge_id,
        expected_probe_content_hash=probe.content_hash,
        expected_catalog_revision=0,
        client_request_id="req_register_delete",
    )
    with pytest.raises(ProcessRegistrationConflict):
        await service.delete(
            client_id="pipe-1",
            kind="environment",
            registration_id="user-env",
            expected_catalog_revision=1,
            expected_revision=registered.record_revision,
            expected_content_hash="sha256:" + "0" * 64,
            client_request_id="req_delete_bad_hash",
        )
    deleted = await service.delete(
        client_id="pipe-1",
        kind="environment",
        registration_id="user-env",
        expected_catalog_revision=1,
        expected_revision=registered.record_revision,
        expected_content_hash=registered.record_content_hash,
        client_request_id="req_delete_good",
    )
    assert deleted.deleted is True
    assert (
        await service.delete(
            client_id="pipe-new",
            kind="environment",
            registration_id="user-env",
            expected_catalog_revision=1,
            expected_revision=registered.record_revision,
            expected_content_hash=registered.record_content_hash,
            client_request_id="req_delete_good",
        )
        == deleted
    )


@pytest.mark.asyncio
async def test_update_requires_record_revision_hash_and_request_ids_cannot_be_rebound(tmp_path: Path) -> None:
    service = _service(tmp_path)
    initial_probe = await service.probe_environment(
        client_id="pipe-1",
        proposal=EnvironmentRegistrationProposal("user-env", frozenset(), frozenset()),
    )
    initial = await service.confirm(
        client_id="pipe-1",
        challenge_id=initial_probe.challenge_id,
        expected_probe_content_hash=initial_probe.content_hash,
        expected_catalog_revision=0,
        client_request_id="req_initial_environment",
    )
    update_probe = await service.probe_environment(
        client_id="pipe-1",
        proposal=EnvironmentRegistrationProposal(
            "user-env",
            frozenset({"LANG"}),
            frozenset(),
            expected_revision=initial.record_revision,
            expected_content_hash=initial.record_content_hash,
        ),
    )
    updated = await service.confirm(
        client_id="pipe-1",
        challenge_id=update_probe.challenge_id,
        expected_probe_content_hash=update_probe.content_hash,
        expected_catalog_revision=1,
        client_request_id="req_update_environment",
    )
    assert updated.record_revision == 2
    assert updated.record_content_hash != initial.record_content_hash

    with pytest.raises(ProcessRegistrationConflict, match="clientRequestId"):
        await service.delete(
            client_id="pipe-1",
            kind="environment",
            registration_id="user-env",
            expected_catalog_revision=2,
            expected_revision=updated.record_revision,
            expected_content_hash=updated.record_content_hash,
            client_request_id="req_update_environment",
        )


@pytest.mark.asyncio
async def test_receipt_never_contains_absolute_executable_path(tmp_path: Path) -> None:
    factory = SqliteUnitOfWorkFactory(tmp_path / "state.sqlite")
    service = WorkspaceProcessRegistrationService(
        workspace_id="ws_test",
        unit_of_work=factory,
        clock=ManualClock(),
        ids=_Ids(),
        authenticode=_Authenticode(),
        builtin_executables=(),
        builtin_environments=(ProcessEnvironmentProfile("minimal", frozenset()),),
        allowed_workspace_root_ids=frozenset({"process-scratch"}),
    )
    path = _executable(tmp_path)
    probe = await service.probe_executable(client_id="pipe-1", proposal=_proposal(path))
    await service.confirm(
        client_id="pipe-1",
        challenge_id=probe.challenge_id,
        expected_probe_content_hash=probe.content_hash,
        expected_catalog_revision=0,
        client_request_id="req_path_receipt",
    )
    rows = await factory.list_entities(PROCESS_REGISTRATION_RECEIPT_COLLECTION)
    assert len(rows) == 1
    assert str(path) not in repr(rows[0].value)


@pytest.mark.asyncio
async def test_environment_rejects_path_proxy_and_sensitive_plain_names(tmp_path: Path) -> None:
    service = _service(tmp_path)
    for plain, secret in [({"PATH"}, set()), ({"HTTPS_PROXY"}, set()), ({"API_KEY"}, set()), (set(), {"PATH"})]:
        with pytest.raises((ProcessProfileError, ValueError)):
            await service.probe_environment(
                client_id="pipe-1",
                proposal=EnvironmentRegistrationProposal("unsafe-env", frozenset(plain), frozenset(secret)),
            )


@pytest.mark.asyncio
async def test_executable_probe_rejects_interpreter_relative_and_hardlink(tmp_path: Path) -> None:
    service = _service(tmp_path)
    interpreter = _executable(tmp_path, "cmd.exe")
    with pytest.raises(ProcessProfileError, match="interpreters"):
        await service.probe_executable(client_id="pipe-1", proposal=_proposal(interpreter))
    with pytest.raises(ProcessRegistrationError, match="absolute"):
        await service.probe_executable(
            client_id="pipe-1",
            proposal=_proposal(interpreter, executable_path="relative.exe"),
        )
    with pytest.raises(ProcessRegistrationError, match="absolute"):
        await service.probe_executable(
            client_id="pipe-1",
            proposal=_proposal(interpreter, executable_path=r"\\server\share\tool.exe"),
        )

    source = _executable(tmp_path, "linked-source.exe")
    linked = source.parent / "linked-copy.exe"
    os.link(source, linked)
    with pytest.raises(ProcessRegistrationError, match="non-linked"):
        await service.probe_executable(client_id="pipe-1", proposal=_proposal(linked))

    symlink = source.parent / "symlink-copy.exe"
    try:
        symlink.symlink_to(source)
    except OSError:
        return
    with pytest.raises(ProcessRegistrationError, match="reparse"):
        await service.probe_executable(client_id="pipe-1", proposal=_proposal(symlink))
