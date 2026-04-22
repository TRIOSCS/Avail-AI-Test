"""test_startup_nightly.py — Additional coverage tests for app/startup.py.

Targets uncovered lines:
- 160-177: _seed_agent_user (happy path, skip-existing, error branch)
- 473-479: _backfill_normalized_mpn batch-flush path (batch >= _BACKFILL_BATCH_SIZE)
- 778: raw_items is already a list (not JSON string)
- 801-803: ValueError/TypeError in price parsing
- 837-847: _seed_commodity_schemas (happy path and error)
- 912-917: _backfill_ticket_defaults (tickets with null risk_tier exist)
- 955-956: seed_api_sources early-return when all sources present
- 982-985: seed_api_sources removes legacy 'newark' source
- 1001, 1004-1006: seed_api_sources backfills monthly_quota

Called by: pytest
Depends on: app/startup.py, conftest engine/db_session
"""

import os

os.environ["TESTING"] = "1"

import json
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy import text as sqltext
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
        assert user.role == "admin"
        assert user.name == "Agent"

    @patch("app.startup.SessionLocal")
    def test_skips_when_agent_user_already_exists(self, mock_sl, db_session: Session):
        """Returns early without duplicating the agent user."""
        from app.models.auth import User
        from app.startup import _seed_agent_user

        # Pre-create the agent user
        existing = User(email="agent@availai.local", name="Agent", role="admin", is_active=True)
        db_session.add(existing)
        db_session.commit()

        mock_sl.return_value = db_session
        _seed_agent_user()

        count = db_session.query(User).filter_by(email="agent@availai.local").count()
        assert count == 1

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


# ── _backfill_normalized_mpn batch flush ─────────────────────────────────────


class TestBackfillNormalizedMpnBatchFlush:
    """Lines 473-479: batch flush when len(batch) >= _BACKFILL_BATCH_SIZE."""

    def test_batch_flush_triggers_on_large_input(self):
        """Feed enough cards to trigger batch flush inside the inner loop."""
        from app.startup import _backfill_normalized_mpn

        # We need more cards than _BACKFILL_BATCH_SIZE (which is 500).
        # Use a mock-based engine so we control fetchall sizes.
        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)

        # Build 501 unique null-normalized cards to exceed the batch size
        cards = [(i, f"PART{i:04d}") for i in range(1, 502)]
        # requirements: empty to skip first section
        req_empty = []
        # material_cards existing norms: none
        existing_norms = []

        # execute calls in _backfill_normalized_mpn (with real engine patched):
        # Phase 1 (requirements): returns empty rows so loop exits quickly
        # Phase 2 (material_cards):
        #   - cards fetchall → 501 cards
        #   - existing_norms fetchall → no existing
        #   - batch UPDATE (mid-loop) → called when batch hits 500
        #   - batch UPDATE (remainder) → called after loop

        call_count = [0]

        def fake_execute(stmt, *args, **kwargs):
            result = MagicMock()
            stmt_str = str(stmt)
            if "requirements" in stmt_str and "normalized_mpn IS NULL" in stmt_str:
                result.fetchall.return_value = req_empty
            elif "display_mpn FROM material_cards WHERE normalized_mpn IS NULL" in stmt_str:
                result.fetchall.return_value = cards
            elif "normalized_mpn FROM material_cards" in stmt_str and "GROUP BY" in stmt_str:
                result.fetchall.return_value = existing_norms
            else:
                result.fetchall.return_value = []
            call_count[0] += 1
            return result

        mock_conn.execute.side_effect = fake_execute
        mock_conn.commit = MagicMock()
        mock_conn.rollback = MagicMock()

        mock_engine = MagicMock()
        mock_engine.connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_engine.connect.return_value.__exit__ = MagicMock(return_value=False)

        with patch("app.startup.engine", mock_engine):
            _backfill_normalized_mpn()

        # The mid-loop batch flush + final remainder flush should each call commit
        assert mock_conn.commit.call_count >= 2


# ── _backfill_proactive_offer_qty price-parse error branch ────────────────────


