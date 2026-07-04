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
#  HELPERS — _days_since_activity, _was_warned_today
# ═══════════════════════════════════════════════════════════════════════


class TestDaysSinceActivity:
    """Tests for _days_since_activity()."""

    def test_returns_none_when_no_activity(self, db_session: Session, test_company: Company):
        """No last_activity_at returns None."""
        from app.services.ownership_service import _days_since_activity

        test_company.last_activity_at = None
        result = _days_since_activity(test_company, datetime.now(timezone.utc))
        assert result is None

    @pytest.mark.parametrize(
        ("days", "naive"),
        [
            pytest.param(10, False, id="aware_datetime"),
            pytest.param(5, True, id="naive_datetime_gets_utc"),
        ],
    )
    def test_returns_days_since_last_activity(self, db_session: Session, test_company: Company, days: int, naive: bool):
        """Correct day count; naive datetimes get UTC timezone applied."""
        from app.services.ownership_service import _days_since_activity

        now = datetime.now(timezone.utc)
        last_activity = now - timedelta(days=days)
        test_company.last_activity_at = last_activity.replace(tzinfo=None) if naive else last_activity
        result = _days_since_activity(test_company, now)
        assert result == days


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

    @pytest.mark.parametrize(
        ("days", "naive"),
        [
            pytest.param(15, False, id="aware_datetime"),
            pytest.param(7, True, id="naive_datetime_gets_utc"),
        ],
    )
    def test_returns_correct_days(self, db_session: Session, test_customer_site: CustomerSite, days: int, naive: bool):
        """Correct day count; naive datetimes get UTC timezone applied."""
        from app.services.ownership_service import _site_days_since_activity

        now = datetime.now(timezone.utc)
        last_activity = now - timedelta(days=days)
        test_customer_site.last_activity_at = last_activity.replace(tzinfo=None) if naive else last_activity
        result = _site_days_since_activity(test_customer_site, now)
        assert result == days


# ═══════════════════════════════════════════════════════════════════════
#  run_ownership_sweep
# ═══════════════════════════════════════════════════════════════════════


