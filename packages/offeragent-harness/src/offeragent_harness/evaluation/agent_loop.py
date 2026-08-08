"""Metrics derived from canonical Agent Loop events, not retrieval shortcuts.

The scorer is deliberately deterministic. Gold answers and evidence are used
only after a run has terminated; callers must never place them in model input.
"""

from __future__ import annotations

import math
import re
import statistics
import unicodedata
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from itertools import pairwise
from typing import Any

_WORD = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*", re.IGNORECASE)
_SPACE = re.compile(r"\s+")
_EVIDENCE_PAGE = re.compile(
    r"^\.offeragent/knowledge/objects/(?P<source>[^/]+)/[^/]+/evidence/pages/(?P<page>\d{4})\.md$",
    re.IGNORECASE,
)
_PAGE_REFERENCE = re.compile(
    r"(?:pages?|pp?\.|p\.|pageindex[^\r\n,;:]*?pages?|\u7b2c)\s*[:\uff1a]?\s*"
    r"(?P<start>\d{1,4})(?:\s*[-\u2013\u2014\u81f3]\s*(?P<end>\d{1,4}))?\s*(?:\u9875)?",
    re.IGNORECASE,
)
_ABSTENTION = re.compile(
    r"(?:"
    r"(?:local\s+)?(?:knowledge\s+base|corpus).{0,48}(?:does\s+not|doesn't|cannot|can't|no\s+evidence|not\s+found)"
    r"|(?:does\s+not|doesn't|cannot|can't|no\s+evidence|not\s+found).{0,48}(?:knowledge\s+base|corpus)"
    r"|\u77e5\u8bc6\u5e93.{0,32}(?:\u6ca1\u6709|\u672a|\u65e0\u6cd5|\u4e0d\u80fd|\u4e0d\u8db3)"
    r"|(?:\u65e0\u6cd5|\u4e0d\u80fd|\u672a).{0,32}(?:\u627e\u5230|\u786e\u5b9a|\u5efa\u7acb|\u8bc1\u660e|\u652f\u6301)"
    r")",
    re.IGNORECASE | re.DOTALL,
)


@dataclass(frozen=True, slots=True)
class GoldEvidence:
    source_path: str
    pages: tuple[int, ...]

    def __post_init__(self) -> None:
        if not self.source_path or not self.pages or any(page < 1 for page in self.pages):
            raise ValueError("gold evidence requires a source path and positive pages")
        if len(self.pages) != len(set(self.pages)):
            raise ValueError("gold evidence pages must be unique")


@dataclass(frozen=True, slots=True)
class AgentEvaluationCase:
    case_id: str
    prompt: str
    case_type: str
    answer: str
    evidence: tuple[GoldEvidence, ...]
    requires_knowledge: bool

    def __post_init__(self) -> None:
        if not self.case_id or not self.prompt or not self.case_type:
            raise ValueError("evaluation case identity, prompt, and type are required")
        if self.requires_knowledge and self.case_type != "unanswerable" and not self.answer:
            raise ValueError("answerable knowledge cases require a gold answer")
        if not self.requires_knowledge and self.evidence:
            raise ValueError("routing controls cannot contain knowledge evidence")


