"""
logging_config.py — Centralized Logging Configuration for AVAIL AI

Sets up Loguru as the single logging backend. Intercepts Python's stdlib
logging module so all existing getLogger() calls automatically route
through Loguru with structured output, log rotation, and request context.

Business Rules:
- All logs go through Loguru (no direct print() or stdlib logging)
- JSON format in production for machine parsing
- Human-readable format in development
- Request ID from middleware is included when available
- Log rotation: 50MB files, 7-day retention

Called by: app/main.py (on startup)
Depends on: app/config.py (for log_level, app_url)
"""

import logging
import os
import sys

from loguru import logger


def setup_logging() -> None:
    """Configure Loguru and intercept stdlib logging.

    Call once at app startup, before any other imports that log.
    """
    # Remove Loguru's default stderr handler so we control format
    logger.remove()

    log_level = os.getenv("LOG_LEVEL", "INFO").upper()
    is_production = "availai.net" in os.getenv("APP_URL", "")

    if is_production:
        # Production: JSON lines to stdout (Docker captures these)
        logger.add(
            sys.stdout,
            level=log_level,
            format="{message}",
            serialize=True,  # JSON output
        )
        # Rotate file for persistent logs on the server
        logger.add(
            "/var/log/avail/avail.log",
            level=log_level,
            rotation="50 MB",
            retention="7 days",
            compression="gz",
            serialize=True,
        )
    else:
        # Development: human-readable with colors
        logger.add(
            sys.stdout,
            level=log_level,
            format=(
                "<green>{time:HH:mm:ss}</green> | "
                "<level>{level: <8}</level> | "
                "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> | "
                "{message}"
            ),
            colorize=True,
        )

    # Intercept stdlib logging → route through Loguru
    # This makes all existing logging.getLogger() calls use Loguru
    logging.basicConfig(handlers=[_InterceptHandler()], level=0, force=True)

    # Quiet noisy third-party loggers
    for noisy in ("httpx", "httpcore", "uvicorn.access", "sqlalchemy.engine"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    logger.info("Logging configured", level=log_level, production=is_production)


class _InterceptHandler(logging.Handler):
    """Route stdlib logging records to Loguru.

    This is the bridge: any code using logging.getLogger("x").info("msg")
    will have that message captured by Loguru with full context.
    """

    def emit(self, record: logging.LogRecord) -> None:
        # Find the Loguru level that matches the stdlib level
        try:
            level = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno

        # Find the caller (skip frames from stdlib logging internals)
        frame, depth = logging.currentframe(), 0
        while frame and (depth == 0 or frame.f_code.co_filename == logging.__file__):
            frame = frame.f_back
            depth += 1

        logger.opt(depth=depth, exception=record.exc_info).log(
            level, record.getMessage()
        )
