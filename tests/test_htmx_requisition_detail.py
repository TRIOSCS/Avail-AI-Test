"""
test_htmx_requisition_detail.py — Tests for Phase 3 Task 4: Requisition detail + tabbed drill-down.
Verifies detail page rendering, tab content loading, invalid tab 404, and requirement row partial.
Called by: pytest
Depends on: app/routers/views.py, app/templates/partials/requisitions/
"""

import os

os.environ.setdefault("TESTING", "1")
os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("USE_HTMX", "true")

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.models import ActivityLog, BuyPlan, Offer, Quote, Requirement, Requisition, RequisitionTask


@pytest.fixture()
def htmx_client(db_session, test_user):
    """TestClient with views router registered and auth overridden."""
    from app.database import get_db
    from app.dependencies import require_user
    from app.main import app
    from app.routers.views import router as views_router

    route_paths = [r.path for r in app.routes]
    if "/views/requisitions/{req_id}" not in route_paths:
        app.include_router(views_router)

    def _override_db():
        yield db_session

    def _override_user():
        return test_user

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[require_user] = _override_user

    with TestClient(app) as c:
        yield c

    app.dependency_overrides.clear()


@pytest.fixture()
def sample_req_with_parts(db_session, test_user):
    """A requisition with multiple requirements for detail/tab tests."""
    req = Requisition(
        name="Detail Test Req",
        customer_name="Acme Corp",
        status="open",
        deadline="2026-04-01",
        urgency="hot",
        created_by=test_user.id,
        created_at=datetime(2026, 3, 10, tzinfo=timezone.utc),
    )
    db_session.add(req)
    db_session.flush()

    for i, (mpn, qty, price) in enumerate([
        ("LM317T", 1000, 0.50),
        ("NE555P", 500, 0.25),
        ("LM7805", 200, None),
    ]):
        db_session.add(Requirement(
            requisition_id=req.id,
            primary_mpn=mpn,
            target_qty=qty,
            target_price=price,
            sourcing_status="open",
            created_at=datetime.now(timezone.utc),
        ))

    db_session.commit()
    db_session.refresh(req)
    return req


