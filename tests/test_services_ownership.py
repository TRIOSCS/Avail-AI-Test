"""
test_services_ownership.py — Tests for ownership_service.

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
    get_accounts_at_risk,
    get_manager_digest,
    get_my_accounts,
    get_open_pool_accounts,
    run_ownership_sweep,
)

# ── Helpers ─────────────────────────────────────────────────────────


def _make_company(db, name="Test Co", owner_id=None, last_activity_at=None):
    co = Company(
        name=name, is_active=True,
        account_owner_id=owner_id,
        last_activity_at=last_activity_at,
        created_at=datetime.now(timezone.utc),
    )
    db.add(co)
    db.flush()
    return co


def _make_sales_user(db, email="sales1@trioscs.com"):
    u = User(
        email=email, name="Sales User", role="sales",
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


class TestGetAccountsAtRisk:
    def test_no_at_risk_accounts(self, db_session):
        result = get_accounts_at_risk(db_session)
        assert result == []

    def test_old_account_at_risk(self, db_session):
        sales = _make_sales_user(db_session)
        co = _make_company(
            db_session, owner_id=sales.id,
            last_activity_at=datetime.now(timezone.utc) - timedelta(days=25),
        )
        db_session.commit()

        result = get_accounts_at_risk(db_session)
        # Should show as at-risk (within 7-day warning window of 30-day limit)
        names = [r["company_name"] for r in result]
        assert co.name in names


# ── Open pool ───────────────────────────────────────────────────────


class TestGetOpenPoolAccounts:
    def test_unowned_companies_in_pool(self, db_session):
        _make_company(db_session, name="Unowned Corp")
        db_session.commit()

        result = get_open_pool_accounts(db_session)
        names = [r["company_name"] for r in result]
        assert "Unowned Corp" in names

    def test_owned_companies_not_in_pool(self, db_session):
        sales = _make_sales_user(db_session)
        _make_company(db_session, name="Owned Corp", owner_id=sales.id)
        db_session.commit()

        result = get_open_pool_accounts(db_session)
        names = [r["company_name"] for r in result]
        assert "Owned Corp" not in names


# ── My accounts ─────────────────────────────────────────────────────


class TestGetMyAccounts:
    def test_returns_owned_accounts(self, db_session):
        sales = _make_sales_user(db_session)
        _make_company(
            db_session, name="My Account", owner_id=sales.id,
            last_activity_at=datetime.now(timezone.utc),
        )
        db_session.commit()

        result = get_my_accounts(sales.id, db_session)
        names = [r["company_name"] for r in result]
        assert "My Account" in names

    def test_excludes_other_users(self, db_session):
        sales1 = _make_sales_user(db_session, "s1@t.com")
        sales2 = _make_sales_user(db_session, "s2@t.com")
        _make_company(db_session, name="S1 Account", owner_id=sales1.id)
        _make_company(db_session, name="S2 Account", owner_id=sales2.id)
        db_session.commit()

        result = get_my_accounts(sales1.id, db_session)
        names = [r["company_name"] for r in result]
        assert "S1 Account" in names
        assert "S2 Account" not in names

    def test_health_status_green(self, db_session):
        sales = _make_sales_user(db_session)
        _make_company(
            db_session, name="Fresh Co", owner_id=sales.id,
            last_activity_at=datetime.now(timezone.utc) - timedelta(days=2),
        )
        db_session.commit()

        result = get_my_accounts(sales.id, db_session)
        assert result[0]["status"] in ("green", "no_activity")


# ── Ownership sweep ────────────────────────────────────────────────


class TestRunOwnershipSweep:
    @pytest.mark.asyncio
    @patch("app.services.ownership_service._send_warning_alert", new_callable=AsyncMock)
    async def test_stale_account_cleared(self, mock_alert, db_session):
        """35 days inactive (>30 limit) → ownership cleared."""
        sales = _make_sales_user(db_session)
        co = _make_company(
            db_session, owner_id=sales.id,
            last_activity_at=datetime.now(timezone.utc) - timedelta(days=35),
        )
        db_session.commit()

        result = await run_ownership_sweep(db_session)
        assert result["cleared"] >= 1
        db_session.refresh(co)
        assert co.account_owner_id is None

    @pytest.mark.asyncio
    @patch("app.services.ownership_service._send_warning_alert", new_callable=AsyncMock)
    async def test_warning_zone_sends_alert(self, mock_alert, db_session):
        """24 days inactive (in 23-30 warning window) → alert sent."""
        sales = _make_sales_user(db_session)
        _make_company(
            db_session, owner_id=sales.id,
            last_activity_at=datetime.now(timezone.utc) - timedelta(days=24),
        )
        db_session.commit()

        result = await run_ownership_sweep(db_session)
        assert result["warned"] >= 1
        mock_alert.assert_called_once()

    @pytest.mark.asyncio
    @patch("app.services.ownership_service._send_warning_alert", new_callable=AsyncMock)
    async def test_strategic_account_not_cleared_at_35_days(self, mock_alert, db_session):
        """Strategic account (90-day limit) at 35 days → NOT cleared."""
        sales = _make_sales_user(db_session)
        co = _make_company(
            db_session, owner_id=sales.id,
            last_activity_at=datetime.now(timezone.utc) - timedelta(days=35),
        )
        co.is_strategic = True
        db_session.commit()

        result = await run_ownership_sweep(db_session)
        assert result["cleared"] == 0
        db_session.refresh(co)
        assert co.account_owner_id == sales.id

    @pytest.mark.asyncio
    @patch("app.services.ownership_service._send_warning_alert", new_callable=AsyncMock)
    async def test_fresh_account_no_action(self, mock_alert, db_session):
        """5 days inactive → no warning, no clear."""
        sales = _make_sales_user(db_session)
        _make_company(
            db_session, owner_id=sales.id,
            last_activity_at=datetime.now(timezone.utc) - timedelta(days=5),
        )
        db_session.commit()

        result = await run_ownership_sweep(db_session)
        assert result["cleared"] == 0
        assert result["warned"] == 0
        mock_alert.assert_not_called()

    @pytest.mark.asyncio
    @patch("app.services.ownership_service._send_warning_alert", new_callable=AsyncMock)
    async def test_no_activity_falls_back_to_created_at(self, mock_alert, db_session):
        """No last_activity_at → uses created_at as baseline."""
        sales = _make_sales_user(db_session)
        co = _make_company(db_session, owner_id=sales.id, last_activity_at=None)
        co.created_at = datetime.now(timezone.utc) - timedelta(days=35)
        db_session.commit()

        result = await run_ownership_sweep(db_session)
        assert result["cleared"] >= 1
        db_session.refresh(co)
        assert co.account_owner_id is None


# ── Manager digest ─────────────────────────────────────────────────


class TestGetManagerDigest:
    def test_empty_db_returns_zeroed_structure(self, db_session):
        digest = get_manager_digest(db_session)
        assert digest["at_risk_count"] == 0
        assert digest["at_risk_accounts"] == []
        assert digest["recently_cleared"] == []
        assert "generated_at" in digest

    def test_at_risk_accounts_appear(self, db_session):
        sales = _make_sales_user(db_session)
        _make_company(
            db_session, name="Risky Co", owner_id=sales.id,
            last_activity_at=datetime.now(timezone.utc) - timedelta(days=25),
        )
        db_session.commit()

        digest = get_manager_digest(db_session)
        assert digest["at_risk_count"] >= 1
        names = [a["company_name"] for a in digest["at_risk_accounts"]]
        assert "Risky Co" in names

    def test_team_activity_populated(self, db_session):
        sales = _make_sales_user(db_session)
        db_session.commit()

        digest = get_manager_digest(db_session)
        user_ids = [t["user_id"] for t in digest["team_activity"]]
        assert sales.id in user_ids


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
