"""Dependency-inversion ports for the Windows-local Harness core."""

from .approvals import ApprovalPort
from .artifacts import ArtifactMetadata, ArtifactState, ArtifactStore, Sensitivity
from .cancellation import CancellationCodeLike, CancellationReasonLike, CancellationToken, OperationCancelled
from .client_tools import ClientToolInvocation, ClientToolPort
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
from .knowledge import KnowledgeHit, KnowledgeQuery, KnowledgeSource
from .model import ModelGateway
from .policy import PolicyEvaluator
from .sessions import ConversationStore
from .storage import (
    EntityRevisionConflict,
    EntityStore,
    InvocationJournal,
    InvocationJournalConflict,
    InvocationRecord,
    JournalState,
)
from .system import Clock, IdGenerator
from .tool import ToolExecutor
from .unit_of_work import UnitOfWork, UnitOfWorkFactory
from .vault import VaultEntry, VaultEntryKind, VaultPort, VaultRead, VaultTransaction

__all__ = [
    "ApprovalPort",
    "ArtifactMetadata",
    "ArtifactState",
    "ArtifactStore",
    "CancellationCodeLike",
    "CancellationReasonLike",
    "CancellationToken",
    "ClientToolInvocation",
    "ClientToolPort",
    "Clock",
    "ConversationStore",
    "EntityRevisionConflict",
    "EntityStore",
    "EventIdConflict",
    "EventIdempotencyConflict",
    "EventSink",
    "EventStore",
    "EventStoreError",
    "IdGenerator",
    "InvocationJournal",
    "InvocationJournalConflict",
    "InvocationRecord",
    "JournalState",
    "KnowledgeHit",
    "KnowledgeQuery",
    "KnowledgeSource",
    "ModelGateway",
    "NewEvent",
    "OperationCancelled",
    "PolicyEvaluator",
    "Sensitivity",
    "SequenceConflict",
    "StoredEvent",
    "TerminalEventConflict",
    "ToolExecutor",
    "UnitOfWork",
    "UnitOfWorkFactory",
    "VaultEntry",
    "VaultEntryKind",
    "VaultPort",
    "VaultRead",
    "VaultTransaction",
]
