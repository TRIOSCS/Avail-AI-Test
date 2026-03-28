"""Tests for app/services/ownership_service.py — customer ownership lifecycle.

Called by: pytest
Depends on: conftest fixtures, unittest.mock
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from app.models import ActivityLog, Company, CustomerSite, User

# ═══════════════════════════════════════════════════════════════════════
#  HELPERS — _days_since_activity, _clear_ownership, _was_warned_today
# ═══════════════════════════════════════════════════════════════════════


class TestDaysSinceActivity:
    """Tests for _days_since_activity()."""

    def test_returns_none_when_no_activity(self, db_session: Session, test_company: Company):
        """No last_activity_at returns None."""
        from app.services.ownership_service import _days_since_activity

        test_company.last_activity_at = None
        result = _days_since_activity(test_company, datetime.now(timezone.utc))
        assert result is None

    def test_returns_days_since_last_activity(self, db_session: Session, test_company: Company):
        """Correct day count when last_activity_at is set."""
        from app.services.ownership_service import _days_since_activity

        now = datetime.now(timezone.utc)
        test_company.last_activity_at = now - timedelta(days=10)
        result = _days_since_activity(test_company, now)
        assert result == 10

    def test_handles_naive_datetime(self, db_session: Session, test_company: Company):
        """Naive datetimes get UTC timezone applied."""
        from app.services.ownership_service import _days_since_activity

        now = datetime.now(timezone.utc)
        test_company.last_activity_at = (now - timedelta(days=5)).replace(tzinfo=None)
        result = _days_since_activity(test_company, now)
        assert result == 5


class TestClearOwnership:
    """Tests for _clear_ownership()."""

    def test_clears_owner_and_sets_timestamp(self, db_session: Session, test_company: Company, sales_user: User):
        """Ownership is cleared and timestamp set."""
        from app.services.ownership_service import _clear_ownership

        test_company.account_owner_id = sales_user.id
        db_session.flush()

        _clear_ownership(test_company, db_session)

        assert test_company.account_owner_id is None
        assert test_company.ownership_cleared_at is not None


class TestWasWarnedToday:
    """Tests for _was_warned_today()."""

    def test_returns_false_when_no_warning(self, db_session: Session, test_company: Company, sales_user: User):
        """No warning logged today returns False."""
        from app.services.ownership_service import _was_warned_today

        result = _was_warned_today(test_company.id, sales_user.id, db_session)
        assert result is False

    def test_returns_true_when_warned_today(self, db_session: Session, test_company: Company, sales_user: User):
        """Warning logged today returns True."""
        from app.services.ownership_service import _was_warned_today

        warning = ActivityLog(
            user_id=sales_user.id,
            activity_type="ownership_warning",
            channel="system",
            company_id=test_company.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(warning)
        db_session.flush()

        result = _was_warned_today(test_company.id, sales_user.id, db_session)
        assert result is True


# ═══════════════════════════════════════════════════════════════════════
#  SITE HELPERS — _site_days_since_activity
# ═══════════════════════════════════════════════════════════════════════


class TestSiteDaysSinceActivity:
    """Tests for _site_days_since_activity()."""

    def test_returns_none_when_no_activity(self, db_session: Session, test_customer_site: CustomerSite):
        """No last_activity_at returns None."""
        from app.services.ownership_service import _site_days_since_activity

        test_customer_site.last_activity_at = None
        result = _site_days_since_activity(test_customer_site, datetime.now(timezone.utc))
        assert result is None

    def test_returns_correct_days(self, db_session: Session, test_customer_site: CustomerSite):
        """Correct day count."""
        from app.services.ownership_service import _site_days_since_activity

        now = datetime.now(timezone.utc)
        test_customer_site.last_activity_at = now - timedelta(days=15)
        result = _site_days_since_activity(test_customer_site, now)
        assert result == 15

    def test_handles_naive_datetime(self, db_session: Session, test_customer_site: CustomerSite):
        """Naive datetimes get UTC timezone applied."""
        from app.services.ownership_service import _site_days_since_activity

        now = datetime.now(timezone.utc)
        test_customer_site.last_activity_at = (now - timedelta(days=7)).replace(tzinfo=None)
        result = _site_days_since_activity(test_customer_site, now)
        assert result == 7


# ═══════════════════════════════════════════════════════════════════════
#  run_ownership_sweep
# ═══════════════════════════════════════════════════════════════════════


class TestRunOwnershipSweep:
    """Tests for run_ownership_sweep()."""

    @pytest.mark.asyncio
    async def test_clears_stale_ownership(self, db_session: Session, test_company: Company, sales_user: User):
        """Company inactive >30 days loses ownership."""
        from app.services.ownership_service import run_ownership_sweep

        test_company.account_owner_id = sales_user.id
        test_company.last_activity_at = datetime.now(timezone.utc) - timedelta(days=31)
        test_company.is_active = True
        db_session.commit()

        with patch("app.services.ownership_service.settings") as mock_settings:
            mock_settings.customer_inactivity_days = 30
            mock_settings.strategic_inactivity_days = 90
            result = await run_ownership_sweep(db_session)

        assert result["cleared"] == 1
        db_session.refresh(test_company)
        assert test_company.account_owner_id is None

    @pytest.mark.asyncio
    async def test_warns_in_warning_zone(self, db_session: Session, test_company: Company, sales_user: User):
        """Company in warning zone (day 23-29) gets warning."""
        from app.services.ownership_service import run_ownership_sweep

        test_company.account_owner_id = sales_user.id
        test_company.last_activity_at = datetime.now(timezone.utc) - timedelta(days=25)
        test_company.is_active = True
        db_session.commit()

        mock_send = AsyncMock()

        with (
            patch("app.services.ownership_service.settings") as mock_settings,
            patch("app.services.ownership_service._send_warning_alert", mock_send),
        ):
            mock_settings.customer_inactivity_days = 30
            mock_settings.strategic_inactivity_days = 90
            result = await run_ownership_sweep(db_session)

        assert result["warned"] == 1
        mock_send.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_does_not_warn_twice_same_day(self, db_session: Session, test_company: Company, sales_user: User):
        """No duplicate warnings on same day."""
        from app.services.ownership_service import run_ownership_sweep

        test_company.account_owner_id = sales_user.id
        test_company.last_activity_at = datetime.now(timezone.utc) - timedelta(days=25)
        test_company.is_active = True
        db_session.flush()

        # Create existing warning for today
        warning = ActivityLog(
            user_id=sales_user.id,
            activity_type="ownership_warning",
            channel="system",
            company_id=test_company.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(warning)
        db_session.commit()

        mock_send = AsyncMock()

        with (
            patch("app.services.ownership_service.settings") as mock_settings,
            patch("app.services.ownership_service._send_warning_alert", mock_send),
        ):
            mock_settings.customer_inactivity_days = 30
            mock_settings.strategic_inactivity_days = 90
            result = await run_ownership_sweep(db_session)

        assert result["warned"] == 0
        mock_send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_strategic_accounts_get_90_day_limit(
        self, db_session: Session, test_company: Company, sales_user: User
    ):
        """Strategic accounts use 90-day inactivity limit."""
        from app.services.ownership_service import run_ownership_sweep

        test_company.account_owner_id = sales_user.id
        test_company.is_strategic = True
        test_company.last_activity_at = datetime.now(timezone.utc) - timedelta(days=35)
        test_company.is_active = True
        db_session.commit()

        with patch("app.services.ownership_service.settings") as mock_settings:
            mock_settings.customer_inactivity_days = 30
            mock_settings.strategic_inactivity_days = 90
            result = await run_ownership_sweep(db_session)

        # 35 days < 90, should NOT be cleared
        assert result["cleared"] == 0
        db_session.refresh(test_company)
        assert test_company.account_owner_id == sales_user.id

    @pytest.mark.asyncio
    async def test_no_activity_uses_created_at(self, db_session: Session, test_company: Company, sales_user: User):
        """No activity at all uses created_at as baseline."""
        from app.services.ownership_service import run_ownership_sweep

        test_company.account_owner_id = sales_user.id
        test_company.last_activity_at = None
        test_company.created_at = datetime.now(timezone.utc) - timedelta(days=40)
        test_company.is_active = True
        db_session.commit()

        with patch("app.services.ownership_service.settings") as mock_settings:
            mock_settings.customer_inactivity_days = 30
            mock_settings.strategic_inactivity_days = 90
            result = await run_ownership_sweep(db_session)

        assert result["cleared"] == 1

    @pytest.mark.asyncio
    async def test_no_activity_no_created_at_forces_clear(self, db_session: Session, sales_user: User):
        """No activity and no created_at forces clear (999 days)."""
        from app.services.ownership_service import run_ownership_sweep

        co = Company(
            name="Ghost Corp",
            is_active=True,
            account_owner_id=sales_user.id,
            last_activity_at=None,
            created_at=None,
        )
        db_session.add(co)
        db_session.commit()

        with patch("app.services.ownership_service.settings") as mock_settings:
            mock_settings.customer_inactivity_days = 30
            mock_settings.strategic_inactivity_days = 90
            result = await run_ownership_sweep(db_session)

        assert result["cleared"] >= 1

    @pytest.mark.asyncio
    async def test_recent_activity_keeps_ownership(self, db_session: Session, test_company: Company, sales_user: User):
        """Active company within 23 days keeps ownership, no warning."""
        from app.services.ownership_service import run_ownership_sweep

        test_company.account_owner_id = sales_user.id
        test_company.last_activity_at = datetime.now(timezone.utc) - timedelta(days=10)
        test_company.is_active = True
        db_session.commit()

        with patch("app.services.ownership_service.settings") as mock_settings:
            mock_settings.customer_inactivity_days = 30
            mock_settings.strategic_inactivity_days = 90
            result = await run_ownership_sweep(db_session)

        assert result["cleared"] == 0
        assert result["warned"] == 0

    @pytest.mark.asyncio
    async def test_empty_owned_list(self, db_session: Session):
        """No owned companies means no work done."""
        from app.services.ownership_service import run_ownership_sweep

        with patch("app.services.ownership_service.settings") as mock_settings:
            mock_settings.customer_inactivity_days = 30
            mock_settings.strategic_inactivity_days = 90
            result = await run_ownership_sweep(db_session)

        assert result["total_owned"] == 0
        assert result["cleared"] == 0
        assert result["warned"] == 0


# ═══════════════════════════════════════════════════════════════════════
#  check_and_claim_open_account
# ═══════════════════════════════════════════════════════════════════════


class TestCheckAndClaimOpenAccount:
    """Tests for check_and_claim_open_account()."""

    def test_sales_user_claims_unowned_company(self, db_session: Session, test_company: Company, sales_user: User):
        """Sales user can claim an unowned company."""
        from app.services.ownership_service import check_and_claim_open_account

        test_company.account_owner_id = None
        db_session.commit()

        result = check_and_claim_open_account(test_company.id, sales_user.id, db_session)

        assert result is True
        db_session.refresh(test_company)
        assert test_company.account_owner_id == sales_user.id

    def test_trader_user_claims_unowned_company(self, db_session: Session, test_company: Company, trader_user: User):
        """Trader user can claim an unowned company."""
        from app.services.ownership_service import check_and_claim_open_account

        test_company.account_owner_id = None
        db_session.commit()

        result = check_and_claim_open_account(test_company.id, trader_user.id, db_session)
        assert result is True

    def test_buyer_cannot_claim(self, db_session: Session, test_company: Company, test_user: User):
        """Buyer role cannot claim accounts."""
        from app.services.ownership_service import check_and_claim_open_account

        test_company.account_owner_id = None
        db_session.commit()

        result = check_and_claim_open_account(test_company.id, test_user.id, db_session)
        assert result is False

    def test_already_owned_returns_false(self, db_session: Session, test_company: Company, sales_user: User):
        """Cannot claim a company already owned by someone."""
        from app.services.ownership_service import check_and_claim_open_account

        test_company.account_owner_id = sales_user.id
        db_session.commit()

        result = check_and_claim_open_account(test_company.id, sales_user.id, db_session)
        assert result is False

    def test_nonexistent_company_returns_false(self, db_session: Session, sales_user: User):
        """Non-existent company ID returns False."""
        from app.services.ownership_service import check_and_claim_open_account

        result = check_and_claim_open_account(99999, sales_user.id, db_session)
        assert result is False

    def test_nonexistent_user_returns_false(self, db_session: Session, test_company: Company):
        """Non-existent user ID returns False."""
        from app.services.ownership_service import check_and_claim_open_account

        test_company.account_owner_id = None
        db_session.commit()

        result = check_and_claim_open_account(test_company.id, 99999, db_session)
        assert result is False

    def test_clears_ownership_cleared_at(self, db_session: Session, test_company: Company, sales_user: User):
        """Claiming clears the ownership_cleared_at timestamp."""
        from app.services.ownership_service import check_and_claim_open_account

        test_company.account_owner_id = None
        test_company.ownership_cleared_at = datetime.now(timezone.utc)
        db_session.commit()

        check_and_claim_open_account(test_company.id, sales_user.id, db_session)

        db_session.refresh(test_company)
        assert test_company.ownership_cleared_at is None


# ═══════════════════════════════════════════════════════════════════════
#  get_accounts_at_risk
# ═══════════════════════════════════════════════════════════════════════


class TestGetAccountsAtRisk:
    """Tests for get_accounts_at_risk()."""

    def test_returns_at_risk_accounts(self, db_session: Session, test_company: Company, sales_user: User):
        """Accounts in warning zone are returned."""
        from app.services.ownership_service import get_accounts_at_risk

        test_company.account_owner_id = sales_user.id
        test_company.last_activity_at = datetime.now(timezone.utc) - timedelta(days=25)
        test_company.is_active = True
        db_session.commit()

        with patch("app.services.ownership_service.settings") as mock_settings:
            mock_settings.customer_inactivity_days = 30
            mock_settings.strategic_inactivity_days = 90
            result = get_accounts_at_risk(db_session)

        assert len(result) == 1
        assert result[0]["company_id"] == test_company.id
        assert result[0]["days_remaining"] == 5

    def test_excludes_healthy_accounts(self, db_session: Session, test_company: Company, sales_user: User):
        """Accounts with recent activity are excluded."""
        from app.services.ownership_service import get_accounts_at_risk

        test_company.account_owner_id = sales_user.id
        test_company.last_activity_at = datetime.now(timezone.utc) - timedelta(days=5)
        test_company.is_active = True
        db_session.commit()

        with patch("app.services.ownership_service.settings") as mock_settings:
            mock_settings.customer_inactivity_days = 30
            mock_settings.strategic_inactivity_days = 90
            result = get_accounts_at_risk(db_session)

        assert len(result) == 0

    def test_sorts_by_most_urgent(self, db_session: Session, sales_user: User):
        """Results are sorted with most urgent first."""
        from app.services.ownership_service import get_accounts_at_risk

        now = datetime.now(timezone.utc)
        co1 = Company(
            name="Urgent", is_active=True, account_owner_id=sales_user.id, last_activity_at=now - timedelta(days=29)
        )
        co2 = Company(
            name="Less Urgent",
            is_active=True,
            account_owner_id=sales_user.id,
            last_activity_at=now - timedelta(days=24),
        )
        db_session.add_all([co1, co2])
        db_session.commit()

        with patch("app.services.ownership_service.settings") as mock_settings:
            mock_settings.customer_inactivity_days = 30
            mock_settings.strategic_inactivity_days = 90
            result = get_accounts_at_risk(db_session)

        assert len(result) == 2
        assert result[0]["days_remaining"] < result[1]["days_remaining"]

    def test_no_activity_shows_999_days(self, db_session: Session, sales_user: User):
        """Accounts with no activity at all show 999 days inactive."""
        from app.services.ownership_service import get_accounts_at_risk

        co = Company(name="No Activity", is_active=True, account_owner_id=sales_user.id, last_activity_at=None)
        db_session.add(co)
        db_session.commit()

        with patch("app.services.ownership_service.settings") as mock_settings:
            mock_settings.customer_inactivity_days = 30
            mock_settings.strategic_inactivity_days = 90
            result = get_accounts_at_risk(db_session)

        assert len(result) == 1
        assert result[0]["days_inactive"] == 999


# ═══════════════════════════════════════════════════════════════════════
#  get_open_pool_accounts
# ═══════════════════════════════════════════════════════════════════════


class TestGetOpenPoolAccounts:
    """Tests for get_open_pool_accounts()."""

    def test_returns_unowned_active_companies(self, db_session: Session, test_company: Company):
        """Unowned active companies appear in pool."""
        from app.services.ownership_service import get_open_pool_accounts

        test_company.account_owner_id = None
        test_company.is_active = True
        db_session.commit()

        result = get_open_pool_accounts(db_session)

        assert len(result) >= 1
        ids = [r["company_id"] for r in result]
        assert test_company.id in ids

    def test_excludes_owned_companies(self, db_session: Session, test_company: Company, sales_user: User):
        """Owned companies do not appear in pool."""
        from app.services.ownership_service import get_open_pool_accounts

        test_company.account_owner_id = sales_user.id
        test_company.is_active = True
        db_session.commit()

        result = get_open_pool_accounts(db_session)
        ids = [r["company_id"] for r in result]
        assert test_company.id not in ids

    def test_excludes_inactive_companies(self, db_session: Session, test_company: Company):
        """Inactive companies do not appear in pool."""
        from app.services.ownership_service import get_open_pool_accounts

        test_company.account_owner_id = None
        test_company.is_active = False
        db_session.commit()

        result = get_open_pool_accounts(db_session)
        ids = [r["company_id"] for r in result]
        assert test_company.id not in ids


# ═══════════════════════════════════════════════════════════════════════
#  get_my_accounts
# ═══════════════════════════════════════════════════════════════════════


class TestGetMyAccounts:
    """Tests for get_my_accounts()."""

    def test_returns_owned_accounts_with_status(self, db_session: Session, test_company: Company, sales_user: User):
        """Returns accounts owned by user with health status."""
        from app.services.ownership_service import get_my_accounts

        test_company.account_owner_id = sales_user.id
        test_company.last_activity_at = datetime.now(timezone.utc) - timedelta(days=5)
        test_company.is_active = True
        db_session.commit()

        with patch("app.services.ownership_service.settings") as mock_settings:
            mock_settings.customer_inactivity_days = 30
            mock_settings.strategic_inactivity_days = 90
            result = get_my_accounts(sales_user.id, db_session)

        assert len(result) == 1
        assert result[0]["status"] == "green"

    def test_yellow_status_in_warning_zone(self, db_session: Session, test_company: Company, sales_user: User):
        """Account in warning zone shows yellow status."""
        from app.services.ownership_service import get_my_accounts

        test_company.account_owner_id = sales_user.id
        test_company.last_activity_at = datetime.now(timezone.utc) - timedelta(days=25)
        test_company.is_active = True
        db_session.commit()

        with patch("app.services.ownership_service.settings") as mock_settings:
            mock_settings.customer_inactivity_days = 30
            mock_settings.strategic_inactivity_days = 90
            result = get_my_accounts(sales_user.id, db_session)

        assert result[0]["status"] == "yellow"

    def test_red_status_past_limit(self, db_session: Session, test_company: Company, sales_user: User):
        """Account past inactivity limit shows red status."""
        from app.services.ownership_service import get_my_accounts

        test_company.account_owner_id = sales_user.id
        test_company.last_activity_at = datetime.now(timezone.utc) - timedelta(days=35)
        test_company.is_active = True
        db_session.commit()

        with patch("app.services.ownership_service.settings") as mock_settings:
            mock_settings.customer_inactivity_days = 30
            mock_settings.strategic_inactivity_days = 90
            result = get_my_accounts(sales_user.id, db_session)

        assert result[0]["status"] == "red"

    def test_no_activity_status(self, db_session: Session, test_company: Company, sales_user: User):
        """Account with no activity shows no_activity status."""
        from app.services.ownership_service import get_my_accounts

        test_company.account_owner_id = sales_user.id
        test_company.last_activity_at = None
        test_company.is_active = True
        db_session.commit()

        with patch("app.services.ownership_service.settings") as mock_settings:
            mock_settings.customer_inactivity_days = 30
            mock_settings.strategic_inactivity_days = 90
            result = get_my_accounts(sales_user.id, db_session)

        assert result[0]["status"] == "no_activity"

    def test_excludes_other_users_accounts(
        self, db_session: Session, test_company: Company, sales_user: User, test_user: User
    ):
        """Only returns accounts owned by the specified user."""
        from app.services.ownership_service import get_my_accounts

        test_company.account_owner_id = sales_user.id
        test_company.is_active = True
        db_session.commit()

        with patch("app.services.ownership_service.settings") as mock_settings:
            mock_settings.customer_inactivity_days = 30
            mock_settings.strategic_inactivity_days = 90
            result = get_my_accounts(test_user.id, db_session)

        assert len(result) == 0


# ═══════════════════════════════════════════════════════════════════════
#  get_manager_digest
# ═══════════════════════════════════════════════════════════════════════


class TestGetManagerDigest:
    """Tests for get_manager_digest()."""

    def test_returns_digest_structure(self, db_session: Session):
        """Digest has expected keys."""
        from app.services.ownership_service import get_manager_digest

        with patch("app.services.ownership_service.settings") as mock_settings:
            mock_settings.customer_inactivity_days = 30
            mock_settings.strategic_inactivity_days = 90
            result = get_manager_digest(db_session)

        assert "at_risk_count" in result
        assert "at_risk_accounts" in result
        assert "recently_cleared" in result
        assert "team_activity" in result
        assert "generated_at" in result

    def test_includes_recently_cleared(self, db_session: Session, test_company: Company):
        """Recently cleared companies appear in digest."""
        from app.services.ownership_service import get_manager_digest

        test_company.ownership_cleared_at = datetime.now(timezone.utc) - timedelta(days=2)
        test_company.account_owner_id = None
        db_session.commit()

        with patch("app.services.ownership_service.settings") as mock_settings:
            mock_settings.customer_inactivity_days = 30
            mock_settings.strategic_inactivity_days = 90
            result = get_manager_digest(db_session)

        assert len(result["recently_cleared"]) >= 1

    def test_includes_team_activity(self, db_session: Session, sales_user: User):
        """Sales/trader users appear in team activity."""
        from app.services.ownership_service import get_manager_digest

        activity = ActivityLog(
            user_id=sales_user.id,
            activity_type="email_sent",
            channel="email",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(activity)
        db_session.commit()

        with patch("app.services.ownership_service.settings") as mock_settings:
            mock_settings.customer_inactivity_days = 30
            mock_settings.strategic_inactivity_days = 90
            result = get_manager_digest(db_session)

        user_ids = [ta["user_id"] for ta in result["team_activity"]]
        assert sales_user.id in user_ids


# ═══════════════════════════════════════════════════════════════════════
#  SITE-LEVEL OWNERSHIP
# ═══════════════════════════════════════════════════════════════════════


class TestRunSiteOwnershipSweep:
    """Tests for run_site_ownership_sweep()."""

    def test_clears_stale_sites(self, db_session: Session, test_customer_site: CustomerSite, sales_user: User):
        """Sites inactive >30 days lose ownership."""
        from app.services.ownership_service import run_site_ownership_sweep

        test_customer_site.owner_id = sales_user.id
        test_customer_site.last_activity_at = datetime.now(timezone.utc) - timedelta(days=31)
        test_customer_site.is_active = True
        db_session.commit()

        with patch("app.services.ownership_service.settings") as mock_settings:
            mock_settings.customer_inactivity_days = 30
            result = run_site_ownership_sweep(db_session)

        assert result["cleared"] == 1
        db_session.refresh(test_customer_site)
        assert test_customer_site.owner_id is None

    def test_warns_sites_in_warning_zone(self, db_session: Session, test_customer_site: CustomerSite, sales_user: User):
        """Sites in warning zone get warning logged."""
        from app.services.ownership_service import run_site_ownership_sweep

        test_customer_site.owner_id = sales_user.id
        test_customer_site.last_activity_at = datetime.now(timezone.utc) - timedelta(days=25)
        test_customer_site.is_active = True
        db_session.commit()

        with patch("app.services.ownership_service.settings") as mock_settings:
            mock_settings.customer_inactivity_days = 30
            result = run_site_ownership_sweep(db_session)

        assert result["warned"] == 1

    def test_no_activity_uses_created_at(self, db_session: Session, test_customer_site: CustomerSite, sales_user: User):
        """Sites with no activity use created_at as baseline."""
        from app.services.ownership_service import run_site_ownership_sweep

        test_customer_site.owner_id = sales_user.id
        test_customer_site.last_activity_at = None
        test_customer_site.created_at = datetime.now(timezone.utc) - timedelta(days=40)
        test_customer_site.is_active = True
        db_session.commit()

        with patch("app.services.ownership_service.settings") as mock_settings:
            mock_settings.customer_inactivity_days = 30
            result = run_site_ownership_sweep(db_session)

        assert result["cleared"] == 1

    def test_recent_site_keeps_ownership(self, db_session: Session, test_customer_site: CustomerSite, sales_user: User):
        """Active sites within threshold keep ownership."""
        from app.services.ownership_service import run_site_ownership_sweep

        test_customer_site.owner_id = sales_user.id
        test_customer_site.last_activity_at = datetime.now(timezone.utc) - timedelta(days=5)
        test_customer_site.is_active = True
        db_session.commit()

        with patch("app.services.ownership_service.settings") as mock_settings:
            mock_settings.customer_inactivity_days = 30
            result = run_site_ownership_sweep(db_session)

        assert result["cleared"] == 0
        assert result["warned"] == 0


class TestClaimSite:
    """Tests for claim_site()."""

    def test_sales_claims_unowned_site(self, db_session: Session, test_customer_site: CustomerSite, sales_user: User):
        """Sales user can claim an unowned site."""
        from app.services.ownership_service import claim_site

        test_customer_site.owner_id = None
        db_session.commit()

        result = claim_site(test_customer_site.id, sales_user.id, db_session)
        assert result is True
        db_session.refresh(test_customer_site)
        assert test_customer_site.owner_id == sales_user.id

    def test_buyer_cannot_claim_site(self, db_session: Session, test_customer_site: CustomerSite, test_user: User):
        """Buyer cannot claim a site."""
        from app.services.ownership_service import claim_site

        test_customer_site.owner_id = None
        db_session.commit()

        result = claim_site(test_customer_site.id, test_user.id, db_session)
        assert result is False

    def test_already_owned_site_returns_false(
        self, db_session: Session, test_customer_site: CustomerSite, sales_user: User
    ):
        """Cannot claim an already-owned site."""
        from app.services.ownership_service import claim_site

        test_customer_site.owner_id = sales_user.id
        db_session.commit()

        result = claim_site(test_customer_site.id, sales_user.id, db_session)
        assert result is False

    def test_nonexistent_site_returns_false(self, db_session: Session, sales_user: User):
        """Non-existent site returns False."""
        from app.services.ownership_service import claim_site

        result = claim_site(99999, sales_user.id, db_session)
        assert result is False

    def test_clears_ownership_cleared_at(self, db_session: Session, test_customer_site: CustomerSite, sales_user: User):
        """Claiming a site clears ownership_cleared_at."""
        from app.services.ownership_service import claim_site

        test_customer_site.owner_id = None
        test_customer_site.ownership_cleared_at = datetime.now(timezone.utc)
        db_session.commit()

        claim_site(test_customer_site.id, sales_user.id, db_session)
        db_session.refresh(test_customer_site)
        assert test_customer_site.ownership_cleared_at is None


class TestGetOpenPoolSites:
    """Tests for get_open_pool_sites()."""

    def test_returns_unowned_active_sites(
        self, db_session: Session, test_customer_site: CustomerSite, test_company: Company
    ):
        """Unowned active sites appear in pool."""
        from app.services.ownership_service import get_open_pool_sites

        test_customer_site.owner_id = None
        test_customer_site.is_active = True
        db_session.commit()

        result = get_open_pool_sites(db_session)
        site_ids = [r["site_id"] for r in result]
        assert test_customer_site.id in site_ids
        assert result[0]["company_name"] == test_company.name

    def test_excludes_owned_sites(self, db_session: Session, test_customer_site: CustomerSite, sales_user: User):
        """Owned sites don't appear in pool."""
        from app.services.ownership_service import get_open_pool_sites

        test_customer_site.owner_id = sales_user.id
        test_customer_site.is_active = True
        db_session.commit()

        result = get_open_pool_sites(db_session)
        site_ids = [r["site_id"] for r in result]
        assert test_customer_site.id not in site_ids


