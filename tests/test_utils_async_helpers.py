"""tests/test_utils_async_helpers.py — Tests for app/utils/async_helpers.py.

safe_background_task() returns None (fire-and-forget); tests locate the scheduled task
via the delta on the shared async_helpers._bg_tasks retention set.
"""

import asyncio
import os

os.environ["TESTING"] = "1"


from app.utils import async_helpers
from app.utils.async_helpers import safe_background_task


def _new_task(before: set) -> asyncio.Task:
    """Return the single task added to async_helpers._bg_tasks since `before`."""
    new_tasks = async_helpers._bg_tasks - before
    assert len(new_tasks) == 1, f"expected exactly one new task, got {len(new_tasks)}"
    return next(iter(new_tasks))


class TestSafeBackgroundTask:
    async def test_suppress_in_testing_returns_noop_task(self):
        """Under TESTING=1, suppress_in_testing closes the coro and returns a no-op."""
        called = []

        async def my_coro():
            called.append(1)

        before = set(async_helpers._bg_tasks)
        result = await safe_background_task(my_coro(), task_name="test", suppress_in_testing=True)
        assert result is None
        task = _new_task(before)
        assert isinstance(task, asyncio.Task)
        # Wait for the no-op task to complete
        await task
        # The coro should NOT have been called (suppressed)
        assert called == []

    async def test_no_suppress_runs_coro(self):
        """Without suppress, the coro runs normally."""
        result = []

        async def my_coro():
            result.append(42)

        before = set(async_helpers._bg_tasks)
        await safe_background_task(my_coro(), task_name="test", suppress_in_testing=False)
        task = _new_task(before)
        await task
        assert result == [42]

    async def test_exception_in_coro_does_not_propagate(self):
        """Exceptions inside the coro are swallowed, task still completes."""

        async def bad_coro():
            raise ValueError("intentional error")

        before = set(async_helpers._bg_tasks)
        await safe_background_task(bad_coro(), task_name="test", suppress_in_testing=False)
        task = _new_task(before)
        # Should not raise
        outcome = await task
        assert outcome is None

    async def test_task_name_set(self):
        """The task should have the specified name."""

        async def my_coro():
            return None

        before = set(async_helpers._bg_tasks)
        await safe_background_task(my_coro(), task_name="my_named_task", suppress_in_testing=False)
        task = _new_task(before)
        assert task.get_name() == "my_named_task"
        await task

    async def test_default_suppress_false_runs(self):
        """Default suppress_in_testing=False runs coro even in TESTING mode."""
        result = []

        async def my_coro():
            result.append(1)

        before = set(async_helpers._bg_tasks)
        await safe_background_task(my_coro(), task_name="test")
        task = _new_task(before)
        await task
        assert result == [1]

    async def test_returns_none(self):
        """safe_background_task always returns None (fire-and-forget)."""

        async def my_coro():
            return 123

        before = set(async_helpers._bg_tasks)
        result = await safe_background_task(my_coro(), suppress_in_testing=True)
        assert result is None
        task = _new_task(before)
        await task
