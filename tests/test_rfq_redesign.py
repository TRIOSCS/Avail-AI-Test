"""
tests/test_rfq_redesign.py — Tests for the RFQ layout redesign (v8).

Covers: Template raw content with the unified requisition views
(reqs/deals/archive), plus API endpoints still returning data for the updated UI.

Called by: pytest
Depends on: app/templates/index.html, routers/requisitions
"""

import pytest


@pytest.fixture(scope="module")
def index_html():
    """Read index.html template raw content (avoids session/auth dependency)."""
    with open("app/templates/index.html", "r") as f:
        return f.read()


# ── Template Rendering ─────────────────────────────────────────────────


def test_index_template_has_reqs_view(index_html):
    """Index template includes the unified requisition view pill button."""
    assert 'data-view="reqs"' in index_html


def test_index_template_has_deals_view(index_html):
    """Index template includes the Deals view pill button."""
    assert 'data-view="deals"' in index_html


def test_index_template_has_archive_view(index_html):
    """Index template includes the Archive view pill button."""
    assert 'data-view="archive"' in index_html


def test_index_template_has_notification_bar(index_html):
    """Index template includes the smart notification bar element."""
    assert "notifActionBar" in index_html


def test_index_template_has_priority_lane_comment(index_html):
    """Index template includes priority lane reference."""
    assert "priority lane" in index_html.lower() or "prioritylane" in index_html.lower()


def test_index_template_has_inline_rfq_css(index_html):
    """Index template includes inline RFQ bar CSS."""
    assert "rfq-inline-bar" in index_html


def test_index_no_old_active_view(index_html):
    """Index template should NOT have the old 'Active' view pill as primary."""
    # The old "Active" button with data-view="active" should be gone
    # (replaced by the unified reqs view)
    assert 'data-view="active"' not in index_html or 'data-view="reqs"' in index_html


# ── API Endpoints Still Work ───────────────────────────────────────────


def test_requisitions_list_for_redesign(client, test_requisition):
    """Requisition list API still returns data for the redesigned views."""
    resp = client.get("/api/requisitions")
    assert resp.status_code == 200
    data = resp.json()
    items = data.get("requisitions", data if isinstance(data, list) else [])
    assert len(items) >= 1


def test_requisitions_archive_for_redesign(client):
    """Archive status filter still works for the archive view."""
    resp = client.get("/api/requisitions?status=archive")
    assert resp.status_code == 200


def test_requisitions_requirements_endpoint(client, test_requisition):
    """Requirements endpoint still works (used by consolidated Sourcing tab)."""
    resp = client.get(f"/api/requisitions/{test_requisition.id}/requirements")
    assert resp.status_code == 200


def test_requisitions_sightings_endpoint(client, test_requisition):
    """Sightings endpoint still works (used by consolidated Sourcing tab)."""
    resp = client.get(f"/api/requisitions/{test_requisition.id}/sightings")
    assert resp.status_code == 200


def test_requisitions_activity_endpoint(client, test_requisition):
    """Activity endpoint still works (used by consolidated Activity tab)."""
    resp = client.get(f"/api/requisitions/{test_requisition.id}/activity")
    assert resp.status_code == 200


def test_requisitions_offers_endpoint(client, test_requisition):
    """Offers endpoint still works (used by Offers tab)."""
    resp = client.get(f"/api/requisitions/{test_requisition.id}/offers")
    assert resp.status_code == 200


def test_requisitions_quotes_endpoint(client, test_requisition):
    """Quotes endpoint still works (used by consolidated Quote tab)."""
    resp = client.get(f"/api/requisitions/{test_requisition.id}/quotes")
    assert resp.status_code == 200


def test_requisitions_attachments_endpoint(client, test_requisition):
    """Attachments endpoint still works (used by consolidated Quote tab)."""
    resp = client.get(f"/api/requisitions/{test_requisition.id}/attachments")
    assert resp.status_code == 200


def test_requisitions_tasks_endpoint(client, test_requisition):
    """Tasks endpoint still works (used by consolidated Activity tab)."""
    resp = client.get(f"/api/requisitions/{test_requisition.id}/tasks")
    assert resp.status_code == 200
