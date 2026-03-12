"""
test_main_debt_followups.py — Follow-up tests for main.py debt cleanup and optimization.

Purpose:
- Verify temporary Clear-Site-Data behavior is date-gated (no manual TODO removal needed).
- Verify _seed_api_sources quota backfill uses in-memory source map and applies enrichment quotas.

Called by: pytest
Depends on: app/main.py, tests/conftest.py fixtures
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch


def test_should_set_clear_site_data_before_cutoff() -> None:
    """Header gate stays enabled before the configured cutoff timestamp."""
    from app.main import _should_set_clear_site_data

    assert _should_set_clear_site_data(datetime(2026, 3, 17, 12, 0, tzinfo=timezone.utc)) is True


def test_should_set_clear_site_data_after_cutoff() -> None:
    """Header gate turns off automatically after the configured cutoff timestamp."""
    from app.main import _should_set_clear_site_data

    assert _should_set_clear_site_data(datetime(2026, 3, 18, 0, 0, tzinfo=timezone.utc)) is False


def test_clear_site_data_header_respects_gate(client) -> None:
    """Non-health responses should only include the header while gate is active."""
    with patch("app.main._should_set_clear_site_data", return_value=True):
        enabled = client.get("/")
        assert enabled.status_code == 200
        assert enabled.headers.get("Clear-Site-Data") == '"cache", "storage"'

    with patch("app.main._should_set_clear_site_data", return_value=False):
        disabled = client.get("/")
        assert disabled.status_code == 200
        assert "Clear-Site-Data" not in disabled.headers


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
