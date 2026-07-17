"""Direct-stdio application boundary for Workspace Process registrations."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal, cast

from offeragent_harness.ports import ApplicationCommandContext, CancellationToken, ProcessStdinMode
from offeragent_harness.protocol._base import WireModel
from offeragent_harness.protocol.messages import (
    ProcessEnvironmentProbeSnapshot,
    ProcessEnvironmentRegistrationInput,
    ProcessEnvironmentRegistrationSnapshot,
    ProcessExecutableProbeSnapshot,
    ProcessExecutableRegistrationInput,
    ProcessExecutableRegistrationSnapshot,
    ProcessFilesystemRegistrationInput,
    ProcessRegistrationsConfirmParams,
    ProcessRegistrationsDeleteParams,
    ProcessRegistrationsListParams,
    ProcessRegistrationsListResult,
    ProcessRegistrationsMutationResult,
    ProcessRegistrationsProbeParams,
    ProcessRegistrationsProbeResult,
)

from .application_dispatcher import ApplicationCommandHandler
from .process_registration import (
    EnvironmentRegistrationProposal,
    ExecutableRegistrationProposal,
    ProcessFilesystemRegistration,
    ProcessRegistrationAvailability,
    ProcessRegistrationMutationResult,
    ProcessRegistrationProbe,
    ProcessRegistrationRuntimeSnapshot,
    WorkspaceProcessRegistrationService,
)
from .process_supervisor import ProcessFilesystemAccess


def process_registration_command_handlers(
    service: WorkspaceProcessRegistrationService | None,
) -> Mapping[str, ApplicationCommandHandler]:
    """Expose the complete registration surface without ever serving Loopback."""

    def require_service(context: ApplicationCommandContext) -> WorkspaceProcessRegistrationService:
        _require_direct_stdio(context)
        if service is None:
            raise RuntimeError("Workspace Process registration service is unavailable")
        return service

    async def list_registrations(
        raw: WireModel,
        cancellation: CancellationToken,
        context: ApplicationCommandContext,
    ) -> WireModel:
        cast(ProcessRegistrationsListParams, raw)
        owner = require_service(context)
        cancellation.checkpoint()
        snapshot = await owner.runtime_snapshot()
        cancellation.checkpoint()
        return _list_result(owner, snapshot)

    async def probe(
        raw: WireModel,
        cancellation: CancellationToken,
        context: ApplicationCommandContext,
    ) -> WireModel:
        params = cast(ProcessRegistrationsProbeParams, raw)
        owner = require_service(context)
        cancellation.checkpoint()
        if params.executable is not None:
            result = await owner.probe_executable(
                client_id=context.client_id,
                proposal=_executable_proposal(params.executable),
            )
        else:
            assert params.environment is not None
            result = await owner.probe_environment(
                client_id=context.client_id,
                proposal=_environment_proposal(params.environment),
            )
        cancellation.checkpoint()
        return _probe_result(result)

    async def confirm(
        raw: WireModel,
        cancellation: CancellationToken,
        context: ApplicationCommandContext,
    ) -> WireModel:
        params = cast(ProcessRegistrationsConfirmParams, raw)
        owner = require_service(context)
        cancellation.checkpoint()
        result = await owner.confirm(
            client_id=context.client_id,
            challenge_id=params.challenge_id,
            expected_probe_content_hash=params.expected_probe_content_hash,
            expected_catalog_revision=params.expected_catalog_revision,
            client_request_id=params.client_request_id,
        )
        cancellation.checkpoint()
        return _mutation_result(result)

    async def delete(
        raw: WireModel,
        cancellation: CancellationToken,
        context: ApplicationCommandContext,
    ) -> WireModel:
        params = cast(ProcessRegistrationsDeleteParams, raw)
        owner = require_service(context)
        cancellation.checkpoint()
        result = await owner.delete(
            client_id=context.client_id,
            kind=params.kind,
            registration_id=params.registration_id,
            expected_catalog_revision=params.expected_catalog_revision,
            expected_revision=params.expected_revision,
            expected_content_hash=params.expected_content_hash,
            client_request_id=params.client_request_id,
        )
        cancellation.checkpoint()
        return _mutation_result(result)

    return {
        "process/registrations/list": list_registrations,
        "process/registrations/probe": probe,
        "process/registrations/confirm": confirm,
        "process/registrations/delete": delete,
    }


def _require_direct_stdio(context: ApplicationCommandContext) -> None:
    if context.transport != "stdio":
        raise PermissionError("Process registration is available only to the direct plugin stdio connection")


def _filesystem(value: ProcessFilesystemRegistrationInput) -> ProcessFilesystemRegistration:
    return ProcessFilesystemRegistration(
        root_id=value.root_id,
        relative_path=value.relative_path,
        access=ProcessFilesystemAccess(value.access),
    )


def _executable_proposal(value: ProcessExecutableRegistrationInput) -> ExecutableRegistrationProposal:
    return ExecutableRegistrationProposal(
        executable_id=value.executable_id,
        executable_path=value.executable_path,
        fixed_arguments=tuple(value.fixed_arguments),
        minimum_variable_arguments=value.minimum_variable_arguments,
        maximum_variable_arguments=value.maximum_variable_arguments,
        variable_argument_pattern=value.variable_argument_pattern,
        environment_profile_ids=frozenset(value.environment_profile_ids),
        allowed_stdin_modes=frozenset(ProcessStdinMode(item) for item in value.allowed_stdin_modes),
        allowed_cwd_root_ids=frozenset(value.allowed_cwd_root_ids),
        appcontainer_filesystem=tuple(_filesystem(item) for item in value.appcontainer_filesystem),
        expected_revision=value.expected_revision,
        expected_content_hash=value.expected_content_hash,
    )


def _environment_proposal(value: ProcessEnvironmentRegistrationInput) -> EnvironmentRegistrationProposal:
    return EnvironmentRegistrationProposal(
        profile_id=value.profile_id,
        allowed_names=frozenset(value.allowed_names),
        allowed_secret_names=frozenset(value.allowed_secret_names),
        expected_revision=value.expected_revision,
        expected_content_hash=value.expected_content_hash,
    )


def _availability(
    values: tuple[ProcessRegistrationAvailability, ...],
) -> dict[tuple[str, str], ProcessRegistrationAvailability]:
    return {(item.kind, item.registration_id): item for item in values}


def _list_result(
    service: WorkspaceProcessRegistrationService,
    snapshot: ProcessRegistrationRuntimeSnapshot,
) -> ProcessRegistrationsListResult:
    statuses = _availability(snapshot.availability)
    catalog = snapshot.catalog
    return ProcessRegistrationsListResult(
        workspace_id=catalog.workspace_id,
        catalog_revision=catalog.revision,
        active_catalog_revision=service.active_catalog_revision,
        snapshot_hash=catalog.snapshot_hash,
        restart_required=catalog.revision != service.active_catalog_revision,
        executables=[
            ProcessExecutableRegistrationSnapshot(
                executable_id=item.executable_id,
                revision=item.revision,
                content_hash=item.content_hash,
                canonical_path=item.canonical_path,
                fixed_root=item.fixed_root,
                trust=item.trust.value,
                authenticode_verified=item.authenticode_verified,
                file_sha256=item.file_sha256,
                file_device=str(item.file_device),
                file_index=str(item.file_index),
                file_size=item.file_size,
                profile_fingerprint=item.profile_fingerprint,
                fixed_arguments=list(item.fixed_arguments),
                minimum_variable_arguments=item.minimum_variable_arguments,
                maximum_variable_arguments=item.maximum_variable_arguments,
                variable_argument_pattern=item.variable_argument_pattern,
                environment_profile_ids=sorted(item.environment_profile_ids),
                allowed_stdin_modes=sorted(mode.value for mode in item.allowed_stdin_modes),
                allowed_cwd_root_ids=sorted(item.allowed_cwd_root_ids),
                appcontainer_filesystem=[
                    ProcessFilesystemRegistrationInput(
                        root_id=grant.root_id,
                        relative_path=grant.relative_path,
                        access=cast(Literal["read", "read_write"], grant.access.value),
                    )
                    for grant in item.appcontainer_filesystem
                ],
                allow_network=False,
                available=statuses[("executable", item.executable_id)].available,
                unavailable_reason=cast(
                    Literal["file_or_profile_drift", "configuration_drift"] | None,
                    statuses[("executable", item.executable_id)].unavailable_reason,
                ),
            )
            for item in catalog.executables
        ],
        environments=[
            ProcessEnvironmentRegistrationSnapshot(
                profile_id=item.profile_id,
                revision=item.revision,
                content_hash=item.content_hash,
                allowed_names=sorted(item.allowed_names),
                allowed_secret_names=sorted(item.allowed_secret_names),
                available=statuses[("environment", item.profile_id)].available,
                unavailable_reason=cast(
                    Literal["configuration_drift"] | None,
                    statuses[("environment", item.profile_id)].unavailable_reason,
                ),
            )
            for item in catalog.environments
        ],
    )


def _probe_result(value: ProcessRegistrationProbe) -> ProcessRegistrationsProbeResult:
    executable = value.executable
    environment = value.environment
    return ProcessRegistrationsProbeResult(
        challenge_id=value.challenge_id,
        kind=value.kind,
        registration_id=value.registration_id,
        content_hash=value.content_hash,
        expires_at=value.expires_at.isoformat(),
        executable=(
            None
            if executable is None
            else ProcessExecutableProbeSnapshot(
                executable_id=executable.executable_id,
                canonical_path=executable.canonical_path,
                fixed_root=executable.fixed_root,
                trust=executable.trust.value,
                authenticode_verified=executable.authenticode_verified,
                file_sha256=executable.file_sha256,
                file_device=str(executable.file_device),
                file_index=str(executable.file_index),
                file_size=executable.file_size,
                profile_fingerprint=executable.profile_fingerprint,
                fixed_arguments=list(executable.fixed_arguments),
                minimum_variable_arguments=executable.minimum_variable_arguments,
                maximum_variable_arguments=executable.maximum_variable_arguments,
                variable_argument_pattern=executable.variable_argument_pattern,
                environment_profile_ids=sorted(executable.environment_profile_ids),
                allowed_stdin_modes=sorted(mode.value for mode in executable.allowed_stdin_modes),
                allowed_cwd_root_ids=sorted(executable.allowed_cwd_root_ids),
                appcontainer_filesystem=[
                    ProcessFilesystemRegistrationInput(
                        root_id=item.root_id,
                        relative_path=item.relative_path,
                        access=cast(Literal["read", "read_write"], item.access.value),
                    )
                    for item in executable.appcontainer_filesystem
                ],
                allow_network=False,
            )
        ),
        environment=(
            None
            if environment is None
            else ProcessEnvironmentProbeSnapshot(
                profile_id=environment.profile_id,
                allowed_names=sorted(environment.allowed_names),
                allowed_secret_names=sorted(environment.allowed_secret_names),
            )
        ),
    )


def _mutation_result(value: ProcessRegistrationMutationResult) -> ProcessRegistrationsMutationResult:
    return ProcessRegistrationsMutationResult(
        client_request_id=value.client_request_id,
        kind=value.kind,
        registration_id=value.registration_id,
        catalog_revision=value.catalog_revision,
        snapshot_hash=value.snapshot_hash,
        record_revision=value.record_revision,
        record_content_hash=value.record_content_hash,
        deleted=value.deleted,
        restart_required=True,
    )


__all__ = ["process_registration_command_handlers"]
