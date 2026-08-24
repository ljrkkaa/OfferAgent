from offeragent_harness.ports import (
    CancellationToken,
    Clock,
    EventSink,
    EventStore,
    IdGenerator,
    ModelGateway,
    ToolExecutor,
    UnitOfWorkFactory,
)
from offeragent_harness.runtime.cancellation import CancellationScope
from offeragent_harness.testing import (
    DeterministicIdGenerator,
    InMemoryEventStore,
    InMemoryUnitOfWorkFactory,
    ManualClock,
    RecordingEventSink,
    ScriptedModelGateway,
    ScriptedToolExecutor,
)


def _accept_cancellation(_: CancellationToken) -> None: ...


def _accept_clock(_: Clock) -> None: ...


def _accept_ids(_: IdGenerator) -> None: ...


def _accept_events(_: EventStore) -> None: ...


def _accept_uow(_: UnitOfWorkFactory) -> None: ...


def _accept_sink(_: EventSink) -> None: ...


def _accept_model(_: ModelGateway) -> None: ...


def _accept_tool(_: ToolExecutor) -> None: ...


def test_runtime_and_fakes_structurally_implement_the_phase_zero_ports() -> None:
    cancellation = CancellationScope()
    clock = ManualClock()
    ids = DeterministicIdGenerator()
    events = InMemoryEventStore()
    uow = InMemoryUnitOfWorkFactory()
    sink = RecordingEventSink()
    model = ScriptedModelGateway(())
    tool = ScriptedToolExecutor(())
    _accept_cancellation(cancellation)
    _accept_clock(clock)
    _accept_ids(ids)
    _accept_events(events)
    _accept_uow(uow)
    _accept_sink(sink)
    _accept_model(model)
    _accept_tool(tool)
    assert isinstance(cancellation, CancellationToken)
    assert isinstance(clock, Clock)
    assert isinstance(ids, IdGenerator)
    assert isinstance(events, EventStore)
    assert isinstance(uow, UnitOfWorkFactory)
    assert isinstance(sink, EventSink)
    assert isinstance(model, ModelGateway)
    assert isinstance(tool, ToolExecutor)
