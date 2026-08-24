"""Complete Draft 2020-12 validation for tool inputs and outputs."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import ValidationError

from offeragent_harness.models.json_types import FrozenJsonObject, FrozenJsonValue, freeze_json, thaw_json

from .canonical import CanonicalJsonError, canonical_json_bytes, canonical_json_sha256
from .definitions import ToolDefinition


def _json_pointer(parts: Iterable[Any]) -> str:
    encoded = [str(part).replace("~", "~0").replace("/", "~1") for part in parts]
    return "" if not encoded else "/" + "/".join(encoded)


@dataclass(frozen=True, order=True)
class ValidationIssue:
    instance_path: str
    schema_path: str
    keyword: str
    message: str


class ToolValidationError(ValueError):
    def __init__(self, target: str, issues: tuple[ValidationIssue, ...]) -> None:
        self.target = target
        self.issues = issues
        summary = "; ".join(f"{issue.instance_path or '/'}: {issue.message}" for issue in issues)
        super().__init__(f"{target} failed Draft 2020-12 validation: {summary}")


@dataclass(frozen=True)
class ValidatedArguments:
    arguments: FrozenJsonObject
    canonical_bytes: bytes
    args_hash: str


def _flatten(error: ValidationError) -> Iterable[ValidationError]:
    yield error
    for child in error.context:
        yield from _flatten(child)


def _issues(errors: Iterable[ValidationError]) -> tuple[ValidationIssue, ...]:
    unique: set[ValidationIssue] = set()
    for root in errors:
        for error in _flatten(root):
            unique.add(
                ValidationIssue(
                    instance_path=_json_pointer(error.absolute_path),
                    schema_path=_json_pointer(error.absolute_schema_path),
                    keyword=str(error.validator or "schema"),
                    message=error.message,
                )
            )
    return tuple(sorted(unique))


class ToolValidator:
    """Uses jsonschema's full Draft 2020-12 validator, including references."""

    def __init__(self, *, enforce_formats: bool = True) -> None:
        self._format_checker = FormatChecker() if enforce_formats else None

    def validate_arguments(self, definition: ToolDefinition, arguments: Mapping[str, Any]) -> ValidatedArguments:
        candidate = thaw_json(freeze_json(arguments))
        validator = Draft202012Validator(thaw_json(definition.input_schema), format_checker=self._format_checker)
        issues = _issues(validator.iter_errors(candidate))
        if issues:
            raise ToolValidationError("tool arguments", issues)
        frozen = freeze_json(candidate)
        if not isinstance(frozen, FrozenJsonObject):
            raise ToolValidationError(
                "tool arguments",
                (ValidationIssue("", "", "type", "arguments must be an object"),),
            )
        try:
            encoded = canonical_json_bytes(frozen)
        except CanonicalJsonError as error:
            raise ToolValidationError(
                "tool arguments",
                (ValidationIssue("", "", "canonicalJson", str(error)),),
            ) from error
        return ValidatedArguments(frozen, encoded, canonical_json_sha256(frozen))

    def validate_output(self, definition: ToolDefinition, output: Any) -> FrozenJsonValue:
        candidate = thaw_json(freeze_json(output))
        validator = Draft202012Validator(thaw_json(definition.output_schema), format_checker=self._format_checker)
        issues = _issues(validator.iter_errors(candidate))
        if issues:
            raise ToolValidationError("tool output", issues)
        try:
            encoded = canonical_json_bytes(candidate)
        except CanonicalJsonError as error:
            raise ToolValidationError(
                "tool output",
                (ValidationIssue("", "", "canonicalJson", str(error)),),
            ) from error
        if len(encoded) > definition.output_limit_bytes:
            raise ToolValidationError(
                "tool output",
                (
                    ValidationIssue(
                        "",
                        "",
                        "maxBytes",
                        f"canonical output is {len(encoded)} bytes; limit is {definition.output_limit_bytes}",
                    ),
                ),
            )
        return freeze_json(candidate)


__all__ = ["ToolValidationError", "ToolValidator", "ValidatedArguments", "ValidationIssue"]
