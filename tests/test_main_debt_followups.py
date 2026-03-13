"""
test_main_debt_followups.py — Follow-up tests for main.py debt cleanup and optimization.

Purpose:
- Verify _seed_api_sources quota backfill uses in-memory source map and applies enrichment quotas.

Called by: pytest
Depends on: app/main.py, tests/conftest.py fixtures
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch


def test_seed_api_sources_creates_session() -> None:
    """_seed_api_sources creates its own DB session and closes it."""
    from app.main import _seed_api_sources

    with patch("app.database.SessionLocal") as mock_session_cls:
        mock_db = MagicMock()
        mock_session_cls.return_value = mock_db
        mock_db.query.return_value.all.return_value = []

        _seed_api_sources()

        mock_db.close.assert_called_once()


def test_seed_api_sources_backfills_hunter_enrichment_quota_without_extra_query() -> None:
    """Quota backfill should use existing_map and set hunter_enrichment monthly quota."""
    from app.main import _seed_api_sources

    with patch("app.database.SessionLocal") as mock_session_cls:
        mock_db = MagicMock()
        mock_session_cls.return_value = mock_db

        existing_hunter = MagicMock()
        existing_hunter.name = "hunter_enrichment"
        existing_hunter.monthly_quota = None

        mock_db.query.return_value.all.return_value = [existing_hunter]

        _seed_api_sources()

        assert existing_hunter.monthly_quota == 500
        assert mock_db.query.call_count == 1
        mock_db.commit.assert_called_once()
        mock_db.close.assert_called_once()


def test_seed_api_sources_skips_when_quota_already_set() -> None:
    """If hunter_enrichment already has a quota, it is not overwritten."""
    from app.main import _seed_api_sources

    with patch("app.database.SessionLocal") as mock_session_cls:
        mock_db = MagicMock()
        mock_session_cls.return_value = mock_db

        existing_hunter = MagicMock()
        existing_hunter.name = "hunter_enrichment"
        existing_hunter.monthly_quota = 1000

        mock_db.query.return_value.all.return_value = [existing_hunter]

        _seed_api_sources()

        assert existing_hunter.monthly_quota == 1000
        mock_db.close.assert_called_once()
