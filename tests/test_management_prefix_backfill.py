"""Tests for app/management/prefix_backfill.py — on-demand prefix tag backfill.

Tests the main() function with mocked DB and service.

Called by: pytest
Depends on: app/management/prefix_backfill.py
"""

import os
import sys

os.environ["TESTING"] = "1"

from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

# The lazy imports inside main() resolve from:
#   app.database.SessionLocal
#   app.services.tagging_backfill.run_prefix_backfill
_SESSION_TARGET = "app.database.SessionLocal"
_BACKFILL_TARGET = "app.services.tagging_backfill.run_prefix_backfill"


@contextmanager
def patched_main(backfill):
    """Patch SessionLocal + run_prefix_backfill and yield (main, mock_db,
    mock_session_cls)."""
    mock_db = MagicMock()
    mock_session_cls = MagicMock(return_value=mock_db)
    with (
        patch(_SESSION_TARGET, mock_session_cls),
        patch(_BACKFILL_TARGET, backfill),
    ):
        from app.management.prefix_backfill import main

        yield main, mock_db, mock_session_cls


class TestPrefixBackfillMain:
    def test_main_calls_backfill_with_default_batch_size(self):
        """Main() calls run_prefix_backfill with db and default batch_size=1000."""
        backfill = MagicMock(return_value={"total_tagged": 5})
        with patched_main(backfill) as (main, mock_db, _):
            main()

        backfill.assert_called_once_with(mock_db, batch_size=1000)

    def test_main_calls_backfill_with_custom_batch_size(self):
        """Main() passes custom batch_size to run_prefix_backfill."""
        backfill = MagicMock(return_value={"total_tagged": 2})
        with patched_main(backfill) as (main, mock_db, _):
            main(batch_size=50)

        backfill.assert_called_once_with(mock_db, batch_size=50)

    def test_main_closes_db_on_success(self):
        """Main() always closes the DB session on success."""
        backfill = MagicMock(return_value={"total_tagged": 0})
        with patched_main(backfill) as (main, mock_db, _):
            main()

        mock_db.close.assert_called_once()

    def test_main_closes_db_when_backfill_raises(self):
        """Main() closes the DB session even when run_prefix_backfill raises."""
        backfill = MagicMock(side_effect=RuntimeError("service crashed"))
        with patched_main(backfill) as (main, mock_db, _):
            with pytest.raises(RuntimeError, match="service crashed"):
                main()

        mock_db.close.assert_called_once()


class TestPrefixBackfillEntrypoint:
    def test_main_block_parses_batch_size(self):
        """The __main__ block parses --batch-size and calls main()."""
        import runpy

        with (
            patch.object(sys, "argv", ["prefix_backfill", "--batch-size", "10"]),
            patch(_SESSION_TARGET, MagicMock(return_value=MagicMock())),
            patch(_BACKFILL_TARGET, MagicMock(return_value={})) as backfill,
        ):
            sys.modules.pop("app.management.prefix_backfill", None)
            runpy.run_module("app.management.prefix_backfill", run_name="__main__", alter_sys=False)

        assert backfill.call_args.kwargs["batch_size"] == 10