class TestGetMySites:
    """Tests for get_my_sites()."""

    def test_returns_owned_sites_with_status(
        self, db_session: Session, test_customer_site: CustomerSite, sales_user: User, test_company: Company
    ):
        """Returns sites owned by user with health status."""
        from app.services.ownership_service import get_my_sites

        test_customer_site.owner_id = sales_user.id
        test_customer_site.last_activity_at = datetime.now(timezone.utc) - timedelta(days=5)
        test_customer_site.is_active = True
        db_session.commit()

        with patch("app.services.ownership_service.settings") as mock_settings:
            mock_settings.customer_inactivity_days = 30
            result = get_my_sites(sales_user.id, db_session)

        assert len(result) == 1
        assert result[0]["status"] == "green"
        assert result[0]["company_name"] == test_company.name

    def test_yellow_status(self, db_session: Session, test_customer_site: CustomerSite, sales_user: User):
        """Site in warning zone shows yellow."""
        from app.services.ownership_service import get_my_sites

        test_customer_site.owner_id = sales_user.id
        test_customer_site.last_activity_at = datetime.now(timezone.utc) - timedelta(days=25)
        test_customer_site.is_active = True
        db_session.commit()

        with patch("app.services.ownership_service.settings") as mock_settings:
            mock_settings.customer_inactivity_days = 30
            result = get_my_sites(sales_user.id, db_session)

        assert result[0]["status"] == "yellow"

    def test_red_status(self, db_session: Session, test_customer_site: CustomerSite, sales_user: User):
        """Site past limit shows red."""
        from app.services.ownership_service import get_my_sites

        test_customer_site.owner_id = sales_user.id
        test_customer_site.last_activity_at = datetime.now(timezone.utc) - timedelta(days=35)
        test_customer_site.is_active = True
        db_session.commit()

        with patch("app.services.ownership_service.settings") as mock_settings:
            mock_settings.customer_inactivity_days = 30
            result = get_my_sites(sales_user.id, db_session)

        assert result[0]["status"] == "red"

    def test_no_activity_status(self, db_session: Session, test_customer_site: CustomerSite, sales_user: User):
        """Site with no activity shows no_activity."""
        from app.services.ownership_service import get_my_sites

        test_customer_site.owner_id = sales_user.id
        test_customer_site.last_activity_at = None
        test_customer_site.is_active = True
        db_session.commit()

        with patch("app.services.ownership_service.settings") as mock_settings:
            mock_settings.customer_inactivity_days = 30
            result = get_my_sites(sales_user.id, db_session)

        assert result[0]["status"] == "no_activity"


