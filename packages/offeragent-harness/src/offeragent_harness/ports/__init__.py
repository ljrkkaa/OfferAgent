"""Dependency-inversion ports for the Windows-local Harness core."""

from .application_commands import ApplicationCommandContext, ApplicationCommandDispatcher
from .approvals import ApprovalObserver, ApprovalPort
from .artifacts import ArtifactMetadata, ArtifactState, ArtifactStore, Sensitivity, StreamingArtifactStore
from .cancellation import CancellationCodeLike, CancellationReasonLike, CancellationToken, OperationCancelled
from .capabilities import CapabilityAuditRecord, CapabilityAuditSink, NullCapabilityAuditSink
from .events import (
    EventIdConflict,
    EventIdempotencyConflict,
    EventSink,
    EventStore,
    EventStoreError,
    NewEvent,
    SequenceConflict,
    StoredEvent,
    TerminalEventConflict,
)
from .hooks import HookHandler, HookLayerSource, HookLifecyclePort
from .model import ModelGateway
from .network_audit import NetworkAuditSink
from .policy import PolicyEvaluator
from .processes import (
    ProcessArtifactBudget,
    ProcessArtifactReservation,
    ProcessLifecycleState,
    ProcessOutputEncoding,
    ProcessOwnerKind,
    ProcessStdinMode,
    ProcessSupervisor,
    SupervisedProcessRequest,
    SupervisedProcessResult,
)
from .secrets import SecretConsumer, SecretHandle, SecretInput, SecretKind, SecretMetadata, SecretResolver, SecretStore
from .storage import (
    EntityRecord,
    EntityRevisionConflict,
    EntityStore,
    InvocationJournal,
    InvocationJournalConflict,
    InvocationRecord,
    JournalState,
)
from .system import Clock, IdGenerator
from .tool import ToolExecutor, ToolLifecycleObserver, ToolObservabilitySink
from .unit_of_work import UnitOfWork, UnitOfWorkFactory
from .vault import VaultEntry, VaultEntryKind, VaultPort, VaultRead, VaultTransaction

__all__ = [
    "ApplicationCommandContext",
    "ApplicationCommandDispatcher",
    "ApprovalObserver",
    "ApprovalPort",
    "ArtifactMetadata",
    "ArtifactState",
    "ArtifactStore",
    "CancellationCodeLike",
    "CancellationReasonLike",
    "CancellationToken",
    "CapabilityAuditRecord",
    "CapabilityAuditSink",
    "Clock",
    "EntityRecord",
    "EntityRevisionConflict",
    "EntityStore",
    "EventIdConflict",
    "EventIdempotencyConflict",
    "EventSink",
    "EventStore",
    "EventStoreError",
    "HookHandler",
    "HookLayerSource",
    "HookLifecyclePort",
    "IdGenerator",
    "InvocationJournal",
    "InvocationJournalConflict",
    "InvocationRecord",
    "JournalState",
    "ModelGateway",
    "NetworkAuditSink",
    "NewEvent",
    "NullCapabilityAuditSink",
    "OperationCancelled",
    "PolicyEvaluator",
    "ProcessArtifactBudget",
    "ProcessArtifactReservation",
    "ProcessLifecycleState",
    "ProcessOutputEncoding",
    "ProcessOwnerKind",
    "ProcessStdinMode",
    "ProcessSupervisor",
    "SecretConsumer",
    "SecretHandle",
    "SecretInput",
    "SecretKind",
    "SecretMetadata",
    "SecretResolver",
    "SecretStore",
    "Sensitivity",
    "SequenceConflict",
    "StoredEvent",
    "StreamingArtifactStore",
    "SupervisedProcessRequest",
    "SupervisedProcessResult",
    "TerminalEventConflict",
    "ToolExecutor",
    "ToolLifecycleObserver",
    "ToolObservabilitySink",
    "UnitOfWork",
    "UnitOfWorkFactory",
    "VaultEntry",
    "VaultEntryKind",
    "VaultPort",
    "VaultRead",
    "VaultTransaction",
]
