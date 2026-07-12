"""Provider-neutral content blocks and provenance references."""

from __future__ import annotations

from enum import Enum
from typing import Annotated, Literal

from pydantic import AfterValidator, AnyHttpUrl, Field, StringConstraints, model_validator
from typing_extensions import TypeAliasType

from ._base import WireModel
from .ids import ArtifactId, Rfc3339DateTime, Sha256Digest, WorkspaceId


def _validate_relative_path(value: str) -> str:
    if "\\" in value or any(ord(character) < 0x20 for character in value):
        raise ValueError("path must be a control-free relative POSIX path")
    if value.startswith("/") or ":" in value:
        raise ValueError("path must not be absolute, a drive path, or an ADS path")
    components = value.split("/")
    if any(component in {"", ".", ".."} for component in components):
        raise ValueError("path must not contain empty, current, or parent components")
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


class WebSourceRef(WireModel):
    type: Literal["web"]
    url: AnyHttpUrl
    title: str | None = Field(default=None, min_length=1, max_length=512)
    content_hash: Sha256Digest | None = None
    retrieved_at: Rfc3339DateTime | None = None


class ArtifactSourceRef(WireModel):
    type: Literal["artifact"]
    artifact: ArtifactRef
    label: str | None = Field(default=None, min_length=1, max_length=512)


class MemorySourceRef(WireModel):
    type: Literal["memory"]
    memory_id: str = Field(min_length=1, max_length=128, pattern=r"^mem_[A-Za-z0-9][A-Za-z0-9_-]*$")
    scope: Literal["session", "workspace", "profile"]
    label: str | None = Field(default=None, min_length=1, max_length=512)


class McpSourceRef(WireModel):
    type: Literal["mcp"]
    server_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    resource_uri: str = Field(min_length=1, max_length=4096)
    content_hash: Sha256Digest | None = None
    label: str | None = Field(default=None, min_length=1, max_length=512)


SourceRef = TypeAliasType(
    "SourceRef",
    Annotated[
        VaultSourceRef | WebSourceRef | ArtifactSourceRef | MemorySourceRef | McpSourceRef,
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


ContentBlock = TypeAliasType(
    "ContentBlock",
    Annotated[
        TextContentBlock | FileContentBlock | ImageContentBlock | ArtifactContentBlock,
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
    "McpSourceRef",
    "MemorySourceRef",
    "NonEmptyText",
    "RelativeVaultPath",
    "SourceRef",
    "TextContentBlock",
    "VaultSourceRef",
    "WebSourceRef",
]
