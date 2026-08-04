"""test_scheduler.py — Tests for APScheduler configuration and utilities.

Covers: _utc helper, configure_scheduler job registration, _traced_job wrapper,
and scheduler configuration tests (conditional flags, job intervals).

Individual job function tests have been split into domain-specific files:
  - test_jobs_core.py (token refresh, batch results, inbox scan, webhooks)
  - test_jobs_email.py (contacts sync, deep email mining, vendor contacts, outbound RFQs, calendar)
  - test_jobs_enrichment.py (engagement scoring, deep enrichment, customer enrichment)
  - test_jobs_inventory.py (stock list import helpers)
  - test_jobs_offers.py (proactive matching)
(The health/maintenance/tagging/prospecting job test files went with their jobs
in W1 — docs/W1_JOB_DISPOSITION.md.)
"""

from datetime import UTC, datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from app.scheduler import _utc, configure_scheduler, scheduler

# ── Fixtures ───────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _clear_scheduler_jobs():
    """Remove all jobs before/after each test to prevent leakage."""
    for job in scheduler.get_jobs():
        job.remove()
    yield
    for job in scheduler.get_jobs():
        job.remove()


# ── Helpers ───────────────────────────────────────────────────────────


def _mock_settings(**overrides):
    """Build a mock settings object with defaults for scheduler tests."""
    defaults = dict(
        inbox_scan_interval_min=30,
        activity_tracking_enabled=False,
        ownership_sweep_enabled=False,
        proactive_matching_enabled=False,
        proactive_scan_interval_hours=4,
        eight_by_eight_enabled=False,
        prospecting_enabled=False,
        customer_enrichment_enabled=False,
        worker_liveness_check_minutes=5,
        worker_heartbeat_stale_minutes=15,
        worker_alert_debounce_minutes=60,
    )
    defaults.update(overrides)
    mock = MagicMock()
    for k, v in defaults.items():
        setattr(mock, k, v)
    return mock


# ── _utc() ─────────────────────────────────────────────────────────────


def test_utc_naive_becomes_utc():
    naive = datetime(2026, 1, 15, 12, 0, 0)
    result = _utc(naive)
    assert result.tzinfo == UTC
    assert result.year == 2026


def test_utc_aware_passthrough():
    tz5 = timezone(timedelta(hours=5))
    aware = datetime(2026, 1, 15, 12, 0, 0, tzinfo=tz5)
    result = _utc(aware)
    assert result.tzinfo == tz5  # unchanged


def test_utc_none_returns_none():
    assert _utc(None) is None


# ── configure_scheduler() ──────────────────────────────────────────────


@pytest.mark.parametrize(
    "expected_present, expected_absent",
    [
        pytest.param(
            ("token_refresh", "inbox_scan", "batch_results"),
            (),
            id="core_jobs",
        ),
        pytest.param(
            (),
            ("contacts_sync", "proactive_matching", "deep_email_mining", "deep_enrichment"),
            id="conditional_flags_off",
        ),
        # W1 (docs/W1_JOB_DISPOSITION.md): po_verification + stock_autocomplete
        # deleted, buyplan_nudge parked — inventory registers nothing.
        pytest.param((), ("po_verification", "stock_autocomplete", "buyplan_nudge"), id="buyplan_jobs"),
        # performance_tracking is parked (Wave 1) — registration removed;
        # cache_cleanup deleted in W1.
        pytest.param((), ("performance_tracking", "cache_cleanup"), id="performance_and_cache"),
    ],
)
def test_configure_scheduler_default_settings_jobs(expected_present, expected_absent):
    """With default settings: core/always-on jobs registered, optional jobs absent."""
    with patch("app.config.settings", _mock_settings()):
        configure_scheduler()

    job_ids = {j.id for j in scheduler.get_jobs()}
    for job_id in expected_present:
        assert job_id in job_ids, f"Missing job: {job_id}"
    for job_id in expected_absent:
        assert job_id not in job_ids, f"Unexpected job: {job_id}"


def test_configure_scheduler_conditional_flags_on():
    """When conditional flags are on, optional jobs are registered."""
    with patch(
        "app.config.settings",
        _mock_settings(
            activity_tracking_enabled=True,
            proactive_matching_enabled=True,
        ),
    ):
        configure_scheduler()

    job_ids = {j.id for j in scheduler.get_jobs()}
    # contacts_sync is deleted (W1, docs/W1_JOB_DISPOSITION.md — its
    # contacts_sync_enabled flag went with it): always absent.
    assert "contacts_sync" not in job_ids
    # calendar_scan is parked (W1): registration removed because
    # activity_tracking_enabled defaults ON, so it stays absent with the flag set.
    assert "calendar_scan" not in job_ids
    # proactive_matching is parked (Wave 1): its registration was removed because
    # the flag defaulted ON, so it stays absent even with the flag set.
    assert "proactive_matching" not in job_ids


def test_configure_scheduler_activity_tracking_jobs():
    """webhook_subs is deleted (W1, spec §3 kernel-only) — absent even with activity
    tracking on; ownership_sweep needs its own flag."""
    with patch("app.config.settings", _mock_settings(activity_tracking_enabled=True)):
        configure_scheduler()

    job_ids = {j.id for j in scheduler.get_jobs()}
    assert "webhook_subs" not in job_ids
    # ownership_sweep requires OWNERSHIP_SWEEP_ENABLED=true separately
    assert "ownership_sweep" not in job_ids


def test_configure_scheduler_ownership_sweep_enabled():
    """Ownership sweep only runs when both flags are true."""
    with patch("app.config.settings", _mock_settings(activity_tracking_enabled=True, ownership_sweep_enabled=True)):
        configure_scheduler()

    job_ids = {j.id for j in scheduler.get_jobs()}
    assert "ownership_sweep" in job_ids
    assert "site_ownership_sweep" in job_ids


def test_reset_connector_errors_not_registered():
    """reset_connector_errors was deleted in W1 (docs/W1_JOB_DISPOSITION.md) and must
    never re-appear in the scheduler."""
    configure_scheduler()
    job_ids = [j.id for j in scheduler.get_jobs()]
    assert "reset_connector_errors" not in job_ids
    scheduler.remove_all_jobs()


def test_ai_tagging_job_not_registered():
    """ai_tagging was deleted in W1 (docs/W1_JOB_DISPOSITION.md — on-demand management
    command only) and must never re-appear in the scheduler."""
    with patch("app.config.settings", _mock_settings()):
        configure_scheduler()

    job_ids = [j.id for j in scheduler.get_jobs()]
    assert "ai_tagging" not in job_ids
    scheduler.remove_all_jobs()
