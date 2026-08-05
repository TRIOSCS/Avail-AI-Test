"""test_startup_nightly.py — Additional coverage tests for app/startup.py.

Targets:
- _seed_agent_user (happy path, skip-existing, error branch)
- _seed_commodity_schemas (happy path and error)
- seed_api_sources early-return when all sources present
- seed_api_sources removes legacy 'newark' source
- seed_api_sources backfills monthly_quota
(The deleted one-time startup backfills' tests went with them — W2.9.)

Called by: pytest
Depends on: app/startup.py, conftest engine/db_session
"""

import os

os.environ["TESTING"] = "1"

import json
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from tests.conftest import engine

_ = engine  # ensure tables are created


def _make_sqlite_engine():
    return create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )


# ── _seed_agent_user ──────────────────────────────────────────────────────────


class TestSeedAgentUser:
    """Lines 160-177: _seed_agent_user."""

    @patch("app.startup.SessionLocal")
    def test_creates_agent_user_when_absent(self, mock_sl, db_session: Session):
        """Creates agent@availai.local when it does not exist."""
        from app.startup import _seed_agent_user

        mock_sl.return_value = db_session
        _seed_agent_user()

        from app.models.auth import User

        user = db_session.query(User).filter_by(email="agent@availai.local").first()
        assert user is not None
        # Least privilege: the agent service account is seeded as AGENT,
        # never admin (CRIT-SEC-1).
        assert user.role == "agent"
        assert user.name == "Agent"

    @patch("app.startup.SessionLocal")
    def test_demotes_legacy_over_privileged_agent(self, mock_sl, db_session: Session):
        """A pre-existing agent row with a stale admin role is demoted to agent without
        creating a duplicate (CRIT-SEC-1)."""
        from app.models.auth import User
        from app.startup import _seed_agent_user

        # Simulate a legacy agent row seeded before the least-privilege fix.
        existing = User(email="agent@availai.local", name="Agent", role="admin", is_active=True)
        db_session.add(existing)
        db_session.commit()

        mock_sl.return_value = db_session
        _seed_agent_user()

        rows = db_session.query(User).filter_by(email="agent@availai.local").all()
        assert len(rows) == 1
        assert rows[0].role == "agent"

    @patch("app.startup.SessionLocal")
    def test_error_branch_rolls_back_and_reraises(self, mock_sl):
        """Exception causes rollback and is re-raised."""
        from app.startup import _seed_agent_user

        mock_db = MagicMock()
        mock_db.query.return_value.filter_by.return_value.first.return_value = None
        mock_db.add.side_effect = RuntimeError("DB exploded")
        mock_db.rollback = MagicMock()
        mock_db.close = MagicMock()
        mock_sl.return_value = mock_db

        with pytest.raises(RuntimeError, match="DB exploded"):
            _seed_agent_user()

        mock_db.rollback.assert_called_once()
        mock_db.close.assert_called_once()


# ── _seed_commodity_schemas ───────────────────────────────────────────────────


class TestSeedCommoditySchemas:
    """Lines 837-847: _seed_commodity_schemas happy path and error branch."""

    @patch("app.startup.SessionLocal")
    def test_calls_seed_function_successfully(self, mock_sl, db_session: Session):
        """Happy path: seed_commodity_schemas is called with the db session."""
        from app.startup import _seed_commodity_schemas

        # Call the real function — the commodity_registry.seed_commodity_schemas
        # is a service function; we mock it at the source
        with patch("app.services.commodity_registry.seed_commodity_schemas") as mock_seed:
            mock_sl.return_value = db_session
            _seed_commodity_schemas()
            mock_seed.assert_called_once_with(db_session)

    @patch("app.startup.SessionLocal")
    def test_error_rolls_back_and_reraises(self, mock_sl):
        """Exception from seed_commodity_schemas causes rollback and re-raise."""
        from app.startup import _seed_commodity_schemas

        mock_db = MagicMock()
        mock_db.rollback = MagicMock()
        mock_db.close = MagicMock()
        mock_sl.return_value = mock_db

        with patch("app.services.commodity_registry.seed_commodity_schemas") as mock_seed:
            mock_seed.side_effect = RuntimeError("schema error")
            with pytest.raises(RuntimeError, match="schema error"):
                _seed_commodity_schemas()

        mock_db.rollback.assert_called_once()
        mock_db.close.assert_called_once()


# ── seed_api_sources ──────────────────────────────────────────────────────────


class TestSeedApiSources:
    """Lines 955-956, 982-985, 1001, 1004-1006: seed_api_sources branches."""

    @patch("app.startup.SessionLocal")
    def test_early_return_when_all_sources_present(self, mock_sl):
        """Lines 955-956: returns early when existing source count matches SOURCES."""
        from pathlib import Path

        from app.startup import seed_api_sources

        sources_path = Path(__file__).parent.parent / "app" / "data" / "api_sources.json"
        sources = json.loads(sources_path.read_text())

        mock_db = MagicMock()
        # Mock existing_map to match all SOURCES exactly
        mock_sources = []
        for s in sources:
            ms = MagicMock()
            ms.name = s["name"]
            mock_sources.append(ms)
        mock_db.query.return_value.all.return_value = mock_sources
        mock_db.close = MagicMock()
        mock_sl.return_value = mock_db

        seed_api_sources()

        # Should return early without calling commit
        mock_db.commit.assert_not_called()
        mock_db.close.assert_called_once()

    @patch("app.startup.SessionLocal")
    def test_removes_legacy_newark_source(self, mock_sl, db_session: Session):
        """Lines 982-985: deletes 'newark' when both 'newark' and 'element14' exist."""
        from app.models import ApiSource
        from app.startup import seed_api_sources

        mock_sl.return_value = db_session

        # Create fake newark and element14 sources in DB
        newark = ApiSource(
            name="newark",
            display_name="Newark",
            category="distributor",
            source_type="api",
            description="Newark Electronics",
            signup_url="",
            env_vars=[],
            setup_notes="",
            status="pending",
            is_active=False,
        )
        element14 = ApiSource(
            name="element14",
            display_name="Element14",
            category="distributor",
            source_type="api",
            description="Element14",
            signup_url="",
            env_vars=[],
            setup_notes="",
            status="pending",
            is_active=False,
        )
        db_session.add_all([newark, element14])
        db_session.commit()

        # Patch so the function detects both and removes newark
        seed_api_sources()

        remaining = db_session.query(ApiSource).filter_by(name="newark").first()
        assert remaining is None

    @patch("app.startup.SessionLocal")
    def test_backfills_monthly_quota_for_known_sources(self, mock_sl, db_session: Session):
        """Lines 1001, 1004-1006: sets monthly_quota where it is NULL."""
        from app.models import ApiSource
        from app.startup import seed_api_sources

        mock_sl.return_value = db_session

        # Add a digikey source with no monthly_quota
        dk = ApiSource(
            name="digikey",
            display_name="DigiKey",
            category="distributor",
            source_type="api",
            description="DigiKey API",
            signup_url="",
            env_vars=[],
            setup_notes="",
            status="pending",
            is_active=False,
            monthly_quota=None,
        )
        db_session.add(dk)
        db_session.commit()

        seed_api_sources()

        # Re-query to pick up changes made in the same session
        db_session.expire_all()
        from app.models import ApiSource as _ApiSource

        updated = db_session.query(_ApiSource).filter_by(name="digikey").first()
        assert updated is not None
        assert updated.monthly_quota == 1000
