"""Application runtime primitives shared by Host/Worker adapters."""

from .cancellation import CancellationReason, CancellationScope, RunCancelled
from .lifecycle import LifecycleMachine, LifecycleSnapshot, WorkerLifecycle
from .turn_manager import ActiveRun, SessionRunConflict, TurnManager

__all__ = [
    "ActiveRun",
    "CancellationReason",
    "CancellationScope",
    "LifecycleMachine",
    "LifecycleSnapshot",
    "RunCancelled",
    "SessionRunConflict",
    "TurnManager",
    "WorkerLifecycle",
]
