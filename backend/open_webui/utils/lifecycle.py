"""Application lifespan cleanup helpers.

Long-running workers must finish their cancellation cleanup before shared
clients are closed.  Otherwise a worker can wake during shutdown and try to
use a session that has already been torn down.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

log = logging.getLogger(__name__)


_LIFESPAN_TASK_NAMES = (
    'redis_task_command_listener',
    'periodic_usage_pool_cleanup',
    'periodic_session_pool_cleanup',
    'scheduler_worker_loop',
)


async def shutdown_lifespan_resources(
    app,
    close_shared_session: Callable[[], Awaitable[None]],
) -> None:
    """Cancel and join lifespan workers, then close the shared client session."""

    named_tasks = []
    seen = set()
    for name in _LIFESPAN_TASK_NAMES:
        task = getattr(app.state, name, None)
        if task is None or id(task) in seen:
            continue
        seen.add(id(task))
        named_tasks.append((name, task))

    for _, task in named_tasks:
        if not task.done():
            task.cancel()

    if named_tasks:
        results = await asyncio.gather(
            *(task for _, task in named_tasks),
            return_exceptions=True,
        )
        for (name, _), result in zip(named_tasks, results):
            if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
                log.error(
                    'Lifespan background task %s failed during shutdown (%s)',
                    name,
                    type(result).__name__,
                )

    await close_shared_session()
