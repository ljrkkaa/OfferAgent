from __future__ import annotations

import pytest

from offeragent_harness.protocol.capabilities import CapabilityName, CapabilitySet
from offeragent_harness.protocol.errors import ErrorCode, ProtocolViolation
from offeragent_harness.protocol.messages import COMMAND_REGISTRY, validate_command_params

EXTENSION_METHODS = {
    "skills/list": CapabilityName.SKILLS,
    "skills/status": CapabilityName.SKILLS,
    "skills/rescan": CapabilityName.SKILLS,
    "skills/confirm-trust": CapabilityName.SKILLS,
    "shell/list": CapabilityName.SHELL,
    "shell/install": CapabilityName.SHELL,
    "shell/confirm": CapabilityName.SHELL,
    "shell/set-enabled": CapabilityName.SHELL,
    "hooks/list": CapabilityName.HOOKS,
    "hooks/install": CapabilityName.HOOKS,
    "hooks/confirm-layer": CapabilityName.HOOKS,
    "hooks/confirm-workspace-command": CapabilityName.HOOKS,
}


def test_extension_management_capabilities_and_commands_are_explicit() -> None:
    capabilities = CapabilitySet(skills=True, shell=True, hooks=True)
    assert {CapabilityName.SKILLS, CapabilityName.SHELL, CapabilityName.HOOKS} <= capabilities.enabled()
    assert all(
        COMMAND_REGISTRY[method].required_capability is capability for method, capability in EXTENSION_METHODS.items()
    )


@pytest.mark.parametrize("field", ["rawCommand", "secret", "environment"])
def test_shell_install_rejects_raw_command_and_secret_shaped_fields(field: str) -> None:
    profile = {
        "profileId": "safe_tool",
        "description": "safe",
        "executableId": "registered",
        "executableProfileFingerprint": "sha256:" + "a" * 64,
        "fixedArguments": [],
        "risk": "execute",
        "sideEffectClass": "execute",
    }
    profile[field] = "forbidden"
    with pytest.raises(ProtocolViolation) as caught:
        validate_command_params(
            "shell/install",
            {"clientRequestId": "req_shell", "expectedRevision": 0, "profile": profile},
        )
    assert caught.value.error.code is ErrorCode.PROTOCOL_INVALID_PARAMS


def test_hook_install_is_discriminated_and_schema_closed() -> None:
    with pytest.raises(ProtocolViolation) as caught:
        validate_command_params(
            "hooks/install",
            {
                "clientRequestId": "req_hook",
                "expectedRevision": 0,
                "layer": {
                    "scope": "workspace",
                    "ownerId": "ws_test",
                    "revision": 1,
                    "hooks": [
                        {
                            "hookId": "unsafe",
                            "event": "TurnStart",
                            "implementation": "command",
                            "handlerId": None,
                            "command": {
                                "executableId": "registered",
                                "arguments": [],
                                "allowedEnvironment": [],
                                "executableProfileFingerprint": "sha256:" + "a" * 64,
                                "rawCommand": "powershell -c whoami",
                            },
                        }
                    ],
                },
            },
        )
    assert caught.value.error.code is ErrorCode.PROTOCOL_INVALID_PARAMS
