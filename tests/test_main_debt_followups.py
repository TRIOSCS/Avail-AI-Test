"""
test_main_debt_followups.py — Follow-up tests for main.py debt cleanup and optimization.

Purpose:
- Verify temporary Clear-Site-Data behavior is date-gated (no manual TODO removal needed).
- Verify _seed_api_sources quota backfill uses in-memory source map and applies enrichment quotas.

Called by: pytest
Depends on: app/main.py, tests/conftest.py fixtures
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch


def test_clear_site_data_helper_removed() -> None:
    """Legacy temporary helper was removed after rollout window."""
    import app.main as main_mod

    assert not hasattr(main_mod, "_should_set_clear_site_data")


def test_clear_site_data_header_not_emitted(client) -> None:
    """Root responses should not include Clear-Site-Data now that rollout ended."""
    resp = client.get("/")
    assert resp.status_code == 200
    assert "Clear-Site-Data" not in resp.headers


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
