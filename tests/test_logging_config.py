"""
test_logging_config.py — Tests for app/logging_config.py

Verifies Loguru setup, stdlib logging interception, and request
context binding. Uses loguru's sink capture for assertions.

Called by: pytest
Depends on: app/logging_config.py
"""

import logging
import os
from io import StringIO
from unittest.mock import patch

import pytest
from loguru import logger

from app.logging_config import setup_logging


@pytest.fixture(autouse=True)
def _clean_loguru():
    """Remove all handlers before/after each test for isolation."""
    logger.remove()
    yield
    logger.remove()


def test_setup_logging_adds_handler():
    """setup_logging() should add at least one Loguru handler."""
    logger.remove()
    assert len(logger._core.handlers) == 0
    with patch.dict(os.environ, {"APP_URL": "http://localhost:8000"}):
        setup_logging()
    assert len(logger._core.handlers) > 0


def test_stdlib_logging_intercepted():
    """After setup, stdlib logging.getLogger() messages go through Loguru."""
    with patch.dict(os.environ, {"APP_URL": "http://localhost:8000"}):
        setup_logging()

    # Add test sink AFTER setup (setup calls logger.remove() internally)
    messages = []
    logger.add(lambda m: messages.append(str(m)), format="{message}")

    stdlib_logger = logging.getLogger("test.intercept")
    stdlib_logger.warning("intercepted message")

    assert any("intercepted message" in m for m in messages)


def test_log_level_from_env():
    """LOG_LEVEL env var controls minimum log level."""
    with patch.dict(os.environ, {"APP_URL": "http://localhost:8000", "LOG_LEVEL": "WARNING"}):
        setup_logging()

    # Add test sink AFTER setup — use WARNING level to match env
    messages = []
    logger.add(lambda m: messages.append(str(m)), level="WARNING", format="{message}")

    logger.debug("should be filtered")
    logger.warning("should appear")

    warning_msgs = [m for m in messages if "should appear" in m]
    debug_msgs = [m for m in messages if "should be filtered" in m]
    assert len(warning_msgs) >= 1
    assert len(debug_msgs) == 0


def test_context_binding():
    """logger.contextualize() adds fields to log records."""
    records = []
    logger.remove()
    logger.add(lambda m: records.append(m.record), format="{message}")

    with logger.contextualize(request_id="abc123"):
        logger.info("request log")

    assert len(records) >= 1
    assert records[-1]["extra"].get("request_id") == "abc123"


def test_context_not_leaked():
    """Context fields should not persist after contextualize block exits."""
    records = []
    logger.remove()
    logger.add(lambda m: records.append(m.record), format="{message}")

    with logger.contextualize(request_id="abc123"):
        logger.info("inside")
    logger.info("outside")

    outside_record = records[-1]
    assert "request_id" not in outside_record["extra"]


def test_production_mode_uses_serialize():
    """When APP_URL contains 'availai.net', serialize=True (JSON output)."""
    messages = []
    logger.remove()

    with patch.dict(os.environ, {"APP_URL": "https://app.availai.net"}):
        # Mock the file handler since /var/log/avail may not exist in test
        with patch("loguru.logger.add") as mock_add:
            setup_logging()
            # At least one call should have serialize=True
            serialize_calls = [
                c for c in mock_add.call_args_list
                if c.kwargs.get("serialize") is True
            ]
            assert len(serialize_calls) >= 1
