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


# ═══════════════════════════════════════════════════════════════════════
#  get_open_pool_accounts
# ═══════════════════════════════════════════════════════════════════════


# ═══════════════════════════════════════════════════════════════════════
#  get_my_accounts
# ═══════════════════════════════════════════════════════════════════════


# ═══════════════════════════════════════════════════════════════════════
#  get_manager_digest
# ═══════════════════════════════════════════════════════════════════════


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


# ═══════════════════════════════════════════════════════════════════════
#  READER DICT SHAPE — locks batch-company-load refactor (no N+1)
# ═══════════════════════════════════════════════════════════════════════
