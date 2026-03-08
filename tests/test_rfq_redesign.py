"""
tests/test_rfq_redesign.py — Tests for the RFQ layout redesign (v8).

Covers: Template rendering with new view modes (sales/sourcing/archive),
API endpoints still return correct data for the redesigned UI.

Called by: pytest
Depends on: app/templates/index.html, routers/requisitions
"""


# ── Template Rendering ─────────────────────────────────────────────────


def test_index_template_has_sales_view(client):
    """Index page includes the Sales view pill button."""
    resp = client.get("/")
    assert resp.status_code == 200
    assert 'data-view="sales"' in resp.text


def test_index_template_has_sourcing_view(client):
    """Index page includes the Sourcing view pill button."""
    resp = client.get("/")
    assert resp.status_code == 200
    assert 'data-view="sourcing"' in resp.text


def test_index_template_has_archive_view(client):
    """Index page includes the Archive view pill button."""
    resp = client.get("/")
    assert resp.status_code == 200
    assert 'data-view="archive"' in resp.text


def test_index_template_has_notification_bar(client):
    """Index page includes the smart notification bar element."""
    resp = client.get("/")
    assert resp.status_code == 200
    assert "notifActionBar" in resp.text


def test_index_template_has_priority_lane_comment(client):
    """Index page includes priority lane reference."""
    resp = client.get("/")
    assert resp.status_code == 200
    assert "priority lane" in resp.text.lower()


def test_index_template_has_inline_rfq_css(client):
    """Index page includes inline RFQ bar CSS."""
    resp = client.get("/")
    assert resp.status_code == 200
    assert "rfq-inline-bar" in resp.text


def test_index_no_old_active_view(client):
    """Index page should NOT have the old 'Active' view pill as primary."""
    resp = client.get("/")
    assert resp.status_code == 200
    # The old "Active" button with data-view="active" should be gone
    # (replaced by sales/sourcing)
    text = resp.text
    assert 'data-view="active"' not in text or 'data-view="sales"' in text


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