class TestGetSitesAtRisk:
    """Tests for get_sites_at_risk()."""

    def test_returns_at_risk_sites(
        self, db_session: Session, test_customer_site: CustomerSite, sales_user: User, test_company: Company
    ):
        """Sites in warning zone appear in at-risk list."""
        from app.services.ownership_service import get_sites_at_risk

        test_customer_site.owner_id = sales_user.id
        test_customer_site.last_activity_at = datetime.now(timezone.utc) - timedelta(days=25)
        test_customer_site.is_active = True
        db_session.commit()

        with patch("app.services.ownership_service.settings") as mock_settings:
            mock_settings.customer_inactivity_days = 30
            result = get_sites_at_risk(db_session)

        assert len(result) == 1
        assert result[0]["site_id"] == test_customer_site.id
        assert result[0]["company_name"] == test_company.name

    def test_excludes_healthy_sites(self, db_session: Session, test_customer_site: CustomerSite, sales_user: User):
        """Healthy sites are excluded."""
        from app.services.ownership_service import get_sites_at_risk

        test_customer_site.owner_id = sales_user.id
        test_customer_site.last_activity_at = datetime.now(timezone.utc) - timedelta(days=5)
        test_customer_site.is_active = True
        db_session.commit()

        with patch("app.services.ownership_service.settings") as mock_settings:
            mock_settings.customer_inactivity_days = 30
            result = get_sites_at_risk(db_session)

        assert len(result) == 0

    def test_sorts_by_most_urgent(self, db_session: Session, test_company: Company, sales_user: User):
        """Results sorted by days remaining (most urgent first)."""
        from app.services.ownership_service import get_sites_at_risk

        now = datetime.now(timezone.utc)
        s1 = CustomerSite(
            company_id=test_company.id,
            site_name="Urgent Site",
            owner_id=sales_user.id,
            last_activity_at=now - timedelta(days=29),
            is_active=True,
        )
        s2 = CustomerSite(
            company_id=test_company.id,
            site_name="Less Urgent Site",
            owner_id=sales_user.id,
            last_activity_at=now - timedelta(days=24),
            is_active=True,
        )
        db_session.add_all([s1, s2])
        db_session.commit()

        with patch("app.services.ownership_service.settings") as mock_settings:
            mock_settings.customer_inactivity_days = 30
            result = get_sites_at_risk(db_session)

        assert len(result) == 2
        assert result[0]["days_remaining"] < result[1]["days_remaining"]


