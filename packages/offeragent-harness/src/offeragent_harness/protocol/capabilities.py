"""Protocol version and capability negotiation.

Negotiation is deterministic and fail-closed.  Major versions are never bridged,
required capabilities must be present on both sides, and an explicitly supplied
schema hash must match exactly.
"""

from __future__ import annotations

from enum import Enum

from pydantic import Field, model_validator

from ._base import WireModel
from .errors import ErrorCode, protocol_error
from .ids import ProtocolVersion, Sha256Digest, split_protocol_version


class CapabilityName(str, Enum):
    EVENT_REPLAY = "eventReplay"
    MULTI_SESSION = "multiSession"
    APPROVALS = "approvals"
    SKILLS = "skills"
    SHELL = "shell"
    HOOKS = "hooks"
    SUBAGENTS = "subagents"
    ARTIFACTS = "artifacts"
    CONTENT_BLOCKS = "contentBlocks"
    CANCELLATION = "cancellation"
    DIAGNOSTICS = "diagnostics"


class CapabilitySet(WireModel):
    event_replay: bool = False
    multi_session: bool = False
    approvals: bool = False
    skills: bool = False
    shell: bool = False
    hooks: bool = False
    subagents: bool = False
    artifacts: bool = False
    content_blocks: bool = False
    cancellation: bool = False
    diagnostics: bool = False

    def enabled(self) -> set[CapabilityName]:
        wire = self.to_wire()
        return {name for name in CapabilityName if wire[name.value] is True}

    @classmethod
    def from_enabled(cls, enabled: set[CapabilityName]) -> CapabilitySet:
        return cls.model_validate({name.value: name in enabled for name in CapabilityName})

    def intersection(self, other: CapabilitySet) -> CapabilitySet:
        return self.from_enabled(self.enabled() & other.enabled())


class ProtocolRange(WireModel):
    minimum: ProtocolVersion
    maximum: ProtocolVersion

    @model_validator(mode="after")
    def _ordered_single_major(self) -> ProtocolRange:
        minimum = split_protocol_version(self.minimum)
        maximum = split_protocol_version(self.maximum)
        if minimum[0] != maximum[0]:
            raise ValueError("a protocol range cannot cross a major version")
        if minimum > maximum:
            raise ValueError("minimum protocol version must not exceed maximum")
        return self


class NegotiatedProtocol(WireModel):
    protocol_version: ProtocolVersion
    schema_hash: Sha256Digest
    capabilities: CapabilitySet
    disabled_optional_capabilities: list[CapabilityName] = Field(default_factory=list)


def negotiate_protocol(
    *,
    client_preferred: ProtocolVersion,
    client_range: ProtocolRange | None,
    client_capabilities: CapabilitySet,
    client_required_capabilities: list[CapabilityName],
    client_schema_hash: Sha256Digest | None,
    server_preferred: ProtocolVersion,
    server_range: ProtocolRange,
    server_capabilities: CapabilitySet,
    server_schema_hash: Sha256Digest,
) -> NegotiatedProtocol:
    """Select the highest common minor version and the capability intersection."""

    client_supported = client_range or ProtocolRange(minimum=client_preferred, maximum=client_preferred)
    client_pref_parts = split_protocol_version(client_preferred)
    server_pref_parts = split_protocol_version(server_preferred)
    client_min = split_protocol_version(client_supported.minimum)
    client_max = split_protocol_version(client_supported.maximum)
    server_min = split_protocol_version(server_range.minimum)
    server_max = split_protocol_version(server_range.maximum)

    if not (client_min <= client_pref_parts <= client_max):
        raise protocol_error(
            ErrorCode.PROTOCOL_INCOMPATIBLE_VERSION,
            "客户端首选协议版本不在其声明的支持范围内。",
            details={"clientPreferred": client_preferred},
        )
    if not (server_min <= server_pref_parts <= server_max):
        raise protocol_error(
            ErrorCode.PROTOCOL_INTERNAL_ERROR,
            "Runtime 协议版本配置无效。",
            details={"serverPreferred": server_preferred},
        )
    majors = {client_min[0], server_min[0]}
    if len(majors) != 1:
        raise protocol_error(
            ErrorCode.PROTOCOL_INCOMPATIBLE_VERSION,
            "客户端与 Runtime 的协议大版本不兼容。",
            details={
                "clientRange": client_supported.to_wire(),
                "serverRange": server_range.to_wire(),
            },
        )

    minimum_minor = max(client_min[1], server_min[1])
    maximum_minor = min(client_max[1], server_max[1], client_pref_parts[1], server_pref_parts[1])
    if minimum_minor > maximum_minor:
        raise protocol_error(
            ErrorCode.PROTOCOL_INCOMPATIBLE_VERSION,
            "客户端与 Runtime 没有共同支持的协议版本。",
            details={
                "clientRange": client_supported.to_wire(),
                "serverRange": server_range.to_wire(),
            },
        )

    if client_schema_hash is not None and client_schema_hash != server_schema_hash:
        raise protocol_error(
            ErrorCode.PROTOCOL_SCHEMA_MISMATCH,
            "客户端协议 Schema 与 Runtime 不一致, 请升级或回滚到兼容版本。",
            details={"clientSchemaHash": client_schema_hash, "serverSchemaHash": server_schema_hash},
        )

    if len(set(client_required_capabilities)) != len(client_required_capabilities):
        raise protocol_error(
            ErrorCode.PROTOCOL_INVALID_PARAMS,
            "requiredCapabilities 不能包含重复项。",
        )
    negotiated = client_capabilities.intersection(server_capabilities)
    missing = sorted(
        (capability for capability in client_required_capabilities if capability not in negotiated.enabled()),
        key=lambda item: item.value,
    )
    if missing:
        raise protocol_error(
            ErrorCode.PROTOCOL_MISSING_CAPABILITY,
            "Runtime 缺少客户端要求的能力。",
            details={"missingCapabilities": [item.value for item in missing]},
        )

    disabled = sorted(client_capabilities.enabled() - negotiated.enabled(), key=lambda item: item.value)
    return NegotiatedProtocol(
        protocol_version=f"{client_min[0]}.{maximum_minor}",
        schema_hash=server_schema_hash,
        capabilities=negotiated,
        disabled_optional_capabilities=disabled,
    )


__all__ = [
    "CapabilityName",
    "CapabilitySet",
    "NegotiatedProtocol",
    "ProtocolRange",
    "negotiate_protocol",
]