def evaluate_run_trace(
    case: AgentEvaluationCase,
    *,
    events: Sequence[Mapping[str, Any]],
    assistant_text: str,
    elapsed_seconds: float,
    source_path_by_id: Mapping[str, str],
    run_index: int,
) -> dict[str, Any]:
    """Score one completed or failed canonical Loop trace."""

    event_types = [str(event.get("eventType", "")) for event in events]
    calls = _accepted_calls(events)
    results = _tool_results(events)
    attempts = _model_attempts(events)
    terminal = next(
        (event for event in reversed(events) if bool(event.get("terminal"))),
        None,
    )
    terminal_type = None if terminal is None else str(terminal.get("eventType"))
    completed = terminal_type == "turn.completed"
    failure = _failure_summary(terminal)

    tool_names = [str(call.get("name", "")) for call in calls]
    skill_calls = [call for call in calls if call.get("name") == "skill"]
    correct_skill_calls = [
        call
        for call in skill_calls
        if isinstance(call.get("arguments"), Mapping) and call["arguments"].get("name") == "knowledge-retrieval"
    ]
    observed_tools = frozenset(tool_names)
    no_tool_control_pass = not case.requires_knowledge and not calls
    skill_first = _skill_first(tool_names, correct=bool(correct_skill_calls), required=case.requires_knowledge)
    call_signatures = [(str(call.get("name", "")), str(call.get("argsHash", ""))) for call in calls]
    signature_counts = Counter(call_signatures)
    redundant_call_count = sum(count - 1 for count in signature_counts.values() if count > 1)
    repeated_batch_count = _repeated_batch_count(events)

    successful_results = sum(_result_status(result) == "succeeded" for result in results)
    knowledge_argument_checks = [_knowledge_argument_conforms(call) for call in calls]
    knowledge_argument_accuracy = (
        sum(knowledge_argument_checks) / len(knowledge_argument_checks) if knowledge_argument_checks else 1.0
    )
    acquired_pages = _acquired_evidence_pages(calls, results, source_path_by_id)
    required_tool_checks = _required_tool_checks(
        case,
        correct_skill=bool(correct_skill_calls),
        observed_tools=observed_tools,
        acquired_pages=bool(acquired_pages),
    )
    required_tool_recall = sum(required_tool_checks) / len(required_tool_checks)
    gold_pages = {(evidence.source_path.casefold(), page) for evidence in case.evidence for page in evidence.pages}
    acquired_gold_pages = acquired_pages & gold_pages
    evidence_page_recall = len(acquired_gold_pages) / len(gold_pages) if gold_pages else None
    evidence_page_precision = len(acquired_gold_pages) / len(acquired_pages) if acquired_pages else None

    citations, source_mentions, parsed_mentions = _answer_citations(assistant_text, tuple(source_path_by_id.values()))
    correct_citations = citations & gold_pages
    citation_page_precision = len(correct_citations) / len(citations) if citations else None
    citation_group_coverage = _citation_group_coverage(citations, case.evidence)
    citation_parse_rate = parsed_mentions / source_mentions if source_mentions else None

    answer_scores = _answer_scores(case, assistant_text)
    answer_correct = bool(answer_scores["answerCorrect"])
    grounded_answer_correct = bool(
        answer_correct
        and (not case.requires_knowledge or case.case_type == "unanswerable" or citation_group_coverage == 1.0)
    )
    schema_repairs = sum(int(attempt.get("repairIndex", 0) or 0) > 0 for attempt in attempts)
    planner_retries = sum(bool(attempt.get("retryOfRequestId")) for attempt in attempts)
    invalid_attempts = sum(attempt.get("outcome") == "invalid" for attempt in attempts)
    failed_attempts = sum(attempt.get("outcome") == "failed" for attempt in attempts)
    usage = _usage(attempts)

    return {
        "caseId": case.case_id,
        "caseType": case.case_type,
        "requiresKnowledge": case.requires_knowledge,
        "runIndex": run_index,
        "terminalEvent": terminal_type,
        "completed": completed,
        "failure": failure,
        "assistantText": assistant_text,
        "elapsedSeconds": round(max(0.0, elapsed_seconds), 6),
        "eventCount": len(events),
        "model": {
            "attemptCount": len(attempts),
            "schemaRepairCount": schema_repairs,
            "invalidAttemptCount": invalid_attempts,
            "failedAttemptCount": failed_attempts,
            "plannerRetryCount": planner_retries,
            "attemptsWithUsageCount": sum(isinstance(attempt.get("usage"), Mapping) for attempt in attempts),
            **usage,
        },
        "routing": {
            "skillRequired": case.requires_knowledge,
            "skillCallCount": len(skill_calls),
            "correctSkillCallCount": len(correct_skill_calls),
            "skillActivated": bool(correct_skill_calls),
            "skillFirst": skill_first,
            "requiredToolRecall": round(required_tool_recall, 6),
            "noToolControlPass": no_tool_control_pass,
            "toolSelectionPass": (
                all(required_tool_checks) and skill_first if case.requires_knowledge else no_tool_control_pass
            ),
        },
        "tools": {
            "acceptedCallCount": len(calls),
            "completedResultCount": len(results),
            "successfulResultCount": successful_results,
            "executionSuccessRate": round(successful_results / len(results), 6) if results else None,
            "knowledgeArgumentConformance": round(knowledge_argument_accuracy, 6),
            "redundantExactCallCount": redundant_call_count,
            "repeatedBatchCount": repeated_batch_count,
            "calls": [
                {
                    "name": str(call.get("name", "")),
                    "argsHash": str(call.get("argsHash", "")),
                    "arguments": call.get("arguments", {}),
                }
                for call in calls
            ],
        },
        "evidence": {
            "acquiredPageCount": len(acquired_pages),
            "goldPageCount": len(gold_pages),
            "goldPageHitCount": len(acquired_gold_pages),
            "pageRecall": _rounded(evidence_page_recall),
            "pagePrecision": _rounded(evidence_page_precision),
        },
        "answer": {
            **answer_scores,
            "groundedAnswerCorrect": grounded_answer_correct,
            "citationSourceMentionCount": source_mentions,
            "parsedCitationMentionCount": parsed_mentions,
            "citationPageCount": len(citations),
            "correctCitationPageCount": len(correct_citations),
            "citationParseRate": _rounded(citation_parse_rate),
            "citationPagePrecision": _rounded(citation_page_precision),
            "citationEvidenceGroupCoverage": _rounded(citation_group_coverage),
        },
        "eventTypes": event_types,
    }


