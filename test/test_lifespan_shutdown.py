"""Regression checks for scheduler cancellation and lifespan shutdown ordering."""

import ast
import asyncio
import logging
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
from sqlalchemy.exc import ProgrammingError


ROOT = Path(__file__).resolve().parents[1]
AUTOMATIONS_SOURCE = ROOT / 'backend/open_webui/utils/automations.py'
MAIN_SOURCE = ROOT / 'backend/open_webui/main.py'


def _scheduler_namespace():
    """Compile the real scheduler functions without importing the full web app."""

    tree = ast.parse(AUTOMATIONS_SOURCE.read_text('utf-8'))
    names = {'_propagate_scheduler_cancellation', 'scheduler_worker_loop'}
    functions = [
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names
    ]
    module = ast.Module(
        body=[
            ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0),
            *functions,
        ],
        type_ignores=[],
    )
    namespace = {
        'asyncio': asyncio,
        'log': logging.getLogger(__name__),
        'random': SimpleNamespace(uniform=lambda *_: 0),
        'time': SimpleNamespace(monotonic=lambda: 0, time_ns=lambda: 1),
        'TIMER_POLL_INTERVAL': 3600,
        'SCHEDULER_POLL_INTERVAL': 3600,
        'execute_automation': lambda *_: None,
        '_check_calendar_alerts': lambda *_: None,
    }
    exec(compile(ast.fix_missing_locations(module), str(AUTOMATIONS_SOURCE), 'exec'), namespace)
    return namespace


@pytest.mark.asyncio
async def test_scheduler_does_not_swallow_cancellation_converted_to_programming_error(monkeypatch):
    namespace = _scheduler_namespace()
    claim_started = asyncio.Event()
    cleanup_converted_cancel = asyncio.Event()

    @asynccontextmanager
    async def converted_db_cleanup():
        try:
            yield object()
        except asyncio.CancelledError as cancelled:
            cleanup_converted_cancel.set()
            raise ProgrammingError(
                'synthetic scheduler query',
                {},
                RuntimeError('synthetic closed database operation'),
            ) from cancelled

    class Config:
        @staticmethod
        async def get(key):
            return key == 'automations.enable'

    class Automations:
        @staticmethod
        async def claim_due(*_args, **_kwargs):
            claim_started.set()
            await asyncio.Event().wait()

    timers = ModuleType('open_webui.utils.timers')
    timers.claim_due_timers = lambda *_args, **_kwargs: ()
    timers.execute_due_timer = lambda *_args, **_kwargs: None
    monkeypatch.setitem(sys.modules, 'open_webui.utils.timers', timers)
    namespace.update(Config=Config, Automations=Automations, get_async_db=converted_db_cleanup)

    task = asyncio.create_task(namespace['scheduler_worker_loop'](SimpleNamespace()))
    try:
        await asyncio.wait_for(claim_started.wait(), 1)
        task.cancel()
        await asyncio.wait_for(cleanup_converted_cancel.wait(), 1)
        await asyncio.sleep(0)
        assert task.done(), 'the scheduler swallowed the converted cancellation and kept polling'
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_lifespan_waits_for_workers_before_closing_shared_session():
    backend = ROOT / 'backend'
    sys.path.insert(0, str(backend))
    try:
        from open_webui.utils.lifecycle import shutdown_lifespan_resources
    finally:
        sys.path.remove(str(backend))

    cleanup_started = set()
    cleanup_finished = set()
    release_cleanup = asyncio.Event()

    async def worker(name):
        try:
            await asyncio.Event().wait()
        finally:
            cleanup_started.add(name)
            await release_cleanup.wait()
            cleanup_finished.add(name)

    names = (
        'redis_task_command_listener',
        'periodic_usage_pool_cleanup',
        'periodic_session_pool_cleanup',
        'scheduler_worker_loop',
    )
    state = SimpleNamespace(**{name: asyncio.create_task(worker(name)) for name in names})
    close_called = asyncio.Event()

    async def close_shared_session():
        assert cleanup_finished == set(names)
        assert all(getattr(state, name).done() for name in names)
        close_called.set()

    shutdown = asyncio.create_task(
        shutdown_lifespan_resources(SimpleNamespace(state=state), close_shared_session)
    )
    try:
        for _ in range(10):
            if cleanup_started == set(names):
                break
            await asyncio.sleep(0)
        assert cleanup_started == set(names)
        assert not close_called.is_set()
        release_cleanup.set()
        await asyncio.wait_for(shutdown, 1)
        assert close_called.is_set()
    finally:
        release_cleanup.set()
        if not shutdown.done():
            shutdown.cancel()
        await asyncio.gather(shutdown, *[getattr(state, name) for name in names], return_exceptions=True)


def test_main_lifespan_delegates_ordered_shutdown_to_lifecycle_helper():
    tree = ast.parse(MAIN_SOURCE.read_text('utf-8'))
    lifespan = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == 'lifespan')
    calls = [
        node
        for node in ast.walk(lifespan)
        if isinstance(node, ast.Await)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
    ]
    shutdown_calls = [node.value for node in calls if node.value.func.id == 'shutdown_lifespan_resources']
    assert len(shutdown_calls) == 1
    assert [arg.id for arg in shutdown_calls[0].args if isinstance(arg, ast.Name)] == ['app', 'close_session']
