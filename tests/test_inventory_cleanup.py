# tests/test_inventory_cleanup.py — Regression tests for the 2026-07-05 unfinished-work
# inventory cleanup.
#
# Covers the two real bugs (quarterly email re-verification kwarg crash; dashboard
# "Refresh insights" 404) and the conservative orphaned-endpoint deletions
# (proactive_send_legacy, admin_data_ops, rfq_prepare_panel).
#
# Called by: pytest.
# Depends on: app.main.app (route registry) + the TestClient/db fixtures in conftest.

import inspect
from unittest.mock import MagicMock, patch

import pytest

from app.main import app
from tests._route_helpers import iter_routes


def _paths(method: str | None = None) -> set[str]:
    """Set of registered route paths, optionally filtered to a single HTTP method."""
    out: set[str] = set()
    for route in iter_routes(app.routes):
        methods = getattr(route, "methods", None) or set()
        if method is None or method in methods:
            out.add(getattr(route, "path", None))
    return out


# ── BUG 1: quarterly email re-verification kwarg crash ──────────────────────


def test_run_email_reverification_rejects_legacy_kwarg():
    """The job used to pass max_contacts=200; the real param is _max_contacts.

    Assert the legacy kwarg does NOT bind (would raise TypeError at call time — the
    crash) while the fixed call site (db only, default batch size) binds cleanly.
    """
    from app.services.customer_enrichment_batch import run_email_reverification

    sig = inspect.signature(run_email_reverification)
    sentinel = MagicMock()
    with pytest.raises(TypeError):
        sig.bind(sentinel, max_contacts=200)
    sig.bind(sentinel)  # the fixed call binds fine


@pytest.mark.asyncio
async def test_email_reverification_job_runs_without_crashing():
    """_job_email_reverification must complete against the REAL stub (no kwarg
    TypeError).

    Regression for inventory Bug 1: the old call raised
    'unexpected keyword argument max_contacts' on every quarterly run and re-raised it
    to Sentry. run_email_reverification is left unpatched here so a bad kwarg would surface.
    """
    from app.jobs.email_jobs import _job_email_reverification

    with patch("app.database.SessionLocal") as mock_sessionlocal:
        mock_sessionlocal.return_value = MagicMock()
        await _job_email_reverification()


def test_email_reverification_job_still_scheduled_when_enrichment_enabled():
    """The kwarg fix (not an unschedule) keeps the job registered so it can no-op
    cleanly."""
    from types import SimpleNamespace

    from app.jobs.email_jobs import register_email_jobs

    scheduler = MagicMock()
    settings = SimpleNamespace(
        contacts_sync_enabled=False,
        ownership_sweep_enabled=False,
        contact_scoring_enabled=False,
        activity_tracking_enabled=False,
        customer_enrichment_enabled=True,
    )
    register_email_jobs(scheduler, settings)
    job_ids = [call.kwargs.get("id") for call in scheduler.add_job.call_args_list]
    assert "email_reverification" in job_ids


# ── BUG 2: dashboard "Refresh insights" 404 ─────────────────────────────────


def test_dashboard_pipeline_insights_refresh_route_registered():
    assert "/v2/partials/dashboard/pipeline-insights/refresh" in _paths("POST")
    # The buggy URL the panel used to emit is NOT a registered route.
    assert "/v2/partials/dashboard/0/insights/refresh" not in _paths("POST")


def test_dashboard_insights_panel_posts_registered_refresh_route(client):
    resp = client.get("/v2/partials/dashboard/pipeline-insights")
    assert resp.status_code == 200
    assert "/v2/partials/dashboard/pipeline-insights/refresh" in resp.text
    assert "/v2/partials/dashboard/0/insights/refresh" not in resp.text


def test_requisition_insights_panel_still_posts_pattern_route(client, test_requisition):
    """Non-dashboard callers of insights_panel.html keep the {entity}/{id} pattern
    URL."""
    rid = test_requisition.id
    resp = client.get(f"/v2/partials/requisitions/{rid}/insights")
    assert resp.status_code == 200
    assert f"/v2/partials/requisitions/{rid}/insights/refresh" in resp.text


# ── Orphaned-endpoint deletions ─────────────────────────────────────────────


def test_proactive_send_legacy_route_removed():
    assert "/v2/partials/proactive/{match_id}/send" not in _paths()


def test_admin_data_ops_route_removed():
    assert "/v2/partials/admin/data-ops" not in _paths()


def test_rfq_prepare_panel_route_removed():
    assert "/v2/partials/requisitions/{req_id}/rfq-prepare" not in _paths()


def test_superseding_routes_still_present():
    """The kept twins that supersede the deleted routes must remain registered."""
    all_paths = _paths()
    assert "/v2/proactive/send" in all_paths
    assert "/v2/partials/settings/data-ops" in all_paths
    assert "/v2/partials/requisitions/{req_id}/rfq-compose" in all_paths
