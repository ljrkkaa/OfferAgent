"""Application-only administration adapters for Skills, Shell profiles, and Hooks.

The adapters expose the existing mutable owners.  They add transport/trust
authorization, strict DTO conversion, registered-process validation, and a
durable client request receipt; they do not implement extension domain state.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import cast

from offeragent_harness.config import HarnessConfig
from offeragent_harness.hooks import (
    HookCommandSpec,
    HookDefinition,
    HookEvent,
    HookImplementation,
    HookLayer,
    HookScope,
)
from offeragent_harness.hooks.state import (
    HookConfigurationService,
    HookLayerRecord,
    hook_definition_hash,
)
from offeragent_harness.permissions import RiskClass
from offeragent_harness.ports import ApplicationCommandContext, CancellationToken, UnitOfWorkFactory
from offeragent_harness.protocol._base import WireModel, validate_wire
from offeragent_harness.protocol.messages import (
    HookCommandInput,
    HookDefinitionInput,
    HookDefinitionSnapshot,
    HookLayerInput,
    HookLayerSnapshot,
    HooksConfirmLayerParams,
    HooksConfirmWorkspaceCommandParams,
    HooksInstallParams,
    HooksListParams,
    HooksListResult,
    HooksMutationResult,
    ShellConfirmParams,
    ShellEnvironmentRegistrationSnapshot,
    ShellExecutableRegistrationSnapshot,
    ShellInstallParams,
    ShellListParams,
    ShellListResult,
    ShellMutationResult,
    ShellProfileInput,
    ShellProfileSnapshot,
    ShellSetEnabledParams,
    SkillCatalogStatusSnapshot,
    SkillDiagnosticSnapshot,
    SkillsListParams,
    SkillsListResult,
    SkillSnapshot,
    SkillsStatusParams,
    SkillsStatusResult,
)
from offeragent_harness.shell import ShellCommandProfile
from offeragent_harness.shell.state import (
    ShellProfileRecord,
    ShellProfileService,
    ShellProfileSource,
    ShellProfileTrust,
)
from offeragent_harness.skills import SkillCatalog, SkillDescriptor, SkillDiagnostic
from offeragent_harness.tools import SideEffectClass, canonical_json_sha256

from .application_dispatcher import ApplicationCommandHandler
from .config_service import ConfigService
from .harness_service import HarnessService
from .process_registration import WorkspaceProcessRegistrationService
from .process_registration_application_handlers import process_registration_command_handlers
from .process_supervisor import ProcessEnvironmentProfile, ProcessExecutableProfile
from .production_skills import ProductionSkillBundleFactory
from .session_service import SessionGetCommand

_RECEIPT_COLLECTION = "extension_management_receipts"
_MUTATING_METHODS = frozenset(
    {
        "shell/install",
        "shell/confirm",
        "shell/set-enabled",
        "hooks/install",
        "hooks/confirm-layer",
        "hooks/confirm-workspace-command",
    }
)
_RECEIPT_STATES = frozenset({"pending", "complete"})
_SENSITIVE_ARGUMENT = re.compile(
    r"(?:^|[-_/])(?:api[-_]?key|auth(?:orization)?|cookie|credential|password|secret|token)(?:$|[=:])",
    re.IGNORECASE,
)


class ExtensionManagementReceiptJournal:
    """Persist secret-free results around the existing CAS domain mutations."""

    def __init__(self, *, workspace_id: str, unit_of_work: UnitOfWorkFactory) -> None:
        if not workspace_id or "\x00" in workspace_id:
            raise ValueError("extension management receipt requires a Workspace identity")
        self._workspace_id = workspace_id
        self._unit_of_work = unit_of_work
        self._lock = asyncio.Lock()

    async def execute(
        self,
        *,
        method: str,
        params: WireModel,
        client_request_id: str,
        result_type: type[WireModel],
        operation: Callable[[bool], Awaitable[WireModel]],
    ) -> WireModel:
        if method not in _MUTATING_METHODS:
            raise RuntimeError("receipt journal is available only to extension mutations")
        request_hash = canonical_json_sha256({"method": method, "params": params.to_wire()})
        entity_id = _receipt_entity_id(self._workspace_id, client_request_id)
        async with self._lock:
            async with self._unit_of_work.begin() as unit_of_work:
                existing = await unit_of_work.entities.get(_RECEIPT_COLLECTION, entity_id)
            recovering = existing is not None
            if existing is not None:
                state, result = _parse_receipt(
                    existing,
                    workspace_id=self._workspace_id,
                    client_request_id=client_request_id,
                    method=method,
                    request_hash=request_hash,
                )
                if state == "complete":
                    assert result is not None
                    return validate_wire(result_type, result)
            else:
                pending = _receipt_value(
                    workspace_id=self._workspace_id,
                    client_request_id=client_request_id,
                    method=method,
                    request_hash=request_hash,
                    state="pending",
                    result=None,
                )
                async with self._unit_of_work.begin() as unit_of_work:
                    revision = await unit_of_work.entities.put(
                        _RECEIPT_COLLECTION,
                        entity_id,
                        pending,
                        expected_revision=0,
                    )
                    if revision != 1:
                        raise ValueError("extension management pending receipt revision is invalid")
                    await unit_of_work.commit()

            value = await operation(recovering)
            result = value.to_wire()
            complete = _receipt_value(
                workspace_id=self._workspace_id,
                client_request_id=client_request_id,
                method=method,
                request_hash=request_hash,
                state="complete",
                result=result,
            )
            async with self._unit_of_work.begin() as unit_of_work:
                revision = await unit_of_work.entities.put(
                    _RECEIPT_COLLECTION,
                    entity_id,
                    complete,
                    expected_revision=1,
                )
                if revision != 2:
                    raise ValueError("extension management complete receipt revision is invalid")
                await unit_of_work.commit()
            return value


def extension_management_command_handlers(
    *,
    workspace_id: str,
    profile_id: str,
    managed_owner_id: str,
    config: ConfigService,
    harness: HarnessService,
    skills: ProductionSkillBundleFactory,
    shell: ShellProfileService,
    hooks: HookConfigurationService,
    unit_of_work: UnitOfWorkFactory,
    executable_profiles: Sequence[ProcessExecutableProfile],
    environment_profiles: Sequence[ProcessEnvironmentProfile],
    builtin_hook_handler_ids: Sequence[str],
    process_registrations: WorkspaceProcessRegistrationService | None = None,
) -> Mapping[str, ApplicationCommandHandler]:
    """Bind every extension management command to the production singletons."""

    if not all((workspace_id, profile_id, managed_owner_id)):
        raise ValueError("extension management identity is incomplete")
    executables = {item.executable_id: item for item in executable_profiles}
    environments = {item.profile_id: item for item in environment_profiles}
    if len(executables) != len(executable_profiles) or len(environments) != len(environment_profiles):
        raise ValueError("registered process profile IDs must be unique")
    receipts = ExtensionManagementReceiptJournal(workspace_id=workspace_id, unit_of_work=unit_of_work)
    handlers = dict(
        _skill_handlers(
            workspace_id=workspace_id,
            profile_id=profile_id,
            managed_owner_id=managed_owner_id,
            config=config,
            skills=skills,
            receipts=receipts,
        )
    )
    handlers.update(
        _shell_handlers(
            workspace_id=workspace_id,
            profile_id=profile_id,
            managed_owner_id=managed_owner_id,
            config=config,
            shell=shell,
            receipts=receipts,
            executables=executables,
            environments=environments,
        )
    )
    handlers.update(
        _hook_handlers(
            workspace_id=workspace_id,
            profile_id=profile_id,
            managed_owner_id=managed_owner_id,
            config=config,
            harness=harness,
            hooks=hooks,
            receipts=receipts,
            executables=executables,
            environments=environments,
            builtin_hook_handler_ids=tuple(sorted(set(builtin_hook_handler_ids))),
        )
    )
    handlers.update(process_registration_command_handlers(process_registrations))
    return handlers


def _skill_handlers(
    *,
    workspace_id: str,
    profile_id: str,
    managed_owner_id: str,
    config: ConfigService,
    skills: ProductionSkillBundleFactory,
    receipts: ExtensionManagementReceiptJournal,
) -> Mapping[str, ApplicationCommandHandler]:
    async def catalog(cancellation: CancellationToken) -> SkillCatalog:
        snapshot = await _effective_config(
            config,
            workspace_id=workspace_id,
            profile_id=profile_id,
            managed_owner_id=managed_owner_id,
        )
        return await skills.catalog_for_management(
            workspace_trusted=snapshot.policy.workspace_trusted,
            cancellation=cancellation,
        )

    async def list_skills(
        raw: WireModel, cancellation: CancellationToken, context: ApplicationCommandContext
    ) -> WireModel:
        del context
        cast(SkillsListParams, raw)
        current = await catalog(cancellation)
        snapshot = current.snapshot
        return SkillsListResult(
            workspace_id=workspace_id,
            revision=snapshot.revision,
            snapshot_hash=snapshot.snapshot_hash,
            skills=[_skill_snapshot(item) for item in snapshot.descriptors],
            diagnostics=[_skill_diagnostic(item) for item in current.status().diagnostics],
        )

    async def status(raw: WireModel, cancellation: CancellationToken, context: ApplicationCommandContext) -> WireModel:
        del context
        cast(SkillsStatusParams, raw)
        current = await catalog(cancellation)
        return SkillsStatusResult(status=_skill_status(current))

    return {
        "skills/list": list_skills,
        "skills/status": status,
    }


def _shell_handlers(
    *,
    workspace_id: str,
    profile_id: str,
    managed_owner_id: str,
    config: ConfigService,
    shell: ShellProfileService,
    receipts: ExtensionManagementReceiptJournal,
    executables: Mapping[str, ProcessExecutableProfile],
    environments: Mapping[str, ProcessEnvironmentProfile],
) -> Mapping[str, ApplicationCommandHandler]:
    async def require_mutation(context: ApplicationCommandContext) -> None:
        await _require_mutation_authority(
            context,
            config=config,
            workspace_id=workspace_id,
            profile_id=profile_id,
            managed_owner_id=managed_owner_id,
        )

    async def initialized(cancellation: CancellationToken) -> None:
        if not shell.initialized:
            await shell.initialize(cancellation)

    async def list_profiles(
        raw: WireModel, cancellation: CancellationToken, context: ApplicationCommandContext
    ) -> WireModel:
        del context
        params = cast(ShellListParams, raw)
        await initialized(cancellation)
        snapshot = shell.snapshot
        return ShellListResult(
            workspace_id=workspace_id,
            revision=snapshot.revision,
            snapshot_hash=snapshot.snapshot_hash,
            profiles=[_shell_record(item) for item in snapshot.records if params.include_disabled or item.enabled],
            executables=[
                ShellExecutableRegistrationSnapshot(
                    executable_id=item.executable_id,
                    fingerprint=item.fingerprint,
                    fixed_arguments=list(item.fixed_arguments),
                    minimum_variable_arguments=item.minimum_variable_arguments,
                    maximum_variable_arguments=item.maximum_variable_arguments,
                    variable_argument_pattern=item.variable_argument_pattern,
                    allowed_stdin_modes=sorted(mode.value for mode in item.allowed_stdin_modes),
                    allowed_cwd_root_ids=sorted(item.allowed_cwd_roots),
                    environment_profile_ids=sorted(item.environment_profiles),
                    allow_network=item.allow_network,
                )
                for item in sorted(executables.values(), key=lambda value: value.executable_id)
            ],
            environments=[
                ShellEnvironmentRegistrationSnapshot(
                    profile_id=item.profile_id,
                    allowed_names=sorted(item.allowed_names),
                    allowed_secret_names=sorted(item.allowed_secret_names),
                )
                for item in sorted(environments.values(), key=lambda value: value.profile_id)
            ],
        )

    async def install(raw: WireModel, cancellation: CancellationToken, context: ApplicationCommandContext) -> WireModel:
        params = cast(ShellInstallParams, raw)
        await require_mutation(context)
        profile = _shell_profile(params.profile)
        _validate_shell_registration(profile, executables, environments)

        async def apply(recovering: bool) -> WireModel:
            await require_mutation(context)
            await initialized(cancellation)
            record = _recover_shell_install(shell, profile, params.expected_revision) if recovering else None
            if record is None:
                record = await shell.install_user_profile(
                    profile,
                    expected_revision=params.expected_revision,
                    idempotency_key=params.client_request_id,
                    cancellation=cancellation,
                )
            return _shell_mutation(params.client_request_id, shell, record)

        return await receipts.execute(
            method="shell/install",
            params=params,
            client_request_id=params.client_request_id,
            result_type=ShellMutationResult,
            operation=apply,
        )

    async def confirm(raw: WireModel, cancellation: CancellationToken, context: ApplicationCommandContext) -> WireModel:
        params = cast(ShellConfirmParams, raw)
        await require_mutation(context)

        async def apply(recovering: bool) -> WireModel:
            await require_mutation(context)
            await initialized(cancellation)
            record = (
                _recover_shell_confirm(shell, params.profile_id, params.content_hash, params.expected_revision)
                if recovering
                else None
            )
            if record is None:
                record = await shell.confirm_user_profile(
                    params.profile_id,
                    params.content_hash,
                    expected_revision=params.expected_revision,
                    idempotency_key=params.client_request_id,
                    cancellation=cancellation,
                )
            return _shell_mutation(params.client_request_id, shell, record)

        return await receipts.execute(
            method="shell/confirm",
            params=params,
            client_request_id=params.client_request_id,
            result_type=ShellMutationResult,
            operation=apply,
        )

    async def set_enabled(
        raw: WireModel, cancellation: CancellationToken, context: ApplicationCommandContext
    ) -> WireModel:
        params = cast(ShellSetEnabledParams, raw)
        await require_mutation(context)

        async def apply(recovering: bool) -> WireModel:
            await require_mutation(context)
            await initialized(cancellation)
            record = (
                _recover_shell_enabled(shell, params.profile_id, params.enabled, params.expected_revision)
                if recovering
                else None
            )
            if record is None:
                record = await shell.set_enabled(
                    params.profile_id,
                    params.enabled,
                    expected_revision=params.expected_revision,
                    idempotency_key=params.client_request_id,
                    cancellation=cancellation,
                )
            return _shell_mutation(params.client_request_id, shell, record)

        return await receipts.execute(
            method="shell/set-enabled",
            params=params,
            client_request_id=params.client_request_id,
            result_type=ShellMutationResult,
            operation=apply,
        )

    return {
        "shell/list": list_profiles,
        "shell/install": install,
        "shell/confirm": confirm,
        "shell/set-enabled": set_enabled,
    }


def _hook_handlers(
    *,
    workspace_id: str,
    profile_id: str,
    managed_owner_id: str,
    config: ConfigService,
    harness: HarnessService,
    hooks: HookConfigurationService,
    receipts: ExtensionManagementReceiptJournal,
    executables: Mapping[str, ProcessExecutableProfile],
    environments: Mapping[str, ProcessEnvironmentProfile],
    builtin_hook_handler_ids: tuple[str, ...],
) -> Mapping[str, ApplicationCommandHandler]:
    async def require_mutation(context: ApplicationCommandContext) -> None:
        await _require_mutation_authority(
            context,
            config=config,
            workspace_id=workspace_id,
            profile_id=profile_id,
            managed_owner_id=managed_owner_id,
        )

    async def initialized(cancellation: CancellationToken) -> None:
        if not hooks.initialized:
            await hooks.initialize(cancellation)

    async def list_layers(
        raw: WireModel, cancellation: CancellationToken, context: ApplicationCommandContext
    ) -> WireModel:
        del context
        cast(HooksListParams, raw)
        await initialized(cancellation)
        snapshot = hooks.snapshot
        return HooksListResult(
            workspace_id=workspace_id,
            profile_id=profile_id,
            revision=snapshot.revision,
            snapshot_hash=snapshot.snapshot_hash,
            layers=[_hook_record(item) for item in snapshot.records],
            builtin_handler_ids=list(builtin_hook_handler_ids),
        )

    async def authorize_owner(scope: HookScope, owner_id: str) -> None:
        if scope is HookScope.USER:
            if owner_id != profile_id:
                raise PermissionError("user Hook layer owner differs from the local profile")
            return
        if scope is HookScope.WORKSPACE:
            if owner_id != workspace_id:
                raise PermissionError("workspace Hook layer owner differs from this Workspace")
            return
        if scope is not HookScope.SESSION:
            raise PermissionError("managed Hook layers are not application-managed")
        await harness.get_session(SessionGetCommand(workspace_id, owner_id))

    async def install(raw: WireModel, cancellation: CancellationToken, context: ApplicationCommandContext) -> WireModel:
        params = cast(HooksInstallParams, raw)
        await require_mutation(context)
        layer = _hook_layer(params.layer)
        await authorize_owner(layer.scope, layer.owner_id)
        _validate_hook_registrations(layer, executables, environments)

        async def apply(recovering: bool) -> WireModel:
            del recovering
            await require_mutation(context)
            await initialized(cancellation)
            record = await hooks.install_layer(
                layer,
                expected_revision=params.expected_revision,
                idempotency_key=params.client_request_id,
                cancellation=cancellation,
            )
            return _hook_mutation(params.client_request_id, hooks, record)

        return await receipts.execute(
            method="hooks/install",
            params=params,
            client_request_id=params.client_request_id,
            result_type=HooksMutationResult,
            operation=apply,
        )

    async def confirm_layer(
        raw: WireModel, cancellation: CancellationToken, context: ApplicationCommandContext
    ) -> WireModel:
        params = cast(HooksConfirmLayerParams, raw)
        await require_mutation(context)
        scope = HookScope(params.scope)
        await authorize_owner(scope, params.owner_id)

        async def apply(recovering: bool) -> WireModel:
            del recovering
            await require_mutation(context)
            await initialized(cancellation)
            record = await hooks.confirm_layer(
                scope,
                params.owner_id,
                params.content_hash,
                expected_revision=params.expected_revision,
                idempotency_key=params.client_request_id,
                cancellation=cancellation,
            )
            return _hook_mutation(params.client_request_id, hooks, record)

        return await receipts.execute(
            method="hooks/confirm-layer",
            params=params,
            client_request_id=params.client_request_id,
            result_type=HooksMutationResult,
            operation=apply,
        )

    async def confirm_workspace(
        raw: WireModel, cancellation: CancellationToken, context: ApplicationCommandContext
    ) -> WireModel:
        params = cast(HooksConfirmWorkspaceCommandParams, raw)
        await require_mutation(context)
        await authorize_owner(HookScope.WORKSPACE, params.owner_id)

        async def apply(recovering: bool) -> WireModel:
            del recovering
            await require_mutation(context)
            await initialized(cancellation)
            record = await hooks.confirm_workspace_command(
                params.owner_id,
                params.hook_id,
                params.definition_hash,
                expected_revision=params.expected_revision,
                idempotency_key=params.client_request_id,
                cancellation=cancellation,
            )
            return _hook_mutation(params.client_request_id, hooks, record)

        return await receipts.execute(
            method="hooks/confirm-workspace-command",
            params=params,
            client_request_id=params.client_request_id,
            result_type=HooksMutationResult,
            operation=apply,
        )

    return {
        "hooks/list": list_layers,
        "hooks/install": install,
        "hooks/confirm-layer": confirm_layer,
        "hooks/confirm-workspace-command": confirm_workspace,
    }


async def _effective_config(
    config: ConfigService,
    *,
    workspace_id: str,
    profile_id: str,
    managed_owner_id: str,
) -> HarnessConfig:
    snapshot = await config.snapshot(
        managed_owner_id=managed_owner_id,
        profile_id=profile_id,
        workspace_id=workspace_id,
    )
    return snapshot.config


async def _require_mutation_authority(
    context: ApplicationCommandContext,
    *,
    config: ConfigService,
    workspace_id: str,
    profile_id: str,
    managed_owner_id: str,
) -> None:
    if context.transport != "stdio":
        raise PermissionError("extension mutations require the direct plugin stdio connection")
    effective = await _effective_config(
        config,
        workspace_id=workspace_id,
        profile_id=profile_id,
        managed_owner_id=managed_owner_id,
    )
    if not effective.policy.workspace_trusted or effective.policy.read_only:
        raise PermissionError("extension mutation requires a trusted writable Workspace")


def _skill_status(catalog: SkillCatalog) -> SkillCatalogStatusSnapshot:
    status = catalog.status()
    return SkillCatalogStatusSnapshot(
        revision=status.revision,
        snapshot_hash=status.snapshot_hash,
        discovered_count=status.discovered_count,
        enabled_count=status.enabled_count,
        partial=status.partial,
        diagnostics=[_skill_diagnostic(item) for item in status.diagnostics],
    )


def _skill_diagnostic(value: SkillDiagnostic) -> SkillDiagnosticSnapshot:
    message = re.sub(r"(?:[A-Za-z]:\\|\\\\)[^\r\n]*", "[local path redacted]", value.message)
    path = value.path
    if path is not None and (re.match(r"^(?:[A-Za-z]:[\\/]|[\\/]{1,2})", path) or "\\" in path):
        path = None
    return SkillDiagnosticSnapshot(
        severity=value.severity.value,
        code=value.code.value,
        message=message,
        root_id=value.root_id,
        path=path,
    )


def _skill_snapshot(value: SkillDescriptor) -> SkillSnapshot:
    return SkillSnapshot(
        root_id=value.root_id,
        package_path=value.package_path,
        layer=value.layer.value,
        name=value.name,
        description=value.description,
        metadata_hash=value.content_hash,
        allowed_tools=sorted(value.allowed_tools),
    )


def _shell_profile_input(profile: ShellCommandProfile) -> ShellProfileInput:
    return ShellProfileInput(
        profile_id=profile.profile_id,
        description=profile.description,
        executable_id=profile.executable_id,
        executable_profile_fingerprint=profile.executable_profile_fingerprint,
        fixed_arguments=list(profile.fixed_arguments),
        minimum_variable_arguments=profile.minimum_variable_arguments,
        maximum_variable_arguments=profile.maximum_variable_arguments,
        variable_argument_pattern=profile.variable_argument_pattern,
        cwd_root_id=profile.cwd_root_id,
        environment_profile_id=profile.environment_profile_id,
        timeout_ms=profile.timeout_ms,
        inline_output_limit_bytes=profile.inline_output_limit_bytes,
        artifact_output_limit_bytes=profile.artifact_output_limit_bytes,
        allow_network=profile.allow_network,
        risk=profile.risk.value,
        side_effect_class=profile.side_effect_class.value,
        concurrency_safe=profile.concurrency_safe,
        idempotent=profile.idempotent,
        retryable=profile.retryable,
        version=profile.version,
    )


def _shell_record(record: ShellProfileRecord) -> ShellProfileSnapshot:
    return ShellProfileSnapshot(
        profile=_shell_profile_input(record.profile),
        source=record.source.value,
        trust=record.trust.value,
        enabled=record.enabled,
        revision=record.revision,
        content_hash=record.content_hash,
    )


def _shell_profile(value: ShellProfileInput) -> ShellCommandProfile:
    return ShellCommandProfile(
        profile_id=value.profile_id,
        description=value.description,
        executable_id=value.executable_id,
        executable_profile_fingerprint=value.executable_profile_fingerprint,
        fixed_arguments=tuple(value.fixed_arguments),
        risk=RiskClass(value.risk),
        side_effect_class=SideEffectClass(value.side_effect_class),
        cwd_root_id=value.cwd_root_id,
        environment_profile_id=value.environment_profile_id,
        environment_allowlist=frozenset(),
        environment={},
        minimum_variable_arguments=value.minimum_variable_arguments,
        maximum_variable_arguments=value.maximum_variable_arguments,
        variable_argument_pattern=value.variable_argument_pattern,
        timeout_ms=value.timeout_ms,
        inline_output_limit_bytes=value.inline_output_limit_bytes,
        artifact_output_limit_bytes=value.artifact_output_limit_bytes,
        allow_network=value.allow_network,
        concurrency_safe=False,
        idempotent=value.idempotent,
        retryable=False,
        version=value.version,
    )


def _validate_shell_registration(
    profile: ShellCommandProfile,
    executables: Mapping[str, ProcessExecutableProfile],
    environments: Mapping[str, ProcessEnvironmentProfile],
) -> None:
    if any(_SENSITIVE_ARGUMENT.search(argument) is not None for argument in profile.fixed_arguments):
        raise ValueError("Shell argv cannot contain credential-like values")
    executable = executables.get(profile.executable_id)
    environment = environments.get(profile.environment_profile_id)
    if executable is None or executable.fingerprint != profile.executable_profile_fingerprint:
        raise ValueError("Shell executable ID/fingerprint is not registered")
    if profile.cwd_root_id not in executable.allowed_cwd_roots:
        raise ValueError("Shell cwd root is not registered for the executable")
    if environment is None or profile.environment_profile_id not in executable.environment_profiles:
        raise ValueError("Shell environment profile is not registered for the executable")
    if profile.allow_network and not executable.allow_network:
        raise ValueError("Shell network access exceeds the registered executable profile")
    prefix = executable.fixed_arguments
    if profile.fixed_arguments[: len(prefix)] != prefix:
        raise ValueError("Shell fixed argv does not retain the registered executable prefix")
    fixed_variable_arguments = profile.fixed_arguments[len(prefix) :]
    pattern = re.compile(executable.variable_argument_pattern)
    if any(pattern.fullmatch(argument) is None for argument in fixed_variable_arguments):
        raise ValueError("Shell fixed argv value exceeds the registered executable profile")
    fixed_variable_count = len(fixed_variable_arguments)
    if (
        fixed_variable_count + profile.minimum_variable_arguments < executable.minimum_variable_arguments
        or fixed_variable_count + profile.maximum_variable_arguments > executable.maximum_variable_arguments
        or profile.variable_argument_pattern != executable.variable_argument_pattern
    ):
        raise ValueError("Shell argv schema exceeds the registered executable profile")


def _recover_shell_install(
    shell: ShellProfileService, profile: ShellCommandProfile, expected_revision: int
) -> ShellProfileRecord | None:
    record = next((item for item in shell.records if item.profile.profile_id == profile.profile_id), None)
    if (
        record is not None
        and record.profile == profile
        and record.source is ShellProfileSource.USER
        and record.trust is ShellProfileTrust.CONFIRMATION_REQUIRED
        and record.revision == expected_revision + 1
    ):
        return record
    return None


def _recover_shell_confirm(
    shell: ShellProfileService, profile_id: str, content_hash: str, expected_revision: int
) -> ShellProfileRecord | None:
    record = next((item for item in shell.records if item.profile.profile_id == profile_id), None)
    if (
        record is not None
        and record.content_hash == content_hash
        and record.trust is ShellProfileTrust.CONFIRMED
        and record.revision == expected_revision + 1
    ):
        return record
    return None


def _recover_shell_enabled(
    shell: ShellProfileService, profile_id: str, enabled: bool, expected_revision: int
) -> ShellProfileRecord | None:
    record = next((item for item in shell.records if item.profile.profile_id == profile_id), None)
    if record is not None and record.enabled is enabled and record.revision == expected_revision + 1:
        return record
    return None


def _shell_mutation(
    client_request_id: str, shell: ShellProfileService, record: ShellProfileRecord
) -> ShellMutationResult:
    snapshot = shell.snapshot
    return ShellMutationResult(
        client_request_id=client_request_id,
        catalog_revision=snapshot.revision,
        snapshot_hash=snapshot.snapshot_hash,
        profile=_shell_record(record),
    )


def _hook_layer(value: HookLayerInput) -> HookLayer:
    scope = HookScope(value.scope)
    definitions = tuple(_hook_definition(item, scope=scope, owner_id=value.owner_id) for item in value.hooks)
    return HookLayer(scope, value.owner_id, value.revision, definitions)


def _hook_definition(value: HookDefinitionInput, *, scope: HookScope, owner_id: str) -> HookDefinition:
    command = None if value.command is None else _hook_command(value.command)
    return HookDefinition(
        hook_id=value.hook_id,
        scope=scope,
        owner_id=owner_id,
        event=HookEvent(value.event),
        implementation=HookImplementation(value.implementation),
        priority=value.priority,
        timeout_ms=value.timeout_ms,
        output_limit_bytes=value.output_limit_bytes,
        enabled=value.enabled,
        handler_id=value.handler_id,
        command=command,
    )


def _hook_command(value: HookCommandInput) -> HookCommandSpec:
    return HookCommandSpec(
        executable_id=value.executable_id,
        arguments=tuple(value.arguments),
        allowed_environment=frozenset(value.allowed_environment),
        executable_profile_fingerprint=value.executable_profile_fingerprint,
        cwd_root_id=value.cwd_root_id,
        cwd=value.cwd,
        environment_profile_id=value.environment_profile_id,
        artifact_output_limit_bytes=value.artifact_output_limit_bytes,
    )


def _hook_definition_snapshot(value: HookDefinition) -> HookDefinitionSnapshot:
    command = value.command
    return HookDefinitionSnapshot(
        hook_id=value.hook_id,
        event=value.event.value,
        implementation=value.implementation.value,
        priority=value.priority,
        timeout_ms=value.timeout_ms,
        output_limit_bytes=value.output_limit_bytes,
        enabled=value.enabled,
        handler_id=value.handler_id,
        command=(
            None
            if command is None
            else HookCommandInput(
                executable_id=command.executable_id,
                arguments=list(command.arguments),
                allowed_environment=sorted(command.allowed_environment),
                executable_profile_fingerprint=cast(str, command.executable_profile_fingerprint),
                cwd_root_id=command.cwd_root_id,
                cwd=command.cwd,
                environment_profile_id=command.environment_profile_id,
                artifact_output_limit_bytes=command.artifact_output_limit_bytes,
            )
        ),
        definition_hash=hook_definition_hash(value),
    )


def _hook_record(record: HookLayerRecord) -> HookLayerSnapshot:
    layer = record.layer
    return HookLayerSnapshot(
        scope=layer.scope.value,
        owner_id=layer.owner_id,
        revision=layer.revision,
        hooks=[_hook_definition_snapshot(item) for item in layer.hooks],
        denied_events=sorted(item.value for item in layer.denied_events),
        denied_hook_ids=sorted(layer.denied_hook_ids),
        trust=record.trust.value,
        content_hash=record.content_hash,
        command_confirmations=dict(sorted(record.command_confirmations.items())),
        record_revision=record.revision,
    )


def _validate_hook_registrations(
    layer: HookLayer,
    executables: Mapping[str, ProcessExecutableProfile],
    environments: Mapping[str, ProcessEnvironmentProfile],
) -> None:
    for definition in layer.hooks:
        command = definition.command
        if command is None:
            continue
        if any(_SENSITIVE_ARGUMENT.search(argument) is not None for argument in command.arguments):
            raise ValueError("Hook argv cannot contain credential-like values")
        executable = executables.get(command.executable_id)
        environment = environments.get(command.environment_profile_id)
        if executable is None or executable.fingerprint != command.executable_profile_fingerprint:
            raise ValueError("Hook executable ID/fingerprint is not registered")
        if command.cwd_root_id not in executable.allowed_cwd_roots:
            raise ValueError("Hook cwd root is not registered for the executable")
        if environment is None or command.environment_profile_id not in executable.environment_profiles:
            raise ValueError("Hook environment profile is not registered for the executable")
        if not command.allowed_environment <= environment.allowed_names:
            raise ValueError("Hook environment names exceed the registered non-secret profile")
        prefix = executable.fixed_arguments
        if command.arguments[: len(prefix)] != prefix:
            raise ValueError("Hook argv does not retain the registered executable prefix")
        variable = command.arguments[len(prefix) :]
        if not executable.minimum_variable_arguments <= len(variable) <= executable.maximum_variable_arguments:
            raise ValueError("Hook argv count exceeds the registered executable profile")
        pattern = re.compile(executable.variable_argument_pattern)
        if any(pattern.fullmatch(argument) is None for argument in variable):
            raise ValueError("Hook argv value exceeds the registered executable profile")


def _hook_mutation(
    client_request_id: str, hooks: HookConfigurationService, record: HookLayerRecord
) -> HooksMutationResult:
    snapshot = hooks.snapshot
    return HooksMutationResult(
        client_request_id=client_request_id,
        catalog_revision=snapshot.revision,
        snapshot_hash=snapshot.snapshot_hash,
        layer=_hook_record(record),
    )


def _receipt_entity_id(workspace_id: str, client_request_id: str) -> str:
    digest = hashlib.sha256(f"{workspace_id}\0{client_request_id}".encode()).hexdigest()
    return f"extension-admin-{digest}"


def _receipt_value(
    *,
    workspace_id: str,
    client_request_id: str,
    method: str,
    request_hash: str,
    state: str,
    result: Mapping[str, object] | None,
) -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "workspaceId": workspace_id,
        "clientRequestId": client_request_id,
        "method": method,
        "requestHash": request_hash,
        "state": state,
        "result": result,
    }


def _parse_receipt(
    raw: object,
    *,
    workspace_id: str,
    client_request_id: str,
    method: str,
    request_hash: str,
) -> tuple[str, dict[str, object] | None]:
    expected = {
        "schemaVersion",
        "workspaceId",
        "clientRequestId",
        "method",
        "requestHash",
        "state",
        "result",
    }
    if not isinstance(raw, Mapping) or set(raw) != expected:
        raise ValueError("extension management receipt fields are invalid")
    if (
        raw["schemaVersion"] != 1
        or raw["workspaceId"] != workspace_id
        or raw["clientRequestId"] != client_request_id
        or raw["method"] != method
        or raw["requestHash"] != request_hash
        or raw["state"] not in _RECEIPT_STATES
    ):
        raise ValueError("clientRequestId was reused for another extension management command")
    state = cast(str, raw["state"])
    result = raw["result"]
    if state == "pending":
        if result is not None:
            raise ValueError("pending extension management receipt cannot contain a result")
        return state, None
    if not isinstance(result, Mapping) or any(not isinstance(key, str) for key in result):
        raise ValueError("complete extension management receipt result is invalid")
    return state, dict(cast(Mapping[str, object], result))


__all__ = ["ExtensionManagementReceiptJournal", "extension_management_command_handlers"]
