"""Strongly named identifiers, hashes, versions, and timestamps."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Annotated

from pydantic import AfterValidator, StringConstraints
from typing_extensions import TypeAliasType


def _id_constraints(prefix: str) -> StringConstraints:
    return StringConstraints(
        min_length=len(prefix) + 1,
        max_length=128,
        pattern=r"^" + re.escape(prefix) + r"[A-Za-z0-9][A-Za-z0-9_-]*$",
        strict=True,
    )


# Keep each alias as a direct TypeAliasType declaration.  Besides giving Pydantic
# stable definition names, this form is understood as a true type alias by mypy.
WorkspaceId = TypeAliasType("WorkspaceId", Annotated[str, _id_constraints("ws_")])
WorkspaceInstanceId = TypeAliasType("WorkspaceInstanceId", Annotated[str, _id_constraints("wsi_")])
ProfileId = TypeAliasType("ProfileId", Annotated[str, _id_constraints("profile_")])
SessionId = TypeAliasType("SessionId", Annotated[str, _id_constraints("ses_")])
TurnId = TypeAliasType("TurnId", Annotated[str, _id_constraints("turn_")])
RunId = TypeAliasType("RunId", Annotated[str, _id_constraints("run_")])
EventId = TypeAliasType("EventId", Annotated[str, _id_constraints("evt_")])
RequestId = TypeAliasType("RequestId", Annotated[str, _id_constraints("req_")])
ToolCallId = TypeAliasType("ToolCallId", Annotated[str, _id_constraints("call_")])
InvocationId = TypeAliasType("InvocationId", Annotated[str, _id_constraints("inv_")])
ApprovalId = TypeAliasType("ApprovalId", Annotated[str, _id_constraints("apr_")])
ArtifactId = TypeAliasType("ArtifactId", Annotated[str, _id_constraints("art_")])
UploadId = TypeAliasType("UploadId", Annotated[str, _id_constraints("upload_")])
TraceId = TypeAliasType("TraceId", Annotated[str, _id_constraints("trace_")])
TransactionId = TypeAliasType("TransactionId", Annotated[str, _id_constraints("tx_")])
DocumentId = TypeAliasType("DocumentId", Annotated[str, _id_constraints("doc_")])
MessageId = TypeAliasType("MessageId", Annotated[str, _id_constraints("msg_")])
CompactBoundaryId = TypeAliasType("CompactBoundaryId", Annotated[str, _id_constraints("cmp_")])


Sha256Digest = TypeAliasType(
    "Sha256Digest",
    Annotated[
        str,
        StringConstraints(
            min_length=71,
            max_length=71,
            pattern=r"^sha256:[0-9a-f]{64}$",
            strict=True,
        ),
    ],
)


SemanticVersion = TypeAliasType(
    "SemanticVersion",
    Annotated[
        str,
        StringConstraints(
            min_length=5,
            max_length=64,
            pattern=(
                r"^(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
                r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
                r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
            ),
            strict=True,
        ),
    ],
)


ProtocolVersion = TypeAliasType(
    "ProtocolVersion",
    Annotated[
        str,
        StringConstraints(
            min_length=3,
            max_length=15,
            pattern=r"^(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)$",
            strict=True,
        ),
    ],
)


SchemaVersion = TypeAliasType(
    "SchemaVersion",
    Annotated[
        str,
        StringConstraints(
            min_length=1,
            max_length=16,
            pattern=r"^(?:0|[1-9][0-9]*)$",
            strict=True,
        ),
    ],
)


_RFC3339_RE = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T"
    r"[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{1,9})?(?:Z|[+-][0-9]{2}:[0-9]{2})$"
)


def _validate_rfc3339(value: str) -> str:
    if not _RFC3339_RE.fullmatch(value):
        raise ValueError("timestamp must be an RFC 3339 date-time with an explicit offset")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    except ValueError as error:
        raise ValueError("timestamp is not a valid RFC 3339 date-time") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp must include a UTC offset")
    # RFC 3339 limits numeric offsets to actual clock offsets.  fromisoformat
    # already rejects invalid hours/minutes, so no lossy normalization is done.
    return value


Rfc3339DateTime = TypeAliasType(
    "Rfc3339DateTime",
    Annotated[
        str,
        StringConstraints(
            min_length=20,
            max_length=40,
            pattern=_RFC3339_RE.pattern,
            strict=True,
        ),
        AfterValidator(_validate_rfc3339),
    ],
)


def split_protocol_version(version: str) -> tuple[int, int]:
    """Parse a previously validated protocol version into comparable parts."""

    major, minor = version.split(".", 1)
    return int(major), int(minor)


__all__ = [
    "ApprovalId",
    "ArtifactId",
    "CompactBoundaryId",
    "DocumentId",
    "EventId",
    "InvocationId",
    "MessageId",
    "ProfileId",
    "ProtocolVersion",
    "RequestId",
    "Rfc3339DateTime",
    "RunId",
    "SchemaVersion",
    "SemanticVersion",
    "SessionId",
    "Sha256Digest",
    "ToolCallId",
    "TraceId",
    "TransactionId",
    "TurnId",
    "UploadId",
    "WorkspaceId",
    "WorkspaceInstanceId",
    "split_protocol_version",
]
