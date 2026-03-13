"""
test_main_debt_followups.py — Follow-up tests for main.py seed-time debt cleanup.

Purpose:
- Verify _seed_api_sources backfills known monthly quotas onto existing API sources.

Called by: pytest
Depends on: app/main.py
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch


def test_seed_api_sources_backfills_hunter_enrichment_quota_without_extra_query() -> None:
    """Quota backfill sets missing monthly_quota on existing hunter source."""
    from app.main import _seed_api_sources

    with patch("app.database.SessionLocal") as mock_session_cls:
        mock_db = MagicMock()
        mock_session_cls.return_value = mock_db
        query = mock_db.query.return_value

        existing_hunter = MagicMock()
        existing_hunter.name = "hunter_enrichment"
        existing_hunter.monthly_quota = None

        query.all.return_value = [existing_hunter]

        def _filter_by_side_effect(**kwargs):
            result = MagicMock()
            result.first.return_value = existing_hunter if kwargs.get("name") == "hunter_enrichment" else None
            return result

        query.filter_by.side_effect = _filter_by_side_effect

        _seed_api_sources()

        assert existing_hunter.monthly_quota == 500
        mock_db.commit.assert_called_once()
        mock_db.close.assert_called_once()
