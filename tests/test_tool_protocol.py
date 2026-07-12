import pytest

from khoj.processor.conversation.knowledge_workspace import OPENKB_TOOL, PROPOSE_EDIT_TOOL
from khoj.processor.conversation.tool_protocol import parse_tool_plan, validate_tool_arguments
from khoj.utils.helpers import AgentToolName, ToolDefinition, agent_tool_definitions


def test_parse_tool_plan_requires_write_intent_and_canonical_calls():
    requires_write_action, calls = parse_tool_plan(
        '{"requires_write_action":true,"calls":[{"name":"view_file","args":{"path":"notes.md"},"id":"1"}]}'
    )

    assert requires_write_action is True
    assert [(call.name, call.args, call.id) for call in calls] == [("view_file", {"path": "notes.md"}, "1")]


@pytest.mark.parametrize(
    "payload",
    [
        "plain text",
        "{'calls':[]}",
        '{"calls":[]}',
        '{"calls":[]} trailing text',
        '```json\n{"calls":[]}\n```',
        '{"tools":[]}',
        '{"calls":[{"tool":"view_file","args":{"path":"notes.md"},"id":"1"}]}',
        '{"calls":[{"name":"view_file","arguments":{"path":"notes.md"},"id":"1"}]}',
        '{"calls":[{"name":"view_file","args":"{\\"path\\":\\"notes.md\\"}","id":"1"}]}',
        '{"calls":[{"name":"view_file","args":{"path":"notes.md"}}]}',
    ],
)
def test_parse_tool_plan_rejects_compatibility_shapes(payload):
    with pytest.raises(ValueError, match="tool plan"):
        parse_tool_plan(payload)


def test_validate_tool_arguments_enforces_required_types_and_unknown_fields():
    tool = ToolDefinition(
        name="view_file",
        description="Read a file",
        schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "minLength": 1},
                "start_line": {"type": "integer"},
            },
            "required": ["path"],
        },
    )

    validate_tool_arguments(tool, {"path": "notes.md", "start_line": 2})
    with pytest.raises(ValueError, match="required.*path"):
        validate_tool_arguments(tool, {})
    with pytest.raises(ValueError, match="path.*at least 1"):
        validate_tool_arguments(tool, {"path": ""})
    with pytest.raises(ValueError, match="start_line.*integer"):
        validate_tool_arguments(tool, {"path": "notes.md", "start_line": "2"})
    with pytest.raises(ValueError, match="unknown"):
        validate_tool_arguments(tool, {"path": "notes.md", "guess": True})


def test_validate_tool_arguments_recursively_validates_discriminated_arrays():
    tool = ToolDefinition(
        name="append_note",
        description="Append grounded content",
        schema={
            "type": "object",
            "properties": {
                "source_refs": {
                    "type": "array",
                    "minItems": 1,
                    "items": {
                        "oneOf": [
                            {
                                "type": "object",
                                "properties": {"type": {"const": "current_user_request"}},
                                "required": ["type"],
                                "additionalProperties": False,
                            },
                            {
                                "type": "object",
                                "properties": {
                                    "type": {"const": "file"},
                                    "path": {"type": "string", "minLength": 1},
                                },
                                "required": ["type", "path"],
                                "additionalProperties": False,
                            },
                        ]
                    },
                }
            },
            "required": ["source_refs"],
        },
    )

    validate_tool_arguments(tool, {"source_refs": [{"type": "file", "path": "notes.md"}]})
    with pytest.raises(ValueError, match="at least 1 items"):
        validate_tool_arguments(tool, {"source_refs": []})
    with pytest.raises(ValueError, match="exactly one allowed shape"):
        validate_tool_arguments(tool, {"source_refs": [{"type": "file"}]})
    with pytest.raises(ValueError, match="exactly one allowed shape"):
        validate_tool_arguments(tool, {"source_refs": [{"type": "file", "path": "notes.md", "guess": True}]})


def test_validate_tool_arguments_enforces_numeric_and_size_bounds():
    tool = ToolDefinition(
        name="regex_search_files",
        description="Search files",
        schema={
            "type": "object",
            "properties": {
                "lines_before": {"type": "integer", "minimum": 0, "maximum": 20},
                "query": {"type": "string", "minLength": 1, "maxLength": 5},
                "paths": {"type": "array", "minItems": 1, "maxItems": 2, "items": {"type": "string"}},
            },
            "required": ["lines_before", "query", "paths"],
        },
    )

    validate_tool_arguments(tool, {"lines_before": 20, "query": "notes", "paths": ["a", "b"]})
    with pytest.raises(ValueError, match="at least 0"):
        validate_tool_arguments(tool, {"lines_before": -1, "query": "notes", "paths": ["a"]})
    with pytest.raises(ValueError, match="at most 20"):
        validate_tool_arguments(tool, {"lines_before": 21, "query": "notes", "paths": ["a"]})
    with pytest.raises(ValueError, match="at most 5 characters"):
        validate_tool_arguments(tool, {"lines_before": 1, "query": "longer", "paths": ["a"]})
    with pytest.raises(ValueError, match="at most 2 items"):
        validate_tool_arguments(tool, {"lines_before": 1, "query": "notes", "paths": ["a", "b", "c"]})


def test_real_workspace_schemas_allow_empty_replacement_and_enforce_limits():
    validate_tool_arguments(PROPOSE_EDIT_TOOL, {"path": "notes.md", "find": "obsolete", "replace": ""})
    with pytest.raises(ValueError, match="find.*at least 1"):
        validate_tool_arguments(PROPOSE_EDIT_TOOL, {"path": "notes.md", "find": "", "replace": ""})

    validate_tool_arguments(OPENKB_TOOL, {"query": "Redis", "n": 10})
    with pytest.raises(ValueError, match="n.*at least 1"):
        validate_tool_arguments(OPENKB_TOOL, {"query": "Redis", "n": 0})
    with pytest.raises(ValueError, match="n.*at most 10"):
        validate_tool_arguments(OPENKB_TOOL, {"query": "Redis", "n": 11})

    view_file = agent_tool_definitions[AgentToolName.ViewFile]
    with pytest.raises(ValueError, match="start_line.*at least 1"):
        validate_tool_arguments(view_file, {"path": "notes.md", "start_line": 0})
