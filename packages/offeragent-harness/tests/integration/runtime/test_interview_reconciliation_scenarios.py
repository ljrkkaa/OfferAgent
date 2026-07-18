from __future__ import annotations

# ruff: noqa: RUF001 -- Chinese product-facing responses intentionally use Chinese punctuation.
import hashlib
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, cast

import pytest
import test_interview_submission as base

from offeragent_harness.runtime.plugin_tools import PluginToolExecutor
from offeragent_harness.sessions import RunStatus
from offeragent_harness.tools import ToolResult, ToolResultStatus

EXISTING_EXPERIENCE_PATH = "experiences/existing-event.md"
EXISTING_EXPERIENCE_VERSION = "mtime:200:size:512"
EXISTING_EXPERIENCE_CONTENT = (
    "---\n"
    "type: interview-experience\n"
    "experience-id: existing_event\n"
    "source-kind: public_url\n"
    f"source-url: {base.CANONICAL_SOURCE_URL}\n"
    "company: unknown\n"
    "role: unknown\n"
    "event-date: unknown\n"
    "round: unknown\n"
    "---\n"
    "# Existing event\n\n"
    "## Questions\n"
    "- [[../interview/node-event-loop-scheduling]]\n"
)
EXISTING_EXPERIENCE_HASH = f"sha256:{hashlib.sha256(EXISTING_EXPERIENCE_CONTENT.encode()).hexdigest()}"
EXISTING_QUESTION_PATH = "interview/node-event-loop-scheduling.md"
EXISTING_QUESTION_VERSION = "mtime:201:size:480"
EXISTING_QUESTION_CONTENT = (
    "---\n"
    "type: interview-question\n"
    "question-id: question_node_event_loop_scheduling\n"
    "title: Explain Node.js event-loop scheduling\n"
    "answer-state: needs-research\n"
    "frequency: 2\n"
    "---\n"
    "# Explain Node.js event-loop scheduling\n\n"
    "## Occurrences\n"
    "- [[../experiences/prior-event-a]] · phone screen\n"
    "- [[../experiences/prior-event-b]] · technical round\n"
)
EXISTING_QUESTION_HASH = f"sha256:{hashlib.sha256(EXISTING_QUESTION_CONTENT.encode()).hexdigest()}"
SAME_EVENT_QUESTION_CONTENT = EXISTING_QUESTION_CONTENT.replace(
    "- [[../experiences/prior-event-a]] · phone screen",
    f"- [[../{EXISTING_EXPERIENCE_PATH.removesuffix('.md')}]] · phone screen",
)
SAME_EVENT_QUESTION_HASH = f"sha256:{hashlib.sha256(SAME_EVENT_QUESTION_CONTENT.encode()).hexdigest()}"
NEW_EXPERIENCE_PATH = "experiences/distinct-event-20260718.md"


def _catalog(
    *,
    experience_candidates: Sequence[Mapping[str, object]] = (),
    question_candidates: Sequence[Mapping[str, object]] = (),
) -> dict[str, object]:
    return {
        "normalizedSource": {
            "canonicalUrls": [base.CANONICAL_SOURCE_URL],
            "sourceFingerprint": base.SOURCE_FINGERPRINT,
            "orderedImageContentHashes": [base.PAGE_ONE_HASH, base.PAGE_TWO_HASH],
        },
        "experienceCandidates": [dict(candidate) for candidate in experience_candidates],
        "questionCandidates": [dict(candidate) for candidate in question_candidates],
        "indexes": [
            {
                "kind": "experience",
                "path": base.EXPERIENCE_INDEX_PATH,
                "exists": True,
                "modifiedVersion": base.EXPERIENCE_INDEX_VERSION,
                "contentHash": base.EXPERIENCE_INDEX_HASH,
            },
            {
                "kind": "question",
                "path": base.QUESTION_INDEX_PATH,
                "exists": True,
                "modifiedVersion": base.QUESTION_INDEX_VERSION,
                "contentHash": base.QUESTION_INDEX_HASH,
            },
        ],
        "truncated": False,
    }


def _experience_candidate(*, exact_source_match: bool) -> dict[str, object]:
    return {
        "path": EXISTING_EXPERIENCE_PATH,
        "experienceId": "existing_event",
        "sourceKind": "public_url",
        "sourceUrl": base.CANONICAL_SOURCE_URL,
        "sourceFingerprint": base.SOURCE_FINGERPRINT,
        "exactSourceMatch": exact_source_match,
        "contentHash": EXISTING_EXPERIENCE_HASH,
        "modifiedVersion": EXISTING_EXPERIENCE_VERSION,
    }


