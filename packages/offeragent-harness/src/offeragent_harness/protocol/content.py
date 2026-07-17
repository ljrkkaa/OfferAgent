"""Provider-neutral content blocks and provenance references."""

from __future__ import annotations

from enum import Enum
from typing import Annotated, Literal

from pydantic import AfterValidator, AnyHttpUrl, Field, StringConstraints, field_validator, model_validator
from typing_extensions import TypeAliasType

from ._base import WireModel
from .ids import ArtifactId, Sha256Digest, WorkspaceId

_WINDOWS_FORBIDDEN = frozenset('<>"|?*')
_WINDOWS_RESERVED_BASENAMES = frozenset(
    {
        "CON",
        "CONIN$",
        "CONOUT$",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{suffix}" for suffix in "123456789¹²³"),
        *(f"LPT{suffix}" for suffix in "123456789¹²³"),
    }
)


def _validate_relative_path(value: str) -> str:
    if "\\" in value or any(ord(character) < 0x20 or character in _WINDOWS_FORBIDDEN for character in value):
        raise ValueError("path must be a control-free relative POSIX path")
    if value.startswith("/") or ":" in value:
        raise ValueError("path must not be absolute, a drive path, or an ADS path")
    components = value.split("/")
    if any(component in {"", ".", ".."} for component in components):
        raise ValueError("path must not contain empty, current, or parent components")
    if any(component[-1] in {".", " "} for component in components):
        raise ValueError("path must not contain Windows-ambiguous trailing dots or spaces")
    if any(component.split(".", maxsplit=1)[0].upper() in _WINDOWS_RESERVED_BASENAMES for component in components):
        raise ValueError("path must not contain a reserved Windows device name")
    return value


RelativeVaultPath = TypeAliasType(
    "RelativeVaultPath",
    Annotated[
        str,
        StringConstraints(min_length=1, max_length=1024, strict=True),
        AfterValidator(_validate_relative_path),
    ],
)

NonEmptyText = TypeAliasType(
    "NonEmptyText",
    Annotated[str, StringConstraints(min_length=1, max_length=1_048_576, strict=True)],
)


class Freshness(str, Enum):
    FRESH = "fresh"
    STALE = "stale"
    PARTIAL = "partial"
    STALE_PARTIAL = "stale_partial"
    UNKNOWN = "unknown"


class ContentFormat(str, Enum):
    PLAIN = "plain"
    MARKDOWN = "markdown"


class ArtifactSensitivity(str, Enum):
    PUBLIC = "public"
    WORKSPACE = "workspace"
    PRIVATE = "private"
    SECRET = "secret"


