"""Deterministic conformance fakes; never infer behavior from prompt keywords."""

from .barrier import ControlledBarrier
from .cancellation import ManualCancellationCode, ManualCancellationReason, ManualCancellationToken
from .clock import DeterministicIdGenerator, ManualClock
from .errors import AcknowledgementLost, FakeRunCancelled, ScriptMismatch, ScriptNotExhausted
from .event_sink import RecordingEventSink
from .in_memory import InMemoryEventStore, InMemoryUnitOfWork, InMemoryUnitOfWorkFactory
from .network_audit import RecordingNetworkAuditSink
from .scripted_model import ModelScriptStep, ScriptedModelEvent, ScriptedModelGateway
from .scripted_tools import ScriptedToolExecutor, ToolScriptStep

__all__ = [
    "AcknowledgementLost",
    "ControlledBarrier",
    "DeterministicIdGenerator",
    "FakeRunCancelled",
    "InMemoryEventStore",
    "InMemoryUnitOfWork",
    "InMemoryUnitOfWorkFactory",
    "ManualCancellationCode",
    "ManualCancellationReason",
    "ManualCancellationToken",
    "ManualClock",
    "ModelScriptStep",
    "RecordingEventSink",
    "RecordingNetworkAuditSink",
    "ScriptMismatch",
    "ScriptNotExhausted",
    "ScriptedModelEvent",
    "ScriptedModelGateway",
    "ScriptedToolExecutor",
    "ToolScriptStep",
]
