"""Tests for larger job/service modules with 0% coverage.

Targets:
  - app/jobs/offers_jobs.py
  - app/jobs/inventory_jobs.py
  - app/jobs/core_jobs.py
  - app/services/global_search_service.py
  - app/services/enrichment.py

Called by: pytest
Depends on: unittest.mock, conftest fixtures
"""

import os

os.environ["TESTING"] = "1"

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.orm import Session

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _run(coro):
    """Run an async coroutine synchronously."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _mock_db():
    db = MagicMock(spec=Session)
    db.query.return_value.filter.return_value.update.return_value = 0
    db.query.return_value.filter.return_value.all.return_value = []
    db.query.return_value.filter.return_value.first.return_value = None
    db.query.return_value.filter.return_value.count.return_value = 0
    db.query.return_value.filter.return_value.limit.return_value.all.return_value = []
    db.query.return_value.filter.return_value.order_by.return_value.limit.return_value.all.return_value = []
    return db


# ===========================================================================
# offers_jobs.py
# ===========================================================================


class TestRegisterOffersJobs:
    """Tests for register_offers_jobs()."""

    def test_register_with_proactive_matching_enabled(self):
        from app.jobs.offers_jobs import register_offers_jobs

        scheduler = MagicMock()
        settings = MagicMock()
        settings.proactive_matching_enabled = True
        settings.proactive_scan_interval_hours = 4

        register_offers_jobs(scheduler, settings)

        # 6 jobs: proactive_matching + performance_tracking + proactive_offer_expiry
        # + flag_stale_offers + expire_strategic_vendors + warn_strategic_expiring
        assert scheduler.add_job.call_count == 6

    def test_register_with_proactive_matching_disabled(self):
        from app.jobs.offers_jobs import register_offers_jobs

        scheduler = MagicMock()
        settings = MagicMock()
        settings.proactive_matching_enabled = False

        register_offers_jobs(scheduler, settings)

        # 5 jobs (no proactive_matching)
        assert scheduler.add_job.call_count == 5

    def test_proactive_scan_interval_minimum_1(self):
        from app.jobs.offers_jobs import register_offers_jobs

        scheduler = MagicMock()
        settings = MagicMock()
        settings.proactive_matching_enabled = True
        settings.proactive_scan_interval_hours = 0  # below min

        register_offers_jobs(scheduler, settings)
        assert scheduler.add_job.call_count == 6


class TestJobProactiveMatching:
    """Tests for _job_proactive_matching()."""

    @patch("app.jobs.offers_jobs.logger")
    def test_happy_path(self, mock_logger):
        from app.jobs.offers_jobs import _job_proactive_matching

        mock_db = _mock_db()
        mock_db.query.return_value.filter.return_value.count.return_value = 5

        with patch("app.jobs.offers_jobs.asyncio.get_running_loop") as mock_loop_fn:
            mock_loop = MagicMock()
            mock_loop_fn.return_value = mock_loop

            # run_in_executor returns futures
            scan_result = {"matches_created": 3, "scanned_offers": 10}
            expired_count = 2

            future_scan = asyncio.Future()
            future_scan.set_result(scan_result)
            future_expire = asyncio.Future()
            future_expire.set_result(expired_count)

            mock_loop.run_in_executor.side_effect = [future_scan, future_expire]

            with patch("app.database.SessionLocal", return_value=mock_db):
                _run(_job_proactive_matching.__wrapped__())

        mock_db.close.assert_called_once()

    @patch("app.jobs.offers_jobs.logger")
    def test_timeout_error(self, mock_logger):
        from app.jobs.offers_jobs import _job_proactive_matching

        mock_db = _mock_db()

        with patch("app.jobs.offers_jobs.asyncio.get_running_loop") as mock_loop_fn:
            mock_loop = MagicMock()
            mock_loop_fn.return_value = mock_loop

            future = asyncio.Future()
            future.set_result({"matches_created": 0, "scanned_offers": 0})
            mock_loop.run_in_executor.return_value = future

            with patch("app.jobs.offers_jobs.asyncio.wait_for", side_effect=asyncio.TimeoutError):
                with patch("app.database.SessionLocal", return_value=mock_db):
                    with pytest.raises(asyncio.TimeoutError):
                        _run(_job_proactive_matching.__wrapped__())

        mock_db.rollback.assert_called_once()
        mock_db.close.assert_called_once()

    @patch("app.jobs.offers_jobs.logger")
    def test_generic_exception(self, mock_logger):
        from app.jobs.offers_jobs import _job_proactive_matching

        mock_db = _mock_db()

        with patch("app.jobs.offers_jobs.asyncio.get_running_loop") as mock_loop_fn:
            mock_loop = MagicMock()
            mock_loop_fn.return_value = mock_loop

            with patch("app.jobs.offers_jobs.asyncio.wait_for", side_effect=RuntimeError("boom")):
                with patch("app.database.SessionLocal", return_value=mock_db):
                    with pytest.raises(RuntimeError, match="boom"):
                        _run(_job_proactive_matching.__wrapped__())

        mock_db.rollback.assert_called_once()
        mock_db.close.assert_called_once()


class TestJobPerformanceTracking:
    """Tests for _job_performance_tracking()."""

    @patch("app.jobs.offers_jobs.logger")
    def test_happy_path_day_1(self, mock_logger):
        """Day 1 of month: previous-month recomputation should run."""
        from app.jobs.offers_jobs import _job_performance_tracking

        mock_db = _mock_db()

        vs_result = {"updated": 5, "skipped_cold_start": 1}
        bl_result = {"entries": 10}
        as_result = {"buyers": 3, "sales": 2, "saved": 5}
        ms_result = {"buyers": 3, "sales": 2, "saved": 5}
        us_result = {"computed": 10, "saved": 10}

        results = [
            vs_result,
            bl_result,
            as_result,
            ms_result,
            us_result,
            bl_result,
            as_result,
            ms_result,
            us_result,
        ]  # prev month too

        with patch("app.jobs.offers_jobs.asyncio.wait_for", side_effect=results):
            with patch("app.jobs.offers_jobs.asyncio.get_running_loop") as mock_loop_fn:
                mock_loop_fn.return_value = MagicMock()
                with patch("app.jobs.offers_jobs.datetime") as mock_dt:
                    mock_dt.now.return_value = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)
                    mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
                    with patch("app.database.SessionLocal", return_value=mock_db):
                        _run(_job_performance_tracking.__wrapped__())

        mock_db.close.assert_called_once()

    @patch("app.jobs.offers_jobs.logger")
    def test_happy_path_day_15(self, mock_logger):
        """Day 15: no previous-month recomputation."""
        from app.jobs.offers_jobs import _job_performance_tracking

        mock_db = _mock_db()

        vs_result = {"updated": 5, "skipped_cold_start": 1}
        bl_result = {"entries": 10}
        as_result = {"buyers": 3, "sales": 2, "saved": 5}
        ms_result = {"buyers": 3, "sales": 2, "saved": 5}
        us_result = {"computed": 10, "saved": 10}

        results = [vs_result, bl_result, as_result, ms_result, us_result]

        with patch("app.jobs.offers_jobs.asyncio.wait_for", side_effect=results):
            with patch("app.jobs.offers_jobs.asyncio.get_running_loop") as mock_loop_fn:
                mock_loop_fn.return_value = MagicMock()
                with patch("app.jobs.offers_jobs.datetime") as mock_dt:
                    mock_dt.now.return_value = datetime(2026, 3, 15, 12, 0, tzinfo=timezone.utc)
                    mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
                    with patch("app.database.SessionLocal", return_value=mock_db):
                        _run(_job_performance_tracking.__wrapped__())

        mock_db.close.assert_called_once()

    @patch("app.jobs.offers_jobs.logger")
    def test_timeout_error(self, mock_logger):
        from app.jobs.offers_jobs import _job_performance_tracking

        mock_db = _mock_db()

        with patch("app.jobs.offers_jobs.asyncio.wait_for", side_effect=asyncio.TimeoutError):
            with patch("app.jobs.offers_jobs.asyncio.get_running_loop") as mock_loop_fn:
                mock_loop_fn.return_value = MagicMock()
                with patch("app.jobs.offers_jobs.datetime") as mock_dt:
                    mock_dt.now.return_value = datetime(2026, 3, 15, 12, 0, tzinfo=timezone.utc)
                    with patch("app.database.SessionLocal", return_value=mock_db):
                        _run(_job_performance_tracking.__wrapped__())

        mock_db.rollback.assert_called_once()
        mock_db.close.assert_called_once()

    @patch("app.jobs.offers_jobs.logger")
    def test_generic_exception(self, mock_logger):
        from app.jobs.offers_jobs import _job_performance_tracking

        mock_db = _mock_db()

        with patch("app.jobs.offers_jobs.asyncio.wait_for", side_effect=ValueError("bad")):
            with patch("app.jobs.offers_jobs.asyncio.get_running_loop") as mock_loop_fn:
                mock_loop_fn.return_value = MagicMock()
                with patch("app.jobs.offers_jobs.datetime") as mock_dt:
                    mock_dt.now.return_value = datetime(2026, 3, 15, 12, 0, tzinfo=timezone.utc)
                    with patch("app.database.SessionLocal", return_value=mock_db):
                        _run(_job_performance_tracking.__wrapped__())

        mock_db.rollback.assert_called_once()
        mock_db.close.assert_called_once()


class TestJobProactiveOfferExpiry:
    """Tests for _job_proactive_offer_expiry()."""

    @patch("app.jobs.offers_jobs.logger")
    def test_expires_stale_offers(self, mock_logger):
        from app.jobs.offers_jobs import _job_proactive_offer_expiry

        mock_db = _mock_db()
        mock_db.query.return_value.filter.return_value.update.return_value = 3

        with patch("app.database.SessionLocal", return_value=mock_db):
            _run(_job_proactive_offer_expiry.__wrapped__())

        mock_db.commit.assert_called_once()
        mock_db.close.assert_called_once()

    @patch("app.jobs.offers_jobs.logger")
    def test_no_stale_offers(self, mock_logger):
        from app.jobs.offers_jobs import _job_proactive_offer_expiry

        mock_db = _mock_db()
        mock_db.query.return_value.filter.return_value.update.return_value = 0

        with patch("app.database.SessionLocal", return_value=mock_db):
            _run(_job_proactive_offer_expiry.__wrapped__())

        mock_db.commit.assert_not_called()
        mock_db.close.assert_called_once()

    @patch("app.jobs.offers_jobs.logger")
    def test_sqlalchemy_error(self, mock_logger):
        import sqlalchemy.exc

        from app.jobs.offers_jobs import _job_proactive_offer_expiry

        mock_db = _mock_db()
        mock_db.query.return_value.filter.return_value.update.side_effect = sqlalchemy.exc.SQLAlchemyError("db error")

        with patch("app.database.SessionLocal", return_value=mock_db):
            _run(_job_proactive_offer_expiry.__wrapped__())

        mock_db.rollback.assert_called_once()
        mock_db.close.assert_called_once()

    @patch("app.jobs.offers_jobs.logger")
    def test_generic_exception(self, mock_logger):
        from app.jobs.offers_jobs import _job_proactive_offer_expiry

        mock_db = _mock_db()
        mock_db.query.return_value.filter.return_value.update.side_effect = RuntimeError("boom")

        with patch("app.database.SessionLocal", return_value=mock_db):
            _run(_job_proactive_offer_expiry.__wrapped__())

        mock_db.rollback.assert_called_once()
        mock_db.close.assert_called_once()


class TestJobFlagStaleOffers:
    """Tests for _job_flag_stale_offers()."""

    @patch("app.jobs.offers_jobs.logger")
    def test_flags_stale_offers(self, mock_logger):
        from app.jobs.offers_jobs import _job_flag_stale_offers

        mock_db = _mock_db()
        mock_db.query.return_value.filter.return_value.update.return_value = 5

        with patch("app.database.SessionLocal", return_value=mock_db):
            _run(_job_flag_stale_offers.__wrapped__())

        mock_db.commit.assert_called_once()
        mock_db.close.assert_called_once()

    @patch("app.jobs.offers_jobs.logger")
    def test_no_stale_offers(self, mock_logger):
        from app.jobs.offers_jobs import _job_flag_stale_offers

        mock_db = _mock_db()
        mock_db.query.return_value.filter.return_value.update.return_value = 0

        with patch("app.database.SessionLocal", return_value=mock_db):
            _run(_job_flag_stale_offers.__wrapped__())

        mock_db.commit.assert_not_called()
        mock_db.close.assert_called_once()

    @patch("app.jobs.offers_jobs.logger")
    def test_sqlalchemy_error(self, mock_logger):
        import sqlalchemy.exc

        from app.jobs.offers_jobs import _job_flag_stale_offers

        mock_db = _mock_db()
        mock_db.query.return_value.filter.return_value.update.side_effect = sqlalchemy.exc.SQLAlchemyError("err")

        with patch("app.database.SessionLocal", return_value=mock_db):
            _run(_job_flag_stale_offers.__wrapped__())

        mock_db.rollback.assert_called_once()
        mock_db.close.assert_called_once()


class TestJobExpireStrategicVendors:
    """Tests for _job_expire_strategic_vendors()."""

    @patch("app.jobs.offers_jobs.logger")
    def test_expires_vendors(self, mock_logger):
        from app.jobs.offers_jobs import _job_expire_strategic_vendors

        mock_db = _mock_db()

        with patch("app.services.strategic_vendor_service.expire_stale", return_value=3) as mock_expire:
            with patch("app.database.SessionLocal", return_value=mock_db):
                _run(_job_expire_strategic_vendors.__wrapped__())

        mock_expire.assert_called_once_with(mock_db)
        mock_db.close.assert_called_once()

    @patch("app.jobs.offers_jobs.logger")
    def test_no_expired_vendors(self, mock_logger):
        from app.jobs.offers_jobs import _job_expire_strategic_vendors

        mock_db = _mock_db()

        with patch("app.services.strategic_vendor_service.expire_stale", return_value=0):
            with patch("app.database.SessionLocal", return_value=mock_db):
                _run(_job_expire_strategic_vendors.__wrapped__())

        mock_db.close.assert_called_once()

    @patch("app.jobs.offers_jobs.logger")
    def test_sqlalchemy_error(self, mock_logger):
        import sqlalchemy.exc

        from app.jobs.offers_jobs import _job_expire_strategic_vendors

        mock_db = _mock_db()

        with patch(
            "app.services.strategic_vendor_service.expire_stale",
            side_effect=sqlalchemy.exc.SQLAlchemyError("err"),
        ):
            with patch("app.database.SessionLocal", return_value=mock_db):
                _run(_job_expire_strategic_vendors.__wrapped__())

        mock_db.rollback.assert_called_once()
        mock_db.close.assert_called_once()

    @patch("app.jobs.offers_jobs.logger")
    def test_generic_exception(self, mock_logger):
        from app.jobs.offers_jobs import _job_expire_strategic_vendors

        mock_db = _mock_db()

        with patch(
            "app.services.strategic_vendor_service.expire_stale",
            side_effect=RuntimeError("boom"),
        ):
            with patch("app.database.SessionLocal", return_value=mock_db):
                _run(_job_expire_strategic_vendors.__wrapped__())

        mock_db.rollback.assert_called_once()
        mock_db.close.assert_called_once()


class TestJobWarnStrategicExpiring:
    """Tests for _job_warn_strategic_expiring()."""

    @patch("app.jobs.offers_jobs.logger")
    def test_warns_expiring_vendors(self, mock_logger):
        from app.jobs.offers_jobs import _job_warn_strategic_expiring

        mock_db = _mock_db()

        sv = MagicMock()
        sv.id = 1
        sv.user_id = 10
        sv.expires_at = datetime.now(timezone.utc) + timedelta(days=3)
        sv.vendor_card.display_name = "Acme Parts"

        # No existing log entry
        mock_db.query.return_value.filter.return_value.first.return_value = None

        with patch(
            "app.services.strategic_vendor_service.get_expiring_soon",
            return_value=[sv],
        ):
            with patch("app.database.SessionLocal", return_value=mock_db):
                _run(_job_warn_strategic_expiring.__wrapped__())

        mock_db.add.assert_called_once()
        mock_db.commit.assert_called_once()
        mock_db.close.assert_called_once()

    @patch("app.jobs.offers_jobs.logger")
    def test_skips_existing_warning(self, mock_logger):
        from app.jobs.offers_jobs import _job_warn_strategic_expiring

        mock_db = _mock_db()

        sv = MagicMock()
        sv.id = 1
        sv.user_id = 10
        sv.expires_at = datetime.now(timezone.utc) + timedelta(days=3)
        sv.vendor_card.display_name = "Acme Parts"

        # Existing log entry found
        mock_db.query.return_value.filter.return_value.first.return_value = MagicMock(id=99)

        with patch(
            "app.services.strategic_vendor_service.get_expiring_soon",
            return_value=[sv],
        ):
            with patch("app.database.SessionLocal", return_value=mock_db):
                _run(_job_warn_strategic_expiring.__wrapped__())

        mock_db.add.assert_not_called()
        mock_db.commit.assert_called_once()
        mock_db.close.assert_called_once()

    @patch("app.jobs.offers_jobs.logger")
    def test_naive_expires_at(self, mock_logger):
        """Test that naive datetime gets tzinfo added."""
        from app.jobs.offers_jobs import _job_warn_strategic_expiring

        mock_db = _mock_db()

        sv = MagicMock()
        sv.id = 1
        sv.user_id = 10
        sv.expires_at = datetime(2026, 3, 30, 12, 0)  # naive
        sv.vendor_card.display_name = "NaiveTZ Corp"

        mock_db.query.return_value.filter.return_value.first.return_value = None

        with patch(
            "app.services.strategic_vendor_service.get_expiring_soon",
            return_value=[sv],
        ):
            with patch("app.database.SessionLocal", return_value=mock_db):
                _run(_job_warn_strategic_expiring.__wrapped__())

        mock_db.add.assert_called_once()
        mock_db.close.assert_called_once()

    @patch("app.jobs.offers_jobs.logger")
    def test_no_expiring_vendors(self, mock_logger):
        from app.jobs.offers_jobs import _job_warn_strategic_expiring

        mock_db = _mock_db()

        with patch(
            "app.services.strategic_vendor_service.get_expiring_soon",
            return_value=[],
        ):
            with patch("app.database.SessionLocal", return_value=mock_db):
                _run(_job_warn_strategic_expiring.__wrapped__())

        mock_db.add.assert_not_called()
        mock_db.commit.assert_called_once()
        mock_db.close.assert_called_once()

    @patch("app.jobs.offers_jobs.logger")
    def test_sqlalchemy_error(self, mock_logger):
        import sqlalchemy.exc

        from app.jobs.offers_jobs import _job_warn_strategic_expiring

        mock_db = _mock_db()

        with patch(
            "app.services.strategic_vendor_service.get_expiring_soon",
            side_effect=sqlalchemy.exc.SQLAlchemyError("err"),
        ):
            with patch("app.database.SessionLocal", return_value=mock_db):
                _run(_job_warn_strategic_expiring.__wrapped__())

        mock_db.rollback.assert_called_once()
        mock_db.close.assert_called_once()


# ===========================================================================
# inventory_jobs.py
# ===========================================================================


class TestRegisterInventoryJobs:
    def test_registers_two_jobs(self):
        from app.jobs.inventory_jobs import register_inventory_jobs

        scheduler = MagicMock()
        settings = MagicMock()
        settings.po_verify_interval_min = 15
        settings.buyplan_auto_complete_hour = 3
        settings.buyplan_auto_complete_tz = "UTC"

        register_inventory_jobs(scheduler, settings)
        assert scheduler.add_job.call_count == 2


class TestJobPoVerification:
    """Tests for _job_po_verification()."""

    @patch("app.jobs.inventory_jobs.logger")
    def test_no_plans_to_verify(self, mock_logger):
        from app.jobs.inventory_jobs import _job_po_verification

        mock_db = _mock_db()
        mock_db.query.return_value.filter.return_value.all.return_value = []

        with patch("app.database.SessionLocal", return_value=mock_db):
            _run(_job_po_verification.__wrapped__())

        mock_db.close.assert_called_once()

    @patch("app.jobs.inventory_jobs.logger")
    def test_plans_with_pending_verify_lines(self, mock_logger):
        from app.jobs.inventory_jobs import _job_po_verification

        mock_db = _mock_db()

        line = MagicMock()
        line.status = "pending_verify"
        plan = MagicMock()
        plan.id = 1
        plan.lines = [line]
        mock_db.query.return_value.filter.return_value.all.return_value = [plan]

        with patch(
            "app.services.buyplan_workflow.verify_po_sent",
            new_callable=AsyncMock,
        ) as mock_verify:
            with patch("app.database.SessionLocal", return_value=mock_db):
                _run(_job_po_verification.__wrapped__())

            mock_verify.assert_called_once_with(plan, mock_db)

        mock_db.close.assert_called_once()

    @patch("app.jobs.inventory_jobs.logger")
    def test_verify_error_per_plan(self, mock_logger):
        """Individual plan verify errors are caught per-plan (not propagated)."""
        from app.jobs.inventory_jobs import _job_po_verification

        mock_db = _mock_db()

        line = MagicMock()
        line.status = "pending_verify"
        plan = MagicMock()
        plan.id = 1
        plan.lines = [line]
        mock_db.query.return_value.filter.return_value.all.return_value = [plan]

        with patch(
            "app.services.buyplan_workflow.verify_po_sent",
            new_callable=AsyncMock,
            side_effect=RuntimeError("verify failed"),
        ):
            with patch("app.database.SessionLocal", return_value=mock_db):
                # Should NOT raise — _safe_verify catches it
                _run(_job_po_verification.__wrapped__())

        mock_db.close.assert_called_once()

    @patch("app.jobs.inventory_jobs.logger")
    def test_scan_error(self, mock_logger):
        from app.jobs.inventory_jobs import _job_po_verification

        mock_db = _mock_db()
        mock_db.query.return_value.filter.side_effect = RuntimeError("db boom")

        with patch("app.database.SessionLocal", return_value=mock_db):
            with pytest.raises(RuntimeError, match="db boom"):
                _run(_job_po_verification.__wrapped__())

        mock_db.rollback.assert_called_once()
        mock_db.close.assert_called_once()


class TestJobStockAutocomplete:
    """Tests for _job_stock_autocomplete()."""

    @patch("app.jobs.inventory_jobs.logger")
    def test_completes_stuck_plans(self, mock_logger):
        from app.jobs.inventory_jobs import _job_stock_autocomplete

        mock_db = _mock_db()

        plan = MagicMock()
        plan.id = 1
        plan.is_stock_sale = True
        plan.status = "active"
        plan.approved_at = datetime.now(timezone.utc) - timedelta(hours=2)
        mock_db.query.return_value.filter.return_value.all.return_value = [plan]

        with patch("app.database.SessionLocal", return_value=mock_db):
            _run(_job_stock_autocomplete.__wrapped__())

        assert plan.status == "completed"
        mock_db.commit.assert_called_once()
        mock_db.close.assert_called_once()

    @patch("app.jobs.inventory_jobs.logger")
    def test_no_stuck_plans(self, mock_logger):
        from app.jobs.inventory_jobs import _job_stock_autocomplete

        mock_db = _mock_db()
        mock_db.query.return_value.filter.return_value.all.return_value = []

        with patch("app.database.SessionLocal", return_value=mock_db):
            _run(_job_stock_autocomplete.__wrapped__())

        mock_db.commit.assert_not_called()
        mock_db.close.assert_called_once()

    @patch("app.jobs.inventory_jobs.logger")
    def test_error_rolls_back(self, mock_logger):
        from app.jobs.inventory_jobs import _job_stock_autocomplete

        mock_db = _mock_db()
        mock_db.query.return_value.filter.return_value.all.side_effect = RuntimeError("fail")

        with patch("app.database.SessionLocal", return_value=mock_db):
            with pytest.raises(RuntimeError, match="fail"):
                _run(_job_stock_autocomplete.__wrapped__())

        mock_db.rollback.assert_called_once()
        mock_db.close.assert_called_once()


class TestParseStockFile:
    """Tests for _parse_stock_file()."""

    def test_parses_rows(self):
        from app.jobs.inventory_jobs import _parse_stock_file

        with patch("app.file_utils.parse_tabular_file", return_value=[{"mpn": "LM358"}]):
            with patch("app.file_utils.normalize_stock_row", return_value={"mpn": "LM358", "qty": 100}):
                result = _parse_stock_file(b"data", "test.csv")

        assert len(result) == 1
        assert result[0]["mpn"] == "LM358"

    def test_caps_at_5000_rows(self):
        from app.jobs.inventory_jobs import _parse_stock_file

        raw_rows = [{"mpn": f"PN{i}"} for i in range(6000)]

        with patch("app.file_utils.parse_tabular_file", return_value=raw_rows):
            with patch(
                "app.file_utils.normalize_stock_row",
                side_effect=lambda r: {"mpn": r["mpn"], "qty": 1},
            ):
                result = _parse_stock_file(b"data", "test.csv")

        assert len(result) == 5000

    def test_skips_none_rows(self):
        from app.jobs.inventory_jobs import _parse_stock_file

        with patch("app.file_utils.parse_tabular_file", return_value=[{"bad": "row"}]):
            with patch("app.file_utils.normalize_stock_row", return_value=None):
                result = _parse_stock_file(b"data", "test.csv")

        assert len(result) == 0


# ===========================================================================
# core_jobs.py
# ===========================================================================


class TestRegisterCoreJobs:
    def test_registers_with_activity_tracking(self):
        from app.jobs.core_jobs import register_core_jobs

        scheduler = MagicMock()
        settings = MagicMock()
        settings.inbox_scan_interval_min = 5
        settings.activity_tracking_enabled = True

        register_core_jobs(scheduler, settings)
        assert scheduler.add_job.call_count == 7

    def test_registers_without_activity_tracking(self):
        from app.jobs.core_jobs import register_core_jobs

        scheduler = MagicMock()
        settings = MagicMock()
        settings.inbox_scan_interval_min = 5
        settings.activity_tracking_enabled = False

        register_core_jobs(scheduler, settings)
        assert scheduler.add_job.call_count == 6


class TestJobAutoArchive:
    """Tests for _job_auto_archive()."""

    @patch("app.jobs.core_jobs.logger")
    def test_archives_stale_reqs(self, mock_logger):
        from app.jobs.core_jobs import _job_auto_archive

        mock_db = _mock_db()
        mock_db.query.return_value.filter.return_value.update.return_value = 2

        with patch("app.database.SessionLocal", return_value=mock_db):
            _run(_job_auto_archive.__wrapped__())

        mock_db.commit.assert_called_once()
        mock_db.close.assert_called_once()

    @patch("app.jobs.core_jobs.logger")
    def test_no_stale_reqs(self, mock_logger):
        from app.jobs.core_jobs import _job_auto_archive

        mock_db = _mock_db()
        mock_db.query.return_value.filter.return_value.update.return_value = 0

        with patch("app.database.SessionLocal", return_value=mock_db):
            _run(_job_auto_archive.__wrapped__())

        mock_db.commit.assert_not_called()
        mock_db.close.assert_called_once()

    @patch("app.jobs.core_jobs.logger")
    def test_operational_error(self, mock_logger):
        import sqlalchemy.exc

        from app.jobs.core_jobs import _job_auto_archive

        mock_db = _mock_db()
        mock_db.query.return_value.filter.return_value.update.side_effect = sqlalchemy.exc.OperationalError(
            "stmt", {}, Exception("conn")
        )

        with patch("app.database.SessionLocal", return_value=mock_db):
            with pytest.raises(sqlalchemy.exc.OperationalError):
                _run(_job_auto_archive.__wrapped__())

        mock_db.rollback.assert_called_once()
        mock_db.close.assert_called_once()

    @patch("app.jobs.core_jobs.logger")
    def test_generic_exception(self, mock_logger):
        from app.jobs.core_jobs import _job_auto_archive

        mock_db = _mock_db()
        mock_db.query.return_value.filter.return_value.update.side_effect = RuntimeError("bad")

        with patch("app.database.SessionLocal", return_value=mock_db):
            with pytest.raises(RuntimeError, match="bad"):
                _run(_job_auto_archive.__wrapped__())

        mock_db.rollback.assert_called_once()
        mock_db.close.assert_called_once()


class TestJobTokenRefresh:
    """Tests for _job_token_refresh()."""

    @patch("app.jobs.core_jobs.logger")
    def test_no_users_needing_refresh(self, mock_logger):
        from app.jobs.core_jobs import _job_token_refresh

        mock_db = _mock_db()
        mock_db.query.return_value.filter.return_value.all.return_value = []

        with patch("app.database.SessionLocal", return_value=mock_db):
            _run(_job_token_refresh.__wrapped__())

        mock_db.close.assert_called_once()

    @patch("app.jobs.core_jobs.logger")
    def test_user_with_expired_token(self, mock_logger):
        from app.jobs.core_jobs import _job_token_refresh

        user = MagicMock()
        user.id = 1
        user.refresh_token = "rt_123"
        user.token_expires_at = datetime.now(timezone.utc) - timedelta(minutes=5)
        user.access_token = "at_123"

        selector_db = _mock_db()
        selector_db.query.return_value.filter.return_value.all.return_value = [user]

        task_db = _mock_db()
        task_db.get.return_value = user

        call_count = [0]

        def session_factory():
            call_count[0] += 1
            if call_count[0] == 1:
                return selector_db
            return task_db

        with patch("app.database.SessionLocal", side_effect=session_factory):
            with patch("app.jobs.core_jobs._utc", side_effect=lambda x: x):
                with patch("app.cache.intel_cache._get_redis", return_value=None):
                    with patch(
                        "app.utils.token_manager.refresh_user_token",
                        new_callable=AsyncMock,
                    ):
                        _run(_job_token_refresh.__wrapped__())

        selector_db.close.assert_called_once()

    @patch("app.jobs.core_jobs.logger")
    def test_user_no_access_token(self, mock_logger):
        from app.jobs.core_jobs import _job_token_refresh

        user = MagicMock()
        user.id = 1
        user.refresh_token = "rt_123"
        user.token_expires_at = None
        user.access_token = None

        selector_db = _mock_db()
        selector_db.query.return_value.filter.return_value.all.return_value = [user]

        task_db = _mock_db()
        task_db.get.return_value = user

        call_count = [0]

        def session_factory():
            call_count[0] += 1
            if call_count[0] == 1:
                return selector_db
            return task_db

        with patch("app.database.SessionLocal", side_effect=session_factory):
            with patch("app.cache.intel_cache._get_redis", return_value=None):
                with patch(
                    "app.utils.token_manager.refresh_user_token",
                    new_callable=AsyncMock,
                ):
                    _run(_job_token_refresh.__wrapped__())

        selector_db.close.assert_called_once()

    @patch("app.jobs.core_jobs.logger")
    def test_selector_db_error(self, mock_logger):
        import sqlalchemy.exc

        from app.jobs.core_jobs import _job_token_refresh

        mock_db = _mock_db()
        mock_db.query.return_value.filter.return_value.all.side_effect = sqlalchemy.exc.OperationalError(
            "stmt", {}, Exception("conn")
        )

        with patch("app.database.SessionLocal", return_value=mock_db):
            with pytest.raises(sqlalchemy.exc.OperationalError):
                _run(_job_token_refresh.__wrapped__())

        mock_db.close.assert_called_once()


class TestJobBatchResults:
    """Tests for _job_batch_results()."""

    @patch("app.jobs.core_jobs.logger")
    def test_happy_path(self, mock_logger):
        from app.jobs.core_jobs import _job_batch_results

        mock_db = _mock_db()

        with patch("app.jobs.core_jobs.asyncio.wait_for", new_callable=AsyncMock, return_value=5):
            with patch("app.database.SessionLocal", return_value=mock_db):
                _run(_job_batch_results.__wrapped__())

        mock_db.close.assert_called_once()

    @patch("app.jobs.core_jobs.logger")
    def test_no_results(self, mock_logger):
        from app.jobs.core_jobs import _job_batch_results

        mock_db = _mock_db()

        with patch("app.jobs.core_jobs.asyncio.wait_for", new_callable=AsyncMock, return_value=0):
            with patch("app.database.SessionLocal", return_value=mock_db):
                _run(_job_batch_results.__wrapped__())

        mock_db.close.assert_called_once()

    @patch("app.jobs.core_jobs.logger")
    def test_timeout_error(self, mock_logger):
        from app.jobs.core_jobs import _job_batch_results

        mock_db = _mock_db()

        with patch(
            "app.jobs.core_jobs.asyncio.wait_for",
            new_callable=AsyncMock,
            side_effect=asyncio.TimeoutError,
        ):
            with patch("app.database.SessionLocal", return_value=mock_db):
                with pytest.raises(asyncio.TimeoutError):
                    _run(_job_batch_results.__wrapped__())

        mock_db.close.assert_called_once()

    @patch("app.jobs.core_jobs.logger")
    def test_generic_exception(self, mock_logger):
        from app.jobs.core_jobs import _job_batch_results

        mock_db = _mock_db()

        with patch(
            "app.jobs.core_jobs.asyncio.wait_for",
            new_callable=AsyncMock,
            side_effect=ValueError("bad"),
        ):
            with patch("app.database.SessionLocal", return_value=mock_db):
                with pytest.raises(ValueError, match="bad"):
                    _run(_job_batch_results.__wrapped__())

        mock_db.close.assert_called_once()


class TestJobBatchParseSignatures:
    """Tests for _job_batch_parse_signatures()."""

    @patch("app.jobs.core_jobs.logger")
    def test_happy_path(self, mock_logger):
        from app.jobs.core_jobs import _job_batch_parse_signatures

        mock_db = _mock_db()

        with patch(
            "app.jobs.core_jobs.asyncio.wait_for",
            new_callable=AsyncMock,
            return_value="batch-123",
        ):
            with patch("app.database.SessionLocal", return_value=mock_db):
                _run(_job_batch_parse_signatures.__wrapped__())

        mock_db.close.assert_called_once()

    @patch("app.jobs.core_jobs.logger")
    def test_no_batch(self, mock_logger):
        from app.jobs.core_jobs import _job_batch_parse_signatures

        mock_db = _mock_db()

        with patch(
            "app.jobs.core_jobs.asyncio.wait_for",
            new_callable=AsyncMock,
            return_value=None,
        ):
            with patch("app.database.SessionLocal", return_value=mock_db):
                _run(_job_batch_parse_signatures.__wrapped__())

        mock_db.close.assert_called_once()

    @patch("app.jobs.core_jobs.logger")
    def test_timeout(self, mock_logger):
        from app.jobs.core_jobs import _job_batch_parse_signatures

        mock_db = _mock_db()

        with patch(
            "app.jobs.core_jobs.asyncio.wait_for",
            new_callable=AsyncMock,
            side_effect=asyncio.TimeoutError,
        ):
            with patch("app.database.SessionLocal", return_value=mock_db):
                with pytest.raises(asyncio.TimeoutError):
                    _run(_job_batch_parse_signatures.__wrapped__())

        mock_db.rollback.assert_called_once()
        mock_db.close.assert_called_once()


class TestJobPollSignatureBatch:
    """Tests for _job_poll_signature_batch()."""

    @patch("app.jobs.core_jobs.logger")
    def test_happy_path(self, mock_logger):
        from app.jobs.core_jobs import _job_poll_signature_batch

        mock_db = _mock_db()

        with patch(
            "app.jobs.core_jobs.asyncio.wait_for",
            new_callable=AsyncMock,
            return_value={"applied": 5, "errors": 1},
        ):
            with patch("app.database.SessionLocal", return_value=mock_db):
                _run(_job_poll_signature_batch.__wrapped__())

        mock_db.close.assert_called_once()

    @patch("app.jobs.core_jobs.logger")
    def test_no_results(self, mock_logger):
        from app.jobs.core_jobs import _job_poll_signature_batch

        mock_db = _mock_db()

        with patch(
            "app.jobs.core_jobs.asyncio.wait_for",
            new_callable=AsyncMock,
            return_value=None,
        ):
            with patch("app.database.SessionLocal", return_value=mock_db):
                _run(_job_poll_signature_batch.__wrapped__())

        mock_db.close.assert_called_once()

    @patch("app.jobs.core_jobs.logger")
    def test_generic_exception(self, mock_logger):
        from app.jobs.core_jobs import _job_poll_signature_batch

        mock_db = _mock_db()

        with patch(
            "app.jobs.core_jobs.asyncio.wait_for",
            new_callable=AsyncMock,
            side_effect=RuntimeError("fail"),
        ):
            with patch("app.database.SessionLocal", return_value=mock_db):
                with pytest.raises(RuntimeError, match="fail"):
                    _run(_job_poll_signature_batch.__wrapped__())

        mock_db.rollback.assert_called_once()
        mock_db.close.assert_called_once()


class TestJobWebhookSubscriptions:
    """Tests for _job_webhook_subscriptions()."""

    @patch("app.jobs.core_jobs.logger")
    def test_happy_path(self, mock_logger):
        from app.jobs.core_jobs import _job_webhook_subscriptions

        mock_db = _mock_db()

        with patch(
            "app.services.webhook_service.renew_expiring_subscriptions",
            new_callable=AsyncMock,
        ):
            with patch(
                "app.services.webhook_service.ensure_all_users_subscribed",
                new_callable=AsyncMock,
            ):
                with patch("app.database.SessionLocal", return_value=mock_db):
                    _run(_job_webhook_subscriptions.__wrapped__())

        mock_db.close.assert_called_once()

    @patch("app.jobs.core_jobs.logger")
    def test_http_error(self, mock_logger):
        import httpx

        from app.jobs.core_jobs import _job_webhook_subscriptions

        mock_db = _mock_db()

        with patch(
            "app.services.webhook_service.renew_expiring_subscriptions",
            new_callable=AsyncMock,
            side_effect=httpx.HTTPError("network error"),
        ):
            with patch("app.database.SessionLocal", return_value=mock_db):
                with pytest.raises(httpx.HTTPError):
                    _run(_job_webhook_subscriptions.__wrapped__())

        mock_db.rollback.assert_called_once()
        mock_db.close.assert_called_once()

    @patch("app.jobs.core_jobs.logger")
    def test_generic_exception(self, mock_logger):
        from app.jobs.core_jobs import _job_webhook_subscriptions

        mock_db = _mock_db()

        with patch(
            "app.services.webhook_service.renew_expiring_subscriptions",
            new_callable=AsyncMock,
            side_effect=RuntimeError("fail"),
        ):
            with patch("app.database.SessionLocal", return_value=mock_db):
                with pytest.raises(RuntimeError, match="fail"):
                    _run(_job_webhook_subscriptions.__wrapped__())

        mock_db.rollback.assert_called_once()
        mock_db.close.assert_called_once()


class TestJobInboxScan:
    """Tests for _job_inbox_scan()."""

    @patch("app.jobs.core_jobs.logger")
    def test_no_users_to_scan(self, mock_logger):
        from app.jobs.core_jobs import _job_inbox_scan

        mock_db = _mock_db()
        mock_db.query.return_value.filter.return_value.all.return_value = []

        with patch("app.database.SessionLocal", return_value=mock_db):
            with patch("app.config.settings") as mock_settings:
                mock_settings.inbox_scan_interval_min = 5
                _run(_job_inbox_scan.__wrapped__())

        mock_db.close.assert_called_once()

    @patch("app.jobs.core_jobs.logger")
    def test_user_without_access_token(self, mock_logger):
        from app.jobs.core_jobs import _job_inbox_scan

        user = MagicMock()
        user.id = 1
        user.access_token = None
        user.m365_connected = True
        user.refresh_token = "rt_123"
        user.last_inbox_scan = None

        mock_db = _mock_db()
        mock_db.query.return_value.filter.return_value.all.return_value = [user]

        with patch("app.database.SessionLocal", return_value=mock_db):
            with patch("app.config.settings") as mock_settings:
                mock_settings.inbox_scan_interval_min = 5
                _run(_job_inbox_scan.__wrapped__())

        # User skipped because no access_token
        mock_db.close.assert_called_once()

    @patch("app.jobs.core_jobs.logger")
    def test_user_needing_scan(self, mock_logger):
        from app.jobs.core_jobs import _job_inbox_scan

        user = MagicMock()
        user.id = 1
        user.access_token = "at_123"
        user.m365_connected = True
        user.refresh_token = "rt_123"
        user.last_inbox_scan = None

        selector_db = _mock_db()
        selector_db.query.return_value.filter.return_value.all.return_value = [user]

        scan_db = _mock_db()
        scan_db.get.return_value = user

        call_count = [0]

        def session_factory():
            call_count[0] += 1
            if call_count[0] == 1:
                return selector_db
            return scan_db

        with patch("app.database.SessionLocal", side_effect=session_factory):
            with patch("app.config.settings") as mock_settings:
                mock_settings.inbox_scan_interval_min = 5
                with patch(
                    "app.jobs.email_jobs._scan_user_inbox",
                    new_callable=AsyncMock,
                ):
                    with patch("app.jobs.core_jobs.asyncio.wait_for", new_callable=AsyncMock):
                        _run(_job_inbox_scan.__wrapped__())

        selector_db.close.assert_called_once()

    @patch("app.jobs.core_jobs.logger")
    def test_operational_error(self, mock_logger):
        import sqlalchemy.exc

        from app.jobs.core_jobs import _job_inbox_scan

        mock_db = _mock_db()
        mock_db.query.return_value.filter.return_value.all.side_effect = sqlalchemy.exc.OperationalError(
            "stmt", {}, Exception("conn")
        )

        with patch("app.database.SessionLocal", return_value=mock_db):
            with patch("app.config.settings") as mock_settings:
                mock_settings.inbox_scan_interval_min = 5
                with pytest.raises(sqlalchemy.exc.OperationalError):
                    _run(_job_inbox_scan.__wrapped__())

        mock_db.close.assert_called_once()


# ===========================================================================
# global_search_service.py
# ===========================================================================


class TestGlobalSearchHelpers:
    def test_ai_cache_key(self):
        from app.services.global_search_service import _ai_cache_key

        key = _ai_cache_key("LM358")
        assert key.startswith("ai_search:")
        assert len(key) > 12

    def test_ai_cache_key_normalizes(self):
        from app.services.global_search_service import _ai_cache_key

        assert _ai_cache_key("LM358") == _ai_cache_key("  lm358  ")

    def test_get_ai_cache_returns_none_on_error(self):
        from app.services.global_search_service import _get_ai_cache

        with patch("app.cache.intel_cache.get_cached", side_effect=Exception("redis down")):
            assert _get_ai_cache("test") is None

    def test_get_ai_cache_returns_value(self):
        from app.services.global_search_service import _get_ai_cache

        cached = {"groups": {}, "total_count": 1}
        with patch("app.cache.intel_cache.get_cached", return_value=cached):
            assert _get_ai_cache("test") == cached

    def test_set_ai_cache_handles_error(self):
        from app.services.global_search_service import _set_ai_cache

        with patch("app.cache.intel_cache.set_cached", side_effect=Exception("redis down")):
            # Should not raise
            _set_ai_cache("test", {"groups": {}})

    def test_empty_result(self):
        from app.services.global_search_service import _empty_result

        result = _empty_result()
        assert result["best_match"] is None
        assert result["total_count"] == 0
        assert "requisitions" in result["groups"]

    def test_to_dict(self):
        from app.services.global_search_service import _to_dict

        obj = MagicMock()
        obj.id = 42
        obj.name = "Test"
        obj.status = "active"

        d = _to_dict(obj, ["name", "status"], "requisition")
        assert d["type"] == "requisition"
        assert d["id"] == 42
        assert d["name"] == "Test"
        assert d["status"] == "active"

    def test_is_postgres(self):
        from app.services.global_search_service import _is_postgres

        db = MagicMock()
        db.bind.dialect.name = "postgresql"
        assert _is_postgres(db) is True

        db.bind.dialect.name = "sqlite"
        assert _is_postgres(db) is False


class TestFastSearch:
    def test_empty_query_returns_empty(self):
        from app.services.global_search_service import fast_search

        result = fast_search("", MagicMock())
        assert result["total_count"] == 0
        assert result["best_match"] is None

    def test_short_query_returns_empty(self):
        from app.services.global_search_service import fast_search

        result = fast_search("a", MagicMock())
        assert result["total_count"] == 0

    def test_sqlite_search(self):
        from app.services.global_search_service import fast_search

        db = MagicMock()
        db.bind.dialect.name = "sqlite"

        # Build a chainable mock that handles any depth of chaining
        chain = MagicMock()
        chain.limit.return_value.all.return_value = []
        chain.order_by.return_value.limit.return_value.all.return_value = []

        db.query.return_value.filter.return_value = chain

        result = fast_search("LM358", db)
        assert result["total_count"] == 0
        assert result["best_match"] is None

    def test_search_with_results(self):
        from app.services.global_search_service import fast_search

        db = MagicMock()
        db.bind.dialect.name = "sqlite"

        req_mock = MagicMock()
        req_mock.id = 1
        req_mock.name = "LM358 Order"
        req_mock.customer_name = "Acme"
        req_mock.status = "active"

        # Track filter calls to return results only on the first call (requisitions)
        call_count = [0]

        def filter_side_effect(*args, **kwargs):
            call_count[0] += 1
            m = MagicMock()
            if call_count[0] == 1:
                m.limit.return_value.all.return_value = [req_mock]
            else:
                m.limit.return_value.all.return_value = []
            m.order_by.return_value.limit.return_value.all.return_value = []
            return m

        db.query.return_value.filter = MagicMock(side_effect=filter_side_effect)

        result = fast_search("LM358", db)
        assert result["total_count"] >= 1
        assert result["best_match"] is not None
        assert result["best_match"]["type"] == "requisition"


class TestRunIntentQuery:
    def test_unknown_entity_type(self):
        from app.services.global_search_service import _run_intent_query

        group_key, results = _run_intent_query({"entity_type": "invalid"}, _mock_db())
        assert group_key == ""
        assert results == []

    def test_empty_text_query(self):
        from app.services.global_search_service import _run_intent_query

        group_key, results = _run_intent_query(
            {"entity_type": "requisition", "text_query": "   "},
            _mock_db(),
        )
        assert results == []

    def test_valid_requisition_query(self):
        from app.services.global_search_service import _run_intent_query

        db = _mock_db()

        req = MagicMock()
        req.id = 1
        req.name = "Test Req"
        req.customer_name = "Acme"
        req.status = "active"

        db.query.return_value.filter.return_value.limit.return_value.all.return_value = [req]

        group_key, results = _run_intent_query(
            {"entity_type": "requisition", "text_query": "Test"},
            db,
        )
        assert group_key == "requisitions"
        assert len(results) == 1
        assert results[0]["type"] == "requisition"

    def test_part_dedup(self):
        from app.services.global_search_service import _run_intent_query

        db = _mock_db()

        part1 = MagicMock()
        part1.id = 1
        part1.normalized_mpn = "lm358"
        part1.primary_mpn = "LM358"
        part1.brand = "TI"
        part1.requisition_id = 1

        part2 = MagicMock()
        part2.id = 2
        part2.normalized_mpn = "lm358"
        part2.primary_mpn = "LM358"
        part2.brand = "TI"
        part2.requisition_id = 2

        db.query.return_value.filter.return_value.limit.return_value.all.return_value = [part1, part2]

        group_key, results = _run_intent_query(
            {"entity_type": "part", "text_query": "LM358"},
            db,
        )
        assert group_key == "parts"
        assert len(results) == 1  # deduped

    def test_offer_dedup(self):
        from app.services.global_search_service import _run_intent_query

        db = _mock_db()

        offer1 = MagicMock()
        offer1.id = 1
        offer1.mpn = "LM358"
        offer1.vendor_name = "Acme"
        offer1.unit_price = 1.50
        offer1.qty_available = 100
        offer1.requisition_id = 1

        offer2 = MagicMock()
        offer2.id = 2
        offer2.mpn = "LM358"
        offer2.vendor_name = "Acme"
        offer2.unit_price = 1.60
        offer2.qty_available = 200
        offer2.requisition_id = 1

        db.query.return_value.filter.return_value.limit.return_value.all.return_value = [offer1, offer2]

        group_key, results = _run_intent_query(
            {"entity_type": "offer", "text_query": "LM358"},
            db,
        )
        assert group_key == "offers"
        assert len(results) == 1  # deduped

    def test_filters_applied(self):
        from app.services.global_search_service import _run_intent_query

        db = _mock_db()
        db.query.return_value.filter.return_value.filter.return_value.limit.return_value.all.return_value = []

        _run_intent_query(
            {
                "entity_type": "requisition",
                "text_query": "Raytheon",
                "filters": {"status": "active", "customer_name": "Raytheon"},
            },
            db,
        )
        # Ensures no errors; filters are applied on the query chain

    def test_email_domain_filter(self):
        from app.services.global_search_service import _run_intent_query

        db = _mock_db()
        db.query.return_value.filter.return_value.filter.return_value.limit.return_value.all.return_value = []

        _run_intent_query(
            {
                "entity_type": "vendor_contact",
                "text_query": "john",
                "filters": {"email_domain": "acme.com"},
            },
            db,
        )

    def test_is_blacklisted_filter(self):
        from app.services.global_search_service import _run_intent_query

        db = _mock_db()
        db.query.return_value.filter.return_value.filter.return_value.limit.return_value.all.return_value = []

        _run_intent_query(
            {
                "entity_type": "vendor",
                "text_query": "shady",
                "filters": {"is_blacklisted": True},
            },
            db,
        )


class TestAiSearch:
    def test_returns_cached(self):
        from app.services.global_search_service import ai_search

        cached = {"best_match": None, "groups": {}, "total_count": 0}
        with patch("app.services.global_search_service._get_ai_cache", return_value=cached):
            result = _run(ai_search("test", _mock_db()))
        assert result == cached

    def test_claude_unavailable_falls_back(self):
        from app.services.global_search_service import ai_search
        from app.utils.claude_errors import ClaudeUnavailableError

        with patch("app.services.global_search_service._get_ai_cache", return_value=None):
            with patch(
                "app.services.global_search_service.claude_structured",
                new_callable=AsyncMock,
                side_effect=ClaudeUnavailableError("no key"),
            ):
                with patch("app.services.global_search_service.fast_search") as mock_fast:
                    mock_fast.return_value = {"best_match": None, "groups": {}, "total_count": 0}
                    result = _run(ai_search("test", _mock_db()))
                    mock_fast.assert_called_once()

    def test_claude_error_falls_back(self):
        from app.services.global_search_service import ai_search
        from app.utils.claude_errors import ClaudeError

        with patch("app.services.global_search_service._get_ai_cache", return_value=None):
            with patch(
                "app.services.global_search_service.claude_structured",
                new_callable=AsyncMock,
                side_effect=ClaudeError("fail"),
            ):
                with patch("app.services.global_search_service.fast_search") as mock_fast:
                    mock_fast.return_value = {"best_match": None, "groups": {}, "total_count": 0}
                    result = _run(ai_search("test", _mock_db()))
                    mock_fast.assert_called_once()

    def test_claude_returns_none(self):
        from app.services.global_search_service import ai_search

        with patch("app.services.global_search_service._get_ai_cache", return_value=None):
            with patch(
                "app.services.global_search_service.claude_structured",
                new_callable=AsyncMock,
                return_value=None,
            ):
                with patch("app.services.global_search_service.fast_search") as mock_fast:
                    mock_fast.return_value = {"best_match": None, "groups": {}, "total_count": 0}
                    result = _run(ai_search("test", _mock_db()))
                    mock_fast.assert_called_once()

    def test_claude_success_with_results(self):
        from app.services.global_search_service import ai_search

        intent = {
            "searches": [
                {"entity_type": "requisition", "text_query": "Test"},
            ]
        }

        with patch("app.services.global_search_service._get_ai_cache", return_value=None):
            with patch(
                "app.services.global_search_service.claude_structured",
                new_callable=AsyncMock,
                return_value=intent,
            ):
                with patch(
                    "app.services.global_search_service._run_intent_query",
                    return_value=("requisitions", [{"type": "requisition", "id": 1}]),
                ):
                    with patch("app.services.global_search_service._set_ai_cache"):
                        result = _run(ai_search("test", _mock_db()))

        assert result["total_count"] == 1
        assert result["best_match"]["type"] == "requisition"

    def test_dedup_across_searches(self):
        from app.services.global_search_service import ai_search

        intent = {
            "searches": [
                {"entity_type": "requisition", "text_query": "Test"},
                {"entity_type": "requisition", "text_query": "Test"},
            ]
        }

        with patch("app.services.global_search_service._get_ai_cache", return_value=None):
            with patch(
                "app.services.global_search_service.claude_structured",
                new_callable=AsyncMock,
                return_value=intent,
            ):
                with patch(
                    "app.services.global_search_service._run_intent_query",
                    return_value=("requisitions", [{"type": "requisition", "id": 1}]),
                ):
                    with patch("app.services.global_search_service._set_ai_cache"):
                        result = _run(ai_search("test", _mock_db()))

        # Same id should be deduped
        assert result["total_count"] == 1


# ===========================================================================
# enrichment.py
# ===========================================================================


class TestEnrichMaterialCard:
    def test_returns_first_match(self):
        from app.services.enrichment import enrich_material_card

        result = {"manufacturer": "TI", "category": "IC", "source": "digikey", "confidence": 0.95}

        with patch(
            "app.services.enrichment._try_connector_config",
            new_callable=AsyncMock,
            return_value=result,
        ):
            out = _run(enrich_material_card("LM358", _mock_db()))

        assert out["manufacturer"] == "TI"

    def test_returns_none_when_all_fail(self):
        from app.services.enrichment import enrich_material_card

        with patch(
            "app.services.enrichment._try_connector_config",
            new_callable=AsyncMock,
            return_value=None,
        ):
            out = _run(enrich_material_card("INVALID", _mock_db()))

        assert out is None


class TestTryConnectorConfig:
    def test_missing_credentials(self):
        from app.services.enrichment import _try_connector_config

        config = {
            "name": "digikey",
            "module": "app.connectors.digikey",
            "class": "DigiKeyConnector",
            "creds": [("digikey", "DIGIKEY_CLIENT_ID")],
            "confidence": 0.95,
        }

        with patch("app.services.enrichment.get_credential_cached", return_value=None):
            out = _run(_try_connector_config(config, "LM358"))

        assert out is None

    def test_successful_search(self):
        from app.services.enrichment import _try_connector_config

        config = {
            "name": "digikey",
            "module": "app.connectors.digikey",
            "class": "DigiKeyConnector",
            "creds": [("digikey", "DIGIKEY_CLIENT_ID")],
            "confidence": 0.95,
        }

        mock_connector = MagicMock()
        mock_connector.search = AsyncMock(return_value=[{"manufacturer": "Texas Instruments", "category": "IC"}])

        with patch("app.services.enrichment.get_credential_cached", return_value="key123"):
            with patch("app.services.enrichment.importlib.import_module") as mock_import:
                mock_module = MagicMock()
                mock_module.DigiKeyConnector.return_value = mock_connector
                mock_import.return_value = mock_module
                with patch("app.services.enrichment.asyncio.wait_for", new_callable=AsyncMock) as mock_wait:
                    mock_wait.return_value = [{"manufacturer": "Texas Instruments", "category": "IC"}]
                    out = _run(_try_connector_config(config, "LM358"))

        assert out["manufacturer"] == "Texas Instruments"
        assert out["source"] == "digikey"

    def test_timeout(self):
        from app.services.enrichment import _try_connector_config

        config = {
            "name": "digikey",
            "module": "app.connectors.digikey",
            "class": "DigiKeyConnector",
            "creds": [("digikey", "DIGIKEY_CLIENT_ID")],
            "confidence": 0.95,
        }

        with patch("app.services.enrichment.get_credential_cached", return_value="key123"):
            with patch("app.services.enrichment.importlib.import_module") as mock_import:
                mock_module = MagicMock()
                mock_import.return_value = mock_module
                with patch(
                    "app.services.enrichment.asyncio.wait_for",
                    new_callable=AsyncMock,
                    side_effect=asyncio.TimeoutError,
                ):
                    out = _run(_try_connector_config(config, "LM358"))

        assert out is None

    def test_auth_error(self):
        from app.services.enrichment import _try_connector_config

        config = {
            "name": "digikey",
            "module": "app.connectors.digikey",
            "class": "DigiKeyConnector",
            "creds": [("digikey", "DIGIKEY_CLIENT_ID")],
            "confidence": 0.95,
        }

        with patch("app.services.enrichment.get_credential_cached", return_value="key123"):
            with patch("app.services.enrichment.importlib.import_module") as mock_import:
                mock_module = MagicMock()
                mock_import.return_value = mock_module
                with patch(
                    "app.services.enrichment.asyncio.wait_for",
                    new_callable=AsyncMock,
                    side_effect=Exception("401 Unauthorized"),
                ):
                    out = _run(_try_connector_config(config, "LM358"))

        assert out is None

    def test_rate_limit_error(self):
        from app.services.enrichment import _try_connector_config

        config = {
            "name": "mouser",
            "module": "app.connectors.mouser",
            "class": "MouserConnector",
            "creds": [("mouser", "MOUSER_API_KEY")],
            "confidence": 0.95,
        }

        with patch("app.services.enrichment.get_credential_cached", return_value="key123"):
            with patch("app.services.enrichment.importlib.import_module") as mock_import:
                mock_module = MagicMock()
                mock_import.return_value = mock_module
                with patch(
                    "app.services.enrichment.asyncio.wait_for",
                    new_callable=AsyncMock,
                    side_effect=Exception("429 rate limit exceeded"),
                ):
                    out = _run(_try_connector_config(config, "LM358"))

        assert out is None

    def test_ignored_manufacturer(self):
        from app.services.enrichment import _try_connector_config

        config = {
            "name": "digikey",
            "module": "app.connectors.digikey",
            "class": "DigiKeyConnector",
            "creds": [("digikey", "DIGIKEY_CLIENT_ID")],
            "confidence": 0.95,
        }

        with patch("app.services.enrichment.get_credential_cached", return_value="key123"):
            with patch("app.services.enrichment.importlib.import_module") as mock_import:
                mock_module = MagicMock()
                mock_import.return_value = mock_module
                with patch(
                    "app.services.enrichment.asyncio.wait_for",
                    new_callable=AsyncMock,
                    return_value=[{"manufacturer": "Unknown", "category": ""}],
                ):
                    out = _run(_try_connector_config(config, "LM358"))

        assert out is None


class TestEnrichBatch:
    def test_batch_enrichment(self):
        from app.services.enrichment import enrich_batch

        db = _mock_db()
        card = MagicMock()
        card.id = 1
        card.manufacturer = None
        card.category = None
        card.normalized_mpn = "lm358"
        db.query.return_value.filter_by.return_value.first.return_value = card

        result_data = {"manufacturer": "TI", "category": "IC", "source": "digikey", "confidence": 0.95}

        with patch(
            "app.services.enrichment.enrich_material_card",
            new_callable=AsyncMock,
            return_value=result_data,
        ):
            with patch("app.services.enrichment._apply_enrichment_to_card"):
                out = _run(enrich_batch(["lm358"], db))

        assert out["total"] == 1
        assert out["matched"] == 1
        assert out["skipped"] == 0

    def test_batch_enrichment_no_result(self):
        from app.services.enrichment import enrich_batch

        db = _mock_db()

        with patch(
            "app.services.enrichment.enrich_material_card",
            new_callable=AsyncMock,
            return_value=None,
        ):
            out = _run(enrich_batch(["lm358"], db))

        assert out["total"] == 1
        assert out["matched"] == 0
        assert out["skipped"] == 1

    def test_batch_no_card_found(self):
        from app.services.enrichment import enrich_batch

        db = _mock_db()
        db.query.return_value.filter_by.return_value.first.return_value = None

        result_data = {"manufacturer": "TI", "category": "IC", "source": "digikey", "confidence": 0.95}

        with patch(
            "app.services.enrichment.enrich_material_card",
            new_callable=AsyncMock,
            return_value=result_data,
        ):
            out = _run(enrich_batch(["lm358"], db))

        assert out["skipped"] == 1

    def test_batch_periodic_commit(self):
        """Test that commit happens every 100 items."""
        from app.services.enrichment import enrich_batch

        db = _mock_db()

        with patch(
            "app.services.enrichment.enrich_material_card",
            new_callable=AsyncMock,
            return_value=None,
        ):
            out = _run(enrich_batch([f"mpn{i}" for i in range(150)], db, concurrency=5))

        assert out["total"] == 150
        # Commit called at 100 + final = 2 times
        assert db.commit.call_count == 2


class TestApplyEnrichmentToCard:
    def test_applies_manufacturer_and_tags(self):
        from app.services.enrichment import _apply_enrichment_to_card

        card = MagicMock()
        card.manufacturer = None
        card.category = None
        card.normalized_mpn = "lm358"
        enrichment = {"manufacturer": "TI", "category": "IC", "source": "digikey", "confidence": 0.95}
        db = _mock_db()

        with patch(
            "app.services.enrichment.classify_material_card",
            return_value={"brand": {"name": "TI"}, "commodity": {"name": "IC"}},
        ):
            brand_tag = MagicMock()
            brand_tag.id = 1
            commodity_tag = MagicMock()
            commodity_tag.id = 2

            with patch("app.services.enrichment.get_or_create_brand_tag", return_value=brand_tag):
                with patch("app.services.enrichment.get_or_create_commodity_tag", return_value=commodity_tag):
                    with patch("app.services.enrichment.tag_material_card") as mock_tag:
                        _apply_enrichment_to_card(card, enrichment, db)

        assert card.manufacturer == "TI"
        assert card.category == "IC"
        mock_tag.assert_called_once()

    def test_does_not_overwrite_existing_manufacturer(self):
        from app.services.enrichment import _apply_enrichment_to_card

        card = MagicMock()
        card.manufacturer = "Existing MFR"
        card.category = "Existing Cat"
        card.normalized_mpn = "lm358"
        enrichment = {"manufacturer": "TI", "category": "IC", "source": "digikey", "confidence": 0.95}
        db = _mock_db()

        with patch(
            "app.services.enrichment.classify_material_card",
            return_value={},
        ):
            _apply_enrichment_to_card(card, enrichment, db)

        # Should NOT overwrite
        assert card.manufacturer == "Existing MFR"
        assert card.category == "Existing Cat"

    def test_no_commodity_tag(self):
        from app.services.enrichment import _apply_enrichment_to_card

        card = MagicMock()
        card.manufacturer = None
        card.category = None
        card.normalized_mpn = "lm358"
        enrichment = {"manufacturer": "TI", "source": "digikey", "confidence": 0.95}
        db = _mock_db()

        with patch(
            "app.services.enrichment.classify_material_card",
            return_value={"brand": {"name": "TI"}},
        ):
            brand_tag = MagicMock()
            brand_tag.id = 1
            with patch("app.services.enrichment.get_or_create_brand_tag", return_value=brand_tag):
                with patch("app.services.enrichment.tag_material_card") as mock_tag:
                    _apply_enrichment_to_card(card, enrichment, db)

        mock_tag.assert_called_once()


class TestBoostConfidenceInternal:
    def test_no_rows_to_boost(self):
        from app.services.enrichment import boost_confidence_internal

        db = MagicMock(spec=Session)
        # All query chains return empty lists for .all()
        # The function has 3 while-True loops that all call .all() and break on empty
        chain = MagicMock()
        chain.all.return_value = []
        chain.join.return_value = chain
        chain.filter.return_value = chain
        chain.order_by.return_value = chain
        chain.limit.return_value = chain
        chain.update.return_value = 0
        db.query.return_value = chain

        result = boost_confidence_internal(db)
        assert result["total_boosted"] == 0
        assert result["sighting_boosted"] == 0
        assert result["multi_source_boosted"] == 0

    def test_boosts_single_phase1_row(self):
        from app.services.enrichment import boost_confidence_internal

        db = MagicMock(spec=Session)

        row = MagicMock()
        row.id = 1

        # Use a counter to return rows on first call, empty on subsequent
        all_calls = [0]

        def all_side_effect():
            all_calls[0] += 1
            # First .all() call (Phase 1, batch 1) returns a row
            if all_calls[0] == 1:
                return [row]
            # All subsequent .all() calls return empty (exit loops)
            return []

        chain = MagicMock()
        chain.all.side_effect = all_side_effect
        chain.join.return_value = chain
        chain.filter.return_value = chain
        chain.order_by.return_value = chain
        chain.limit.return_value = chain
        chain.update.return_value = 1
        db.query.return_value = chain

        result = boost_confidence_internal(db)
        assert result["total_boosted"] == 1


class TestNexarBulkValidate:
    def test_no_low_confidence_tags(self):
        from app.services.enrichment import nexar_bulk_validate

        db = _mock_db()
        db.query.return_value.join.return_value.join.return_value.filter.return_value.order_by.return_value.limit.return_value.all.return_value = []

        result = _run(nexar_bulk_validate(db))
        assert result["total_checked"] == 0

    def test_no_nexar_credentials(self):
        from app.services.enrichment import nexar_bulk_validate

        db = _mock_db()

        row = MagicMock()
        row.id = 1
        row.normalized_mpn = "lm358"
        row.mt_id = 10
        row.tag_name = "TI"

        db.query.return_value.join.return_value.join.return_value.filter.return_value.order_by.return_value.limit.return_value.all.return_value = [
            row
        ]

        with patch("app.services.enrichment.get_credential_cached", return_value=None):
            result = _run(nexar_bulk_validate(db))

        assert result["error"] == "no_nexar_creds"


class TestNexarBackfillUntagged:
    def test_no_untagged_cards(self):
        from app.services.enrichment import nexar_backfill_untagged

        db = _mock_db()
        db.query.return_value.filter.return_value.order_by.return_value.limit.return_value.all.return_value = []

        result = _run(nexar_backfill_untagged(db))
        assert result["total_checked"] == 0

    def test_no_nexar_credentials(self):
        from app.services.enrichment import nexar_backfill_untagged

        db = _mock_db()

        row = MagicMock()
        row.id = 1
        row.normalized_mpn = "lm358"

        # The subquery chain
        db.query.return_value.join.return_value.filter.return_value.distinct.return_value.subquery.return_value = (
            MagicMock()
        )
        db.query.return_value.filter.return_value.order_by.return_value.limit.return_value.all.return_value = [row]

        with patch("app.services.enrichment.get_credential_cached", return_value=None):
            result = _run(nexar_backfill_untagged(db))

        assert result["error"] == "no_nexar_creds"


class TestCrossValidateBatch:
    def test_no_low_confidence_tags(self):
        from app.services.enrichment import cross_validate_batch

        db = _mock_db()
        db.query.return_value.join.return_value.join.return_value.filter.return_value.order_by.return_value.limit.return_value.all.return_value = []

        result = _run(cross_validate_batch(db))
        assert result["total"] == 0
        assert result["confirmed"] == 0

    def test_confirmed_tag(self):
        from app.services.enrichment import cross_validate_batch

        db = _mock_db()

        row = MagicMock()
        row.id = 1
        row.normalized_mpn = "lm358"
        row.manufacturer = "TI"
        row.mt_id = 10
        row.confidence = 0.7
        row.tag_name = "TI"

        db.query.return_value.join.return_value.join.return_value.filter.return_value.order_by.return_value.limit.return_value.all.return_value = [
            row
        ]

        mt = MagicMock()
        mt.confidence = 0.7
        db.get.return_value = mt

        enrichment_result = {"manufacturer": "TI", "source": "digikey", "confidence": 0.95}

        with patch(
            "app.services.enrichment.enrich_material_card",
            new_callable=AsyncMock,
            return_value=enrichment_result,
        ):
            result = _run(cross_validate_batch(db))

        assert result["confirmed"] == 1
        assert mt.confidence == 0.95

    def test_changed_manufacturer(self):
        from app.services.enrichment import cross_validate_batch

        db = _mock_db()

        row = MagicMock()
        row.id = 1
        row.normalized_mpn = "lm358"
        row.manufacturer = "TI"
        row.mt_id = 10
        row.confidence = 0.7
        row.tag_name = "Texas Instruments"

        db.query.return_value.join.return_value.join.return_value.filter.return_value.order_by.return_value.limit.return_value.all.return_value = [
            row
        ]

        card = MagicMock()
        db.get.return_value = card

        # Connector says "Analog Devices" — different
        enrichment_result = {"manufacturer": "Analog Devices", "source": "mouser", "confidence": 0.95}

        with patch(
            "app.services.enrichment.enrich_material_card",
            new_callable=AsyncMock,
            return_value=enrichment_result,
        ):
            with patch("app.services.enrichment._apply_enrichment_to_card"):
                result = _run(cross_validate_batch(db))

        assert result["changed_manufacturer"] == 1

    def test_no_enrichment_result(self):
        from app.services.enrichment import cross_validate_batch

        db = _mock_db()

        row = MagicMock()
        row.id = 1
        row.normalized_mpn = "lm358"
        row.manufacturer = "TI"
        row.mt_id = 10
        row.confidence = 0.7
        row.tag_name = "TI"

        db.query.return_value.join.return_value.join.return_value.filter.return_value.order_by.return_value.limit.return_value.all.return_value = [
            row
        ]

        with patch(
            "app.services.enrichment.enrich_material_card",
            new_callable=AsyncMock,
            return_value=None,
        ):
            result = _run(cross_validate_batch(db))

        assert result["no_result"] == 1
