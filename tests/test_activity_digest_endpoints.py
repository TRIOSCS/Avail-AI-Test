"""Tests for the activity digest HTMX endpoints.

Covers /v2/partials/requisitions/{req_id}/activity-digest with 'ready' and
'insufficient' states. The service call is patched at the source module so the route is
exercised end-to-end (auth + DB lookup + template rendering) without hitting the AI
layer.
"""

import pytest


def _patch_digest(monkeypatch, payload):
    """Patch get_or_build_digest at its source module to return a fixed payload."""

    async def fake(*a, **k):
        return payload

    monkeypatch.setattr("app.services.activity_digest_service.get_or_build_digest", fake)


@pytest.mark.asyncio
async def test_requisition_digest_endpoint_renders(client, test_requisition, monkeypatch):
    """Ready-state digest renders headline and next_step."""
    _patch_digest(
        monkeypatch,
        {
            "state": "ready",
            "headline": "3 vendors contacted",
            "narrative": "Summary.",
            "highlights": [{"label": "Replies", "value": "2"}],
            "next_step": "Call vendor X",
            "status_signal": "needs_attention",
            "generated_at": None,
        },
    )

    resp = client.get(f"/v2/partials/requisitions/{test_requisition.id}/activity-digest")
    assert resp.status_code == 200
    assert "3 vendors contacted" in resp.text
    assert "Call vendor X" in resp.text


@pytest.mark.asyncio
async def test_digest_endpoint_insufficient_state(client, test_requisition, monkeypatch):
    """Insufficient-state digest shows the 'not enough activity' message."""
    _patch_digest(monkeypatch, {"state": "insufficient"})

    resp = client.get(f"/v2/partials/requisitions/{test_requisition.id}/activity-digest")
    assert resp.status_code == 200
    assert "Not enough activity" in resp.text


@pytest.mark.asyncio
async def test_digest_endpoint_404_for_missing_requisition(client, monkeypatch):
    """Non-existent requisition returns 404 (get_requisition_or_404 guard)."""
    _patch_digest(monkeypatch, {"state": "ready"})  # pragma: no cover

    resp = client.get("/v2/partials/requisitions/999999/activity-digest")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_digest_endpoint_generating_state(client, test_requisition, monkeypatch):
    """Generating state renders a polling placeholder."""
    _patch_digest(monkeypatch, {"state": "generating"})

    resp = client.get(f"/v2/partials/requisitions/{test_requisition.id}/activity-digest")
    assert resp.status_code == 200
    assert "being prepared" in resp.text


@pytest.mark.asyncio
async def test_customer_digest_endpoint_renders(client, test_company, monkeypatch):
    """Ready-state customer digest renders headline and status signal."""
    _patch_digest(
        monkeypatch,
        {
            "state": "ready",
            "headline": "Account summary",
            "narrative": "Recent engagement.",
            "highlights": [],
            "next_step": None,
            "status_signal": "on_track",
            "generated_at": None,
        },
    )

    resp = client.get(f"/v2/partials/customers/{test_company.id}/activity-digest")
    assert resp.status_code == 200
    assert "Account summary" in resp.text


def test_customer_digest_endpoint_404_for_missing_company(client):
    """Non-existent company returns 404."""
    resp = client.get("/v2/partials/customers/999999/activity-digest")
    assert resp.status_code == 404
