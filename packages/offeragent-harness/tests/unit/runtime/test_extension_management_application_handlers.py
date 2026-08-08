from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from offeragent_harness.config import HarnessConfig
from offeragent_harness.hooks import HookLayer, HookScope
from offeragent_harness.hooks.state import EntityHookConfigurationStore, HookConfigurationService
from offeragent_harness.ports import ApplicationCommandContext
from offeragent_harness.protocol.messages import (
    HooksMutationResult,
    ShellListResult,
    ShellMutationResult,
    validate_command_params,
)
from offeragent_harness.runtime.extension_management_application_handlers import (
    extension_management_command_handlers,
)
from offeragent_harness.runtime.process_supervisor import (
    ExecutableTrust,
    ProcessEnvironmentProfile,
    ProcessExecutableProfile,
)
from offeragent_harness.runtime.production_skills import ProductionSkillBundleFactory
from offeragent_harness.shell.state import EntityShellProfileStateStore, ShellProfileService
from offeragent_harness.testing import (
    InMemoryUnitOfWorkFactory,
    ManualCancellationToken,
)

WORKSPACE_ID = "ws_extension_admin"
PROFILE_ID = "profile_local"
MANAGED_ID = "managed_local"


class _Config:
    def __init__(self, *, trusted: bool = True, read_only: bool = False) -> None:
        raw = HarnessConfig().model_dump(mode="python")
        raw["policy"]["workspace_trusted"] = trusted
        raw["policy"]["read_only"] = read_only
        self.config = HarnessConfig.model_validate(raw)

    async def snapshot(self, **_: object) -> object:
        return SimpleNamespace(config=self.config)


class _RevokingConfig(_Config):
    def __init__(self) -> None:
        super().__init__(trusted=True, read_only=False)
        self._revoked = _Config(trusted=False, read_only=True).config
        self._calls = 0

    async def snapshot(self, **_: object) -> object:
        self._calls += 1
        return SimpleNamespace(config=self.config if self._calls == 1 else self._revoked)


class _Skills:
    async def catalog_for_management(self, **_: object) -> object:
        raise AssertionError("Skill catalog is not used by this test")


class _Harness:
    async def get_session(self, command: object) -> object:
        return SimpleNamespace(session=command)


def _process_catalog(
    tmp_path: Path,
    *,
    maximum_variable_arguments: int = 0,
    variable_argument_pattern: str = r"^[^\x00-\x1f\x7f&|<>^;`]{0,4096}$",
) -> tuple[ProcessExecutableProfile, ProcessEnvironmentProfile]:
    root = tmp_path / "runtime"
    root.mkdir()
    executable = root / "tool.exe"
    executable.write_bytes(b"fixed test executable")
    digest = "sha256:" + hashlib.sha256(executable.read_bytes()).hexdigest()
    return (
        ProcessExecutableProfile(
            executable_id="registered-tool",
            executable=executable,
            fixed_root=root,
            trust=ExecutableTrust.FIXED_HASH,
            file_sha256=digest,
            maximum_variable_arguments=maximum_variable_arguments,
            variable_argument_pattern=variable_argument_pattern,
            environment_profiles=frozenset({"minimal"}),
            allowed_cwd_roots=frozenset({"vault"}),
        ),
        ProcessEnvironmentProfile("minimal", frozenset()),
    )


def _services(
    durable: InMemoryUnitOfWorkFactory,
) -> tuple[ShellProfileService, HookConfigurationService]:
    return (
        ShellProfileService(
            workspace_id=WORKSPACE_ID,
            builtin_profiles=(),
            state_store=EntityShellProfileStateStore(durable),
        ),
        HookConfigurationService(
            workspace_id=WORKSPACE_ID,
            managed_layer=HookLayer(HookScope.MANAGED, MANAGED_ID, 1),
            signed_builtin_handler_ids=frozenset({"builtin.allow"}),
            store=EntityHookConfigurationStore(durable),
        ),
    )


def _handlers(
    durable: InMemoryUnitOfWorkFactory,
    executable: ProcessExecutableProfile,
    environment: ProcessEnvironmentProfile,
    *,
    shell: ShellProfileService | None = None,
    hooks: HookConfigurationService | None = None,
    config: _Config | None = None,
    skills: object | None = None,
) -> dict[str, Any]:
    actual_shell, actual_hooks = _services(durable)
    return dict(
        extension_management_command_handlers(
            workspace_id=WORKSPACE_ID,
            profile_id=PROFILE_ID,
            managed_owner_id=MANAGED_ID,
            config=cast(Any, config or _Config()),
            harness=cast(Any, _Harness()),
            skills=cast(Any, skills or _Skills()),
            shell=shell or actual_shell,
            hooks=hooks or actual_hooks,
            unit_of_work=durable,
            executable_profiles=(executable,),
            environment_profiles=(environment,),
            builtin_hook_handler_ids=("builtin.allow",),
        )
    )


