"""Explicit fault types used by deterministic conformance fakes."""

from offeragent_harness.ports.cancellation import CancellationReasonLike, OperationCancelled


class ScriptMismatch(AssertionError):
    pass


class ScriptNotExhausted(AssertionError):
    pass


class AcknowledgementLost(ConnectionError):
    """The side effect committed, but the caller did not receive its ACK."""


class FakeRunCancelled(OperationCancelled):
    """Cancellation signal used by ManualCancellationToken.

    It derives from ``BaseException`` like ``asyncio.CancelledError`` so generic
    error handlers cannot accidentally turn cancellation into a tool failure.
    """

    def __init__(self, reason: CancellationReasonLike) -> None:
        super().__init__(reason)


__all__ = ["AcknowledgementLost", "FakeRunCancelled", "ScriptMismatch", "ScriptNotExhausted"]