class TestRunOwnershipSweep:
    """Tests for run_ownership_sweep() — WARNINGS ONLY (H5 fix).

    The sweep never clears ownership anymore (SP4 job_account_sweep is the single
    park+cooldown+notify path) and reads the ONE threshold
    settings.account_sweep_inactivity_days (default 45), warning WARNING_LEAD_DAYS
    before it. The result dict no longer carries a "cleared" count.
    """

    @pytest.mark.asyncio
    async def test_never_clears_ownership_past_threshold(
        self, db_session: Session, test_company: Company, sales_user: User
    ):
        """A company well past the inactivity threshold is warned but NOT cleared."""
        from app.services.ownership_service import run_ownership_sweep

        test_company.account_owner_id = sales_user.id
        test_company.last_activity_at = datetime.now(timezone.utc) - timedelta(days=200)
        test_company.is_active = True
        db_session.commit()

        mock_send = AsyncMock()
        with (
            patch("app.services.ownership_service.settings") as mock_settings,
            patch("app.services.ownership_service._send_warning_alert", mock_send),
        ):
            mock_settings.account_sweep_inactivity_days = 90
            result = await run_ownership_sweep(db_session)

        assert "cleared" not in result
        assert result["warned"] == 1
        db_session.refresh(test_company)
        # Ownership is retained — clearing is SP4's job, not this warning sweep's.
        assert test_company.account_owner_id == sales_user.id

    @pytest.mark.asyncio
    async def test_warns_in_warning_zone(self, db_session: Session, test_company: Company, sales_user: User):
        """Company inside the warning zone (past threshold minus lead) gets a
        warning."""
        from app.services.ownership_service import run_ownership_sweep

        test_company.account_owner_id = sales_user.id
        test_company.last_activity_at = datetime.now(timezone.utc) - timedelta(days=85)
        test_company.is_active = True
        db_session.commit()

        mock_send = AsyncMock()

        with (
            patch("app.services.ownership_service.settings") as mock_settings,
            patch("app.services.ownership_service._send_warning_alert", mock_send),
        ):
            mock_settings.account_sweep_inactivity_days = 90
            result = await run_ownership_sweep(db_session)

        assert result["warned"] == 1
        mock_send.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_does_not_warn_twice_same_day(self, db_session: Session, test_company: Company, sales_user: User):
        """No duplicate warnings on same day."""
        from app.services.ownership_service import run_ownership_sweep

        test_company.account_owner_id = sales_user.id
        test_company.last_activity_at = datetime.now(timezone.utc) - timedelta(days=85)
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
            mock_settings.account_sweep_inactivity_days = 90
            result = await run_ownership_sweep(db_session)

        assert result["warned"] == 0
        mock_send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_activity_uses_created_at(self, db_session: Session, test_company: Company, sales_user: User):
        """No activity at all uses created_at as the warning baseline (still no
        clear)."""
        from app.services.ownership_service import run_ownership_sweep

        test_company.account_owner_id = sales_user.id
        test_company.last_activity_at = None
        test_company.created_at = datetime.now(timezone.utc) - timedelta(days=95)
        test_company.is_active = True
        db_session.commit()

        mock_send = AsyncMock()
        with (
            patch("app.services.ownership_service.settings") as mock_settings,
            patch("app.services.ownership_service._send_warning_alert", mock_send),
        ):
            mock_settings.account_sweep_inactivity_days = 90
            result = await run_ownership_sweep(db_session)

        assert result["warned"] == 1
        db_session.refresh(test_company)
        assert test_company.account_owner_id == sales_user.id

    @pytest.mark.asyncio
    async def test_recent_activity_no_action(self, db_session: Session, test_company: Company, sales_user: User):
        """Active company well within the threshold keeps ownership, no warning."""
        from app.services.ownership_service import run_ownership_sweep

        test_company.account_owner_id = sales_user.id
        test_company.last_activity_at = datetime.now(timezone.utc) - timedelta(days=10)
        test_company.is_active = True
        db_session.commit()

        with patch("app.services.ownership_service.settings") as mock_settings:
            mock_settings.account_sweep_inactivity_days = 90
            result = await run_ownership_sweep(db_session)

        assert result["warned"] == 0
        db_session.refresh(test_company)
        assert test_company.account_owner_id == sales_user.id

    @pytest.mark.asyncio
    async def test_empty_owned_list(self, db_session: Session):
        """No owned companies means no work done."""
        from app.services.ownership_service import run_ownership_sweep

        with patch("app.services.ownership_service.settings") as mock_settings:
            mock_settings.account_sweep_inactivity_days = 90
            result = await run_ownership_sweep(db_session)

        assert result["total_owned"] == 0
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

    @pytest.mark.parametrize(
        ("days_inactive", "expected_status"),
        [
            pytest.param(5, "green", id="recent_green"),
            pytest.param(25, "yellow", id="warning_zone_yellow"),
            pytest.param(35, "red", id="past_limit_red"),
            pytest.param(None, "no_activity", id="no_activity"),
        ],
    )
    def test_returns_owned_accounts_with_status(
        self, db_session: Session, test_company: Company, sales_user: User, days_inactive, expected_status: str
    ):
        """Returns accounts owned by user with the correct health status."""
        from app.services.ownership_service import get_my_accounts

        test_company.account_owner_id = sales_user.id
        if days_inactive is None:
            test_company.last_activity_at = None
        else:
            test_company.last_activity_at = datetime.now(timezone.utc) - timedelta(days=days_inactive)
        test_company.is_active = True
        db_session.commit()

        with patch("app.services.ownership_service.settings") as mock_settings:
            mock_settings.customer_inactivity_days = 30
            mock_settings.strategic_inactivity_days = 90
            result = get_my_accounts(sales_user.id, db_session)

        assert len(result) == 1
        assert result[0]["status"] == expected_status

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

    @pytest.mark.parametrize(
        ("days_inactive", "expected_status"),
        [
            pytest.param(5, "green", id="recent_green"),
            pytest.param(25, "yellow", id="warning_zone_yellow"),
            pytest.param(35, "red", id="past_limit_red"),
            pytest.param(None, "no_activity", id="no_activity"),
        ],
    )
    def test_returns_owned_sites_with_status(
        self,
        db_session: Session,
        test_customer_site: CustomerSite,
        sales_user: User,
        test_company: Company,
        days_inactive,
        expected_status: str,
    ):
        """Returns sites owned by user with the correct health status."""
        from app.services.ownership_service import get_my_sites

        test_customer_site.owner_id = sales_user.id
        if days_inactive is None:
            test_customer_site.last_activity_at = None
        else:
            test_customer_site.last_activity_at = datetime.now(timezone.utc) - timedelta(days=days_inactive)
        test_customer_site.is_active = True
        db_session.commit()

        with patch("app.services.ownership_service.settings") as mock_settings:
            mock_settings.customer_inactivity_days = 30
            result = get_my_sites(sales_user.id, db_session)

        assert len(result) == 1
        assert result[0]["status"] == expected_status
        assert result[0]["company_name"] == test_company.name


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
            patch("app.scheduler.get_valid_token", AsyncMock(return_value="token-123")),
            patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
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
            patch("app.scheduler.get_valid_token", AsyncMock(return_value=None)),
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
            patch("app.scheduler.get_valid_token", AsyncMock(return_value="token-123")),
            patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
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
            patch("app.scheduler.get_valid_token", AsyncMock(return_value="token-123")),
            patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
        ):
            mock_settings.customer_inactivity_days = 30
            mock_settings.strategic_inactivity_days = 90
            mock_settings.admin_emails = [admin_user.email]
            await send_manager_digest_email(db_session)

        mock_gc.post_json.assert_awaited_once()


