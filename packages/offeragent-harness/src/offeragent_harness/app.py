from __future__ import annotations

from dataclasses import dataclass

from offeragent_harness import __version__
from offeragent_harness.ports import Clock, EventSink, IdGenerator, UnitOfWorkFactory
from offeragent_harness.protocol.schemas import PROTOCOL_VERSION, schema_hash
from offeragent_harness.runtime import TurnManager
from offeragent_harness.runtime.harness_service import HarnessService, RunComponentsFactory


@dataclass(frozen=True, slots=True)
class ApplicationIdentity:
    runtime_version: str
    core_version: str
    protocol_version: str
    schema_hash: str


@dataclass(slots=True)
class HarnessApplication:
    """Composition-root product shared by every local transport adapter."""

    identity: ApplicationIdentity
    harness: HarnessService


def create_application(
    *,
    unit_of_work: UnitOfWorkFactory,
    event_sink: EventSink,
    clock: Clock,
    ids: IdGenerator,
    components: RunComponentsFactory,
    turn_manager: TurnManager | None = None,
) -> HarnessApplication:
    identity = ApplicationIdentity(
        runtime_version=__version__,
        core_version=__version__,
        protocol_version=PROTOCOL_VERSION,
        schema_hash=schema_hash(),
    )
    harness = HarnessService(
        unit_of_work=unit_of_work,
        event_sink=event_sink,
        clock=clock,
        ids=ids,
        components=components,
        turn_manager=turn_manager,
    )
    return HarnessApplication(identity=identity, harness=harness)


__all__ = ["ApplicationIdentity", "HarnessApplication", "create_application"]
