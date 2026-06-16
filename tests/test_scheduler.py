"""test_scheduler.py — Tests for APScheduler configuration and utilities.

Covers: _utc helper, configure_scheduler job registration, _traced_job wrapper,
and scheduler configuration tests (conditional flags, job intervals).

Individual job function tests have been split into domain-specific files:
  - test_jobs_core.py (auto-archive, token refresh, batch results, inbox scan, webhooks)
  - test_jobs_email.py (contacts sync, deep email mining, vendor contacts, outbound RFQs, calendar)
  - test_jobs_enrichment.py (engagement scoring, deep enrichment, customer enrichment)
  - test_jobs_health.py (health ping, health deep, usage log cleanup, monthly reset)
  - test_jobs_inventory.py (PO verification, stock autocomplete, stock list import)
  - test_jobs_maintenance.py (cache cleanup, connector errors, attribution, dedup, integrity)
  - test_jobs_offers.py (proactive matching, performance tracking, offer expiry, stale offers)
  - test_jobs_prospecting.py (pool health, discover, enrich, contacts, scores, expire)
  - test_jobs_tagging.py (material enrichment, nexar validate, connector enrichment)
"""

from datetime import datetime, timedelta, timezone
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
        contacts_sync_enabled=False,
        activity_tracking_enabled=False,
        ownership_sweep_enabled=False,
        proactive_matching_enabled=False,
        proactive_scan_interval_hours=4,
        po_verify_interval_min=30,
        buyplan_auto_complete_hour=18,
        buyplan_auto_complete_tz="America/New_York",
        contact_scoring_enabled=False,
        eight_by_eight_enabled=False,
        prospecting_enabled=False,
        customer_enrichment_enabled=False,
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
    assert result.tzinfo == timezone.utc
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
            ("auto_archive", "token_refresh", "inbox_scan", "batch_results"),
            (),
            id="core_jobs",
        ),
        pytest.param(
            (),
            ("contacts_sync", "proactive_matching", "deep_email_mining", "deep_enrichment"),
            id="conditional_flags_off",
        ),
        pytest.param(("po_verification", "stock_autocomplete"), (), id="buyplan_jobs"),
        pytest.param(("performance_tracking", "cache_cleanup"), (), id="performance_and_cache"),
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
            contacts_sync_enabled=True,
            activity_tracking_enabled=True,
            proactive_matching_enabled=True,
        ),
    ):
        configure_scheduler()

    job_ids = {j.id for j in scheduler.get_jobs()}
    assert "contacts_sync" in job_ids
    assert "proactive_matching" in job_ids


def test_configure_scheduler_activity_tracking_jobs():
    """Activity tracking flag controls webhook_subs; ownership_sweep needs its own
    flag."""
    with patch("app.config.settings", _mock_settings(activity_tracking_enabled=True)):
        configure_scheduler()

    job_ids = {j.id for j in scheduler.get_jobs()}
    assert "webhook_subs" in job_ids
    # ownership_sweep requires OWNERSHIP_SWEEP_ENABLED=true separately
    assert "ownership_sweep" not in job_ids


def test_configure_scheduler_ownership_sweep_enabled():
    """Ownership sweep only runs when both flags are true."""
    with patch("app.config.settings", _mock_settings(activity_tracking_enabled=True, ownership_sweep_enabled=True)):
        configure_scheduler()

    job_ids = {j.id for j in scheduler.get_jobs()}
    assert "ownership_sweep" in job_ids
    assert "site_ownership_sweep" in job_ids


@pytest.mark.parametrize(
    "configured_hours, expected_hours",
    [
        pytest.param(6, 6, id="configurable_interval"),
        pytest.param(0, 1, id="interval_minimum_1h"),  # clamped to at least 1 hour
    ],
)
def test_proactive_matching_interval(configured_hours, expected_hours):
    """Proactive matching interval is configurable, clamped to a 1-hour minimum."""
    with patch(
        "app.config.settings",
        _mock_settings(
            proactive_matching_enabled=True,
            proactive_scan_interval_hours=configured_hours,
        ),
    ):
        configure_scheduler()

    job = scheduler.get_job("proactive_matching")
    assert job is not None
    assert job.trigger.interval.total_seconds() == expected_hours * 3600


def test_reset_connector_errors_registered():
    """configure_scheduler registers the reset_connector_errors job."""
    configure_scheduler()
    job_ids = [j.id for j in scheduler.get_jobs()]
    assert "reset_connector_errors" in job_ids
    scheduler.remove_all_jobs()


def test_ai_tagging_job_registered():
    """AI tagging job is registered (Claude Haiku, replaced nexar_backfill +
    connector_enrichment)."""
    with patch("app.config.settings", _mock_settings()):
        configure_scheduler()

    job = scheduler.get_job("ai_tagging")
    assert job is not None
    # Runs every 4 hours (was 30 minutes, changed during redesign)
    assert job.trigger.interval.total_seconds() == 14400
