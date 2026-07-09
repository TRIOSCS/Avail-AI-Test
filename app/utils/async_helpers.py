"""async_helpers.py — Safe async utilities for background task execution.

Wraps asyncio tasks with error isolation so background work (notifications,
enrichment, analytics) never crashes the request handler.

Called by: routers, services, jobs
Depends on: loguru
"""

import asyncio
import os
from typing import Any, Coroutine

from loguru import logger

# Strong references to in-flight fire-and-forget tasks. asyncio only keeps a
# weak reference to scheduled tasks, so a discarded create_task() result can be
# garbage-collected mid-flight (P0.4 in docs/CODE_AUDIT_AND_HARDENING_PLAN.md).
# hold_bg_task() is the one canonical entry point every caller routes through;
# its done-callback drops the ref so the set never grows unbounded. Kept
# private — callers retain/observe tasks only via hold_bg_task(), never this
# set directly.
_bg_tasks: set[asyncio.Task] = set()


def hold_bg_task(task: asyncio.Task) -> None:
    """Hold a strong reference to a fire-and-forget asyncio.Task until it completes.

    This is the one canonical strong-ref holder for fire-and-forget tasks across
    the codebase (email_service, prepayment_notifications, safe_background_task,
    etc.) — asyncio only keeps a weak reference to a scheduled Task, so without
    this the event loop can garbage-collect an in-flight task before it runs,
    silently dropping whatever work it was doing.

    Only retains the task if its loop is still running: a task whose loop isn't
    running can never execute, so pinning it would leak the coroutine forever
    (before this guard, such a task was at least eligible for GC).
    """
    if not task.get_loop().is_running():
        logger.warning("hold_bg_task: loop not running for {!r}, not retaining", task.get_name())
        return
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)


async def safe_background_task(
    coro: Coroutine[Any, Any, Any],
    *,
    task_name: str = "background_task",
    suppress_in_testing: bool = False,
) -> None:
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
        None. The task is fire-and-forget — a strong reference is held
        internally via hold_bg_task(), so no caller needs it back.
    """
    # Under the test suite, fire-and-forget tasks that open real async DB sessions
    # cause nondeterministic xdist worker segfaults during teardown.  Close the
    # coroutine immediately (suppresses "coroutine never awaited" warnings) and
    # schedule a trivial no-op task instead. Production (TESTING unset) is
    # completely unchanged.
    if suppress_in_testing and os.environ.get("TESTING"):
        coro.close()

        async def _noop():
            return None

        hold_bg_task(asyncio.create_task(_noop(), name=task_name))
        return

    async def _wrapper():
        try:
            return await coro
        except asyncio.CancelledError:
            logger.info("{} was cancelled", task_name)
            raise  # Re-raise cancellation
        except Exception:
            logger.error("Background task '{}' failed", task_name, exc_info=True)
            return None

    hold_bg_task(asyncio.create_task(_wrapper(), name=task_name))
