"""async_helpers.py — Safe async utilities for background task execution.

Wraps asyncio tasks with error isolation so background work (notifications,
enrichment, analytics) never crashes the request handler.

Called by: routers, services, jobs
Depends on: loguru
"""

import asyncio
from typing import Any, Coroutine

from loguru import logger


async def safe_background_task(
    coro: Coroutine[Any, Any, Any],
    *,
    task_name: str = "background_task",
) -> asyncio.Task:
    """Fire-and-forget an async coroutine with error isolation.

    The coroutine runs in a new asyncio Task. Exceptions are logged
    but never propagate — the caller's request is never affected.

    Important: the coroutine MUST manage its own SQLAlchemy session lifetime
    (e.g., open SessionLocal() internally with try/finally close). Never pass
    a request-scoped Session into the coroutine — web framework finalizers
    close those as soon as the response is sent, and this wrapper would
    swallow the resulting use-after-close exception silently.

    Args:
        coro: The coroutine to execute
        task_name: Label for logging on success/failure

    Returns:
        The created asyncio.Task (can be awaited if needed, but usually ignored)
    """

    async def _wrapper():
        try:
            return await coro
        except asyncio.CancelledError:
            logger.info("{} was cancelled", task_name)
            raise  # Re-raise cancellation
        except Exception:
            logger.error("Background task '{}' failed", task_name, exc_info=True)
            return None

    task = asyncio.create_task(_wrapper(), name=task_name)
    return task
