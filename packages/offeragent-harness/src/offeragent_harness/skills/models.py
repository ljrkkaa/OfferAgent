"""Domain records for Claude-style, progressively disclosed local Skills.

Project Skills inherit the already explicit Workspace trust decision. User and
builtin Skill roots are configuration owned by the local user/runtime. There is
no second per-file trust state: discovery exposes metadata and the Skill body is
opened only when the model or user invokes that Skill.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

_SKILL_NAME = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_TOOL_NAME = re.compile(r"^[a-z][a-z0-9_-]*(?:\.[a-z][a-z0-9_-]*)*$")
_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")
_ROOT_ID = re.compile(r"^[a-z][a-z0-9._-]{0,63}$")


class SkillLayer(str, Enum):
    BUILTIN = "builtin"
    USER = "user"
    WORKSPACE = "workspace"

    @property
    def priority(self) -> int:
        return {SkillLayer.BUILTIN: 100, SkillLayer.USER: 200, SkillLayer.WORKSPACE: 300}[self]


class SkillDiagnosticSeverity(str, Enum):
    WARNING = "warning"
    ERROR = "error"


class SkillErrorCode(str, Enum):
    INVALID_ROOT = "invalid_root"
    PATH_POLICY = "path_policy"
    REPARSE_POINT = "reparse_point"
    CASEFOLD_COLLISION = "casefold_collision"
    LIMIT_EXCEEDED = "limit_exceeded"
    INVALID_FRONTMATTER = "invalid_frontmatter"
    UNKNOWN_FIELD = "unknown_field"
    DUPLICATE_KEY = "duplicate_key"
    ENCODING = "encoding_error"
    HASH_DRIFT = "hash_drift"
    WORKSPACE_UNTRUSTED = "workspace_untrusted"
    CONFLICT = "skill_conflict"
    CAPABILITY_UNAVAILABLE = "capability_unavailable"
    NOT_FOUND = "skill_not_found"
    CAS_CONFLICT = "catalog_revision_conflict"
    CHANGED_DURING_READ = "changed_during_read"


class SkillError(RuntimeError):
    def __init__(self, code: SkillErrorCode, message: str, *, path: str | None = None) -> None:
        self.code = code
        self.path = path
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class SkillDiagnostic:
    severity: SkillDiagnosticSeverity
    code: SkillErrorCode
    message: str
    root_id: str | None = None
    path: str | None = None


@dataclass(frozen=True, slots=True)
class SkillLimits:
    max_roots: int = 8
    max_scanned_entries: int = 10_000
    max_skills: int = 1_000
    max_depth: int = 8
    max_metadata_bytes: int = 64 * 1024
    max_skill_bytes: int = 2 * 1024 * 1024
    max_total_load_bytes: int = 4 * 1024 * 1024
    max_allowed_tools: int = 256
    max_json_depth: int = 16
    max_description_chars: int = 2_048
    max_body_chars: int = 1_000_000

    def __post_init__(self) -> None:
        values = (
            self.max_roots,
            self.max_scanned_entries,
            self.max_skills,
            self.max_depth,
            self.max_metadata_bytes,
            self.max_skill_bytes,
            self.max_total_load_bytes,
            self.max_allowed_tools,
            self.max_json_depth,
            self.max_description_chars,
            self.max_body_chars,
        )
        if any(not isinstance(value, int) or value <= 0 for value in values):
            raise ValueError("Skill limits must be positive integers")
        if self.max_metadata_bytes > self.max_skill_bytes or self.max_skill_bytes > self.max_total_load_bytes:
            raise ValueError("Skill byte limits are inconsistent")
        if self.max_skills > 100_000 or self.max_depth > 32 or self.max_json_depth > 64:
            raise ValueError("Skill limits exceed the domain ceiling")


@dataclass(frozen=True, slots=True)
class SkillFileFact:
    device: int
    inode: int
    size: int
    mtime_ns: int
    ctime_ns: int

    def __post_init__(self) -> None:
        if min(self.device, self.inode, self.size, self.mtime_ns, self.ctime_ns) < 0:
            raise ValueError("Skill file fact cannot contain negative values")


@dataclass(frozen=True, slots=True)
class SkillRoot:
    workspace_id: str
    root_id: str
    layer: SkillLayer
    path: Path
    workspace_root: Path | None = None
    workspace_trusted: bool = False

    def __post_init__(self) -> None:
        if not self.workspace_id or "\x00" in self.workspace_id:
            raise ValueError("Skill root workspace_id is invalid")
        if _ROOT_ID.fullmatch(self.root_id) is None:
            raise ValueError("Skill root_id is invalid")
        if self.layer is SkillLayer.WORKSPACE and self.workspace_root is None:
            raise ValueError("Workspace Skill root requires workspace_root")
        if self.layer is not SkillLayer.WORKSPACE and self.workspace_root is not None:
            raise ValueError("workspace_root is only valid for Workspace Skills")
        if self.layer is not SkillLayer.WORKSPACE and not self.workspace_trusted:
            raise ValueError("non-Workspace Skill roots require explicit trust")


@dataclass(frozen=True, slots=True)
class SkillDescriptor:
    workspace_id: str
    root_id: str
    layer: SkillLayer
    root_path: Path
    package_path: str
    skill_file: Path
    name: str
    description: str
    allowed_tools: frozenset[str]
    content_hash: str
    file_fact: SkillFileFact
    metadata_bytes_read: int

    def __post_init__(self) -> None:
        if _SKILL_NAME.fullmatch(self.name) is None:
            raise ValueError("Skill name must be lowercase kebab-case")
        if not self.description or len(self.description) > 2_048 or "\x00" in self.description:
            raise ValueError("Skill description is invalid")
        if _HASH.fullmatch(self.content_hash) is None:
            raise ValueError("Skill metadata hash must be canonical sha256")
        if any(_TOOL_NAME.fullmatch(name) is None for name in self.allowed_tools):
            raise ValueError("Skill allowed_tools contains an invalid tool name")
        if self.metadata_bytes_read <= 0:
            raise ValueError("Skill discovery must record positive metadata bytes")

    @property
    def origin_key(self) -> tuple[str, str]:
        return (self.root_id, self.package_path)

    @property
    def cache_key(self) -> str:
        material = "\0".join((self.workspace_id, self.root_id, self.package_path, self.content_hash))
        return f"sha256:{hashlib.sha256(material.encode()).hexdigest()}"


@dataclass(frozen=True, slots=True)
class SkillSummary:
    root_id: str
    package_path: str
    name: str
    description: str
    layer: SkillLayer
    content_hash: str
    allowed_tools: frozenset[str]


@dataclass(frozen=True, slots=True)
class SkillAuthority:
    available_tools: frozenset[str]
    policy_allowed_tools: frozenset[str]
    enabled_skills: frozenset[str] | None = None
    workspace_trusted: bool = True

    def __post_init__(self) -> None:
        if not self.policy_allowed_tools <= self.available_tools:
            raise ValueError("Skill policy tool ceiling must be a subset of available tools")
        if self.enabled_skills is not None and any(_SKILL_NAME.fullmatch(item) is None for item in self.enabled_skills):
            raise ValueError("enabled Skill names are invalid")


@dataclass(frozen=True, slots=True)
class SkillSelection:
    descriptor: SkillDescriptor
    effective_allowed_tools: frozenset[str]
    unavailable_declared_tools: frozenset[str]


@dataclass(frozen=True, slots=True)
class UntrustedSkillInstruction:
    text: str
    source_ref: str
    content_hash: str
    precedence: str = "untrusted_skill"

    def __post_init__(self) -> None:
        if self.precedence != "untrusted_skill":
            raise ValueError("Skill instructions must remain below system/user/policy precedence")


@dataclass(frozen=True, slots=True)
class LoadedSkill:
    selection: SkillSelection
    instruction: UntrustedSkillInstruction
    total_bytes: int


@dataclass(frozen=True, slots=True)
class SkillCatalogSnapshot:
    workspace_id: str
    revision: int
    snapshot_hash: str
    descriptors: tuple[SkillDescriptor, ...]
    effective_descriptors: tuple[SkillDescriptor, ...]
    diagnostics: tuple[SkillDiagnostic, ...] = ()


@dataclass(frozen=True, slots=True)
class SkillReloadResult:
    applied: bool
    partial: bool
    snapshot: SkillCatalogSnapshot
    diagnostics: tuple[SkillDiagnostic, ...]


__all__ = [
    "LoadedSkill",
    "SkillAuthority",
    "SkillCatalogSnapshot",
    "SkillDescriptor",
    "SkillDiagnostic",
    "SkillDiagnosticSeverity",
    "SkillError",
    "SkillErrorCode",
    "SkillFileFact",
    "SkillLayer",
    "SkillLimits",
    "SkillReloadResult",
    "SkillRoot",
    "SkillSelection",
    "SkillSummary",
    "UntrustedSkillInstruction",
]
