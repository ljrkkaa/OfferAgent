"""Explicit async barriers for reproducible concurrency tests."""

from __future__ import annotations

import asyncio

from offeragent_harness.ports import CancellationToken


class ControlledBarrier:
    """A one-shot gate with observable arrival count.

    Tests release it explicitly; fake behavior never depends on prompt/tool-name
    keywords or timing sleeps.
    """

    def __init__(self, name: str) -> None:
        self.name = name
        self._released = asyncio.Event()
        self._condition = asyncio.Condition()
        self._arrivals = 0

    @property
    def arrivals(self) -> int:
        return self._arrivals

    @property
    def released(self) -> bool:
        return self._released.is_set()

    async def arrive_and_wait(self, cancellation: CancellationToken) -> None:
        cancellation.checkpoint()
        async with self._condition:
            self._arrivals += 1
            self._condition.notify_all()
        release_wait = asyncio.create_task(self._released.wait())
        cancel_wait = asyncio.create_task(cancellation.wait())
        try:
            done, _ = await asyncio.wait((release_wait, cancel_wait), return_when=asyncio.FIRST_COMPLETED)
            if cancel_wait in done:
                cancellation.checkpoint()
            await release_wait
            cancellation.checkpoint()
        finally:
            for task in (release_wait, cancel_wait):
                if not task.done():
                    task.cancel()
            await asyncio.gather(release_wait, cancel_wait, return_exceptions=True)

    async def wait_for_arrivals(self, count: int) -> None:
        if count < 1:
            raise ValueError("arrival target must be positive")
        async with self._condition:
            await self._condition.wait_for(lambda: self._arrivals >= count)

    def release(self) -> None:
        self._released.set()


__all__ = ["ControlledBarrier"]
