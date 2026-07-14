"""Obsidian-bound proof plus the one Worker-owned Vault transaction executor."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import timedelta

from offeragent_harness.ports import (
    CancellationToken,
    ClientToolCommitObservation,
    ClientToolInvocation,
    ClientToolPreview,
    ClientToolPreviewLeasePort,
    ClientToolPreviewPort,
    Clock,
)
from offeragent_harness.tools import (
    ExecutorLocation,
    PreflightConflict,
    PreflightEvidence,
    ToolCall,
    ToolDefinition,
    ToolError,
    ToolResult,
    ToolResultStatus,
    canonical_json_sha256,
)
from offeragent_harness.tools.dispatcher import ToolDispatcher
from offeragent_harness.vault import VaultTransactionCoordinator
from offeragent_harness.vault.schema import ABSENT_HASH, VAULT_TRANSACTION_PREFLIGHT_PROVIDER


@dataclass(frozen=True, slots=True)
class _ClientBinding:
    evidence_token: str
    proof_hash: str


class ClientBoundVaultTransaction:
    """Require exact Obsidian live-state proof, then mutate only through Worker CAS.

    The plugin is deliberately a read-only authority for active editor state.
    A touched open or unsaved editor fails closed because Obsidian exposes no
    conditional, crash-durable editor commit primitive.  Once the client proves
    that every touched path is closed and matches the Worker's physical plan,
    the existing :class:`VaultTransactionCoordinator` performs the one real
    filesystem transaction.  It retains rollback authority until the same
    Pipe observes closed editors and exact after-hashes after CAS verification.
    """

    provider_id = VAULT_TRANSACTION_PREFLIGHT_PROVIDER

    def __init__(
        self,
        *,
        workspace_id: str,
        client: ClientToolPreviewLeasePort,
        transaction: VaultTransactionCoordinator,
        clock: Clock,
    ) -> None:
        if not workspace_id:
            raise ValueError("Client-bound Vault transaction requires a Workspace")
        if transaction.provider_id != self.provider_id:
            raise ValueError("Client-bound Vault transaction provider identity mismatch")
        self._workspace_id = workspace_id
        self._client = client
        self._transaction = transaction
        self._clock = clock
        self._bindings: dict[tuple[str, str, str], _ClientBinding] = {}
        self._binding_lock = asyncio.Lock()

    async def prepare(
        self,
        definition: ToolDefinition,
        call: ToolCall,
        cancellation: CancellationToken,
    ) -> PreflightEvidence:
        self._validate_route(definition, call)
        preview = await self._preview(self._client, self._invocation(definition, call), cancellation)
        self._require_closed_editors(preview)
        evidence = await self._transaction.prepare(definition, call, cancellation)
        try:
            self._match_worker_plan(evidence, preview)
        except BaseException:
            await self._discard_plan(definition, call, evidence)
            raise
        proof_hash = self._proof_hash(preview)
        facts = dict(evidence.facts)
        facts.update(
            {
                "executionAuthority": "worker-local-client-bound",
                "clientProofHash": proof_hash,
                "clientStateHash": preview.state_hash,
                "clientAfterStateHash": preview.after_state_hash,
                "clientDiffSha256": preview.diff_sha256,
                "hasUnsavedEditors": preview.has_unsaved_editors,
                "hasOpenEditors": preview.has_open_editors,
            }
        )
        bound = replace(evidence, facts=facts)
        async with self._binding_lock:
            self._bindings[self._key(call)] = _ClientBinding(evidence.token, proof_hash)
        return bound

    async def revalidate(
        self,
        definition: ToolDefinition,
        call: ToolCall,
        evidence: PreflightEvidence,
        cancellation: CancellationToken,
    ) -> None:
        self._validate_route(definition, call)
        binding = await self._binding(call, evidence)
        await self._transaction.revalidate(definition, call, evidence, cancellation)
        current = await self._preview(self._client, self._invocation(definition, call), cancellation)
        self._require_closed_editors(current)
        self._match_worker_plan(evidence, current)
        observed = self._proof_hash(current)
        if observed != binding.proof_hash:
            raise PreflightConflict(
                "Obsidian live Vault proof changed after approval",
                details={"expectedClientProofHash": binding.proof_hash, "observedClientProofHash": observed},
            )

    async def execute(self, call: ToolCall, cancellation: CancellationToken) -> ToolResult:
        """Perform a final live connection/state check immediately before CAS."""

        key = self._key(call)
        async with self._binding_lock:
            binding = self._bindings.get(key)
        if binding is None:
            return self._result(
                call,
                ToolResultStatus.FAILED,
                "client_bound_plan_missing",
                "Client-bound Vault transaction has no approved plan",
            )
        definition = self._local_definition_for(call)
        try:
            async with self._client.hold_connection() as leased_client:
                invocation = self._invocation(definition, call)
                preview = await self._preview(leased_client, invocation, cancellation)
                self._require_closed_editors(preview)
                async with self._binding_lock:
                    current = self._bindings.get(key)
                if current != binding or self._proof_hash(preview) != binding.proof_hash:
                    raise PreflightConflict("Obsidian live Vault proof changed immediately before execution")

                async def validate_commit(after_hashes: Mapping[str, str]) -> None:
                    observation = await leased_client.observe_commit(
                        invocation,
                        tuple(preview.paths),
                        cancellation,
                    )
                    self._match_committed_state(after_hashes, observation)

                # The same authenticated connection remains leased while the
                # coordinator holds rollback handles.  Disconnect, reopened
                # editors, or hash drift at the post-CAS observation therefore
                # rolls back before cleanup can make the commit final.
                return await self._transaction.execute_with_commit_validation(
                    call,
                    cancellation,
                    validate_commit,
                )
        except PreflightConflict as error:
            return self._result(call, ToolResultStatus.CONFLICTED, "client_live_state_changed", str(error))
        except Exception as error:
            return self._result(
                call,
                ToolResultStatus.FAILED,
                "client_live_state_unavailable",
                f"Obsidian live state is unavailable: {type(error).__name__}: {error}",
            )
        raise AssertionError("client-bound transaction lease exited without a result")

    async def complete(
        self,
        definition: ToolDefinition,
        call: ToolCall,
        evidence: PreflightEvidence,
        result: ToolResult,
    ) -> None:
        try:
            await self._transaction.complete(definition, call, evidence, result)
        finally:
            async with self._binding_lock:
                current = self._bindings.get(self._key(call))
                if current is not None and current.evidence_token == evidence.token:
                    self._bindings.pop(self._key(call), None)

    async def _preview(
        self,
        client: ClientToolPreviewPort,
        invocation: ClientToolInvocation,
        cancellation: CancellationToken,
    ) -> ClientToolPreview:
        preview = await client.preview(invocation, cancellation)
        if preview.invocation_id != invocation.invocation_id or preview.tool_call_id != invocation.call.tool_call_id:
            raise ValueError("Client Vault preview returned mismatched invocation identity")
        if f"sha256:{hashlib.sha256(preview.diff).hexdigest()}" != preview.diff_sha256:
            raise ValueError("Client Vault preview diff hash mismatch")
        return preview

    def _invocation(self, definition: ToolDefinition, call: ToolCall) -> ClientToolInvocation:
        deadline = call.deadline or self._clock.utcnow() + timedelta(milliseconds=definition.timeout_ms)
        return ClientToolInvocation(ToolDispatcher.client_invocation_id(call), call, deadline)

    @staticmethod
    def _match_committed_state(
        expected_after_hashes: Mapping[str, str],
        observation: ClientToolCommitObservation,
    ) -> None:
        if observation.has_open_editors or observation.has_unsaved_editors:
            affected = [item.path for item in observation.path_states if item.open_editor or item.unsaved_editor]
            raise PreflightConflict(
                "Touched Vault paths opened or became unsaved during commit; the transaction was rolled back",
                details={
                    "reason": "obsidian_editor_changed_during_commit",
                    "paths": affected,
                    "hasUnsavedEditors": observation.has_unsaved_editors,
                },
            )
        observed = {item.path: item.observed_hash for item in observation.path_states}
        if dict(expected_after_hashes) != observed:
            raise PreflightConflict(
                "Obsidian committed-path observation differs from the Worker after-state",
                details={
                    "reason": "obsidian_post_commit_hash_mismatch",
                    "expectedAfterHashes": dict(expected_after_hashes),
                    "observedAfterHashes": observed,
                },
            )

    async def _binding(self, call: ToolCall, evidence: PreflightEvidence) -> _ClientBinding:
        async with self._binding_lock:
            value = self._bindings.get(self._key(call))
        proof = evidence.facts.get("clientProofHash")
        if (
            value is None
            or value.evidence_token != evidence.token
            or not isinstance(proof, str)
            or proof != value.proof_hash
        ):
            raise PreflightConflict("Client-bound Vault proof is missing or changed")
        return value

    @staticmethod
    def _require_closed_editors(preview: ClientToolPreview) -> None:
        if not preview.has_open_editors and not preview.has_unsaved_editors:
            return
        affected = [item.path for item in preview.path_states if item.open_editor or item.unsaved_editor]
        raise PreflightConflict(
            "Touched Vault paths are open in Obsidian; save and close them before approving this transaction",
            details={
                "reason": "obsidian_editor_must_be_closed",
                "paths": affected,
                "hasUnsavedEditors": preview.has_unsaved_editors,
            },
        )

    @staticmethod
    def _match_worker_plan(evidence: PreflightEvidence, preview: ClientToolPreview) -> None:
        expected_paths = ClientBoundVaultTransaction._string_list(evidence.facts.get("paths"), "paths")
        before = ClientBoundVaultTransaction._hash_map(evidence.facts.get("beforeHashes"), "beforeHashes")
        after = ClientBoundVaultTransaction._hash_map(evidence.facts.get("afterHashes"), "afterHashes")
        client_before = {item.path: item.before_hash for item in preview.path_states}
        client_after = {item.path: item.after_hash for item in preview.path_states}
        if (
            set(expected_paths) != set(preview.paths)
            or set(expected_paths) != set(client_before)
            or before != client_before
            or after != client_after
        ):
            raise PreflightConflict(
                "Obsidian live proof differs from the Worker Vault transaction plan",
                details={
                    "expectedPaths": expected_paths,
                    "observedPaths": list(preview.paths),
                    "expectedBeforeHashes": before,
                    "observedBeforeHashes": client_before,
                    "expectedAfterHashes": after,
                    "observedAfterHashes": client_after,
                },
            )

    @staticmethod
    def _proof_hash(preview: ClientToolPreview) -> str:
        return canonical_json_sha256(
            {
                "invocationId": preview.invocation_id,
                "toolCallId": preview.tool_call_id,
                "stateHash": preview.state_hash,
                "afterStateHash": preview.after_state_hash,
                "diffSha256": preview.diff_sha256,
                "paths": list(preview.paths),
                "pathStates": [
                    {
                        "path": item.path,
                        "beforeHash": item.before_hash,
                        "afterHash": item.after_hash,
                        "unsavedEditor": item.unsaved_editor,
                        "openEditor": item.open_editor,
                    }
                    for item in preview.path_states
                ],
            }
        )

    @staticmethod
    def _string_list(value: object, name: str) -> list[str]:
        if not isinstance(value, (list, tuple)) or not value or any(not isinstance(item, str) for item in value):
            raise ValueError(f"Client-bound Vault evidence {name} is corrupt")
        return list(value)

    @staticmethod
    def _hash_map(value: object, name: str) -> dict[str, str]:
        if not isinstance(value, Mapping) or not value:
            raise ValueError(f"Client-bound Vault evidence {name} is corrupt")
        result: dict[str, str] = {}
        for key, item in value.items():
            if not isinstance(key, str) or not isinstance(item, str):
                raise ValueError(f"Client-bound Vault evidence {name} is corrupt")
            if item != ABSENT_HASH and not (item.startswith("sha256:") and len(item) == 71):
                raise ValueError(f"Client-bound Vault evidence {name} has an invalid hash")
            result[key] = item
        return result

    async def _discard_plan(
        self,
        definition: ToolDefinition,
        call: ToolCall,
        evidence: PreflightEvidence,
    ) -> None:
        await self._transaction.complete(
            definition,
            call,
            evidence,
            self._result(call, ToolResultStatus.CONFLICTED, "client_worker_plan_mismatch", "preflight mismatch"),
        )

    def _validate_route(self, definition: ToolDefinition, call: ToolCall) -> None:
        if (
            call.workspace_id != self._workspace_id
            or definition.name != "vault.transaction"
            or call.name != definition.name
            or call.version != definition.version
            or definition.executor_location is not ExecutorLocation.LOCAL
            or definition.preflight_provider != self.provider_id
        ):
            raise ValueError("Client-bound Vault transaction received a foreign or non-local route")

    @staticmethod
    def _key(call: ToolCall) -> tuple[str, str, str]:
        return call.run_id, call.tool_call_id, call.args_hash

    @staticmethod
    def _local_definition_for(call: ToolCall) -> ToolDefinition:
        # The exact persisted fingerprint is checked by Tool Kernel before this
        # executor is called.  This definition is used only to derive the reverse
        # preview deadline/name; it never re-resolves model-facing capabilities.
        from offeragent_harness.vault import vault_transaction_definition

        definition = vault_transaction_definition(executor_location=ExecutorLocation.LOCAL)
        if (definition.name, definition.version, definition.fingerprint) != (
            call.name,
            call.version,
            call.definition_fingerprint,
        ):
            raise ValueError("Client-bound Vault definition fingerprint changed")
        return definition

    @staticmethod
    def _result(call: ToolCall, status: ToolResultStatus, code: str, message: str) -> ToolResult:
        return ToolResult(
            tool_call_id=call.tool_call_id,
            status=status,
            data=None,
            user_visible_summary=message,
            artifact_ids=(),
            source_refs=(),
            side_effects=(),
            retryable=False,
            before_state=None,
            after_state=None,
            error=ToolError(code, message, False, False, {}),
        )


# Transitional import compatibility for tests/extensions; production composition
# binds the explicit name below and no longer uses a CLIENT executor definition.
ClientVaultTransactionPreflightProvider = ClientBoundVaultTransaction


__all__ = ["ClientBoundVaultTransaction", "ClientVaultTransactionPreflightProvider"]
