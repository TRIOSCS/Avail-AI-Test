"""Search worker monitoring — Sentry alerts.

Provides Sentry message capture with worker context, used by the worker
liveness watchdog. Parameterized by component_name so all workers share
one implementation.

Called by: app/jobs/worker_liveness_jobs.py
Depends on: sentry_sdk, loguru
"""

from contextlib import contextmanager

from loguru import logger


@contextmanager
def _sentry_scope(component_name: str, context: dict | None):
    """Yield a Sentry scope tagged with the worker component and extra context.

    Raises ImportError if the Sentry SDK is not installed — callers handle it so the
    missing-SDK log message can stay specific to what was being sent.
    """
    import sentry_sdk

    with sentry_sdk.new_scope() as scope:
        scope.set_tag("component", f"{component_name.lower()}_worker")
        for key, value in (context or {}).items():
            scope.set_extra(key, value)
        yield sentry_sdk


def capture_sentry_message(
    message: str, level: str = "warning", context: dict | None = None, component_name: str = "worker"
):
    """Send a message to Sentry with worker context."""
    try:
        with _sentry_scope(component_name, context) as sentry_sdk:
            sentry_sdk.capture_message(message, level=level)
    except ImportError:
        logger.warning("Sentry SDK not available: {}", message)
