from __future__ import annotations

import argparse
import asyncio
import os
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from offeragent_harness.adapters.local_artifacts import LocalArtifactStore
from offeragent_harness.adapters.sqlite_stores import SqliteInvocationJournal
from offeragent_harness.agent import BudgetLedger, RunBudget
from offeragent_harness.sessions import AgentLineage
from offeragent_harness.testing import ManualCancellationToken, ManualClock
from offeragent_harness.tools import ToolCall, ToolDefinition, canonical_json_sha256
from offeragent_harness.tools.recovery_contract import invocation_journal_scope, invocation_request_fingerprint
from offeragent_harness.vault import (
    ABSENT_HASH,
    VaultTransactionCoordinator,
    content_hash,
    vault_transaction_definition,
)

NOW = datetime(2026, 7, 13, tzinfo=timezone.utc)
CRASH_EXIT = 73
NO_BARRIER_EXIT = 74


def _budget() -> BudgetLedger:
    return BudgetLedger(
        RunBudget(8, 8, 2, 60, 10_000, 10_000, Decimal("1"), 20 * 1024 * 1024, 2, 1),
        started_at=NOW,
    )


def _call(vault: Path, mode: str) -> tuple[ToolDefinition, ToolCall]:
    definition = vault_transaction_definition()
    before = (vault / "note.md").read_bytes()
    if mode == "append":
        arguments: dict[str, object] = {
            "operations": [
                {
                    "op": "append",
                    "path": "note.md",
                    "content": "AFTER_PAYLOAD\n",
                    "expectedHash": content_hash(before),
                }
            ]
        }
    elif mode == "replace":
        arguments = {
            "operations": [
                {
                    "op": "replace",
                    "path": "note.md",
                    "find": "BEFORE_PAYLOAD",
                    "replace": "REPLACED_PAYLOAD",
                    "expectedHash": content_hash(before),
                }
            ]
        }
    elif mode == "patch":
        arguments = {
            "operations": [
                {
                    "op": "patch",
                    "path": "note.md",
                    "edits": [
                        {
                            "startLine": 1,
                            "endLine": 1,
                            "replacement": "PATCHED_PAYLOAD\n",
                        }
                    ],
                    "expectedHash": content_hash(before),
                }
            ]
        }
    elif mode == "create":
        arguments = {
            "operations": [
                {
                    "op": "create",
                    "path": "created.md",
                    "content": "CREATED_PAYLOAD\n",
                    "expectedHash": ABSENT_HASH,
                }
            ]
        }
    else:
        raise ValueError(f"unsupported crash-worker mode: {mode}")
    call = ToolCall(
        tool_call_id="call_durable",
        run_id="run_durable",
        workspace_id="ws_durable",
        name=definition.name,
        version=definition.version,
        arguments=arguments,
        args_hash=canonical_json_sha256(arguments),
        idempotency_key="idem_durable",
        deadline=None,
        lineage=AgentLineage.root("run_durable"),
        definition_fingerprint=definition.fingerprint,
        result_sensitivity=definition.result_sensitivity,
    )
    return definition, call


def _coordinator(root: Path, crash_stage: str) -> VaultTransactionCoordinator:
    vault = root / "vault"
    state = root / "state"

    def barrier(stage: str, relative_path: str) -> None:
        del relative_path
        if stage == crash_stage:
            os._exit(CRASH_EXIT)

    return VaultTransactionCoordinator(
        workspace_id="ws_durable",
        vault_root=vault,
        artifacts=LocalArtifactStore(state / "artifacts", workspace_id="ws_durable"),
        artifact_budget=_budget(),
        clock=ManualClock(NOW),
        manifest_directory=state / "vault-transactions",
        journal=SqliteInvocationJournal(state / "state.sqlite"),
        cas_barrier=barrier,
    )


async def _execute(root: Path, stage: str, mode: str) -> None:
    coordinator = _coordinator(root, stage)
    definition, call = _call(root / "vault", mode)
    cancellation = ManualCancellationToken()
    evidence = await coordinator.prepare(definition, call, cancellation)
    await coordinator.revalidate(definition, call, evidence, cancellation)
    journal = SqliteInvocationJournal(root / "state" / "state.sqlite")
    await journal.start(
        invocation_journal_scope(call, definition),
        call.idempotency_key,
        invocation_request_fingerprint(call),
        NOW,
    )
    await coordinator.execute(call, cancellation)
    raise SystemExit(NO_BARRIER_EXIT)


async def _recover(root: Path, stage: str) -> None:
    coordinator = _coordinator(root, stage)
    await coordinator.recover_after_restart()
    if stage != "none":
        raise SystemExit(NO_BARRIER_EXIT)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("execute", "recover"))
    parser.add_argument("root", type=Path)
    parser.add_argument("stage")
    parser.add_argument(
        "--mode",
        choices=("append", "replace", "patch", "create"),
        default="append",
    )
    arguments = parser.parse_args()
    if arguments.action == "execute":
        asyncio.run(_execute(arguments.root, arguments.stage, arguments.mode))
    else:
        asyncio.run(_recover(arguments.root, arguments.stage))


if __name__ == "__main__":
    main()