# ═══════════════════════════════════════════════════════════════════════
#  _send_warning_alert
# ═══════════════════════════════════════════════════════════════════════


class TestSendWarningAlert:
    """Tests for _send_warning_alert()."""

    @pytest.mark.asyncio
    async def test_sends_email_and_logs_warning(self, db_session: Session, test_company: Company, sales_user: User):
        """Warning email is sent and activity log entry created."""
        from app.services.ownership_service import _send_warning_alert

        test_company.account_owner_id = sales_user.id
        db_session.commit()

        mock_gc = MagicMock()
        mock_gc.post_json = AsyncMock()

        with (
            patch("app.services.ownership_service.get_valid_token", AsyncMock(return_value="token-123")),
            patch("app.services.ownership_service.GraphClient", return_value=mock_gc),
            patch("app.services.ownership_service.settings") as mock_settings,
        ):
            mock_settings.app_url = "http://localhost:8000"
            await _send_warning_alert(test_company, 25, 30, db_session)

        # Warning logged as activity
        warnings = db_session.query(ActivityLog).filter(ActivityLog.activity_type == "ownership_warning").all()
        assert len(warnings) == 1
        mock_gc.post_json.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_skips_when_no_owner(self, db_session: Session, test_company: Company):
        """No owner means no email sent."""
        from app.services.ownership_service import _send_warning_alert

        test_company.account_owner_id = None
        db_session.commit()

        await _send_warning_alert(test_company, 25, 30, db_session)

        warnings = db_session.query(ActivityLog).filter(ActivityLog.activity_type == "ownership_warning").count()
        assert warnings == 0

    @pytest.mark.asyncio
    async def test_handles_no_token(self, db_session: Session, test_company: Company, sales_user: User):
        """No token available still logs warning but doesn't send email."""
        from app.services.ownership_service import _send_warning_alert

        test_company.account_owner_id = sales_user.id
        db_session.commit()

        with (
            patch("app.services.ownership_service.get_valid_token", AsyncMock(return_value=None)),
            patch("app.services.ownership_service.settings") as mock_settings,
        ):
            mock_settings.app_url = "http://localhost:8000"
            await _send_warning_alert(test_company, 25, 30, db_session)

        # Warning still logged even without email
        warnings = db_session.query(ActivityLog).filter(ActivityLog.activity_type == "ownership_warning").count()
        assert warnings == 1

    @pytest.mark.asyncio
    async def test_handles_email_send_failure(self, db_session: Session, test_company: Company, sales_user: User):
        """Email send failure is caught and logged, not raised."""
        from app.services.ownership_service import _send_warning_alert

        test_company.account_owner_id = sales_user.id
        db_session.commit()

        mock_gc = MagicMock()
        mock_gc.post_json = AsyncMock(side_effect=RuntimeError("Graph API error"))

        with (
            patch("app.services.ownership_service.get_valid_token", AsyncMock(return_value="token-123")),
            patch("app.services.ownership_service.GraphClient", return_value=mock_gc),
            patch("app.services.ownership_service.settings") as mock_settings,
        ):
            mock_settings.app_url = "http://localhost:8000"
            # Should NOT raise
            await _send_warning_alert(test_company, 25, 30, db_session)


