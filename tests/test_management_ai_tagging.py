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
    def test_returns_early_when_no_untagged_cards(self):
        """Main() returns before any Claude call when no untagged cards exist."""
        mock_db = MagicMock()
        # Query chain for untagged cards resolves to an empty list.
        (mock_db.query.return_value.filter.return_value.order_by.return_value.limit.return_value.all.return_value) = []
        mock_classify = AsyncMock()

        with (
            patch(_SESSION_TARGET, return_value=mock_db),
            patch("app.services.tagging_ai.classify_parts_with_ai", mock_classify),
        ):
            from app.management.ai_tagging import main

            asyncio.run(main())

        mock_classify.assert_not_called()
        mock_db.commit.assert_not_called()
        mock_db.close.assert_called_once()

    def test_classifies_and_applies_results(self):
        """Main() classifies untagged cards and applies results via
        _apply_ai_results."""
        row = MagicMock()
        row.id = 1
        row.normalized_mpn = "lm317t"

        mock_db = MagicMock()
        (mock_db.query.return_value.filter.return_value.order_by.return_value.limit.return_value.all.return_value) = [
            row
        ]
        classified = [{"mpn": "lm317t", "manufacturer": "TI", "category": "Voltage Regulators"}]
        mock_classify = AsyncMock(return_value=classified)
        mock_apply = MagicMock(return_value=(1, 0))

        with (
            patch(_SESSION_TARGET, return_value=mock_db),
            patch("app.services.tagging_ai.classify_parts_with_ai", mock_classify),
            patch("app.services.tagging_ai._apply_ai_results", mock_apply),
        ):
            from app.management.ai_tagging import main

            asyncio.run(main(limit=10))

        mock_classify.assert_called_once_with(["lm317t"])
        mock_apply.assert_called_once_with(classified, [(1, "lm317t")], mock_db)
        mock_db.commit.assert_called_once()
        mock_db.close.assert_called_once()

    def test_db_error_rolls_back_and_reraises(self):
        """A DB failure rolls back, re-raises, and still closes the session."""
        mock_db = MagicMock()
        mock_db.query.side_effect = Exception("DB crashed")

        with patch(_SESSION_TARGET, return_value=mock_db):
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
