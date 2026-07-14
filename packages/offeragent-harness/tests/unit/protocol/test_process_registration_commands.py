from __future__ import annotations

from collections.abc import Mapping

import pytest

from offeragent_harness.protocol.errors import ProtocolViolation
from offeragent_harness.protocol.messages import validate_command_params


def _executable(path: str = r"C:\Vendor\tool.exe") -> dict[str, object]:
    return {
        "executableId": "local_tool",
        "executablePath": path,
        "fixedArguments": ["--stdio"],
        "minimumVariableArguments": 0,
        "maximumVariableArguments": 1,
        "variableArgumentPattern": "^[a-z]+$",
        "environmentProfileIds": ["minimal"],
        "allowedStdinModes": ["duplex"],
        "allowedCwdRootIds": ["process-scratch"],
        "appcontainerFilesystem": [{"rootId": "process-scratch", "relativePath": "working", "access": "read_write"}],
        "allowNetwork": False,
        "expectedRevision": 0,
        "expectedContentHash": None,
    }


def test_process_probe_requires_one_closed_network_registration() -> None:
    params = validate_command_params(
        "process/registrations/probe",
        {"executable": _executable(), "environment": None},
    )
    executable = params.to_wire()["executable"]
    assert isinstance(executable, Mapping)
    assert executable["allowNetwork"] is False

    for raw in (
        {"executable": None, "environment": None},
        {
            "executable": _executable(),
            "environment": {
                "profileId": "user-env",
                "allowedNames": [],
                "allowedSecretNames": [],
                "expectedRevision": 0,
                "expectedContentHash": None,
            },
        },
    ):
        with pytest.raises(ProtocolViolation):
            validate_command_params("process/registrations/probe", raw)


@pytest.mark.parametrize("path", ["relative.exe", r"\\server\share\tool.exe", r"C:\tool.exe"])
def test_process_probe_rejects_nonlocal_or_drive_root_executable(path: str) -> None:
    with pytest.raises(ProtocolViolation):
        validate_command_params(
            "process/registrations/probe",
            {"executable": _executable(path), "environment": None},
        )


def test_process_probe_rejects_network_and_wide_filesystem_grants() -> None:
    network = _executable()
    network["allowNetwork"] = True
    with pytest.raises(ProtocolViolation):
        validate_command_params(
            "process/registrations/probe",
            {"executable": network, "environment": None},
        )

    wide = _executable()
    wide["appcontainerFilesystem"] = [{"rootId": "process-scratch", "relativePath": "", "access": "read_write"}]
    with pytest.raises(ProtocolViolation):
        validate_command_params(
            "process/registrations/probe",
            {"executable": wide, "environment": None},
        )
