"""
test_main_debt_followups.py — Follow-up tests for main.py utilities.

Purpose:
- Verify _seed_api_sources creates sessions and handles errors gracefully.

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


def test_seed_api_sources_handles_exception() -> None:
    """_seed_api_sources handles DB errors gracefully without crashing."""
    from app.main import _seed_api_sources

    with patch("app.database.SessionLocal") as mock_session_cls:
        mock_db = MagicMock()
        mock_session_cls.return_value = mock_db
        mock_db.query.side_effect = Exception("DB unavailable")

        _seed_api_sources()

        mock_db.rollback.assert_called_once()
        mock_db.close.assert_called_once()


def test_seed_api_sources_callable() -> None:
    """_seed_api_sources is callable and importable."""
    from app.main import _seed_api_sources

    assert callable(_seed_api_sources)