def _question_candidate(*, content_hash: str = EXISTING_QUESTION_HASH) -> dict[str, object]:
    return {
        "path": EXISTING_QUESTION_PATH,
        "questionId": "question_node_event_loop_scheduling",
        "title": "Explain Node.js event-loop scheduling",
        "answerState": "needs-research",
        "frequency": 2,
        "matchedTerms": ["Node.js event loop", "microtasks and timers"],
        "contentHash": content_hash,
        "modifiedVersion": EXISTING_QUESTION_VERSION,
    }


def _new_experience_content() -> str:
    return (
        "---\n"
        "type: interview-experience\n"
        "experience-id: distinct_event_20260718\n"
        "source-kind: mixed\n"
        "captured-on: 2026-07-18\n"
        f"source-url: {base.CANONICAL_SOURCE_URL}\n"
        f"source-fingerprint: {base.SOURCE_FINGERPRINT}\n"
        "company: unknown\n"
        "role: unknown\n"
        "event-date: unknown\n"
        "round: unknown\n"
        "---\n"
        "# Distinct interview event\n\n"
        "## Questions\n"
        "- [[../interview/node-event-loop-scheduling]]\n"
    )


def _read_call(path: str, version: str, content_hash: str) -> dict[str, object]:
    return base._tool_call(
        "vault.read",
        {
            "path": path,
            "expectedModifiedVersion": version,
            "expectedContentHash": content_hash,
        },
        f"Read the exact current content of {path} before deciding identity or mutation.",
    )


def _catalog_call() -> dict[str, object]:
    return base._tool_call(
        "interview_catalog.search",
        {
            "sourceUrls": [base.RAW_SOURCE_URL],
            "orderedImageContentHashes": [base.PAGE_ONE_HASH, base.PAGE_TWO_HASH],
            "company": "unknown",
            "role": "unknown",
            "questionTerms": ["Node.js event loop", "microtasks and timers"],
        },
        "Discover exact source and semantic candidates before deciding whether any write is needed.",
    )


def _preamble() -> tuple[dict[str, object], dict[str, object]]:
    return (
        base._agent_step(
            base._tool_call("agent_contract.read", {}, "Read the Vault Agent Contract before taking action.")
        ),
        base._agent_step(
            base._tool_call(
                "planning_memory.list",
                {},
                "Check bounded Planning Memory metadata before reconciling the submission.",
            )
        ),
    )


def _submission_metadata(review_items: Sequence[Mapping[str, object]]) -> dict[str, object]:
    return {
        "sourceKind": "mixed",
        "capturedOn": "2026-07-18",
        "canonicalUrls": [base.CANONICAL_SOURCE_URL],
        "orderedImageContentHashes": [base.PAGE_ONE_HASH, base.PAGE_TWO_HASH],
        "sourceFingerprint": base.SOURCE_FINGERPRINT,
        "reviewItems": [dict(item) for item in review_items],
    }


def _source_binding(path: str, version: str, content_hash: str) -> dict[str, object]:
    return {
        "path": path,
        "expectedModifiedVersion": version,
        "expectedContentHash": content_hash,
    }


class _ScenarioPluginToolAdapter(base._FakePluginToolAdapter):
    def __init__(
        self,
        executor: PluginToolExecutor,
        *,
        catalog: Mapping[str, object],
        reads: Mapping[str, tuple[str, str, str]],
    ) -> None:
        super().__init__(executor)
        self._catalog = dict(catalog)
        self._reads = dict(reads)

    def _result_for(self, call: Mapping[str, Any]) -> ToolResult:
        name = cast(str, call["name"])
        arguments = cast(dict[str, Any], call["arguments"])
        if name == "interview_catalog.search":
            self.catalog_result = self._catalog
            data: object = self._catalog
        elif name == "vault.read" and cast(str, arguments["path"]) in self._reads:
            path = cast(str, arguments["path"])
            version, content_hash, content = self._reads[path]
            data = {
                "path": path,
                "lineStart": 1,
                "lineEnd": max(1, len(content.splitlines())),
                "modifiedVersion": version,
                "contentHash": content_hash,
                "content": content,
                "truncated": False,
            }
        else:
            return super()._result_for(call)
        return ToolResult(
            tool_call_id=cast(str, call["toolCallId"]),
            status=ToolResultStatus.SUCCEEDED,
            data=cast(Any, data),
            user_visible_summary=f"Plugin completed {name}.",
            artifact_ids=(),
            source_refs=(),
            side_effects=(),
            retryable=False,
            before_state=None,
            after_state=None,
            error=None,
        )


