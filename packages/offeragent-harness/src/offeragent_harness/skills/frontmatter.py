"""Bounded parser for the public Claude Code ``SKILL.md`` metadata surface."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

from .models import SkillError, SkillErrorCode, SkillLimits

_TOP_LEVEL = re.compile(r"^([a-z][a-z0-9-]*)[ \t]*:(?:[ \t]*(.*))?$")
_FIELDS = frozenset({"name", "description", "allowed-tools"})


@dataclass(frozen=True, slots=True)
class SkillMetadata:
    name: str
    description: str
    allowed_tools: frozenset[str]
    content_hash: str


@dataclass(frozen=True, slots=True)
class ParsedSkillDocument:
    metadata: SkillMetadata
    body: str
    body_bytes: bytes
    metadata_bytes: int


def parse_skill_header(content: bytes, limits: SkillLimits) -> tuple[SkillMetadata, int]:
    header, _, metadata_bytes = _split_document(content, require_complete_body=False, limits=limits)
    return _parse_header(header, limits), metadata_bytes


def parse_skill_document(content: bytes, limits: SkillLimits) -> ParsedSkillDocument:
    if len(content) > limits.max_skill_bytes:
        raise SkillError(SkillErrorCode.LIMIT_EXCEEDED, "SKILL.md exceeds the configured byte limit")
    header, body_bytes, metadata_bytes = _split_document(content, require_complete_body=True, limits=limits)
    metadata = _parse_header(header, limits)
    try:
        body = body_bytes.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise SkillError(SkillErrorCode.ENCODING, "SKILL.md body is not strict UTF-8") from error
    if len(body) > limits.max_body_chars:
        raise SkillError(SkillErrorCode.LIMIT_EXCEEDED, "SKILL.md body exceeds the character limit")
    return ParsedSkillDocument(metadata, body, body_bytes, metadata_bytes)


def _split_document(
    content: bytes,
    *,
    require_complete_body: bool,
    limits: SkillLimits,
) -> tuple[bytes, bytes, int]:
    if content.startswith(b"\xef\xbb\xbf"):
        raise SkillError(SkillErrorCode.ENCODING, "SKILL.md must not contain a UTF-8 BOM")
    if not content.startswith(b"---\n") and not content.startswith(b"---\r\n"):
        raise SkillError(SkillErrorCode.INVALID_FRONTMATTER, "SKILL.md must begin with an exact --- line")
    cursor = 4 if content.startswith(b"---\n") else 5
    start = cursor
    while cursor <= min(len(content), start + limits.max_metadata_bytes + 6):
        line_end = content.find(b"\n", cursor)
        if line_end < 0:
            line_end = len(content)
            next_cursor = len(content)
        else:
            next_cursor = line_end + 1
        if content[cursor:line_end].rstrip(b"\r") == b"---":
            if cursor - start > limits.max_metadata_bytes:
                raise SkillError(SkillErrorCode.LIMIT_EXCEEDED, "SKILL.md frontmatter exceeds the metadata limit")
            return content[start:cursor], content[next_cursor:] if require_complete_body else b"", next_cursor
        if next_cursor == len(content):
            break
        cursor = next_cursor
    if len(content) - start > limits.max_metadata_bytes:
        raise SkillError(SkillErrorCode.LIMIT_EXCEEDED, "SKILL.md frontmatter exceeds the metadata limit")
    raise SkillError(SkillErrorCode.INVALID_FRONTMATTER, "SKILL.md frontmatter is not closed")


def _parse_header(header: bytes, limits: SkillLimits) -> SkillMetadata:
    try:
        text = header.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise SkillError(SkillErrorCode.ENCODING, "SKILL.md frontmatter is not strict UTF-8") from error
    values: dict[str, Any] = {}
    for number, line in enumerate(text.splitlines(), start=2):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line[:1].isspace():
            raise SkillError(
                SkillErrorCode.INVALID_FRONTMATTER, f"frontmatter line {number} uses unsupported indentation"
            )
        match = _TOP_LEVEL.fullmatch(line)
        if match is None:
            raise SkillError(SkillErrorCode.INVALID_FRONTMATTER, f"frontmatter line {number} is invalid")
        key = match.group(1)
        if key in values:
            raise SkillError(SkillErrorCode.DUPLICATE_KEY, f"duplicate frontmatter key: {key}")
        if key not in _FIELDS:
            raise SkillError(SkillErrorCode.UNKNOWN_FIELD, f"unsupported SKILL.md frontmatter field: {key}")
        raw = match.group(2) or ""
        if not raw:
            raise SkillError(SkillErrorCode.INVALID_FRONTMATTER, f"frontmatter line {number} has no value")
        values[key] = _value(raw, number, limits)
    if set(values) - {"allowed-tools"} != {"name", "description"}:
        raise SkillError(SkillErrorCode.INVALID_FRONTMATTER, "SKILL.md requires name and description")
    name = _string(values["name"], "name")
    description = _string(values["description"], "description")
    if len(description) > limits.max_description_chars:
        raise SkillError(SkillErrorCode.LIMIT_EXCEEDED, "Skill description exceeds the configured limit")
    tools = _strings(values.get("allowed-tools", []), "allowed-tools")
    if len(tools) > limits.max_allowed_tools:
        raise SkillError(SkillErrorCode.LIMIT_EXCEEDED, "Skill allowed-tools exceeds the configured limit")
    allowed_tools = frozenset(tools)
    digest = hashlib.sha256(
        json.dumps(
            {"name": name, "description": description, "allowed-tools": sorted(allowed_tools)},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return SkillMetadata(name, description, allowed_tools, f"sha256:{digest}")


def _value(raw: str, line: int, limits: SkillLimits) -> Any:
    if raw[:1] in {'"', "[", "{"}:
        try:
            value = json.loads(raw, object_pairs_hook=_unique_object)
        except (json.JSONDecodeError, SkillError) as error:
            if isinstance(error, SkillError):
                raise
            raise SkillError(SkillErrorCode.INVALID_FRONTMATTER, f"frontmatter line {line} has invalid JSON") from error
        _json_depth(value, limits.max_json_depth)
        return value
    return raw


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise SkillError(SkillErrorCode.DUPLICATE_KEY, f"duplicate nested key: {key}")
        result[key] = value
    return result


def _json_depth(value: Any, maximum: int) -> None:
    pending = [(value, 1)]
    while pending:
        current, depth = pending.pop()
        if depth > maximum:
            raise SkillError(SkillErrorCode.LIMIT_EXCEEDED, "frontmatter JSON nesting exceeds the limit")
        if isinstance(current, dict):
            pending.extend((child, depth + 1) for child in current.values())
        elif isinstance(current, list):
            pending.extend((child, depth + 1) for child in current)


def _string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise SkillError(SkillErrorCode.INVALID_FRONTMATTER, f"{field} must be a non-empty string")
    return value


def _strings(value: Any, field: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item or "\x00" in item for item in value):
        raise SkillError(SkillErrorCode.INVALID_FRONTMATTER, f"{field} must be an array of non-empty strings")
    if len(value) != len(set(value)):
        raise SkillError(SkillErrorCode.DUPLICATE_KEY, f"{field} cannot contain duplicates")
    return value


__all__ = ["ParsedSkillDocument", "SkillMetadata", "parse_skill_document", "parse_skill_header"]
