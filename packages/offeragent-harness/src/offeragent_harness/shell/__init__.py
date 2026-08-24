"""Profile-gated Shell tool definitions and executor."""

from .executor import ShellToolExecutor
from .powershell import (
    POWERSHELL_OUTPUT_LIMIT_BYTES,
    POWERSHELL_TOOL_VERSION,
    PowerShellToolError,
    PowerShellToolExecutor,
    powershell_tool_definitions,
)
from .profiles import ShellCommandProfile
from .state import (
    EntityShellProfileStateStore,
    ShellProfileRecord,
    ShellProfileService,
    ShellProfileSnapshot,
    ShellProfileSource,
    ShellProfileTrust,
    shell_profile_hash,
)

__all__ = [
    "POWERSHELL_OUTPUT_LIMIT_BYTES",
    "POWERSHELL_TOOL_VERSION",
    "EntityShellProfileStateStore",
    "PowerShellToolError",
    "PowerShellToolExecutor",
    "ShellCommandProfile",
    "ShellProfileRecord",
    "ShellProfileService",
    "ShellProfileSnapshot",
    "ShellProfileSource",
    "ShellProfileTrust",
    "ShellToolExecutor",
    "powershell_tool_definitions",
    "shell_profile_hash",
]
