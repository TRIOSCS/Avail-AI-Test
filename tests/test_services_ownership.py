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
    send_manager_digest_email,
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


class TestGetAccountsAtRisk:
    def test_no_at_risk_accounts(self, db_session):
        result = get_accounts_at_risk(db_session)
        assert result == []

    def test_old_account_at_risk(self, db_session):
        sales = _make_sales_user(db_session)
        co = _make_company(
            db_session,
            owner_id=sales.id,
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
            db_session,
            name="My Account",
            owner_id=sales.id,
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
            db_session,
            name="Fresh Co",
            owner_id=sales.id,
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
            db_session,
            owner_id=sales.id,
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
            db_session,
            owner_id=sales.id,
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
            db_session,
            owner_id=sales.id,
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
            db_session,
            owner_id=sales.id,
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
            db_session,
            name="Risky Co",
            owner_id=sales.id,
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


# ═══════════════════════════════════════════════════════════════════════
#  send_manager_digest_email
# ═══════════════════════════════════════════════════════════════════════


class TestSendManagerDigestEmail:
    @pytest.mark.asyncio
    async def test_nothing_to_report_early_return(self, db_session):
        """No at-risk or cleared accounts → early return, no email sent."""
        with patch("app.services.ownership_service.settings") as mock_settings:
            mock_settings.admin_emails = ["admin@trioscs.com"]
            mock_settings.customer_inactivity_days = 30
            mock_settings.strategic_inactivity_days = 90

            # No at-risk accounts in empty DB → nothing to report
            await send_manager_digest_email(db_session)
            # Should not raise; early return

    @pytest.mark.asyncio
    async def test_sends_to_admin_emails(self, db_session):
        """Digest is sent to all admin emails when there are at-risk accounts."""
        # Create an at-risk account
        sales = _make_sales_user(db_session)
        admin = User(
            email="admin@trioscs.com",
            name="Admin",
            role="admin",
            azure_id="az-admin",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(admin)
        db_session.flush()
        _make_company(
            db_session,
            name="At Risk Co",
            owner_id=sales.id,
            last_activity_at=datetime.now(timezone.utc) - timedelta(days=25),
        )
        db_session.commit()

        with (
            patch("app.services.ownership_service.settings") as mock_settings,
            patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="fake-token"),
            patch("app.utils.graph_client.GraphClient") as mock_gc_class,
        ):
            mock_settings.admin_emails = ["admin@trioscs.com"]
            mock_settings.customer_inactivity_days = 30
            mock_settings.strategic_inactivity_days = 90
            mock_gc = mock_gc_class.return_value
            mock_gc.post_json = AsyncMock()

            await send_manager_digest_email(db_session)

        mock_gc.post_json.assert_called_once()

    @pytest.mark.asyncio
    async def test_graph_error_logged_not_raised(self, db_session):
        """Graph API error during digest send is logged, not raised."""
        sales = _make_sales_user(db_session)
        admin = User(
            email="admin@trioscs.com",
            name="Admin",
            role="admin",
            azure_id="az-admin-err",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(admin)
        db_session.flush()
        _make_company(
            db_session,
            name="Risk Co 2",
            owner_id=sales.id,
            last_activity_at=datetime.now(timezone.utc) - timedelta(days=25),
        )
        db_session.commit()

        with (
            patch("app.services.ownership_service.settings") as mock_settings,
            patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="fake-token"),
            patch("app.utils.graph_client.GraphClient") as mock_gc_class,
        ):
            mock_settings.admin_emails = ["admin@trioscs.com"]
            mock_settings.customer_inactivity_days = 30
            mock_settings.strategic_inactivity_days = 90
            mock_gc = mock_gc_class.return_value
            mock_gc.post_json = AsyncMock(side_effect=Exception("Graph API error"))

            # Should not raise
            await send_manager_digest_email(db_session)


# ═══════════════════════════════════════════════════════════════════════
#  get_my_accounts — status classification
# ═══════════════════════════════════════════════════════════════════════


class TestGetMyAccountsStatuses:
    def test_no_activity_status(self, db_session):
        """Account with no last_activity_at → 'no_activity'."""
        sales = _make_sales_user(db_session, "stat1@t.com")
        _make_company(db_session, name="No Activity Co", owner_id=sales.id, last_activity_at=None)
        db_session.commit()

        result = get_my_accounts(sales.id, db_session)
        assert len(result) == 1
        assert result[0]["status"] == "no_activity"

    def test_green_zone(self, db_session):
        """Recent activity → 'green'."""
        sales = _make_sales_user(db_session, "stat2@t.com")
        _make_company(
            db_session,
            name="Green Co",
            owner_id=sales.id,
            last_activity_at=datetime.now(timezone.utc) - timedelta(days=5),
        )
        db_session.commit()

        result = get_my_accounts(sales.id, db_session)
        assert result[0]["status"] == "green"

    def test_yellow_zone(self, db_session):
        """24 days inactive (in 23-30 window) → 'yellow'."""
        sales = _make_sales_user(db_session, "stat3@t.com")
        _make_company(
            db_session,
            name="Yellow Co",
            owner_id=sales.id,
            last_activity_at=datetime.now(timezone.utc) - timedelta(days=24),
        )
        db_session.commit()

        result = get_my_accounts(sales.id, db_session)
        assert result[0]["status"] == "yellow"

    def test_red_zone(self, db_session):
        """31 days inactive (past 30-day limit) → 'red'."""
        sales = _make_sales_user(db_session, "stat4@t.com")
        _make_company(
            db_session,
            name="Red Co",
            owner_id=sales.id,
            last_activity_at=datetime.now(timezone.utc) - timedelta(days=31),
        )
        db_session.commit()

        result = get_my_accounts(sales.id, db_session)
        assert result[0]["status"] == "red"

    def test_strategic_account_longer_limit(self, db_session):
        """Strategic account at 35 days → still 'green' (90-day limit)."""
        sales = _make_sales_user(db_session, "stat5@t.com")
        co = _make_company(
            db_session,
            name="Strategic Co",
            owner_id=sales.id,
            last_activity_at=datetime.now(timezone.utc) - timedelta(days=35),
        )
        co.is_strategic = True
        db_session.commit()

        result = get_my_accounts(sales.id, db_session)
        assert result[0]["status"] == "green"
        assert result[0]["is_strategic"] is True


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
    async def test_graph_error_caught(self, db_session):
        """Graph API error during warning email is caught, not raised."""
        from app.services.ownership_service import _send_warning_alert

        sales = _make_sales_user(db_session, "grapherr@t.com")
        co = _make_company(db_session, owner_id=sales.id)
        db_session.commit()

        with (
            patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="fake-token"),
            patch("app.utils.graph_client.GraphClient") as mock_gc_class,
        ):
            mock_gc = mock_gc_class.return_value
            mock_gc.post_json = AsyncMock(side_effect=Exception("Graph error"))
            # Should not raise — error is caught internally
            await _send_warning_alert(co, 25, 30, db_session)

    @pytest.mark.asyncio
    async def test_teams_error_caught(self, db_session):
        """Graph API error during warning email is caught, not raised."""
        from app.services.ownership_service import _send_warning_alert

        sales = _make_sales_user(db_session, "teamserr@t.com")
        co = _make_company(db_session, owner_id=sales.id)
        db_session.commit()

        with (
            patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="fake-token"),
            patch("app.utils.graph_client.GraphClient") as mock_gc_class,
        ):
            mock_gc = mock_gc_class.return_value
            mock_gc.post_json = AsyncMock(side_effect=Exception("Send failed"))
            # Should not raise — error is caught internally
            await _send_warning_alert(co, 25, 30, db_session)


