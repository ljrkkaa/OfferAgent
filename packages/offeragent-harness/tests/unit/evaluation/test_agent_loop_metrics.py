from __future__ import annotations

from typing import Any

from offeragent_harness.evaluation import (
    AgentEvaluationCase,
    GoldEvidence,
    aggregate_run_results,
    evaluate_run_trace,
)


def _event(event_type: str, payload: dict[str, Any], *, terminal: bool = False) -> dict[str, Any]:
    return {"eventType": event_type, "payload": payload, "terminal": terminal}


def _successful_tool(tool_call_id: str, *, path: str | None = None) -> dict[str, Any]:
    data: dict[str, Any] = {} if path is None else {"path": path}
    return _event(
        "tool.completed",
        {
            "result": {
                "toolCallId": tool_call_id,
                "status": "succeeded",
                "summary": "ok",
                "data": data,
            }
        },
    )


def test_scores_actual_skill_tools_evidence_and_generated_citation() -> None:
    evidence_path = ".offeragent/knowledge/objects/source-1/" + "a" * 64 + "/evidence/pages/0001.md"
    calls = [
        {
            "toolCallId": "call-skill",
            "name": "skill",
            "arguments": {"name": "knowledge-retrieval"},
            "argsHash": "skill-hash",
        },
        {
            "toolCallId": "call-grep",
            "name": "grep",
            "arguments": {"path": "knowledge", "pattern": "Transformer"},
            "argsHash": "grep-hash",
        },
        {
            "toolCallId": "call-read",
            "name": "read",
            "arguments": {"path": evidence_path},
            "argsHash": "read-hash",
        },
    ]
    events = [
        _event(
            "model.attempt",
            {
                "repairIndex": 0,
                "outcome": "succeeded",
                "usage": {"inputTokens": 10, "outputTokens": 3},
            },
        ),
        _event("tool.calls.accepted", {"calls": [calls[0]]}),
        _successful_tool("call-skill"),
        _event("tool.calls.accepted", {"calls": [calls[1]]}),
        _successful_tool("call-grep"),
        _event("tool.calls.accepted", {"calls": [calls[2]]}),
        _successful_tool("call-read", path=evidence_path),
        _event("turn.completed", {"reason": "completed"}, terminal=True),
    ]
    case = AgentEvaluationCase(
        "case-1",
        "What is the title?",
        "alias_to_title",
        "Attention Is All You Need",
        (GoldEvidence("raw/paper.pdf", (1,)),),
        True,
    )

    result = evaluate_run_trace(
        case,
        events=events,
        assistant_text="Attention Is All You Need [raw/paper.pdf, PageIndex: title, page 1]",
        elapsed_seconds=1.25,
        source_path_by_id={"source-1": "raw/paper.pdf"},
        run_index=1,
    )

    assert result["routing"]["toolSelectionPass"] is True
    assert result["routing"]["skillFirst"] is True
    assert result["evidence"]["pageRecall"] == 1.0
    assert result["answer"]["answerCorrect"] is True
    assert result["answer"]["citationPagePrecision"] == 1.0
    assert result["answer"]["citationEvidenceGroupCoverage"] == 1.0
    assert result["answer"]["groundedAnswerCorrect"] is True


def test_control_requires_no_tools_and_unanswerable_requires_explicit_abstention() -> None:
    terminal = [_event("turn.completed", {"reason": "completed"}, terminal=True)]
    control = AgentEvaluationCase("control", "17 + 25", "routing_control", "42", (), False)
    control_result = evaluate_run_trace(
        control,
        events=terminal,
        assistant_text="42",
        elapsed_seconds=0.1,
        source_path_by_id={},
        run_index=1,
    )
    unanswerable = AgentEvaluationCase("unknown", "What is zqxv?", "unanswerable", "", (), True)
    unanswerable_calls = [
        {
            "toolCallId": "call-skill",
            "name": "skill",
            "arguments": {"name": "knowledge-retrieval"},
            "argsHash": "skill-hash",
        },
        {
            "toolCallId": "call-grep",
            "name": "grep",
            "arguments": {"path": "knowledge", "pattern": "zqxv"},
            "argsHash": "grep-hash",
        },
    ]
    unanswerable_result = evaluate_run_trace(
        unanswerable,
        events=[
            _event("tool.calls.accepted", {"calls": [unanswerable_calls[0]]}),
            _successful_tool("call-skill"),
            _event("tool.calls.accepted", {"calls": [unanswerable_calls[1]]}),
            _successful_tool("call-grep"),
            *terminal,
        ],
        assistant_text="The local knowledge base does not establish this term.",
        elapsed_seconds=0.2,
        source_path_by_id={},
        run_index=1,
    )

    assert control_result["routing"]["noToolControlPass"] is True
    assert control_result["answer"]["answerCorrect"] is True
    assert unanswerable_result["answer"]["abstained"] is True
    assert unanswerable_result["answer"]["answerCorrect"] is True
    assert unanswerable_result["routing"]["requiredToolRecall"] == 1.0
    assert unanswerable_result["routing"]["toolSelectionPass"] is True


def test_aggregation_retains_failures_repairs_repeats_and_redundancy() -> None:
    case = AgentEvaluationCase("control", "Return x", "routing_control", "x", (), False)
    first = evaluate_run_trace(
        case,
        events=[
            _event(
                "model.attempt",
                {
                    "repairIndex": 1,
                    "outcome": "succeeded",
                    "usage": {"inputTokens": 4, "outputTokens": 2},
                },
            ),
            _event("turn.completed", {}, terminal=True),
        ],
        assistant_text="x",
        elapsed_seconds=0.5,
        source_path_by_id={},
        run_index=1,
    )
    second = evaluate_run_trace(
        case,
        events=[_event("turn.failed", {"error": {"code": "internal_error"}}, terminal=True)],
        assistant_text="",
        elapsed_seconds=1.0,
        source_path_by_id={},
        run_index=2,
    )

    report = aggregate_run_results((first, second))

    assert report["runCount"] == 2
    assert report["completionRate"] == 0.5
    assert report["model"]["schemaRepairRate"] == 1.0
    assert report["repeatability"]["repeatedCaseCount"] == 1
    assert report["repeatability"]["outcomeAgreementRate"] == 0.0