class ArtifactState(str, Enum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    UNVERIFIED = "unverified"
    CANCELLED = "cancelled"
    FAILED = "failed"


class FileRef(WireModel):
    workspace_id: WorkspaceId
    path: RelativeVaultPath
    content_hash: Sha256Digest | None = None
    line_start: int | None = Field(default=None, ge=1)
    line_end: int | None = Field(default=None, ge=1)
    heading: str | None = Field(default=None, min_length=1, max_length=512)
    block_id: str | None = Field(default=None, min_length=1, max_length=256)

    @model_validator(mode="after")
    def _line_range_is_ordered(self) -> FileRef:
        if self.line_end is not None and self.line_start is None:
            raise ValueError("lineEnd requires lineStart")
        if self.line_start is not None and self.line_end is not None and self.line_end < self.line_start:
            raise ValueError("lineEnd must be greater than or equal to lineStart")
        return self


class ArtifactRef(WireModel):
    artifact_id: ArtifactId
    content_hash: Sha256Digest
    media_type: str = Field(min_length=3, max_length=255, pattern=r"^[^/\s]+/[^/\s]+$")
    size_bytes: int = Field(ge=0)
    sensitivity: ArtifactSensitivity
    state: ArtifactState = ArtifactState.COMPLETE
    title: str | None = Field(default=None, min_length=1, max_length=512)


class VaultSourceRef(WireModel):
    type: Literal["vault"]
    file: FileRef
    workspace_revision: int | None = Field(default=None, ge=0)
    freshness: Freshness = Freshness.UNKNOWN
    label: str | None = Field(default=None, min_length=1, max_length=512)


class ArtifactSourceRef(WireModel):
    type: Literal["artifact"]
    artifact: ArtifactRef
    label: str | None = Field(default=None, min_length=1, max_length=512)


class ProjectSourceRef(WireModel):
    """A precise source inside a Vault-registered external project root."""

    type: Literal["project"]
    project_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
    path: RelativeVaultPath
    content_hash: Sha256Digest
    modified_version: str = Field(min_length=1, max_length=128)
    line_start: int | None = Field(default=None, ge=1)
    line_end: int | None = Field(default=None, ge=1)
    freshness: Freshness = Freshness.UNKNOWN
    label: str | None = Field(default=None, min_length=1, max_length=512)

    @model_validator(mode="after")
    def _line_range_is_ordered(self) -> ProjectSourceRef:
        if self.line_end is not None and self.line_start is None:
            raise ValueError("lineEnd requires lineStart")
        if self.line_start is not None and self.line_end is not None and self.line_end < self.line_start:
            raise ValueError("lineEnd must be greater than or equal to lineStart")
        return self


class WebSourceRef(WireModel):
    """A clickable public-page citation captured by a bounded research tool."""

    type: Literal["web"]
    url: AnyHttpUrl
    content_hash: Sha256Digest
    title: str = Field(min_length=1, max_length=512)
    freshness: Freshness = Freshness.UNKNOWN
    label: str | None = Field(default=None, min_length=1, max_length=512)


SourceRef = TypeAliasType(
    "SourceRef",
    Annotated[
        VaultSourceRef | ArtifactSourceRef | ProjectSourceRef | WebSourceRef,
        Field(discriminator="type"),
    ],
)


class TextContentBlock(WireModel):
    type: Literal["text"]
    text: NonEmptyText
    format: ContentFormat = ContentFormat.MARKDOWN
    references: list[SourceRef] = Field(default_factory=list, max_length=256)


class FileContentBlock(WireModel):
    type: Literal["file"]
    file: FileRef
    excerpt: str | None = Field(default=None, max_length=262_144)
    truncated: bool = False


class ImageContentBlock(WireModel):
    type: Literal["image"]
    artifact: ArtifactRef
    alt_text: str | None = Field(default=None, max_length=2048)

    @model_validator(mode="after")
    def _must_be_an_image(self) -> ImageContentBlock:
        if not self.artifact.media_type.startswith("image/"):
            raise ValueError("image block artifact must use an image media type")
        return self


class ArtifactContentBlock(WireModel):
    type: Literal["artifact"]
    artifact: ArtifactRef
    preview: str | None = Field(default=None, max_length=32_768)


def _validate_pinned_evidence_path(value: str) -> str:
    lowered = value.lower()
    if lowered == "agent.md" or any(segment.startswith(".") for segment in value.split("/")):
        raise ValueError("pinned context must identify discoverable Vault evidence")
    if not lowered.endswith((".md", ".txt")):
        raise ValueError("pinned context must identify Markdown or text evidence")
    return value


class PinnedDocumentContextReference(WireModel):
    kind: Literal["document"]
    path: RelativeVaultPath

    _path_is_discoverable_evidence = field_validator("path")(_validate_pinned_evidence_path)


class PinnedSelectionContextReference(WireModel):
    kind: Literal["selection"]
    path: RelativeVaultPath
    line_start: int = Field(ge=1)
    line_end: int = Field(ge=1)

    _path_is_discoverable_evidence = field_validator("path")(_validate_pinned_evidence_path)

    @model_validator(mode="after")
    def _line_range_is_ordered(self) -> PinnedSelectionContextReference:
        if self.line_end < self.line_start:
            raise ValueError("lineEnd must be greater than or equal to lineStart")
        return self


PinnedContextReference = TypeAliasType(
    "PinnedContextReference",
    Annotated[
        PinnedDocumentContextReference | PinnedSelectionContextReference,
        Field(discriminator="kind"),
    ],
)


class PinnedContextContentBlock(WireModel):
    """Replayable user intent that remains distinct from evidence."""

    type: Literal["pinnedContext"]
    references: list[PinnedContextReference] = Field(min_length=1, max_length=8)


ContentBlock = TypeAliasType(
    "ContentBlock",
    Annotated[
        TextContentBlock | FileContentBlock | ImageContentBlock | ArtifactContentBlock | PinnedContextContentBlock,
        Field(discriminator="type"),
    ],
)


__all__ = [
    "ArtifactContentBlock",
    "ArtifactRef",
    "ArtifactSensitivity",
    "ArtifactSourceRef",
    "ArtifactState",
    "ContentBlock",
    "ContentFormat",
    "FileContentBlock",
    "FileRef",
    "Freshness",
    "ImageContentBlock",
    "NonEmptyText",
    "PinnedContextContentBlock",
    "PinnedContextReference",
    "PinnedDocumentContextReference",
    "PinnedSelectionContextReference",
    "ProjectSourceRef",
    "RelativeVaultPath",
    "SourceRef",
    "TextContentBlock",
    "VaultSourceRef",
    "WebSourceRef",
]
