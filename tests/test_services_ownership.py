"""test_services_ownership.py — Tests for ownership_service.

Tests account claiming, ownership sweep, at-risk detection,
and open pool logic. Uses in-memory SQLite.

Called by: pytest
Depends on: app/services/ownership_service.py, conftest.py
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from app.models import ActivityLog, Company, User
from app.services.ownership_service import (
    _was_warned_today,
    check_and_claim_open_account,
    run_ownership_sweep,
)

# ── Helpers ─────────────────────────────────────────────────────────


def _make_company(db, name="Test Co", owner_id=None, last_activity_at=None):
    co = Company(
        name=name,
        is_active=True,
        account_owner_id=owner_id,
        last_activity_at=last_activity_at,
        created_at=datetime.now(timezone.utc),
    )
    db.add(co)
    db.flush()
    return co


def _make_sales_user(db, email="sales1@trioscs.com"):
    u = User(
        email=email,
        name="Sales User",
        role="sales",
        azure_id=f"azure-{email}",
        created_at=datetime.now(timezone.utc),
    )
    db.add(u)
    db.flush()
    return u


# ── Auto-claim ──────────────────────────────────────────────────────


class TestCheckAndClaimOpenAccount:
    def test_claims_unowned_company(self, db_session):
        sales = _make_sales_user(db_session)
        co = _make_company(db_session)
        db_session.commit()

        result = check_and_claim_open_account(co.id, sales.id, db_session)
        assert result is True
        db_session.refresh(co)
        assert co.account_owner_id == sales.id

    def test_does_not_claim_owned_company(self, db_session):
        sales1 = _make_sales_user(db_session, "sales1@t.com")
        sales2 = _make_sales_user(db_session, "sales2@t.com")
        co = _make_company(db_session, owner_id=sales1.id)
        db_session.commit()

        result = check_and_claim_open_account(co.id, sales2.id, db_session)
        assert result is False
        db_session.refresh(co)
        assert co.account_owner_id == sales1.id

    def test_non_sales_cannot_claim(self, db_session, test_user):
        """test_user is a buyer — should not auto-claim."""
        co = _make_company(db_session)
        db_session.commit()

        result = check_and_claim_open_account(co.id, test_user.id, db_session)
        assert result is False


# ── At-risk accounts ────────────────────────────────────────────────


# ── Open pool ───────────────────────────────────────────────────────


# ── My accounts ─────────────────────────────────────────────────────


# ── Ownership sweep ────────────────────────────────────────────────


class TestRunOwnershipSweep:
    """WARNINGS-ONLY sweep (H5): reads the single account_sweep_inactivity_days
    threshold, warns 7 days before it, and NEVER clears ownership (SP4 does that).

    Each test pins the threshold to 90 so it is deterministic regardless of any local
    .env ACCOUNT_SWEEP_INACTIVITY_DAYS override.
    """

    @pytest.mark.asyncio
    @patch("app.services.ownership_service._send_warning_alert", new_callable=AsyncMock)
    async def test_stale_account_not_cleared(self, mock_alert, db_session):
        """200 days inactive → warned but ownership retained (SP4 clears, not this)."""
        sales = _make_sales_user(db_session)
        co = _make_company(
            db_session,
            owner_id=sales.id,
            last_activity_at=datetime.now(timezone.utc) - timedelta(days=200),
        )
        db_session.commit()

        with patch("app.services.ownership_service.settings") as ms:
            ms.account_sweep_inactivity_days = 90
            result = await run_ownership_sweep(db_session)
        assert "cleared" not in result
        assert result["warned"] >= 1
        db_session.refresh(co)
        assert co.account_owner_id == sales.id

    @pytest.mark.asyncio
    @patch("app.services.ownership_service._send_warning_alert", new_callable=AsyncMock)
    async def test_warning_zone_sends_alert(self, mock_alert, db_session):
        """85 days inactive (in the 83-90 warning window) → alert sent."""
        sales = _make_sales_user(db_session)
        _make_company(
            db_session,
            owner_id=sales.id,
            last_activity_at=datetime.now(timezone.utc) - timedelta(days=85),
        )
        db_session.commit()

        with patch("app.services.ownership_service.settings") as ms:
            ms.account_sweep_inactivity_days = 90
            result = await run_ownership_sweep(db_session)
        assert result["warned"] >= 1
        mock_alert.assert_called_once()

    @pytest.mark.asyncio
    @patch("app.services.ownership_service._send_warning_alert", new_callable=AsyncMock)
    async def test_account_within_threshold_no_action(self, mock_alert, db_session):
        """35 days inactive (< 90 threshold, < 83 warning) → no warn, ownership kept."""
        sales = _make_sales_user(db_session)
        co = _make_company(
            db_session,
            owner_id=sales.id,
            last_activity_at=datetime.now(timezone.utc) - timedelta(days=35),
        )
        db_session.commit()

        with patch("app.services.ownership_service.settings") as ms:
            ms.account_sweep_inactivity_days = 90
            result = await run_ownership_sweep(db_session)
        assert result["warned"] == 0
        db_session.refresh(co)
        assert co.account_owner_id == sales.id

    @pytest.mark.asyncio
    @patch("app.services.ownership_service._send_warning_alert", new_callable=AsyncMock)
    async def test_fresh_account_no_action(self, mock_alert, db_session):
        """5 days inactive → no warning, no clear."""
        sales = _make_sales_user(db_session)
        _make_company(
            db_session,
            owner_id=sales.id,
            last_activity_at=datetime.now(timezone.utc) - timedelta(days=5),
        )
        db_session.commit()

        with patch("app.services.ownership_service.settings") as ms:
            ms.account_sweep_inactivity_days = 90
            result = await run_ownership_sweep(db_session)
        assert result["warned"] == 0
        mock_alert.assert_not_called()

    @pytest.mark.asyncio
    @patch("app.services.ownership_service._send_warning_alert", new_callable=AsyncMock)
    async def test_no_activity_falls_back_to_created_at(self, mock_alert, db_session):
        """No last_activity_at → uses created_at (95 days) → warned, not cleared."""
        sales = _make_sales_user(db_session)
        co = _make_company(db_session, owner_id=sales.id, last_activity_at=None)
        co.created_at = datetime.now(timezone.utc) - timedelta(days=95)
        db_session.commit()

        with patch("app.services.ownership_service.settings") as ms:
            ms.account_sweep_inactivity_days = 90
            result = await run_ownership_sweep(db_session)
        assert result["warned"] >= 1
        db_session.refresh(co)
        assert co.account_owner_id == sales.id


# ── Manager digest ─────────────────────────────────────────────────


# ── _was_warned_today ──────────────────────────────────────────────


class TestWasWarnedToday:
    def test_no_prior_warning(self, db_session):
        assert _was_warned_today(1, 1, db_session) is False

    def test_warning_logged_today(self, db_session):
        sales = _make_sales_user(db_session)
        co = _make_company(db_session, owner_id=sales.id)
        log_entry = ActivityLog(
            user_id=sales.id,
            activity_type="ownership_warning",
            channel="system",
            company_id=co.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(log_entry)
        db_session.commit()

        assert _was_warned_today(co.id, sales.id, db_session) is True

    def test_warning_from_yesterday(self, db_session):
        sales = _make_sales_user(db_session)
        co = _make_company(db_session, owner_id=sales.id)
        log_entry = ActivityLog(
            user_id=sales.id,
            activity_type="ownership_warning",
            channel="system",
            company_id=co.id,
            created_at=datetime.now(timezone.utc) - timedelta(days=1),
        )
        db_session.add(log_entry)
        db_session.commit()

        assert _was_warned_today(co.id, sales.id, db_session) is False


# ═══════════════════════════════════════════════════════════════════════
#  send_manager_digest_email
# ═══════════════════════════════════════════════════════════════════════


# ═══════════════════════════════════════════════════════════════════════
#  get_my_accounts — status classification
# ═══════════════════════════════════════════════════════════════════════


# ═══════════════════════════════════════════════════════════════════════
#  _send_warning_alert — internal paths
# ═══════════════════════════════════════════════════════════════════════


class TestSendWarningAlert:
    @pytest.mark.asyncio
    async def test_no_owner_returns_early(self, db_session):
        """When owner doesn't exist, _send_warning_alert returns without error."""
        from app.services.ownership_service import _send_warning_alert

        co = _make_company(db_session, owner_id=None)
        db_session.commit()

        # Should not raise (no owner -> early return)
        await _send_warning_alert(co, 25, 30, db_session)

    @pytest.mark.asyncio
    async def test_no_token_skips_email(self, db_session):
        """When get_valid_token returns None, email is skipped."""
        from app.services.ownership_service import _send_warning_alert

        sales = _make_sales_user(db_session, "notoken@t.com")
        co = _make_company(db_session, owner_id=sales.id)
        db_session.commit()

        with (
            patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value=None),
            patch("app.utils.graph_client.GraphClient") as mock_gc_class,
        ):
            await _send_warning_alert(co, 25, 30, db_session)
            # GraphClient should never be instantiated
            mock_gc_class.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("email", "error_message"),
        [
            ("grapherr@t.com", "Graph error"),
            ("teamserr@t.com", "Send failed"),
        ],
        ids=["graph_error", "teams_error"],
    )
    async def test_post_json_error_caught(self, db_session, email, error_message):
        """Graph API error during warning email is caught, not raised."""
        from app.services.ownership_service import _send_warning_alert

        sales = _make_sales_user(db_session, email)
        co = _make_company(db_session, owner_id=sales.id)
        db_session.commit()

        with (
            patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="fake-token"),
            patch("app.utils.graph_client.GraphClient") as mock_gc_class,
        ):
            mock_gc = mock_gc_class.return_value
            mock_gc.post_json = AsyncMock(side_effect=Exception(error_message))
            # Should not raise — error is caught internally
            await _send_warning_alert(co, 25, 30, db_session)