async def _run_scenario(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    script: Sequence[Mapping[str, object]],
    expected_tool_result_counts: Sequence[int],
    catalog: Mapping[str, object],
    reads: Mapping[str, tuple[str, str, str]],
    turn_id: str,
) -> tuple[base._DeterministicScriptedGateway, _ScenarioPluginToolAdapter, str]:
    gateway = base._DeterministicScriptedGateway(script, expected_tool_result_counts)

    def adapter_factory(executor: PluginToolExecutor) -> _ScenarioPluginToolAdapter:
        return _ScenarioPluginToolAdapter(executor, catalog=catalog, reads=reads)

    monkeypatch.setattr(base, "_FakePluginToolAdapter", adapter_factory)
    harness, dispatcher, attachments, adapter, _, _ = base._runtime(tmp_path, gateway)
    try:
        run_id = await base._start_submission(
            harness,
            dispatcher,
            attachments,
            (base.PAGE_ONE, base.PAGE_TWO),
            turn_id=turn_id,
        )
        assert await base._wait_terminal(harness, run_id) is RunStatus.COMPLETED
        await adapter.join()
        assistant_text = (await harness.get_run_state(run_id)).assistant_text
    finally:
        await harness.shutdown()
    gateway.assert_exhausted()
    return gateway, cast(_ScenarioPluginToolAdapter, adapter), assistant_text


