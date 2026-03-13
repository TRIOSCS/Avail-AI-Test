"""test_scheduler.py — Tests for APScheduler configuration and utilities

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
        deep_email_mining_enabled=False,
        deep_enrichment_enabled=False,
        po_verify_interval_min=30,
        buyplan_auto_complete_hour=18,
        buyplan_auto_complete_tz="America/New_York",
        contact_scoring_enabled=False,
        eight_by_eight_enabled=False,
        prospecting_enabled=False,
        customer_enrichment_enabled=False,
        material_enrichment_enabled=False,
        mvp_mode=False,
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


def test_configure_scheduler_registers_core_jobs():
    """Core jobs (auto_archive, token_refresh, etc.) always registered."""
    with patch("app.config.settings", _mock_settings()):
        configure_scheduler()

    job_ids = {j.id for j in scheduler.get_jobs()}
    for core_id in ("auto_archive", "token_refresh", "inbox_scan", "batch_results", "engagement_scoring"):
        assert core_id in job_ids, f"Missing core job: {core_id}"


def test_configure_scheduler_conditional_flags_off():
    """When conditional flags are off, optional jobs are not registered."""
    with patch("app.config.settings", _mock_settings()):
        configure_scheduler()

    job_ids = {j.id for j in scheduler.get_jobs()}
    assert "contacts_sync" not in job_ids
    assert "proactive_matching" not in job_ids
    assert "deep_email_mining" not in job_ids
    assert "deep_enrichment" not in job_ids


def test_configure_scheduler_conditional_flags_on():
    """When conditional flags are on, optional jobs are registered."""
    with patch(
        "app.config.settings",
        _mock_settings(
            contacts_sync_enabled=True,
            activity_tracking_enabled=True,
            proactive_matching_enabled=True,
            deep_email_mining_enabled=True,
            deep_enrichment_enabled=True,
        ),
    ):
        configure_scheduler()

    job_ids = {j.id for j in scheduler.get_jobs()}
    assert "contacts_sync" in job_ids
    assert "proactive_matching" in job_ids
    assert "deep_email_mining" in job_ids
    assert "deep_enrichment" in job_ids


def test_configure_scheduler_activity_tracking_jobs():
    """Activity tracking flag controls webhook_subs; ownership_sweep needs its own flag."""
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


def test_configure_scheduler_always_includes_buyplan_jobs():
    """PO verification and stock auto-complete are always registered."""
    with patch("app.config.settings", _mock_settings()):
        configure_scheduler()

    job_ids = {j.id for j in scheduler.get_jobs()}
    assert "po_verification" in job_ids
    assert "stock_autocomplete" in job_ids


def test_configure_scheduler_always_includes_performance_and_cache():
    """Performance tracking and cache cleanup are always registered."""
    with patch("app.config.settings", _mock_settings()):
        configure_scheduler()

    job_ids = {j.id for j in scheduler.get_jobs()}
    assert "performance_tracking" in job_ids
    assert "cache_cleanup" in job_ids


def test_proactive_matching_configurable_interval():
    """Proactive matching interval is configurable via proactive_scan_interval_hours."""
    with patch(
        "app.config.settings",
        _mock_settings(
            proactive_matching_enabled=True,
            proactive_scan_interval_hours=6,
        ),
    ):
        configure_scheduler()

    job = scheduler.get_job("proactive_matching")
    assert job is not None
    trigger = job.trigger
    assert trigger.interval.total_seconds() == 6 * 3600


def test_proactive_matching_interval_minimum_1h():
    """Interval is clamped to at least 1 hour."""
    with patch(
        "app.config.settings",
        _mock_settings(
            proactive_matching_enabled=True,
            proactive_scan_interval_hours=0,
        ),
    ):
        configure_scheduler()

    job = scheduler.get_job("proactive_matching")
    assert job is not None
    trigger = job.trigger
    assert trigger.interval.total_seconds() == 1 * 3600


def test_reset_connector_errors_registered():
    """configure_scheduler registers the reset_connector_errors job."""
    configure_scheduler()
    job_ids = [j.id for j in scheduler.get_jobs()]
    assert "reset_connector_errors" in job_ids
    scheduler.remove_all_jobs()


def test_gradient_ai_tagging_job_registered():
    """Gradient AI tagging job is registered (replaced nexar_backfill + connector_enrichment)."""
    with patch("app.config.settings", _mock_settings()):
        configure_scheduler()

    job = scheduler.get_job("gradient_ai_tagging")
    assert job is not None
    # Runs every 30 minutes
    assert job.trigger.interval.total_seconds() == 1800