# ═══════════════════════════════════════════════════════════════════════
#  send_manager_digest_email
# ═══════════════════════════════════════════════════════════════════════


class TestSendManagerDigestEmail:
    """Tests for send_manager_digest_email()."""

    @pytest.mark.asyncio
    async def test_skips_when_nothing_to_report(self, db_session: Session):
        """No at-risk or cleared accounts means no email sent."""
        from app.services.ownership_service import send_manager_digest_email

        with patch("app.services.ownership_service.settings") as mock_settings:
            mock_settings.customer_inactivity_days = 30
            mock_settings.strategic_inactivity_days = 90
            mock_settings.admin_emails = ["admin@test.com"]
            await send_manager_digest_email(db_session)

        # No error raised — function returned early

    @pytest.mark.asyncio
    async def test_sends_digest_to_admins(
        self, db_session: Session, test_company: Company, sales_user: User, admin_user: User
    ):
        """Digest email sent to admin when there are at-risk accounts."""
        from app.services.ownership_service import send_manager_digest_email

        test_company.account_owner_id = sales_user.id
        test_company.last_activity_at = datetime.now(timezone.utc) - timedelta(days=25)
        test_company.is_active = True
        db_session.commit()

        mock_gc = MagicMock()
        mock_gc.post_json = AsyncMock()

        with (
            patch("app.services.ownership_service.settings") as mock_settings,
            patch("app.services.ownership_service.get_valid_token", AsyncMock(return_value="token-123")),
            patch("app.services.ownership_service.GraphClient", return_value=mock_gc),
        ):
            mock_settings.customer_inactivity_days = 30
            mock_settings.strategic_inactivity_days = 90
            mock_settings.admin_emails = [admin_user.email]
            await send_manager_digest_email(db_session)

        mock_gc.post_json.assert_awaited_once()
