"""Tests for app/management/ai_tagging.py — on-demand AI brand classification.

Tests the async main() with a real (test) DB session for the card query and
mocked Claude classify/apply services. Was the scheduled ai_tagging job (W1);
these tests carry over its behavior pins: early return when nothing is
untagged, classify+apply flow, rollback on failure.

Called by: pytest
Depends on: app/management/ai_tagging.py
"""

import asyncio
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

os.environ["TESTING"] = "1"

_SESSION_TARGET = "app.database.SessionLocal"


class TestAiTaggingMain:
    """The command is a thin wrapper (W1 simplify pass): batching, concurrency, and the
    Claude-unconfigured early exit live in tagging_ai_batch.run_ai_backfill and are
    tested in tests/test_tagging_ai.py.

    These tests pin only the wrapper contract.
    """

    def test_delegates_to_service_with_limit_and_internal_exclusion(self):
        mock_db = MagicMock()
        mock_backfill = AsyncMock(return_value={"processed": 3, "matched": 2, "unknown": 1})

        with (
            patch(_SESSION_TARGET, return_value=mock_db),
            patch("app.services.tagging_ai_batch.run_ai_backfill", mock_backfill),
        ):
            from app.management.ai_tagging import main

            asyncio.run(main(limit=10))

        mock_backfill.assert_awaited_once_with(mock_db, limit=10, exclude_internal=True)
        mock_db.close.assert_called_once()

    def test_default_limit_is_500(self):
        mock_db = MagicMock()
        mock_backfill = AsyncMock(return_value={})

        with (
            patch(_SESSION_TARGET, return_value=mock_db),
            patch("app.services.tagging_ai_batch.run_ai_backfill", mock_backfill),
        ):
            from app.management.ai_tagging import main

            asyncio.run(main())

        mock_backfill.assert_awaited_once_with(mock_db, limit=500, exclude_internal=True)

    def test_service_error_rolls_back_and_reraises(self):
        mock_db = MagicMock()
        mock_backfill = AsyncMock(side_effect=Exception("DB crashed"))

        with (
            patch(_SESSION_TARGET, return_value=mock_db),
            patch("app.services.tagging_ai_batch.run_ai_backfill", mock_backfill),
        ):
            from app.management.ai_tagging import main

            with pytest.raises(Exception, match="DB crashed"):
                asyncio.run(main())

        mock_db.rollback.assert_called_once()
        mock_db.close.assert_called_once()


class TestAiTaggingEntrypoint:
    def test_main_block_runs_asyncio_with_limit(self):
        """The __main__ block parses --limit and calls asyncio.run(main(...))."""
        import runpy

        with (
            patch.object(sys, "argv", ["ai_tagging", "--limit", "10"]),
            patch("asyncio.run") as mock_run,
        ):
            sys.modules.pop("app.management.ai_tagging", None)
            runpy.run_module("app.management.ai_tagging", run_name="__main__", alter_sys=False)

        mock_run.assert_called_once()
        coro = mock_run.call_args[0][0]
        assert asyncio.iscoroutine(coro)
        coro.close()
