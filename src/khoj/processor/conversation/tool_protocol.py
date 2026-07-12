import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from khoj.processor.conversation.utils import ToolCall
from khoj.utils.helpers import ToolDefinition


class PlannedToolCall(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    name: str = Field(min_length=1)
    args: dict[str, Any]
    id: str = Field(min_length=1)


class ToolPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    calls: list[PlannedToolCall]


def parse_tool_plan(text: str) -> list[ToolCall]:
    try:
        plan = ToolPlan.model_validate(json.loads(text))
    except (json.JSONDecodeError, TypeError, ValueError, ValidationError) as error:
        raise ValueError(f"invalid tool plan: {error}") from None
    return [ToolCall(name=call.name, args=call.args, id=call.id) for call in plan.calls]


def validate_tool_arguments(tool: ToolDefinition, args: dict[str, Any]) -> None:
    schema = tool.schema or {}
    properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
    required = schema.get("required") if isinstance(schema.get("required"), list) else []

    unknown = sorted(set(args) - set(properties))
    if unknown:
        raise ValueError(f"unknown arguments for {tool.name}: {', '.join(unknown)}")

    for name in required:
        if name not in args:
            raise ValueError(f"required argument missing for {tool.name}: {name}")

    for name, value in args.items():
        field_schema = properties.get(name)
        if not isinstance(field_schema, dict):
            continue
        _validate_value(value, field_schema, f"argument {name} for {tool.name}")


def _validate_value(value: Any, schema: dict[str, Any], path: str) -> None:
    variants = schema.get("oneOf")
    if isinstance(variants, list):
        matches = 0
        for variant in variants:
            if not isinstance(variant, dict):
                continue
            try:
                _validate_value(value, variant, path)
            except ValueError:
                continue
            matches += 1
        if matches != 1:
            raise ValueError(f"{path} must match exactly one allowed shape")
        return

    expected = schema.get("type")
    if expected and not _matches_json_type(value, expected):
        raise ValueError(f"{path} must be {expected}")
    if "const" in schema and value != schema["const"]:
        raise ValueError(f"{path} must equal {schema['const']!r}")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(f"{path} must be one of {schema['enum']}")

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        minimum = schema.get("minimum")
        maximum = schema.get("maximum")
        if isinstance(minimum, (int, float)) and value < minimum:
            raise ValueError(f"{path} must be at least {minimum}")
        if isinstance(maximum, (int, float)) and value > maximum:
            raise ValueError(f"{path} must be at most {maximum}")
        return

    if isinstance(value, str):
        minimum = schema.get("minLength")
        maximum = schema.get("maxLength")
        if isinstance(minimum, int) and len(value) < minimum:
            raise ValueError(f"{path} must contain at least {minimum} characters")
        if isinstance(maximum, int) and len(value) > maximum:
            raise ValueError(f"{path} must contain at most {maximum} characters")
        return

    if isinstance(value, list):
        minimum = schema.get("minItems")
        maximum = schema.get("maxItems")
        if isinstance(minimum, int) and len(value) < minimum:
            raise ValueError(f"{path} must contain at least {minimum} items")
        if isinstance(maximum, int) and len(value) > maximum:
            raise ValueError(f"{path} must contain at most {maximum} items")
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(value):
                _validate_value(item, item_schema, f"{path}[{index}]")
        return

    if isinstance(value, dict):
        properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
        required = schema.get("required") if isinstance(schema.get("required"), list) else []
        if schema.get("additionalProperties") is False:
            unknown = sorted(set(value) - set(properties))
            if unknown:
                raise ValueError(f"unknown fields in {path}: {', '.join(unknown)}")
        for name in required:
            if name not in value:
                raise ValueError(f"required field missing in {path}: {name}")
        for name, item in value.items():
            field_schema = properties.get(name)
            if isinstance(field_schema, dict):
                _validate_value(item, field_schema, f"{path}.{name}")


def _matches_json_type(value: Any, expected: str | list[str]) -> bool:
    if isinstance(expected, list):
        return any(_matches_json_type(value, item) for item in expected)
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "array":
        return isinstance(value, list)
    if expected == "object":
        return isinstance(value, dict)
    if expected == "null":
        return value is None
    return False
