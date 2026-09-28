"""Bounded, ordered image reads; callers keep authenticated writes sequential."""

import asyncio
from collections import deque
from contextlib import asynccontextmanager


@asynccontextmanager
async def prefetch_images(items, load):
    """Read at most two images ahead, preserving order and draining on exit.

    Only reads belong in ``load``. Uploads and chat associations remain with
    the consumer. No cache is shared between requests or users.
    """
    pending = deque()
    source = iter(items)

    def fill():
        while len(pending) < 2:
            try:
                item = next(source)
            except StopIteration:
                break
            pending.append(asyncio.create_task(load(item)))

    async def results():
        fill()
        while pending:
            # Keep the task tracked during the await so cancellation drains it.
            result = await pending[0]
            pending.popleft()
            yield result
            # Refill only when the consumer requests another image. At most
            # two reads are prefetched, plus any value the consumer retains.
            result = None
            fill()

    iterator = results()
    try:
        yield iterator
    finally:
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        await iterator.aclose()