@pytest.mark.asyncio
async def test_exact_canonical_url_duplicate_is_read_and_reports_no_write_without_apply(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = (
        *_preamble(),
        base._agent_step(_catalog_call()),
        base._agent_step(
            _read_call(EXISTING_EXPERIENCE_PATH, EXISTING_EXPERIENCE_VERSION, EXISTING_EXPERIENCE_HASH),
            _read_call(EXISTING_QUESTION_PATH, EXISTING_QUESTION_VERSION, EXISTING_QUESTION_HASH),
        ),
        base._agent_step(final_response="已精读精确 URL 命中的现有面经；内容完整重复，无需写入。"),
    )
    _, adapter, assistant_text = await _run_scenario(
        tmp_path,
        monkeypatch,
        script=script,
        expected_tool_result_counts=(0, 1, 2, 3, 5),
        catalog=_catalog(
            experience_candidates=(_experience_candidate(exact_source_match=True),),
            question_candidates=(_question_candidate(),),
        ),
        reads={
            EXISTING_EXPERIENCE_PATH: (
                EXISTING_EXPERIENCE_VERSION,
                EXISTING_EXPERIENCE_HASH,
                EXISTING_EXPERIENCE_CONTENT,
            ),
            EXISTING_QUESTION_PATH: (
                EXISTING_QUESTION_VERSION,
                EXISTING_QUESTION_HASH,
                EXISTING_QUESTION_CONTENT,
            ),
        },
        turn_id="turn_exact_url_duplicate",
    )

    assert [call["name"] for call in adapter.started_calls] == [
        "agent_contract.read",
        "planning_memory.list",
        "interview_catalog.search",
        "vault.read",
        "vault.read",
    ]
    assert assistant_text == "已精读精确 URL 命中的现有面经；内容完整重复，无需写入。"
    assert not any(call["name"] == "vault.changes.apply" for call in adapter.started_calls)


@pytest.mark.asyncio
async def test_exact_ordered_screenshot_duplicate_is_read_and_reports_no_write_without_apply(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fingerprint_candidate = _experience_candidate(exact_source_match=True)
    fingerprint_candidate["sourceKind"] = "ordered_images"
    fingerprint_candidate.pop("sourceUrl")
    script = (
        *_preamble(),
        base._agent_step(_catalog_call()),
        base._agent_step(_read_call(EXISTING_EXPERIENCE_PATH, EXISTING_EXPERIENCE_VERSION, EXISTING_EXPERIENCE_HASH)),
        base._agent_step(final_response="有序截图指纹精确命中现有面经；内容完整重复，无需写入。"),
    )
    _, adapter, assistant_text = await _run_scenario(
        tmp_path,
        monkeypatch,
        script=script,
        expected_tool_result_counts=(0, 1, 2, 3, 4),
        catalog=_catalog(experience_candidates=(fingerprint_candidate,)),
        reads={
            EXISTING_EXPERIENCE_PATH: (
                EXISTING_EXPERIENCE_VERSION,
                EXISTING_EXPERIENCE_HASH,
                EXISTING_EXPERIENCE_CONTENT,
            )
        },
        turn_id="turn_exact_screenshot_duplicate",
    )

    catalog_result = cast(dict[str, Any], adapter.catalog_result)
    candidate = cast(dict[str, Any], cast(list[object], catalog_result["experienceCandidates"])[0])
    assert candidate["sourceFingerprint"] == base.SOURCE_FINGERPRINT
    assert candidate["exactSourceMatch"] is True
    assert assistant_text.endswith("内容完整重复，无需写入。")
    assert not any(call["name"] == "vault.changes.apply" for call in adapter.started_calls)


@pytest.mark.asyncio
async def test_obvious_repost_merges_the_read_experience_without_creating_a_second_event(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repost_candidate = _experience_candidate(exact_source_match=False)
    repost_candidate["sourceUrl"] = "https://mirror.example/interviews/node-event-loop"
    batch = {
        "batchId": "interview_repost_merge_20260718",
        "task": "Merge source-supported context from an obvious repost into the existing event",
        "changeKind": "interview_submission",
        "sourceBindings": [
            _source_binding(
                EXISTING_EXPERIENCE_PATH,
                EXISTING_EXPERIENCE_VERSION,
                EXISTING_EXPERIENCE_HASH,
            )
        ],
        "interviewSubmission": _submission_metadata(
            (
                {
                    "kind": "experience",
                    "path": EXISTING_EXPERIENCE_PATH,
                    "identity": "existing",
                    "mutation": "modify",
                },
            )
        ),
        "operations": [
            {
                "op": "append",
                "path": EXISTING_EXPERIENCE_PATH,
                "content": "\n## Repost context\n- 同一题序与逐句追问，来源为明显搬运。\n",
                "expectedContentHash": EXISTING_EXPERIENCE_HASH,
                "expectedModifiedVersion": EXISTING_EXPERIENCE_VERSION,
            }
        ],
    }
    script = (
        *_preamble(),
        base._agent_step(_catalog_call()),
        base._agent_step(_read_call(EXISTING_EXPERIENCE_PATH, EXISTING_EXPERIENCE_VERSION, EXISTING_EXPERIENCE_HASH)),
        base._agent_step(
            base._tool_call(
                "vault.changes.apply",
                batch,
                "The exact candidate body establishes an obvious repost; merge it into that one event.",
            ),
            requires_write_outcome=True,
        ),
        base._agent_step(final_response="明显搬运内容已合并到既有面经，没有创建第二篇 Experience。"),
    )
    _, adapter, assistant_text = await _run_scenario(
        tmp_path,
        monkeypatch,
        script=script,
        expected_tool_result_counts=(0, 1, 2, 3, 4, 5),
        catalog=_catalog(experience_candidates=(repost_candidate,)),
        reads={
            EXISTING_EXPERIENCE_PATH: (
                EXISTING_EXPERIENCE_VERSION,
                EXISTING_EXPERIENCE_HASH,
                EXISTING_EXPERIENCE_CONTENT,
            )
        },
        turn_id="turn_obvious_repost_merge",
    )

    assert [call["name"] for call in adapter.started_calls] == [
        "agent_contract.read",
        "planning_memory.list",
        "interview_catalog.search",
        "vault.read",
        "vault.changes.apply",
    ]
    apply_arguments = cast(dict[str, Any], adapter.started_calls[-1]["arguments"])
    assert apply_arguments["operations"] == batch["operations"]
    assert all(operation["op"] != "create" for operation in cast(list[dict[str, Any]], batch["operations"]))
    assert not any("index.md" in operation["path"] for operation in cast(list[dict[str, Any]], batch["operations"]))
    assert assistant_text == "明显搬运内容已合并到既有面经，没有创建第二篇 Experience。"


@pytest.mark.asyncio
async def test_distinct_event_reuses_a_question_and_adds_one_unique_occurrence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    updated_question = EXISTING_QUESTION_CONTENT.replace("frequency: 2", "frequency: 3") + (
        f"- [[../{NEW_EXPERIENCE_PATH.removesuffix('.md')}]] · technical round\n"
    )
    batch = {
        "batchId": "interview_distinct_shared_question_20260718",
        "task": "Create one distinct event and add one unique occurrence to a shared Question",
        "changeKind": "interview_submission",
        "sourceBindings": [
            _source_binding(EXISTING_QUESTION_PATH, EXISTING_QUESTION_VERSION, EXISTING_QUESTION_HASH),
            _source_binding(
                base.EXPERIENCE_INDEX_PATH,
                base.EXPERIENCE_INDEX_VERSION,
                base.EXPERIENCE_INDEX_HASH,
            ),
        ],
        "interviewSubmission": _submission_metadata(
            (
                {
                    "kind": "experience",
                    "path": NEW_EXPERIENCE_PATH,
                    "identity": "new",
                    "mutation": "create",
                },
                {
                    "kind": "question",
                    "path": EXISTING_QUESTION_PATH,
                    "identity": "existing",
                    "mutation": "modify",
                },
                {
                    "kind": "index",
                    "path": base.EXPERIENCE_INDEX_PATH,
                    "identity": "existing",
                    "mutation": "modify",
                },
            )
        ),
        "operations": [
            {
                "op": "create",
                "path": NEW_EXPERIENCE_PATH,
                "content": _new_experience_content(),
                "expectedContentHash": "absent",
                "expectedModifiedVersion": "missing",
            },
            {
                "op": "replace",
                "path": EXISTING_QUESTION_PATH,
                "find": EXISTING_QUESTION_CONTENT,
                "replacement": updated_question,
                "expectedContentHash": EXISTING_QUESTION_HASH,
                "expectedModifiedVersion": EXISTING_QUESTION_VERSION,
            },
            {
                "op": "append",
                "path": base.EXPERIENCE_INDEX_PATH,
                "content": "\n- [[distinct-event-20260718]]\n",
                "expectedContentHash": base.EXPERIENCE_INDEX_HASH,
                "expectedModifiedVersion": base.EXPERIENCE_INDEX_VERSION,
            },
        ],
    }
    script = (
        *_preamble(),
        base._agent_step(_catalog_call()),
        base._agent_step(
            _read_call(EXISTING_QUESTION_PATH, EXISTING_QUESTION_VERSION, EXISTING_QUESTION_HASH),
            _read_call(
                base.EXPERIENCE_INDEX_PATH,
                base.EXPERIENCE_INDEX_VERSION,
                base.EXPERIENCE_INDEX_HASH,
            ),
        ),
        base._agent_step(
            base._tool_call(
                "vault.changes.apply",
                batch,
                "This is a distinct source event; reuse the exact Question and add one occurrence.",
            ),
            requires_write_outcome=True,
        ),
        base._agent_step(final_response="已创建一篇独立面经，并给复用题目增加一次唯一出现。"),
    )
    _, adapter, _ = await _run_scenario(
        tmp_path,
        monkeypatch,
        script=script,
        expected_tool_result_counts=(0, 1, 2, 3, 5, 6),
        catalog=_catalog(question_candidates=(_question_candidate(),)),
        reads={
            EXISTING_QUESTION_PATH: (
                EXISTING_QUESTION_VERSION,
                EXISTING_QUESTION_HASH,
                EXISTING_QUESTION_CONTENT,
            )
        },
        turn_id="turn_distinct_shared_question",
    )

    apply_arguments = cast(dict[str, Any], adapter.started_calls[-1]["arguments"])
    operations = cast(list[dict[str, Any]], apply_arguments["operations"])
    question_operation = next(operation for operation in operations if operation["path"] == EXISTING_QUESTION_PATH)
    replacement = cast(str, question_operation["replacement"])
    occurrence_link = f"[[../{NEW_EXPERIENCE_PATH.removesuffix('.md')}]]"
    assert "frequency: 3" in replacement
    assert replacement.count(occurrence_link) == 1
    assert sum(operation["op"] == "create" for operation in operations) == 1
    assert not any(operation["path"] == base.QUESTION_INDEX_PATH for operation in operations)


@pytest.mark.asyncio
async def test_repeated_question_mentions_in_one_experience_do_not_increment_frequency(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    batch = {
        "batchId": "interview_same_event_repeated_mention_20260718",
        "task": "Merge same-event context without adding a duplicate Question occurrence",
        "changeKind": "interview_submission",
        "sourceBindings": [
            _source_binding(
                EXISTING_EXPERIENCE_PATH,
                EXISTING_EXPERIENCE_VERSION,
                EXISTING_EXPERIENCE_HASH,
            ),
            _source_binding(EXISTING_QUESTION_PATH, EXISTING_QUESTION_VERSION, SAME_EVENT_QUESTION_HASH),
        ],
        "interviewSubmission": _submission_metadata(
            (
                {
                    "kind": "experience",
                    "path": EXISTING_EXPERIENCE_PATH,
                    "identity": "existing",
                    "mutation": "modify",
                },
                {
                    "kind": "question",
                    "path": EXISTING_QUESTION_PATH,
                    "identity": "existing",
                    "mutation": "none",
                },
            )
        ),
        "operations": [
            {
                "op": "append",
                "path": EXISTING_EXPERIENCE_PATH,
                "content": "\n## Additional wording\n- 同一面试内再次提到事件循环题，不构成新的出现。\n",
                "expectedContentHash": EXISTING_EXPERIENCE_HASH,
                "expectedModifiedVersion": EXISTING_EXPERIENCE_VERSION,
            }
        ],
    }
    script = (
        *_preamble(),
        base._agent_step(_catalog_call()),
        base._agent_step(
            _read_call(EXISTING_EXPERIENCE_PATH, EXISTING_EXPERIENCE_VERSION, EXISTING_EXPERIENCE_HASH),
            _read_call(EXISTING_QUESTION_PATH, EXISTING_QUESTION_VERSION, SAME_EVENT_QUESTION_HASH),
        ),
        base._agent_step(
            base._tool_call(
                "vault.changes.apply",
                batch,
                "Merge only the additional Experience wording; the Question already links this event once.",
            ),
            requires_write_outcome=True,
        ),
        base._agent_step(final_response="已合并同一事件的补充表述；重复提题未增加题目频次。"),
    )
    _, adapter, _ = await _run_scenario(
        tmp_path,
        monkeypatch,
        script=script,
        expected_tool_result_counts=(0, 1, 2, 3, 5, 6),
        catalog=_catalog(
            experience_candidates=(_experience_candidate(exact_source_match=True),),
            question_candidates=(_question_candidate(content_hash=SAME_EVENT_QUESTION_HASH),),
        ),
        reads={
            EXISTING_EXPERIENCE_PATH: (
                EXISTING_EXPERIENCE_VERSION,
                EXISTING_EXPERIENCE_HASH,
                EXISTING_EXPERIENCE_CONTENT,
            ),
            EXISTING_QUESTION_PATH: (
                EXISTING_QUESTION_VERSION,
                SAME_EVENT_QUESTION_HASH,
                SAME_EVENT_QUESTION_CONTENT,
            ),
        },
        turn_id="turn_same_event_repeated_mention",
    )

    apply_arguments = cast(dict[str, Any], adapter.started_calls[-1]["arguments"])
    operations = cast(list[dict[str, Any]], apply_arguments["operations"])
    review_items = cast(list[dict[str, Any]], apply_arguments["interviewSubmission"]["reviewItems"])
    question_review = next(item for item in review_items if item["kind"] == "question")
    assert question_review == {
        "kind": "question",
        "path": EXISTING_QUESTION_PATH,
        "identity": "existing",
        "mutation": "none",
    }
    assert not any(operation["path"] == EXISTING_QUESTION_PATH for operation in operations)
    assert "frequency: 2" in SAME_EVENT_QUESTION_CONTENT
    assert SAME_EVENT_QUESTION_CONTENT.count(f"[[../{EXISTING_EXPERIENCE_PATH.removesuffix('.md')}]]") == 1


@pytest.mark.asyncio
async def test_semantic_question_ambiguity_asks_one_clarification_and_creates_no_apply(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = (
        *_preamble(),
        base._agent_step(_catalog_call()),
        base._agent_step(_read_call(EXISTING_QUESTION_PATH, EXISTING_QUESTION_VERSION, EXISTING_QUESTION_HASH)),
        base._agent_step(final_response="这次的“任务队列顺序”是同一道事件循环题的改写，还是新的独立问题？"),
    )
    _, adapter, assistant_text = await _run_scenario(
        tmp_path,
        monkeypatch,
        script=script,
        expected_tool_result_counts=(0, 1, 2, 3, 4),
        catalog=_catalog(question_candidates=(_question_candidate(),)),
        reads={
            EXISTING_QUESTION_PATH: (
                EXISTING_QUESTION_VERSION,
                EXISTING_QUESTION_HASH,
                EXISTING_QUESTION_CONTENT,
            )
        },
        turn_id="turn_semantic_question_ambiguity",
    )

    assert assistant_text.count("？") == 1
    assert assistant_text.endswith("？")
    assert not any(call["name"] == "vault.changes.apply" for call in adapter.started_calls)
