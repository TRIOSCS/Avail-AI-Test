"""tests/test_utils_async_helpers.py — Tests for app/utils/async_helpers.py."""

import asyncio
import os

os.environ["TESTING"] = "1"


from app.utils.async_helpers import safe_background_task


class TestSafeBackgroundTask:
    async def test_suppress_in_testing_returns_noop_task(self):
        """Under TESTING=1, suppress_in_testing closes the coro and returns a no-op."""
        called = []

        async def my_coro():
            called.append(1)

        task = await safe_background_task(my_coro(), task_name="test", suppress_in_testing=True)
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

        task = await safe_background_task(my_coro(), task_name="test", suppress_in_testing=False)
        await task
        assert result == [42]

    async def test_exception_in_coro_does_not_propagate(self):
        """Exceptions inside the coro are swallowed, task still completes."""

        async def bad_coro():
            raise ValueError("intentional error")

        task = await safe_background_task(bad_coro(), task_name="test", suppress_in_testing=False)
        # Should not raise
        result = await task
        assert result is None

    async def test_task_name_set(self):
        """The task should have the specified name."""

        async def my_coro():
            return None

        task = await safe_background_task(my_coro(), task_name="my_named_task", suppress_in_testing=False)
        assert task.get_name() == "my_named_task"
        await task

    async def test_default_suppress_false_runs(self):
        """Default suppress_in_testing=False runs coro even in TESTING mode."""
        result = []

        async def my_coro():
            result.append(1)

        task = await safe_background_task(my_coro(), task_name="test")
        await task
        assert result == [1]

    async def test_returns_asyncio_task(self):
        """Always returns an asyncio.Task."""

        async def my_coro():
            return 123

        task = await safe_background_task(my_coro(), suppress_in_testing=True)
        assert isinstance(task, asyncio.Task)
        await task
