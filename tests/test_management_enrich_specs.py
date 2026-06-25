"""tests/test_management_enrich_specs.py — Tests for app/management/enrich_specs.py.

Covers: main() async function.
All DB and service calls are mocked.

Called by: pytest
Depends on: unittest.mock
"""

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

os.environ["TESTING"] = "1"


class TestEnrichSpecsMain:
    async def test_main_calls_enrich_pending_specs(self):
        """main() calls enrich_pending_specs with the given limit and logs the result."""
        from app.management.enrich_specs import main

        mock_db = MagicMock()
        mock_session_cls = MagicMock(return_value=mock_db)
        mock_stats = {"processed": 5, "updated": 3}

        with patch("app.database.SessionLocal", mock_session_cls):
            with patch(
                "app.services.spec_enrichment_service.enrich_pending_specs",
                new=AsyncMock(return_value=mock_stats),
            ) as mock_enrich:
                await main(limit=50)

        mock_enrich.assert_called_once_with(mock_db, limit=50)
        mock_db.close.assert_called_once()

    async def test_main_uses_default_limit_100(self):
        """main() defaults to limit=100 when called with no arguments."""
        from app.management.enrich_specs import main

        mock_db = MagicMock()
        with patch("app.database.SessionLocal", MagicMock(return_value=mock_db)):
            with patch(
                "app.services.spec_enrichment_service.enrich_pending_specs",
                new=AsyncMock(return_value={}),
            ) as mock_enrich:
                await main()

        mock_enrich.assert_called_once_with(mock_db, limit=100)

    async def test_main_closes_db_on_exception(self):
        """main() closes the DB session even when enrich_pending_specs raises."""
        from app.management.enrich_specs import main

        mock_db = MagicMock()
        with patch("app.database.SessionLocal", MagicMock(return_value=mock_db)):
            with patch(
                "app.services.spec_enrichment_service.enrich_pending_specs",
                new=AsyncMock(side_effect=RuntimeError("service failure")),
            ):
                with pytest.raises(RuntimeError, match="service failure"):
                    await main(limit=10)

        mock_db.close.assert_called_once()


class TestEnrichSpecsMainBlock:
    def test_main_block_runs_with_default_limit(self):
        """Simulate running the script as __main__ with no args (default limit=100)."""
        import runpy
        import sys

        mock_db = MagicMock()
        with patch.object(sys, "argv", ["enrich_specs"]):
            with patch("app.database.SessionLocal", MagicMock(return_value=mock_db)):
                with patch(
                    "app.services.spec_enrichment_service.enrich_pending_specs",
                    new=AsyncMock(return_value={"processed": 0}),
                ) as mock_enrich:
                    runpy.run_module("app.management.enrich_specs", run_name="__main__")

        mock_enrich.assert_called_once_with(mock_db, limit=100)

    def test_main_block_passes_limit_arg(self):
        """Simulate running the script with --limit 25."""
        import runpy
        import sys

        mock_db = MagicMock()
        with patch.object(sys, "argv", ["enrich_specs", "--limit", "25"]):
            with patch("app.database.SessionLocal", MagicMock(return_value=mock_db)):
                with patch(
                    "app.services.spec_enrichment_service.enrich_pending_specs",
                    new=AsyncMock(return_value={}),
                ) as mock_enrich:
                    runpy.run_module("app.management.enrich_specs", run_name="__main__")

        mock_enrich.assert_called_once_with(mock_db, limit=25)