# ═══════════════════════════════════════════════════════════════════════
#  send_manager_digest_email — recently_cleared and team_activity paths
# ═══════════════════════════════════════════════════════════════════════


class TestSendManagerDigestEmailPaths:
    @pytest.mark.asyncio
    async def test_recently_cleared_section(self, db_session):
        """Digest includes recently cleared accounts."""
        sales = _make_sales_user(db_session, "cleared@t.com")
        admin = User(
            email="digestadmin@trioscs.com",
            name="Digest Admin",
            role="admin",
            azure_id="az-digestadmin",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(admin)
        db_session.flush()
        co = _make_company(
            db_session,
            name="Cleared Co",
            owner_id=None,
            last_activity_at=datetime.now(timezone.utc) - timedelta(days=35),
        )
        co.ownership_cleared_at = datetime.now(timezone.utc) - timedelta(days=1)
        # Also add an at-risk account so the digest is not empty
        _make_company(
            db_session,
            name="Risk2",
            owner_id=sales.id,
            last_activity_at=datetime.now(timezone.utc) - timedelta(days=25),
        )
        db_session.commit()

        with (
            patch("app.services.ownership_service.settings") as mock_settings,
            patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="fake-token"),
            patch("app.utils.graph_client.GraphClient") as mock_gc_class,
        ):
            mock_settings.admin_emails = ["digestadmin@trioscs.com"]
            mock_settings.customer_inactivity_days = 30
            mock_settings.strategic_inactivity_days = 90
            mock_gc = mock_gc_class.return_value
            mock_gc.post_json = AsyncMock()

            await send_manager_digest_email(db_session)

        # Should have called post_json (sent email)
        mock_gc.post_json.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_admin_in_db_skips(self, db_session):
        """When admin email not found in DB, sending is skipped."""
        sales = _make_sales_user(db_session, "noadmin@t.com")
        _make_company(
            db_session,
            name="Risk3",
            owner_id=sales.id,
            last_activity_at=datetime.now(timezone.utc) - timedelta(days=25),
        )
        db_session.commit()

        with (
            patch("app.services.ownership_service.settings") as mock_settings,
            patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="fake-token"),
            patch("app.utils.graph_client.GraphClient") as mock_gc_class,
        ):
            mock_settings.admin_emails = ["nonexistent@trioscs.com"]
            mock_settings.customer_inactivity_days = 30
            mock_settings.strategic_inactivity_days = 90
            mock_gc = mock_gc_class.return_value
            mock_gc.post_json = AsyncMock()

            await send_manager_digest_email(db_session)

        # No admin found -> post_json should not be called
        mock_gc.post_json.assert_not_called()


# ═══════════════════════════════════════════════════════════════════════
#  Sweep — no created_at fallback edge case
# ═══════════════════════════════════════════════════════════════════════


class TestSweepNoCreatedAt:
    @pytest.mark.asyncio
    @patch("app.services.ownership_service._send_warning_alert", new_callable=AsyncMock)
    async def test_no_activity_no_created_at_forces_clear(self, mock_alert, db_session):
        """No last_activity_at AND no created_at -> days_inactive=999 -> cleared."""
        sales = _make_sales_user(db_session, "nocreated@t.com")
        co = _make_company(db_session, owner_id=sales.id, last_activity_at=None)
        co.created_at = None
        db_session.commit()

        result = await run_ownership_sweep(db_session)
        assert result["cleared"] >= 1
        db_session.refresh(co)
        assert co.account_owner_id is None


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