# ═══════════════════════════════════════════════════════════════════════
#  READER DICT SHAPE — locks batch-company-load refactor (no N+1)
# ═══════════════════════════════════════════════════════════════════════


class TestReaderDictShape:
    """get_open_pool_sites / get_my_sites / get_sites_at_risk return stable row dicts.

    These exercise multiple sites across multiple companies (the N+1 path) and pin the
    exact returned dict shape + ordering so the batch-load refactor stays behavior-
    preserving.
    """

    def test_open_pool_sites_dicts(self, db_session: Session):
        from app.services.ownership_service import get_open_pool_sites

        co_a = Company(name="Alpha Co", is_active=True)
        co_b = Company(name="Bravo Co", is_active=True)
        db_session.add_all([co_a, co_b])
        db_session.flush()
        # Two sites under co_a (dedup), one under co_b, one orphan (no company)
        s1 = CustomerSite(company_id=co_a.id, site_name="A-2nd", contact_name="N1", contact_email="n1@x.com")
        s2 = CustomerSite(company_id=co_a.id, site_name="A-1st", contact_email="n0@x.com", city="Austin", state="TX")
        s3 = CustomerSite(company_id=co_b.id, site_name="B-site")
        db_session.add_all([s1, s2, s3])
        db_session.commit()

        rows = get_open_pool_sites(db_session)
        # ordered by site_name
        names = [r["site_name"] for r in rows]
        assert names == sorted(names)
        by_name = {r["site_name"]: r for r in rows}
        assert by_name["A-1st"] == {
            "site_id": s2.id,
            "site_name": "A-1st",
            "company_id": co_a.id,
            "company_name": "Alpha Co",
            "contact_name": None,
            "contact_email": "n0@x.com",
            "city": "Austin",
            "state": "TX",
            "last_activity_at": None,
            "ownership_cleared_at": None,
        }
        assert by_name["B-site"]["company_name"] == "Bravo Co"

    def test_my_sites_dicts(self, db_session: Session, sales_user: User):
        from app.services.ownership_service import get_my_sites

        co_a = Company(name="Alpha Co", is_active=True)
        co_b = Company(name="Bravo Co", is_active=True)
        db_session.add_all([co_a, co_b])
        db_session.flush()
        s1 = CustomerSite(company_id=co_a.id, site_name="M-2", owner_id=sales_user.id, is_active=True)
        s2 = CustomerSite(company_id=co_a.id, site_name="M-1", owner_id=sales_user.id, is_active=True)
        s3 = CustomerSite(company_id=co_b.id, site_name="M-3", owner_id=sales_user.id, is_active=True)
        db_session.add_all([s1, s2, s3])
        db_session.commit()

        rows = get_my_sites(sales_user.id, db_session)
        names = [r["site_name"] for r in rows]
        assert names == ["M-1", "M-2", "M-3"]
        assert {r["company_name"] for r in rows} == {"Alpha Co", "Bravo Co"}
        first = next(r for r in rows if r["site_name"] == "M-1")
        assert set(first.keys()) == {
            "site_id",
            "site_name",
            "company_id",
            "company_name",
            "contact_name",
            "contact_email",
            "city",
            "state",
            "days_inactive",
            "inactivity_limit",
            "status",
            "last_activity_at",
        }
        assert first["company_id"] == co_a.id
