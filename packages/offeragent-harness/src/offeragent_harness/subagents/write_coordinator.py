"""Central canonical resource locking for explicitly authorized child writes."""

from __future__ import annotations

import asyncio
import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import PurePosixPath

from offeragent_harness.ports import CancellationToken, EntityRecord, UnitOfWorkFactory
from offeragent_harness.sessions import AgentLineage

_HASH = re.compile(r"^(?:absent|sha256:[0-9a-f]{64})$")


class WriteCoordinationError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class WriteClaim:
    resource_ids: tuple[str, ...]
    expected_hashes: tuple[str, ...]
    idempotency_key: str
    approval_ref: str
    lineage: AgentLineage

    def __post_init__(self) -> None:
        if (
            not self.resource_ids
            or len(self.resource_ids) != len(self.expected_hashes)
            or len(self.resource_ids) > 256
            or not self.idempotency_key
            or not self.approval_ref
        ):
            raise ValueError("write claim identity/cardinality is invalid")
        if any(_HASH.fullmatch(value) is None for value in self.expected_hashes):
            raise ValueError("write claim expectedHash is invalid")


class WriteLease:
    def __init__(self, coordinator: WriteCoordinator, owner_run_id: str, resources: tuple[str, ...]) -> None:
        self._coordinator = coordinator
        self.owner_run_id = owner_run_id
        self.resources = resources
        self._released = False

    async def release(self) -> None:
        if self._released:
            return
        await self._coordinator._release(self.owner_run_id, self.resources)
        self._released = True

    async def __aenter__(self) -> WriteLease:
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        await self.release()


class WriteCoordinator:
    def __init__(self, unit_of_work: UnitOfWorkFactory) -> None:
        self._unit_of_work = unit_of_work
        self._owners: dict[str, str] = {}
        self._condition = asyncio.Condition()

    async def acquire(
        self,
        claim: WriteClaim,
        cancellation: CancellationToken,
        *,
        timeout_seconds: float = 30,
    ) -> WriteLease:
        if claim.lineage.depth < 1:
            raise WriteCoordinationError("root_claim", "WriteCoordinator child path requires Subagent lineage")
        expected_by_resource: dict[str, str] = {}
        for resource, expected_hash in zip(claim.resource_ids, claim.expected_hashes, strict=True):
            canonical = _canonical_resource(resource)
            previous = expected_by_resource.setdefault(canonical, expected_hash)
            if previous != expected_hash:
                raise WriteCoordinationError("resource_claim", "duplicate resource has conflicting expectedHash")
        resources = tuple(sorted(expected_by_resource))
        owner = claim.lineage.run_id
        await _wait_cancellable(
            self._condition,
            lambda: all(resource not in self._owners or self._owners[resource] == owner for resource in resources),
            cancellation,
            timeout_seconds,
        )
        async with self._condition:
            if any(resource in self._owners and self._owners[resource] != owner for resource in resources):
                raise WriteCoordinationError("write_race", "write resource ownership changed while acquiring")
            acquired: list[str] = []
            try:
                async with self._unit_of_work.begin() as uow:
                    for resource in resources:
                        entity_id = _resource_entity_id(resource)
                        existing = await uow.entities.get("subagent_write_locks", entity_id)
                        if existing is not None:
                            raise WriteCoordinationError("write_locked", "write resource is durably locked")
                        await uow.entities.put(
                            "subagent_write_locks",
                            entity_id,
                            {
                                "schemaVersion": 1,
                                "resourceId": resource,
                                "ownerRunId": owner,
                                "rootRunId": claim.lineage.root_run_id,
                                "idempotencyKey": claim.idempotency_key,
                                "approvalRef": claim.approval_ref,
                                "expectedHash": expected_by_resource[resource],
                            },
                            expected_revision=0,
                        )
                        acquired.append(resource)
                    await uow.commit()
            except BaseException:
                # UOW rollback owns durable cleanup.  No in-memory ownership was
                # published before the commit succeeded.
                raise
            for resource in acquired:
                self._owners[resource] = owner
        return WriteLease(self, owner, resources)

    async def _release(self, owner_run_id: str, resources: tuple[str, ...]) -> None:
        async with self._condition:
            async with self._unit_of_work.begin() as uow:
                for resource in resources:
                    if self._owners.get(resource) != owner_run_id:
                        raise WriteCoordinationError("write_owner", "write lease belongs to another Run")
                    value = await uow.entities.get("subagent_write_locks", _resource_entity_id(resource))
                    if not isinstance(value, dict) or value.get("ownerRunId") != owner_run_id:
                        raise WriteCoordinationError("write_owner", "durable write lease belongs to another Run")
                    await uow.entities.delete(
                        "subagent_write_locks",
                        _resource_entity_id(resource),
                        expected_revision=1,
                    )
                await uow.commit()
            for resource in resources:
                self._owners.pop(resource, None)
            self._condition.notify_all()

    async def cleanup_owner(self, owner_run_id: str) -> tuple[str, ...]:
        async with self._condition:
            records: list[EntityRecord] = []
            after: str | None = None
            while True:
                async with self._unit_of_work.begin() as uow:
                    page = await uow.entities.list("subagent_write_locks", after_id=after, limit=256)
                if not page:
                    break
                records.extend(
                    item
                    for item in page
                    if isinstance(item.value, dict) and item.value.get("ownerRunId") == owner_run_id
                )
                after = page[-1].entity_id
            if not records:
                return ()
            resources = tuple(sorted(str(item.value["resourceId"]) for item in records))
            async with self._unit_of_work.begin() as uow:
                for item in records:
                    await uow.entities.delete(
                        "subagent_write_locks",
                        item.entity_id,
                        expected_revision=item.revision,
                    )
                await uow.commit()
            for resource in resources:
                if self._owners.get(resource) == owner_run_id:
                    self._owners.pop(resource, None)
            self._condition.notify_all()
            return resources


def _canonical_resource(value: str) -> str:
    if not value or "\\" in value or "\x00" in value:
        raise WriteCoordinationError("resource_path", "write resource path is invalid")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise WriteCoordinationError("resource_path", "write resource path must be normalized and relative")
    return path.as_posix().casefold()


def _resource_entity_id(resource: str) -> str:
    return f"write-{hashlib.sha256(resource.encode()).hexdigest()}"


async def _wait_cancellable(
    condition: asyncio.Condition,
    predicate: Callable[[], bool],
    cancellation: CancellationToken,
    timeout: float,
) -> None:
    if timeout <= 0:
        raise ValueError("write wait arguments are invalid")

    async def wait() -> None:
        async with condition:
            await condition.wait_for(predicate)

    wait_task = asyncio.create_task(wait())
    cancel_task = asyncio.create_task(cancellation.wait())
    done, pending = await asyncio.wait(
        {wait_task, cancel_task},
        timeout=timeout,
        return_when=asyncio.FIRST_COMPLETED,
    )
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)
    if not done:
        raise WriteCoordinationError("write_timeout", "timed out waiting for write resources")
    if cancel_task in done:
        cancellation.checkpoint()
    wait_task.result()


__all__ = ["WriteClaim", "WriteCoordinationError", "WriteCoordinator", "WriteLease"]
