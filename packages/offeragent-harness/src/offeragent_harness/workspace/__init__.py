"""Workspace identity and filesystem security boundaries."""

from .code_tools import (
    CODE_TOOL_OUTPUT_LIMIT_BYTES,
    CODE_TOOL_VERSION,
    CodeToolError,
    CodeToolExecutor,
    CodeToolLimits,
    code_tool_definitions,
)
from .filesystem import (
    VaultFileSystem,
    VaultFilesystemError,
    VaultFilesystemErrorCode,
    VaultReadPolicy,
    VaultTransactionExecutor,
)
from .identity import (
    CanonicalRootIdentity,
    WorkspaceInstanceRecord,
    WorkspaceRegistry,
    WorkspaceRegistryCorrupt,
    identify_workspace_root,
)
from .path_policy import (
    PathPolicyError,
    PathPolicyErrorCode,
    ResolvedWorkspacePath,
    WorkspacePathPolicy,
    WorkspaceRoot,
)

__all__ = [
    "CODE_TOOL_OUTPUT_LIMIT_BYTES",
    "CODE_TOOL_VERSION",
    "CanonicalRootIdentity",
    "CodeToolError",
    "CodeToolExecutor",
    "CodeToolLimits",
    "PathPolicyError",
    "PathPolicyErrorCode",
    "ResolvedWorkspacePath",
    "VaultFileSystem",
    "VaultFilesystemError",
    "VaultFilesystemErrorCode",
    "VaultReadPolicy",
    "VaultTransactionExecutor",
    "WorkspaceInstanceRecord",
    "WorkspacePathPolicy",
    "WorkspaceRegistry",
    "WorkspaceRegistryCorrupt",
    "WorkspaceRoot",
    "code_tool_definitions",
    "identify_workspace_root",
]