# ═══════════════════════════════════════════════════════════════════════
#  send_manager_digest_email — recently_cleared and team_activity paths
# ═══════════════════════════════════════════════════════════════════════


# ═══════════════════════════════════════════════════════════════════════
#  Sweep — no created_at fallback edge case
# ═══════════════════════════════════════════════════════════════════════


class TestSweepNoCreatedAt:
    @pytest.mark.asyncio
    @patch("app.services.ownership_service._send_warning_alert", new_callable=AsyncMock)
    async def test_no_activity_no_created_at_warns_not_cleared(self, mock_alert, db_session):
        """No last_activity_at AND no created_at -> days_inactive=999 -> warned,
        kept."""
        sales = _make_sales_user(db_session, "nocreated@t.com")
        co = _make_company(db_session, owner_id=sales.id, last_activity_at=None)
        co.created_at = None
        db_session.commit()

        with patch("app.services.ownership_service.settings") as ms:
            ms.account_sweep_inactivity_days = 90
            result = await run_ownership_sweep(db_session)
        assert "cleared" not in result
        assert result["warned"] >= 1
        db_session.refresh(co)
        # Warnings-only sweep never clears ownership.
        assert co.account_owner_id == sales.id


# ═══════════════════════════════════════════════════════════════════════
#  check_and_claim_open_account — trader role
# ═══════════════════════════════════════════════════════════════════════


class TestClaimTraderRole:
    def test_trader_can_claim(self, db_session):
        """Trader role should be able to claim open accounts."""
        trader = User(
            email="claimtrader@trioscs.com",
            name="Claim Trader",
            role="trader",
            azure_id="az-claim-trader",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(trader)
        db_session.flush()
        co = _make_company(db_session)
        db_session.commit()

        result = check_and_claim_open_account(co.id, trader.id, db_session)
        assert result is True

    def test_nonexistent_company(self, db_session):
        """Claiming a non-existent company returns False."""
        sales = _make_sales_user(db_session, "nocompany@t.com")
        db_session.commit()
        assert check_and_claim_open_account(99999, sales.id, db_session) is False

    def test_nonexistent_user(self, db_session):
        """Claiming with a non-existent user returns False."""
        co = _make_company(db_session)
        db_session.commit()
        assert check_and_claim_open_account(co.id, 99999, db_session) is False
