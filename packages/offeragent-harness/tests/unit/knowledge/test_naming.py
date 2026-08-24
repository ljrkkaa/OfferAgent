from __future__ import annotations

from offeragent_harness.knowledge import readable_slug, stable_readable_id, unique_readable_slugs


def test_readable_names_preserve_meaning_and_are_deterministic() -> None:
    assert readable_slug("Attention Is All You Need") == "attention-is-all-you-need"
    first = stable_readable_id("src", "Attention Is All You Need", namespace="ws:raw/attention.pdf")
    second = stable_readable_id("src", "Attention Is All You Need", namespace="ws:raw/attention.pdf")
    assert first == second
    assert first.startswith("src-attention-is-all-you-need-")


def test_unique_slug_allocator_adds_suffix_only_for_real_collisions() -> None:
    allocated = unique_readable_slugs(
        (
            ("source-a", "ReAct"),
            ("source-b", "ReAct"),
            ("source-c", "Toolformer"),
        )
    )

    assert allocated["source-c"] == "toolformer"
    assert allocated["source-a"].startswith("react-")
    assert allocated["source-b"].startswith("react-")
    assert allocated["source-a"] != allocated["source-b"]