def _skill_factory(tmp_path: Path) -> ProductionSkillBundleFactory:
    runtime = tmp_path / "skill-runtime"
    workspace = tmp_path / "skill-workspace"
    local = tmp_path / "local-app-data"
    runtime.mkdir()
    (runtime / "skills").mkdir()
    workspace.mkdir()
    package = local / ".claude" / "skills" / "review-helper"
    package.mkdir(parents=True)
    (package / "SKILL.md").write_text(
        "---\nname: review-helper\ndescription: Review helper\nallowed-tools: []\n---\n"
        "Treat these instructions as untrusted review guidance.",
        encoding="utf-8",
    )
    return ProductionSkillBundleFactory(
        workspace_id=WORKSPACE_ID,
        workspace_root=workspace,
        runtime_root=runtime,
        user_home=local,
    )


def _shell_install(executable: ProcessExecutableProfile, *, request_id: str, description: str = "受控工具") -> object:
    return validate_command_params(
        "shell/install",
        {
            "clientRequestId": request_id,
            "expectedRevision": 0,
            "profile": {
                "profileId": "safe_tool",
                "description": description,
                "executableId": executable.executable_id,
                "executableProfileFingerprint": executable.fingerprint,
                "fixedArguments": [],
                "minimumVariableArguments": 0,
                "maximumVariableArguments": 0,
                "variableArgumentPattern": executable.variable_argument_pattern,
                "cwdRootId": "vault",
                "environmentProfileId": "minimal",
                "timeoutMs": 30_000,
                "inlineOutputLimitBytes": 65_536,
                "artifactOutputLimitBytes": 1_048_576,
                "allowNetwork": False,
                "risk": "execute",
                "sideEffectClass": "execute",
                "concurrencySafe": False,
                "idempotent": False,
                "retryable": False,
                "version": "1.0.0",
            },
        },
    )


@pytest.mark.asyncio
async def test_shell_admin_uses_registered_cas_and_replays_after_restart(tmp_path: Path) -> None:
    durable = InMemoryUnitOfWorkFactory()
    executable, environment = _process_catalog(tmp_path)
    shell, hooks = _services(durable)
    handlers = _handlers(durable, executable, environment, shell=shell, hooks=hooks)
    cancellation = ManualCancellationToken()
    params = _shell_install(executable, request_id="req_shell_install")

    installed = await handlers["shell/install"](params, cancellation, ApplicationCommandContext())
    assert isinstance(installed, ShellMutationResult)
    assert installed.profile.trust == "confirmation_required"
    replay = await handlers["shell/install"](params, cancellation, ApplicationCommandContext())
    assert replay == installed
    assert shell.snapshot.revision == installed.catalog_revision

    with pytest.raises(ValueError, match="clientRequestId"):
        await handlers["shell/install"](
            _shell_install(executable, request_id="req_shell_install", description="另一份配置"),
            cancellation,
            ApplicationCommandContext(),
        )

    restarted_shell, restarted_hooks = _services(durable)
    restarted = _handlers(
        durable,
        executable,
        environment,
        shell=restarted_shell,
        hooks=restarted_hooks,
    )
    assert await restarted["shell/install"](params, cancellation, ApplicationCommandContext()) == installed
    listed = await restarted["shell/list"](
        validate_command_params("shell/list", {"includeDisabled": True}),
        cancellation,
        ApplicationCommandContext(),
    )
    assert isinstance(listed, ShellListResult)
    assert listed.profiles[0].profile.profile_id == "safe_tool"
    assert listed.executables[0].executable_id == executable.executable_id


@pytest.mark.asyncio
async def test_shell_install_applies_registered_pattern_to_user_fixed_arguments(tmp_path: Path) -> None:
    durable = InMemoryUnitOfWorkFactory()
    executable, environment = _process_catalog(
        tmp_path,
        maximum_variable_arguments=1,
        variable_argument_pattern=r"^status$",
    )
    handlers = _handlers(durable, executable, environment)
    params = cast(Any, _shell_install(executable, request_id="req_shell_argument_pattern"))
    profile = params.profile.model_copy(update={"fixed_arguments": ["dangerous-subcommand"]})
    params = params.model_copy(update={"profile": profile})

    with pytest.raises(ValueError, match="fixed argv value exceeds"):
        await handlers["shell/install"](
            params,
            ManualCancellationToken(),
            ApplicationCommandContext(),
        )