def aggregate_run_results(results: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Aggregate run-level records without dropping failed runs or null metrics."""

    if not results:
        raise ValueError("agent loop aggregation requires at least one result")
    knowledge = [item for item in results if bool(item["requiresKnowledge"])]
    controls = [item for item in results if not bool(item["requiresKnowledge"])]
    skill_calls = sum(int(_nested(item, "routing", "skillCallCount")) for item in results)
    correct_skill_calls = sum(int(_nested(item, "routing", "correctSkillCallCount")) for item in knowledge)
    skill_task_hits = sum(bool(_nested(item, "routing", "skillActivated")) for item in knowledge)
    by_case: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for item in results:
        by_case[str(item["caseId"])].append(item)
    repeated = [items for items in by_case.values() if len(items) > 1]
    completed_results = [item for item in results if bool(item["completed"])]
    failures = Counter(
        str(failure.get("category", "unknown"))
        for item in results
        if isinstance((failure := item.get("failure")), Mapping)
    )
    return {
        "schemaVersion": 1,
        "evaluationKind": "canonical-agent-loop-end-to-end",
        "runCount": len(results),
        "uniqueCaseCount": len(by_case),
        "knowledgeRunCount": len(knowledge),
        "controlRunCount": len(controls),
        "completionRate": _mean_bool(results, "completed"),
        "terminalEventCounts": dict(sorted(Counter(str(item.get("terminalEvent")) for item in results).items())),
        "failureCounts": dict(sorted(failures.items())),
        "byCaseType": {
            case_type: _case_type_metrics(items)
            for case_type, items in sorted(
                (case_type, [item for item in results if item["caseType"] == case_type])
                for case_type in {str(item["caseType"]) for item in results}
            )
        },
        "routing": {
            "skillActivationPrecision": round(correct_skill_calls / skill_calls, 6) if skill_calls else None,
            "skillActivationRecall": round(skill_task_hits / len(knowledge), 6) if knowledge else None,
            "skillFirstRate": _mean_nested(knowledge, "routing", "skillFirst"),
            "requiredToolRecall": _mean_nested(knowledge, "routing", "requiredToolRecall"),
            "toolSelectionPassRate": _mean_nested(results, "routing", "toolSelectionPass"),
            "negativeControlNoToolAccuracy": _mean_nested(controls, "routing", "noToolControlPass"),
        },
        "tools": {
            "acceptedCallCount": sum(int(_nested(item, "tools", "acceptedCallCount")) for item in results),
            "executionSuccessRate": _weighted_tool_success(results),
            "knowledgeArgumentConformance": _mean_nested(results, "tools", "knowledgeArgumentConformance"),
            "runsWithRedundantExactCalls": _rate(
                [int(_nested(item, "tools", "redundantExactCallCount")) > 0 for item in results]
            ),
            "redundantExactCallCount": sum(int(_nested(item, "tools", "redundantExactCallCount")) for item in results),
            "runsWithRepeatedBatches": _rate(
                [int(_nested(item, "tools", "repeatedBatchCount")) > 0 for item in results]
            ),
        },
        "evidence": {
            "pageRecall": _mean_non_null(knowledge, "evidence", "pageRecall"),
            "pagePrecision": _mean_non_null(knowledge, "evidence", "pagePrecision"),
        },
        "generation": {
            "answerCorrectness": _mean_nested(results, "answer", "answerCorrect"),
            "answerCorrectnessOnCompleted": _mean_nested(completed_results, "answer", "answerCorrect"),
            "groundedAnswerCorrectness": _mean_nested(results, "answer", "groundedAnswerCorrect"),
            "groundedAnswerCorrectnessOnCompleted": _mean_nested(completed_results, "answer", "groundedAnswerCorrect"),
            "answerSegmentRecall": _mean_nested(results, "answer", "answerSegmentRecall"),
            "unanswerableAbstentionAccuracy": _mean_nested(
                [item for item in results if item["caseType"] == "unanswerable"],
                "answer",
                "answerCorrect",
            ),
            "citationPagePrecision": _mean_non_null(knowledge, "answer", "citationPagePrecision"),
            "citationEvidenceGroupCoverage": _mean_non_null(knowledge, "answer", "citationEvidenceGroupCoverage"),
            "citationParseRate": _mean_non_null(knowledge, "answer", "citationParseRate"),
        },
        "model": {
            "attemptsPerRun": _mean_nested(results, "model", "attemptCount"),
            "schemaRepairRate": _ratio_sum(results, "model", "schemaRepairCount", "attemptCount"),
            "invalidAttemptRate": _ratio_sum(results, "model", "invalidAttemptCount", "attemptCount"),
            "plannerRetryRate": _ratio_sum(results, "model", "plannerRetryCount", "attemptCount"),
            "usageCoverage": _ratio_sum(results, "model", "attemptsWithUsageCount", "attemptCount"),
            "logicalRequestCount": _optional_sum(results, "model", "logicalRequestCount"),
            "networkAttemptCount": _optional_sum(results, "model", "networkAttemptCount"),
            "networkRetryCount": _optional_sum(results, "model", "networkRetryCount"),
            "networkRetryRate": _ratio_sum_optional(results, "model", "networkRetryCount", "networkAttemptCount"),
            "failedAttemptRate": _ratio_sum(results, "model", "failedAttemptCount", "attemptCount"),
            "inputTokens": sum(int(_nested(item, "model", "inputTokens")) for item in results),
            "outputTokens": sum(int(_nested(item, "model", "outputTokens")) for item in results),
            "reasoningTokens": sum(int(_nested(item, "model", "reasoningTokens")) for item in results),
        },
        "latencySeconds": _latency([float(item["elapsedSeconds"]) for item in results]),
        "repeatability": _repeatability(repeated),
    }


def _accepted_calls(events: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    calls: list[Mapping[str, Any]] = []
    for event in events:
        if event.get("eventType") != "tool.calls.accepted":
            continue
        payload = event.get("payload")
        raw_calls = payload.get("calls") if isinstance(payload, Mapping) else None
        if isinstance(raw_calls, Sequence) and not isinstance(raw_calls, (str, bytes, bytearray)):
            calls.extend(call for call in raw_calls if isinstance(call, Mapping))
    return calls


def _failure_summary(terminal: Mapping[str, Any] | None) -> dict[str, object] | None:
    if terminal is None or terminal.get("eventType") != "turn.failed":
        return None
    payload = terminal.get("payload")
    error = payload.get("error") if isinstance(payload, Mapping) else None
    if not isinstance(error, Mapping):
        return {"category": "turn.failed:unknown"}
    details = error.get("details")
    detail_map = details if isinstance(details, Mapping) else {}
    provider_code = detail_map.get("providerErrorCode")
    protocol_reason = detail_map.get("providerProtocolReason")
    error_code = error.get("code")
    parts = [str(value) for value in (provider_code, protocol_reason) if isinstance(value, str) and value]
    category = ":".join(parts) if parts else str(error_code or "unknown")
    return {
        "category": category,
        "errorCode": error_code if isinstance(error_code, str) else None,
        "providerErrorCode": provider_code if isinstance(provider_code, str) else None,
        "providerProtocolReason": protocol_reason if isinstance(protocol_reason, str) else None,
        "retryable": bool(error.get("retryable")),
        "cancelled": bool(error.get("cancelled")),
    }


def _tool_results(events: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    results: list[Mapping[str, Any]] = []
    for event in events:
        if event.get("eventType") not in {"tool.completed", "tool.failed"}:
            continue
        payload = event.get("payload")
        if not isinstance(payload, Mapping):
            continue
        result = payload.get("result")
        if isinstance(result, Mapping):
            results.append(payload)
    return results


def _model_attempts(events: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    return [
        payload
        for event in events
        if event.get("eventType") == "model.attempt" and isinstance((payload := event.get("payload")), Mapping)
    ]


def _result_status(result: Mapping[str, Any]) -> str:
    descriptor = result.get("result")
    return str(descriptor.get("status", "")) if isinstance(descriptor, Mapping) else ""


def _skill_first(tool_names: Sequence[str], *, correct: bool, required: bool) -> bool:
    if not required:
        return not tool_names
    return bool(tool_names and tool_names[0] == "skill" and correct)


def _required_tool_checks(
    case: AgentEvaluationCase,
    *,
    correct_skill: bool,
    observed_tools: frozenset[str],
    acquired_pages: bool,
) -> tuple[bool, ...]:
    if not case.requires_knowledge:
        return (True,)
    if case.case_type == "unanswerable":
        return (correct_skill, "grep" in observed_tools)
    return (correct_skill, acquired_pages)


def _repeated_batch_count(events: Sequence[Mapping[str, Any]]) -> int:
    batches: list[tuple[tuple[str, str], ...]] = []
    for event in events:
        if event.get("eventType") != "tool.calls.accepted":
            continue
        payload = event.get("payload")
        calls = payload.get("calls") if isinstance(payload, Mapping) else None
        if not isinstance(calls, Sequence) or isinstance(calls, (str, bytes, bytearray)):
            continue
        batches.append(
            tuple(
                (str(call.get("name", "")), str(call.get("argsHash", "")))
                for call in calls
                if isinstance(call, Mapping)
            )
        )
    return sum(left == right for left, right in pairwise(batches))


def _knowledge_argument_conforms(call: Mapping[str, Any]) -> bool:
    name = str(call.get("name", ""))
    arguments = call.get("arguments")
    if not isinstance(arguments, Mapping):
        return False
    if name == "skill":
        return arguments.get("name") == "knowledge-retrieval"
    if name not in {"glob", "grep", "read"}:
        return False
    path = arguments.get("path", "")
    if not isinstance(path, str):
        return False
    normalized = path.replace("\\", "/").casefold().lstrip("./")
    if normalized.startswith("raw/") or normalized.endswith(".pdf"):
        return False
    return (
        not normalized
        or normalized == "knowledge"
        or normalized.startswith("knowledge/")
        or normalized == "offeragent/knowledge"
        or normalized.startswith("offeragent/knowledge/")
    )


def _acquired_evidence_pages(
    calls: Sequence[Mapping[str, Any]],
    results: Sequence[Mapping[str, Any]],
    source_path_by_id: Mapping[str, str],
) -> set[tuple[str, int]]:
    successful_ids = {
        str(descriptor.get("toolCallId"))
        for result in results
        if _result_status(result) == "succeeded" and isinstance((descriptor := result.get("result")), Mapping)
    }
    acquired: set[tuple[str, int]] = set()
    for call in calls:
        if call.get("name") != "read" or str(call.get("toolCallId")) not in successful_ids:
            continue
        arguments = call.get("arguments")
        path = arguments.get("path") if isinstance(arguments, Mapping) else None
        if not isinstance(path, str):
            continue
        match = _EVIDENCE_PAGE.fullmatch(path.replace("\\", "/"))
        if match is None:
            continue
        source_path = source_path_by_id.get(match.group("source"))
        if source_path is not None:
            acquired.add((source_path.casefold(), int(match.group("page"))))
    return acquired


def _answer_citations(answer: str, source_paths: Sequence[str]) -> tuple[set[tuple[str, int]], int, int]:
    folded = answer.casefold()
    citations: set[tuple[str, int]] = set()
    source_mentions = 0
    parsed_mentions = 0
    for source_path in source_paths:
        needle = source_path.casefold()
        offset = 0
        while (index := folded.find(needle, offset)) >= 0:
            source_mentions += 1
            start = max(0, index - 120)
            end = min(len(answer), index + len(source_path) + 180)
            matches = list(_PAGE_REFERENCE.finditer(answer[start:end]))
            if matches:
                source_center = index - start + len(source_path) / 2
                selected = min(matches, key=lambda item: abs((item.start() + item.end()) / 2 - source_center))
                first = int(selected.group("start"))
                last = int(selected.group("end") or first)
                if 1 <= first <= last <= 10_000 and last - first <= 100:
                    parsed_mentions += 1
                    citations.update((needle, page) for page in range(first, last + 1))
            offset = index + len(needle)
    return citations, source_mentions, parsed_mentions


def _citation_group_coverage(citations: set[tuple[str, int]], evidence_groups: Sequence[GoldEvidence]) -> float | None:
    if not evidence_groups:
        return None
    covered = sum(
        any((evidence.source_path.casefold(), page) in citations for page in evidence.pages)
        for evidence in evidence_groups
    )
    return covered / len(evidence_groups)


def _answer_scores(case: AgentEvaluationCase, answer: str) -> dict[str, Any]:
    if case.case_type == "unanswerable":
        abstained = bool(_ABSTENTION.search(answer))
        return {
            "answerCorrect": abstained,
            "answerSegmentRecall": 1.0 if abstained else 0.0,
            "tokenF1": None,
            "abstained": abstained,
        }
    segments = tuple(part.strip() for part in case.answer.split(";") if part.strip())
    normalized_answer = _normalize(answer)
    hits = sum(_normalize(segment) in normalized_answer for segment in segments)
    segment_recall = hits / len(segments) if segments else 0.0
    return {
        "answerCorrect": bool(segments and hits == len(segments)),
        "answerSegmentRecall": round(segment_recall, 6),
        "tokenF1": _rounded(_token_f1(case.answer, answer)),
        "abstained": False,
    }


def _normalize(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return "".join(character for character in normalized if character.isalnum())


def _token_f1(gold: str, predicted: str) -> float:
    gold_tokens = Counter(match.group(0).casefold() for match in _WORD.finditer(gold))
    predicted_tokens = Counter(match.group(0).casefold() for match in _WORD.finditer(predicted))
    overlap = sum((gold_tokens & predicted_tokens).values())
    if not gold_tokens or not predicted_tokens or not overlap:
        return 0.0
    precision = overlap / sum(predicted_tokens.values())
    recall = overlap / sum(gold_tokens.values())
    return 2 * precision * recall / (precision + recall)


def _usage(attempts: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    totals = {"inputTokens": 0, "outputTokens": 0, "cachedInputTokens": 0, "reasoningTokens": 0}
    for attempt in attempts:
        usage = attempt.get("usage")
        if not isinstance(usage, Mapping):
            continue
        for key in totals:
            value = usage.get(key, 0)
            if type(value) is int and value >= 0:
                totals[key] += value
    return totals


def _nested(item: Mapping[str, Any], section: str, key: str) -> Any:
    value = item.get(section)
    if not isinstance(value, Mapping):
        raise ValueError(f"run result section {section!r} is invalid")
    return value.get(key)


def _mean_bool(items: Sequence[Mapping[str, Any]], key: str) -> float | None:
    return _rate([bool(item.get(key)) for item in items])


def _mean_nested(items: Sequence[Mapping[str, Any]], section: str, key: str) -> float | None:
    values = [_nested(item, section, key) for item in items]
    numeric = [float(value) for value in values if value is not None]
    return round(statistics.fmean(numeric), 6) if numeric else None


def _mean_non_null(items: Sequence[Mapping[str, Any]], section: str, key: str) -> float | None:
    return _mean_nested(items, section, key)


def _case_type_metrics(items: Sequence[Mapping[str, Any]]) -> dict[str, object]:
    completed = [item for item in items if bool(item["completed"])]
    knowledge = [item for item in items if bool(item["requiresKnowledge"])]
    return {
        "runCount": len(items),
        "completionRate": _mean_bool(items, "completed"),
        "answerCorrectness": _mean_nested(items, "answer", "answerCorrect"),
        "answerCorrectnessOnCompleted": _mean_nested(completed, "answer", "answerCorrect"),
        "groundedAnswerCorrectness": _mean_nested(items, "answer", "groundedAnswerCorrect"),
        "evidencePageRecall": _mean_non_null(knowledge, "evidence", "pageRecall"),
        "toolSelectionPassRate": _mean_nested(items, "routing", "toolSelectionPass"),
    }


def _ratio_sum(items: Sequence[Mapping[str, Any]], section: str, numerator: str, denominator: str) -> float | None:
    top = sum(int(_nested(item, section, numerator)) for item in items)
    bottom = sum(int(_nested(item, section, denominator)) for item in items)
    return round(top / bottom, 6) if bottom else None


def _optional_sum(items: Sequence[Mapping[str, Any]], section: str, key: str) -> int | None:
    values = [_nested(item, section, key) for item in items]
    if any(value is None for value in values):
        return None
    return sum(int(value) for value in values)


def _ratio_sum_optional(
    items: Sequence[Mapping[str, Any]], section: str, numerator: str, denominator: str
) -> float | None:
    top = _optional_sum(items, section, numerator)
    bottom = _optional_sum(items, section, denominator)
    return round(top / bottom, 6) if top is not None and bottom else None


def _weighted_tool_success(items: Sequence[Mapping[str, Any]]) -> float | None:
    top = sum(int(_nested(item, "tools", "successfulResultCount")) for item in items)
    bottom = sum(int(_nested(item, "tools", "completedResultCount")) for item in items)
    return round(top / bottom, 6) if bottom else None


def _rate(values: Sequence[bool]) -> float | None:
    return round(sum(values) / len(values), 6) if values else None


def _latency(values: Sequence[float]) -> dict[str, float]:
    ordered = sorted(values)
    return {
        "p50": round(_percentile(ordered, 0.50), 6),
        "p95": round(_percentile(ordered, 0.95), 6),
        "maximum": round(max(ordered), 6),
    }


def _percentile(ordered: Sequence[float], quantile: float) -> float:
    if not ordered:
        return 0.0
    position = (len(ordered) - 1) * quantile
    low = math.floor(position)
    high = math.ceil(position)
    if low == high:
        return ordered[low]
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def _repeatability(groups: Sequence[Sequence[Mapping[str, Any]]]) -> dict[str, Any]:
    if not groups:
        return {
            "repeatedCaseCount": 0,
            "allRunsCorrectRate": None,
            "outcomeAgreementRate": None,
            "normalizedAnswerAgreementRate": None,
        }
    all_correct: list[bool] = []
    outcome_agreement: list[bool] = []
    answer_agreement: list[bool] = []
    for group in groups:
        correctness = [bool(_nested(item, "answer", "answerCorrect")) for item in group]
        all_correct.append(all(correctness))
        outcome_agreement.append(len(set(correctness)) == 1)
        answers = {_normalize(str(item.get("assistantText", ""))) for item in group}
        answer_agreement.append(len(answers) == 1)
    return {
        "repeatedCaseCount": len(groups),
        "allRunsCorrectRate": _rate(all_correct),
        "outcomeAgreementRate": _rate(outcome_agreement),
        "normalizedAnswerAgreementRate": _rate(answer_agreement),
    }


def _rounded(value: float | None) -> float | None:
    return None if value is None else round(value, 6)


__all__ = [
    "AgentEvaluationCase",
    "GoldEvidence",
    "aggregate_run_results",
    "evaluate_run_trace",
]
