"""Risk classifications shared by every tool source."""

from enum import Enum


class RiskClass(str, Enum):
    READ = "read"
    NETWORK = "network"
    WRITE = "write"
    EXECUTE = "execute"
    DESTRUCTIVE = "destructive"
    EXTERNAL_PATH = "external_path"
    SECRET_ACCESS = "secret_access"


class PermissionMode(str, Enum):
    READ_ONLY = "read_only"
    NORMAL = "normal"
    TRUSTED_WORKSPACE = "trusted_workspace"
    PLAN = "plan"
    BYPASS = "bypass"


__all__ = ["PermissionMode", "RiskClass"]
