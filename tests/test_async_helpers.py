"""Tests for app.utils.async_helpers — safe background task utility."""
import asyncio

import pytest
from loguru import logger

from app.utils.async_helpers import safe_background_task


@pytest.mark.asyncio
async def test_happy_path_completes():
    """Task runs to completion and returns result."""

    async def _work():
        return 42

    task = await safe_background_task(_work(), task_name="test_happy")
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

        task = await safe_background_task(_boom(), task_name="test_boom")
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

    task = await safe_background_task(_slow(), task_name="test_cancel")
    await asyncio.sleep(0)  # Let the task start running before cancelling
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_task_name_set():
    """The asyncio.Task object carries the provided name."""

    async def _noop():
        pass

    task = await safe_background_task(_noop(), task_name="my_custom_name")
    assert task.get_name() == "my_custom_name"
    await task


@pytest.mark.asyncio
async def test_default_task_name():
    """Without an explicit name, the default 'background_task' is used."""

    async def _noop():
        pass

    task = await safe_background_task(_noop())
    assert task.get_name() == "background_task"
    await task


@pytest.mark.asyncio
async def test_cancellation_logged():
    """Cancellation logs an info message with the task name."""
    captured = []
    sink_id = logger.add(lambda msg: captured.append(str(msg)), level="INFO")
    try:
        async def _slow():
            await asyncio.sleep(100)

        task = await safe_background_task(_slow(), task_name="cancel_log_test")
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
