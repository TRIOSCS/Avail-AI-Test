"""Tests for app.utils.async_helpers — safe background task utility.

safe_background_task() is fire-and-forget and returns None; it schedules the task via
the shared hold_bg_task() retention set instead of handing the Task back to the caller.
Tests locate the scheduled task via the delta on async_helpers._bg_tasks (never assert
an exact set size — other tests on the same worker can leave an unrelated task in it).
"""

import asyncio

import pytest
from loguru import logger

from app.utils import async_helpers
from app.utils.async_helpers import hold_bg_task, safe_background_task


def _new_task(before: set) -> asyncio.Task:
    """Return the single task added to async_helpers._bg_tasks since `before`."""
    new_tasks = async_helpers._bg_tasks - before
    assert len(new_tasks) == 1, f"expected exactly one new task, got {len(new_tasks)}"
    return next(iter(new_tasks))


@pytest.mark.asyncio
async def test_happy_path_completes():
    """Task runs to completion and returns result."""

    async def _work():
        return 42

    before = set(async_helpers._bg_tasks)
    await safe_background_task(_work(), task_name="test_happy")
    task = _new_task(before)
    result = await task
    assert result == 42


@pytest.mark.asyncio
async def test_exception_is_caught_and_logged():
    """Exception inside the coroutine is caught; task resolves to None."""
    captured = []
    sink_id = logger.add(lambda msg: captured.append(str(msg)), level="ERROR")
    try:

        async def _boom():
            raise ValueError("kaboom")

        before = set(async_helpers._bg_tasks)
        await safe_background_task(_boom(), task_name="test_boom")
        task = _new_task(before)
        result = await task
        assert result is None
        assert any("test_boom" in m and "failed" in m for m in captured)
    finally:
        logger.remove(sink_id)


@pytest.mark.asyncio
async def test_cancellation_is_reraised():
    """CancelledError propagates so asyncio can clean up properly."""

    async def _slow():
        await asyncio.sleep(100)

    before = set(async_helpers._bg_tasks)
    await safe_background_task(_slow(), task_name="test_cancel")
    task = _new_task(before)
    await asyncio.sleep(0)  # Let the task start running before cancelling
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_task_name_set():
    """The asyncio.Task object carries the provided name."""

    async def _noop():
        pass

    before = set(async_helpers._bg_tasks)
    await safe_background_task(_noop(), task_name="my_custom_name")
    task = _new_task(before)
    assert task.get_name() == "my_custom_name"
    await task


@pytest.mark.asyncio
async def test_default_task_name():
    """Without an explicit name, the default 'background_task' is used."""

    async def _noop():
        pass

    before = set(async_helpers._bg_tasks)
    await safe_background_task(_noop())
    task = _new_task(before)
    assert task.get_name() == "background_task"
    await task


@pytest.mark.asyncio
async def test_returns_none():
    """safe_background_task is fire-and-forget — it returns None, not the Task."""

    async def _noop():
        pass

    result = await safe_background_task(_noop(), task_name="returns_none_test")
    assert result is None
    # Drain the scheduled task so it doesn't linger past the test.
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_strong_reference_held_until_done():
    """A strong ref is held internally until the task completes (P0.4: asyncio keeps
    only weak refs to scheduled tasks, so a discarded create_task() result could be
    garbage-collected mid-flight), then dropped so the set stays bounded."""
    release = asyncio.Event()

    async def _work():
        await release.wait()

    before = set(async_helpers._bg_tasks)
    await safe_background_task(_work(), task_name="ref_test")
    task = _new_task(before)
    assert task in async_helpers._bg_tasks
    release.set()
    await task
    assert task not in async_helpers._bg_tasks


@pytest.mark.asyncio
async def test_cancellation_logged():
    """Cancellation logs an info message with the task name."""
    captured = []
    sink_id = logger.add(lambda msg: captured.append(str(msg)), level="INFO")
    try:

        async def _slow():
            await asyncio.sleep(100)

        before = set(async_helpers._bg_tasks)
        await safe_background_task(_slow(), task_name="cancel_log_test")
        task = _new_task(before)
        await asyncio.sleep(0)  # Let the task start running before cancelling
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        # Allow event loop to process the log
        await asyncio.sleep(0)
        assert any("cancel_log_test" in m and "cancelled" in m for m in captured)
    finally:
        logger.remove(sink_id)


@pytest.mark.asyncio
async def test_hold_bg_task_skips_task_on_non_running_loop():
    """hold_bg_task() must not retain a task whose loop is no longer running — that task
    can never execute, so pinning it would leak the coroutine forever."""

    async def _noop():
        return None

    task = asyncio.get_event_loop().create_task(_noop())
    await task  # Task is now done; simulate a "loop not running" scenario directly.

    class _FakeTask:
        def get_loop(self):
            class _FakeLoop:
                def is_running(self):
                    return False

            return _FakeLoop()

        def get_name(self):
            return "fake_task"

    before = set(async_helpers._bg_tasks)
    hold_bg_task(_FakeTask())
    assert async_helpers._bg_tasks == before