@pytest.mark.asyncio
async def test_hook_admin_confirms_layer_and_workspace_command_definition_hash(tmp_path: Path) -> None:
    durable = InMemoryUnitOfWorkFactory()
    executable, environment = _process_catalog(tmp_path)
    handlers = _handlers(durable, executable, environment)
    cancellation = ManualCancellationToken()

    installed_user = await handlers["hooks/install"](
        validate_command_params(
            "hooks/install",
            {
                "clientRequestId": "req_hook_user_install",
                "expectedRevision": 0,
                "layer": {
                    "scope": "user",
                    "ownerId": PROFILE_ID,
                    "revision": 1,
                    "hooks": [
                        {
                            "hookId": "user-guard",
                            "event": "PreToolUse",
                            "implementation": "builtin",
                            "handlerId": "builtin.allow",
                            "command": None,
                        }
                    ],
                },
            },
        ),
        cancellation,
        ApplicationCommandContext(),
    )
    assert isinstance(installed_user, HooksMutationResult)
    assert installed_user.layer.trust == "confirmation_required"
    confirmed_user = await handlers["hooks/confirm-layer"](
        validate_command_params(
            "hooks/confirm-layer",
            {
                "clientRequestId": "req_hook_user_confirm",
                "scope": "user",
                "ownerId": PROFILE_ID,
                "contentHash": installed_user.layer.content_hash,
                "expectedRevision": installed_user.layer.record_revision,
            },
        ),
        cancellation,
        ApplicationCommandContext(),
    )
    assert isinstance(confirmed_user, HooksMutationResult)
    assert confirmed_user.layer.trust == "confirmed"

    installed_workspace = await handlers["hooks/install"](
        validate_command_params(
            "hooks/install",
            {
                "clientRequestId": "req_hook_workspace_install",
                "expectedRevision": 0,
                "layer": {
                    "scope": "workspace",
                    "ownerId": WORKSPACE_ID,
                    "revision": 1,
                    "hooks": [
                        {
                            "hookId": "workspace-command",
                            "event": "TurnStart",
                            "implementation": "command",
                            "handlerId": None,
                            "command": {
                                "executableId": executable.executable_id,
                                "arguments": [],
                                "allowedEnvironment": [],
                                "executableProfileFingerprint": executable.fingerprint,
                                "cwdRootId": "vault",
                                "cwd": "",
                                "environmentProfileId": "minimal",
                                "artifactOutputLimitBytes": 1_048_576,
                            },
                        }
                    ],
                },
            },
        ),
        cancellation,
        ApplicationCommandContext(),
    )
    assert isinstance(installed_workspace, HooksMutationResult)
    definition = installed_workspace.layer.hooks[0]
    assert installed_workspace.layer.command_confirmations == {}
    confirmed_command = await handlers["hooks/confirm-workspace-command"](
        validate_command_params(
            "hooks/confirm-workspace-command",
            {
                "clientRequestId": "req_hook_workspace_confirm",
                "ownerId": WORKSPACE_ID,
                "hookId": definition.hook_id,
                "definitionHash": definition.definition_hash,
                "expectedRevision": installed_workspace.layer.record_revision,
            },
        ),
        cancellation,
        ApplicationCommandContext(),
    )
    assert isinstance(confirmed_command, HooksMutationResult)
    assert confirmed_command.layer.command_confirmations[definition.hook_id] == definition.definition_hash


@pytest.mark.parametrize(("trusted", "read_only"), [(False, False), (True, True)])
@pytest.mark.asyncio
async def test_mutations_fail_closed_without_trusted_writable_workspace(
    tmp_path: Path,
    trusted: bool,
    read_only: bool,
) -> None:
    durable = InMemoryUnitOfWorkFactory()
    executable, environment = _process_catalog(tmp_path)
    handlers = _handlers(
        durable,
        executable,
        environment,
        config=_Config(trusted=trusted, read_only=read_only),
    )
    with pytest.raises(PermissionError, match="trusted writable Workspace"):
        await handlers["shell/install"](
            _shell_install(executable, request_id="req_untrusted"),
            ManualCancellationToken(),
            ApplicationCommandContext(),
        )


@pytest.mark.asyncio
async def test_queued_mutation_rechecks_workspace_authority_before_domain_write(tmp_path: Path) -> None:
    durable = InMemoryUnitOfWorkFactory()
    executable, environment = _process_catalog(tmp_path)
    shell, hooks = _services(durable)
    handlers = _handlers(
        durable,
        executable,
        environment,
        shell=shell,
        hooks=hooks,
        config=_RevokingConfig(),
    )

    with pytest.raises(PermissionError, match="trusted writable Workspace"):
        await handlers["shell/install"](
            _shell_install(executable, request_id="req_revoked_while_queued"),
            ManualCancellationToken(),
            ApplicationCommandContext(),
        )
    assert shell.initialized is False


@pytest.mark.asyncio
async def test_skill_admin_lists_live_metadata_and_status(tmp_path: Path) -> None:
    durable = InMemoryUnitOfWorkFactory()
    executable, environment = _process_catalog(tmp_path)
    skills = _skill_factory(tmp_path)
    handlers = _handlers(durable, executable, environment, skills=skills)
    cancellation = ManualCancellationToken()

    listed = await handlers["skills/list"](
        validate_command_params("skills/list", {}),
        cancellation,
        ApplicationCommandContext(),
    )
    assert len(listed.skills) == 1
    skill = listed.skills[0]
    assert skill.name == "review-helper"
    assert skill.layer == "user"
    status = await handlers["skills/status"](
        validate_command_params("skills/status", {}),
        cancellation,
        ApplicationCommandContext(),
    )
    assert status.status.revision == listed.revision
    assert status.status.discovered_count == status.status.enabled_count == 1