@pytest.fixture()
def sample_offer(db_session, sample_req_with_parts, test_user):
    """An offer on the sample requisition."""
    offer = Offer(
        requisition_id=sample_req_with_parts.id,
        vendor_name="Arrow Electronics",
        mpn="LM317T",
        qty_available=1000,
        unit_price=0.45,
        entered_by_id=test_user.id,
        status="active",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(offer)
    db_session.commit()
    db_session.refresh(offer)
    return offer


@pytest.fixture()
def sample_activity(db_session, sample_req_with_parts, test_user):
    """An activity log entry on the sample requisition."""
    act = ActivityLog(
        user_id=test_user.id,
        activity_type="rfq_sent",
        channel="email",
        requisition_id=sample_req_with_parts.id,
        subject="RFQ for LM317T",
        contact_name="Vendor Sales",
        contact_email="sales@vendor.com",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(act)
    db_session.commit()
    db_session.refresh(act)
    return act


@pytest.fixture()
def sample_task(db_session, sample_req_with_parts, test_user):
    """A task on the sample requisition."""
    task = RequisitionTask(
        requisition_id=sample_req_with_parts.id,
        title="Follow up with Arrow",
        task_type="sourcing",
        status="todo",
        priority=3,
        created_by=test_user.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(task)
    db_session.commit()
    db_session.refresh(task)
    return task


class TestRequisitionDetailPage:
    """Tests for GET /views/requisitions/{req_id} — detail page."""

    def test_detail_page_renders(self, htmx_client, sample_req_with_parts):
        resp = htmx_client.get(f"/views/requisitions/{sample_req_with_parts.id}")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]

    def test_detail_page_shows_name(self, htmx_client, sample_req_with_parts):
        resp = htmx_client.get(f"/views/requisitions/{sample_req_with_parts.id}")
        assert "Detail Test Req" in resp.text

    def test_detail_page_shows_status_badge(self, htmx_client, sample_req_with_parts):
        resp = htmx_client.get(f"/views/requisitions/{sample_req_with_parts.id}")
        assert "badge-open" in resp.text

    def test_detail_page_shows_urgency(self, htmx_client, sample_req_with_parts):
        resp = htmx_client.get(f"/views/requisitions/{sample_req_with_parts.id}")
        assert "hot" in resp.text

    def test_detail_page_shows_customer(self, htmx_client, sample_req_with_parts):
        resp = htmx_client.get(f"/views/requisitions/{sample_req_with_parts.id}")
        assert "Acme Corp" in resp.text

    def test_detail_page_shows_part_count(self, htmx_client, sample_req_with_parts):
        resp = htmx_client.get(f"/views/requisitions/{sample_req_with_parts.id}")
        assert "3 parts" in resp.text

    def test_detail_page_has_tab_bar(self, htmx_client, sample_req_with_parts):
        resp = htmx_client.get(f"/views/requisitions/{sample_req_with_parts.id}")
        for tab in ["Parts", "Offers", "Quotes", "Buy Plans", "Activity", "Tasks"]:
            assert tab in resp.text

    def test_detail_page_has_breadcrumb(self, htmx_client, sample_req_with_parts):
        resp = htmx_client.get(f"/views/requisitions/{sample_req_with_parts.id}")
        assert "Requisitions" in resp.text
        assert "breadcrumb" in resp.text

    def test_detail_page_has_action_buttons(self, htmx_client, sample_req_with_parts):
        resp = htmx_client.get(f"/views/requisitions/{sample_req_with_parts.id}")
        assert "Archive" in resp.text
        assert "Clone" in resp.text

    def test_detail_page_404_for_missing(self, htmx_client):
        resp = htmx_client.get("/views/requisitions/99999")
        assert resp.status_code == 404

    def test_detail_page_loads_parts_tab_on_init(self, htmx_client, sample_req_with_parts):
        resp = htmx_client.get(f"/views/requisitions/{sample_req_with_parts.id}")
        rid = sample_req_with_parts.id
        assert f"/views/requisitions/{rid}/tab/parts" in resp.text


class TestRequisitionTabs:
    """Tests for GET /views/requisitions/{req_id}/tab/{tab_name}."""

    def test_tab_parts(self, htmx_client, sample_req_with_parts):
        resp = htmx_client.get(f"/views/requisitions/{sample_req_with_parts.id}/tab/parts")
        assert resp.status_code == 200
        assert "LM317T" in resp.text
        assert "NE555P" in resp.text
        assert "LM7805" in resp.text

    def test_tab_parts_shows_qty(self, htmx_client, sample_req_with_parts):
        resp = htmx_client.get(f"/views/requisitions/{sample_req_with_parts.id}/tab/parts")
        assert "1000" in resp.text
        assert "500" in resp.text

    def test_tab_parts_has_add_form(self, htmx_client, sample_req_with_parts):
        resp = htmx_client.get(f"/views/requisitions/{sample_req_with_parts.id}/tab/parts")
        assert "Add Part" in resp.text
        assert 'name="primary_mpn"' in resp.text

    def test_tab_parts_has_delete_buttons(self, htmx_client, sample_req_with_parts):
        resp = htmx_client.get(f"/views/requisitions/{sample_req_with_parts.id}/tab/parts")
        assert "hx-delete" in resp.text
        assert 'hx-confirm="Delete this part?"' in resp.text

    def test_tab_parts_has_source_buttons(self, htmx_client, sample_req_with_parts):
        resp = htmx_client.get(f"/views/requisitions/{sample_req_with_parts.id}/tab/parts")
        assert "Source" in resp.text

    def test_tab_offers_empty(self, htmx_client, sample_req_with_parts):
        resp = htmx_client.get(f"/views/requisitions/{sample_req_with_parts.id}/tab/offers")
        assert resp.status_code == 200
        assert "No offers received" in resp.text

    def test_tab_offers_with_data(self, htmx_client, sample_req_with_parts, sample_offer):
        resp = htmx_client.get(f"/views/requisitions/{sample_req_with_parts.id}/tab/offers")
        assert "Arrow Electronics" in resp.text
        assert "LM317T" in resp.text

    def test_tab_quotes_empty(self, htmx_client, sample_req_with_parts):
        resp = htmx_client.get(f"/views/requisitions/{sample_req_with_parts.id}/tab/quotes")
        assert resp.status_code == 200
        assert "No quotes created" in resp.text

    def test_tab_buy_plans_empty(self, htmx_client, sample_req_with_parts):
        resp = htmx_client.get(f"/views/requisitions/{sample_req_with_parts.id}/tab/buy_plans")
        assert resp.status_code == 200
        assert "No buy plans" in resp.text

    def test_tab_activity_empty(self, htmx_client, sample_req_with_parts):
        resp = htmx_client.get(f"/views/requisitions/{sample_req_with_parts.id}/tab/activity")
        assert resp.status_code == 200
        assert "No activity recorded" in resp.text

    def test_tab_activity_with_data(self, htmx_client, sample_req_with_parts, sample_activity):
        resp = htmx_client.get(f"/views/requisitions/{sample_req_with_parts.id}/tab/activity")
        assert "rfq_sent" in resp.text
        assert "Vendor Sales" in resp.text

    def test_tab_tasks_empty(self, htmx_client, sample_req_with_parts):
        resp = htmx_client.get(f"/views/requisitions/{sample_req_with_parts.id}/tab/tasks")
        assert resp.status_code == 200
        assert "No tasks yet" in resp.text

    def test_tab_tasks_with_data(self, htmx_client, sample_req_with_parts, sample_task):
        resp = htmx_client.get(f"/views/requisitions/{sample_req_with_parts.id}/tab/tasks")
        assert "Follow up with Arrow" in resp.text
        assert "High" in resp.text

    def test_invalid_tab_404(self, htmx_client, sample_req_with_parts):
        resp = htmx_client.get(f"/views/requisitions/{sample_req_with_parts.id}/tab/bogus")
        assert resp.status_code == 404

    def test_tab_on_missing_requisition_404(self, htmx_client):
        resp = htmx_client.get("/views/requisitions/99999/tab/parts")
        assert resp.status_code == 404