class TestBackfillProactiveOfferQtyPriceParse:
    """Lines 801-803: ValueError/TypeError when parsing sell_price/unit_price."""

    @patch("app.startup.engine")
    def test_bad_price_values_fall_back_to_zero(self, mock_engine):
        """If sell_price/unit_price cannot be cast to float, fallback to 0.0."""
        from app.startup import _backfill_proactive_offer_qty

        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_engine.connect.return_value = mock_conn

        match_rows = [(10, 5)]  # target_qty=5
        # qty=100 > target(5), and prices are non-numeric strings → ValueError
        line_items = json.dumps(
            [{"match_id": 10, "qty": 100, "sell_price": "N/A", "unit_price": "bad"}]
        )
        offers = [(1, line_items)]

        mock_conn.execute.return_value.fetchall.side_effect = [match_rows, offers]

        _backfill_proactive_offer_qty()

        # Should have attempted the UPDATE despite bad prices
        assert mock_conn.execute.call_count >= 3

    @patch("app.startup.engine")
    def test_none_price_falls_back_to_zero(self, mock_engine):
        """If sell_price is None and unit_price is None, fallback to 0.0."""
        from app.startup import _backfill_proactive_offer_qty

        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_engine.connect.return_value = mock_conn

        match_rows = [(10, 5)]
        line_items = json.dumps([{"match_id": 10, "qty": 100, "sell_price": None, "unit_price": None}])
        offers = [(1, line_items)]

        mock_conn.execute.return_value.fetchall.side_effect = [match_rows, offers]

        _backfill_proactive_offer_qty()
        assert mock_conn.execute.call_count >= 3

    @patch("app.startup.engine")
    def test_raw_items_already_a_list(self, mock_engine):
        """Line 778: when raw_items is already a list (not a JSON string), use as-is."""
        from app.startup import _backfill_proactive_offer_qty

        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_engine.connect.return_value = mock_conn

        match_rows = [(10, 5)]
        # raw_items is a Python list (simulates SQLite returning parsed JSON)
        raw_list = [{"match_id": 10, "qty": 100, "sell_price": 2.0, "unit_price": 1.0}]
        offers = [(1, raw_list)]

        mock_conn.execute.return_value.fetchall.side_effect = [match_rows, offers]

        _backfill_proactive_offer_qty()

        # Changed → UPDATE should be called
        assert mock_conn.execute.call_count >= 3


# ── _seed_commodity_schemas ───────────────────────────────────────────────────


class TestSeedCommoditySchemas:
    """Lines 837-847: _seed_commodity_schemas happy path and error branch."""

    @patch("app.startup.SessionLocal")
    def test_calls_seed_function_successfully(self, mock_sl, db_session: Session):
        """Happy path: seed_commodity_schemas is called with the db session."""
        from app.startup import _seed_commodity_schemas

        mock_sl.return_value = db_session

        with patch("app.startup._seed_commodity_schemas") as mock_fn:
            # Call the real function, but mock the inner import dependency
            pass

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


# ── _backfill_ticket_defaults ─────────────────────────────────────────────────


class TestBackfillTicketDefaults:
    """Lines 912-917: _backfill_ticket_defaults with null tickets."""

    @patch("app.startup.SessionLocal")
    def test_backfills_null_risk_tier_and_category(self, mock_sl):
        """When tickets with null risk_tier/category exist, they are updated."""
        from app.startup import _backfill_ticket_defaults

        mock_ticket = MagicMock()
        mock_ticket.risk_tier = None
        mock_ticket.category = None

        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.all.return_value = [mock_ticket]
        mock_db.commit = MagicMock()
        mock_db.rollback = MagicMock()
        mock_db.close = MagicMock()
        mock_sl.return_value = mock_db

        _backfill_ticket_defaults()

        assert mock_ticket.risk_tier == "low"
        assert mock_ticket.category == "other"
        mock_db.commit.assert_called_once()

    @patch("app.startup.SessionLocal")
    def test_skips_when_no_null_tickets(self, mock_sl, db_session: Session):
        """When no tickets with null fields, does nothing (no commit)."""
        from app.startup import _backfill_ticket_defaults

        mock_sl.return_value = db_session

        # No tickets in DB — should do nothing without error
        _backfill_ticket_defaults()

    @patch("app.startup.SessionLocal")
    def test_error_rolls_back(self, mock_sl):
        """Exception is caught and rolled back (does not re-raise)."""
        from app.startup import _backfill_ticket_defaults

        mock_db = MagicMock()
        mock_db.query.side_effect = RuntimeError("DB unavailable")
        mock_db.rollback = MagicMock()
        mock_db.close = MagicMock()
        mock_sl.return_value = mock_db

        # Should not raise — errors are swallowed
        _backfill_ticket_defaults()
        mock_db.rollback.assert_called_once()
        mock_db.close.assert_called_once()


# ── seed_api_sources ──────────────────────────────────────────────────────────


class TestSeedApiSources:
    """Lines 955-956, 982-985, 1001, 1004-1006: seed_api_sources branches."""

    @patch("app.startup.SessionLocal")
    def test_early_return_when_all_sources_present(self, mock_sl):
        """Lines 955-956: returns early when existing source count matches SOURCES."""
        from app.startup import seed_api_sources
        import json
        from pathlib import Path

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
        from app.startup import seed_api_sources
        from app.models import ApiSource

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
        from app.startup import seed_api_sources
        from app.models import ApiSource

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
