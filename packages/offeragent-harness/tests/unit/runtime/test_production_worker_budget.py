from __future__ import annotations

from offeragent_harness.config import HarnessConfig
from offeragent_harness.protocol.common import BudgetSnapshot, RunConfigSnapshot
from offeragent_harness.runtime.production_worker_composition import _run_budget


def test_default_root_budget_covers_bounded_cumulative_multimodal_attempts() -> None:
    budget = _run_budget(
        RunConfigSnapshot(model="gpt-5.5"),
        HarnessConfig(),
        worker_max_parallel_reads=4,
    )

    assert budget.max_input_tokens == 800_000


def test_explicit_root_input_budget_remains_authoritative() -> None:
    budget = _run_budget(
        RunConfigSnapshot(
            model="gpt-5.5",
            budgets=BudgetSnapshot(
                max_model_rounds=8,
                max_tool_calls=16,
                max_parallel_reads=2,
                max_wall_time_ms=600_000,
                max_input_tokens=650_000,
                max_output_tokens=32_000,
                max_cost_micros=0,
                max_artifact_bytes=8 * 1_024 * 1_024,
            ),
        ),
        HarnessConfig(),
        worker_max_parallel_reads=4,
    )

    assert budget.max_input_tokens == 650_000
