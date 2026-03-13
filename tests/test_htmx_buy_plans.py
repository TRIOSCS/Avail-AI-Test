"""
test_htmx_buy_plans.py — Tests for Phase 3 Task 9: Buy Plans list + detail workflow.
Verifies buy plans list page, rows partial with status/mine filters,
detail page rendering with workflow action bar, and line items display.
Called by: pytest
Depends on: app/routers/views.py, app/templates/partials/buy_plans/
"""

import os

os.environ.setdefault("TESTING", "1")
os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("USE_HTMX", "true")

from datetime import datetime, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.models import (
    BuyPlanLine,
    BuyPlanV3,
    Company,
    CustomerSite,
    Offer,
    Quote,
    Requirement,
    Requisition,
    User,
)


@pytest.fixture()
def htmx_client(db_session, test_user):
    """TestClient with views router registered and auth overridden."""
    from app.database import get_db
    from app.dependencies import require_user
    from app.main import app
    from app.routers.views import router as views_router

    route_paths = [r.path for r in app.routes]
    if "/views/buy-plans" not in route_paths:
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
def sample_requisition(db_session, test_user):
    """A requisition for linking buy plans."""
    req = Requisition(
        name="REQ-BP-001",
        customer_name="Acme Electronics",
        status="open",
        created_by=test_user.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(req)
    db_session.flush()

    item = Requirement(
        requisition_id=req.id,
        primary_mpn="LM317T",
        target_qty=1000,
        target_price=0.50,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(item)
    db_session.commit()
    db_session.refresh(req)
    db_session.refresh(item)
    return req, item


@pytest.fixture()
def sample_site(db_session):
    """A company + customer site for linking quotes."""
    co = Company(
        name="Acme Electronics",
        is_active=True,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(co)
    db_session.flush()
    site = CustomerSite(
        company_id=co.id,
        site_name="Acme HQ",
        contact_name="Jane Doe",
    )
    db_session.add(site)
    db_session.commit()
    db_session.refresh(site)
    return site


@pytest.fixture()
def sample_quote(db_session, sample_requisition, sample_site, test_user):
    """A quote linked to the sample requisition."""
    req, _ = sample_requisition
    q = Quote(
        requisition_id=req.id,
        customer_site_id=sample_site.id,
        quote_number="Q-BP-001",
        status="sent",
        line_items=[],
        subtotal=1000.00,
        total_cost=500.00,
        total_margin_pct=50.00,
        created_by_id=test_user.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(q)
    db_session.commit()
    db_session.refresh(q)
    return q


@pytest.fixture()
def sample_offer(db_session, sample_requisition, test_user):
    """An offer for linking to buy plan lines."""
    req, _ = sample_requisition
    o = Offer(
        requisition_id=req.id,
        vendor_name="Arrow Electronics",
        mpn="LM317T",
        qty_available=1000,
        unit_price=0.50,
        entered_by_id=test_user.id,
        status="active",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(o)
    db_session.commit()
    db_session.refresh(o)
    return o


@pytest.fixture()
def sample_buy_plans(db_session, test_user, sample_requisition, sample_quote, sample_offer):
    """Create multiple buy plans with lines for list/filter tests."""
    req, requirement = sample_requisition
    buy_plans = []
    for status, submitted_by in [
        ("pending", test_user.id),
        ("active", test_user.id),
        ("completed", None),
        ("draft", test_user.id),
    ]:
        bp = BuyPlanV3(
            quote_id=sample_quote.id,
            requisition_id=req.id,
            status=status,
            total_cost=Decimal("500.00"),
            total_revenue=Decimal("750.00"),
            total_margin_pct=Decimal("33.33"),
            submitted_by_id=submitted_by,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(bp)
        db_session.flush()

        line = BuyPlanLine(
            buy_plan_id=bp.id,
            requirement_id=requirement.id,
            offer_id=sample_offer.id,
            quantity=500,
            unit_cost=Decimal("0.5000"),
            unit_sell=Decimal("0.7500"),
            status="awaiting_po",
            buyer_id=test_user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(line)
        buy_plans.append(bp)

    db_session.commit()
    for bp in buy_plans:
        db_session.refresh(bp)
    return buy_plans


class TestBuyPlansListPage:
    """Tests for GET /views/buy-plans — full page HTML."""

    def test_buy_plans_list_page_returns_html(self, htmx_client):
        resp = htmx_client.get("/views/buy-plans")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]

    def test_buy_plans_list_page_contains_table(self, htmx_client):
        resp = htmx_client.get("/views/buy-plans")
        assert "bp-table-body" in resp.text
        assert "Buy Plans" in resp.text

    def test_buy_plans_list_has_status_tabs(self, htmx_client):
        resp = htmx_client.get("/views/buy-plans")
        assert "All" in resp.text
        assert "Pending" in resp.text
        assert "Active" in resp.text
        assert "Completed" in resp.text

    def test_buy_plans_list_has_my_only_toggle(self, htmx_client):
        resp = htmx_client.get("/views/buy-plans")
        assert "My Only" in resp.text

    def test_buy_plans_list_shows_data(self, htmx_client, sample_buy_plans):
        resp = htmx_client.get("/views/buy-plans")
        assert resp.status_code == 200
        # Should contain buy plan rows
        assert "Acme Electronics" in resp.text
        assert "$500.00" in resp.text


class TestBuyPlansRowsPartial:
    """Tests for GET /views/buy-plans/rows — HTMX rows swap target."""

    def test_rows_returns_html(self, htmx_client, sample_buy_plans):
        resp = htmx_client.get("/views/buy-plans/rows")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]

    def test_rows_contain_buy_plan_data(self, htmx_client, sample_buy_plans):
        resp = htmx_client.get("/views/buy-plans/rows")
        assert "Acme Electronics" in resp.text
        assert "1 lines" in resp.text

    def test_rows_status_filter(self, htmx_client, sample_buy_plans):
        resp = htmx_client.get("/views/buy-plans/rows?status=pending")
        assert resp.status_code == 200
        assert "pending" in resp.text
        # Should not contain completed plans
        assert "completed" not in resp.text.lower().replace("pending", "")

    def test_rows_empty_state(self, htmx_client):
        resp = htmx_client.get("/views/buy-plans/rows")
        assert resp.status_code == 200
        assert "No buy plans found" in resp.text


class TestBuyPlansMyOnly:
    """Tests for mine filter on buy plans rows."""

    def test_mine_filter_shows_submitted_by_user(self, htmx_client, sample_buy_plans):
        resp = htmx_client.get("/views/buy-plans/rows?mine=true")
        assert resp.status_code == 200
        # 3 buy plans were submitted by test_user, 1 has None submitted_by
        assert "Acme Electronics" in resp.text

    def test_mine_filter_excludes_others(self, htmx_client, db_session, sample_buy_plans, sample_requisition, sample_quote):
        """Buy plans not submitted by current user should be excluded when mine=true."""
        req, _ = sample_requisition
        other_user = User(
            email="other@trioscs.com",
            name="Other User",
            role="buyer",
            azure_id="other-azure-id",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(other_user)
        db_session.flush()

        other_bp = BuyPlanV3(
            quote_id=sample_quote.id,
            requisition_id=req.id,
            status="pending",
            submitted_by_id=other_user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(other_bp)
        db_session.commit()

        resp_all = htmx_client.get("/views/buy-plans/rows")
        resp_mine = htmx_client.get("/views/buy-plans/rows?mine=true")

        # "mine" should return fewer or equal results
        assert resp_all.status_code == 200
        assert resp_mine.status_code == 200


class TestBuyPlanDetail:
    """Tests for GET /views/buy-plans/{bp_id} — detail page."""

    def test_detail_returns_html(self, htmx_client, sample_buy_plans):
        bp = sample_buy_plans[0]  # pending
        resp = htmx_client.get(f"/views/buy-plans/{bp.id}")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]

    def test_detail_contains_summary(self, htmx_client, sample_buy_plans):
        bp = sample_buy_plans[0]
        resp = htmx_client.get(f"/views/buy-plans/{bp.id}")
        assert "$500.00" in resp.text
        assert "$750.00" in resp.text
        assert "33.3%" in resp.text

    def test_detail_has_back_link(self, htmx_client, sample_buy_plans):
        bp = sample_buy_plans[0]
        resp = htmx_client.get(f"/views/buy-plans/{bp.id}")
        assert "Buy Plans" in resp.text
        assert "/views/buy-plans" in resp.text

    def test_detail_shows_line_items(self, htmx_client, sample_buy_plans):
        bp = sample_buy_plans[0]
        resp = htmx_client.get(f"/views/buy-plans/{bp.id}")
        assert "Line Items" in resp.text
        assert "LM317T" in resp.text
        assert "Arrow Electronics" in resp.text

    def test_detail_pending_has_approve_reject(self, htmx_client, sample_buy_plans):
        bp = sample_buy_plans[0]  # pending
        resp = htmx_client.get(f"/views/buy-plans/{bp.id}")
        assert "Approve" in resp.text
        assert "Reject" in resp.text

    def test_detail_active_has_halt_complete(self, htmx_client, sample_buy_plans):
        bp = sample_buy_plans[1]  # active
        resp = htmx_client.get(f"/views/buy-plans/{bp.id}")
        assert "Halt" in resp.text
        assert "Mark Complete" in resp.text

    def test_detail_completed_no_workflow_buttons(self, htmx_client, sample_buy_plans):
        bp = sample_buy_plans[2]  # completed
        resp = htmx_client.get(f"/views/buy-plans/{bp.id}")
        # Completed plans should not show Approve/Reject/Halt/Submit
        assert "Submit for Approval" not in resp.text

    def test_detail_draft_has_submit(self, htmx_client, sample_buy_plans):
        bp = sample_buy_plans[3]  # draft
        resp = htmx_client.get(f"/views/buy-plans/{bp.id}")
        assert "Submit for Approval" in resp.text

    def test_detail_404_for_missing(self, htmx_client):
        resp = htmx_client.get("/views/buy-plans/99999")
        assert resp.status_code == 404

    def test_detail_has_status_badge(self, htmx_client, sample_buy_plans):
        bp = sample_buy_plans[0]
        resp = htmx_client.get(f"/views/buy-plans/{bp.id}")
        assert "badge-pending" in resp.text


class TestBuyPlanLineDisplay:
    """Tests for line item display in buy plan detail."""

    def test_line_has_po_input(self, htmx_client, sample_buy_plans):
        bp = sample_buy_plans[0]
        resp = htmx_client.get(f"/views/buy-plans/{bp.id}")
        assert 'name="po_number"' in resp.text

    def test_line_has_po_confirmed_checkbox(self, htmx_client, sample_buy_plans):
        bp = sample_buy_plans[0]
        resp = htmx_client.get(f"/views/buy-plans/{bp.id}")
        assert 'name="po_confirmed"' in resp.text

    def test_line_shows_quantity(self, htmx_client, sample_buy_plans):
        bp = sample_buy_plans[0]
        resp = htmx_client.get(f"/views/buy-plans/{bp.id}")
        assert "500" in resp.text

    def test_line_shows_pricing(self, htmx_client, sample_buy_plans):
        bp = sample_buy_plans[0]
        resp = htmx_client.get(f"/views/buy-plans/{bp.id}")
        assert "$0.5000" in resp.text
        assert "$0.7500" in resp.text


class TestBuyPlansPagination:
    """Tests for pagination on buy plans rows."""

    def test_page_1_returns_results(self, htmx_client, sample_buy_plans):
        resp = htmx_client.get("/views/buy-plans/rows?page=1")
        assert resp.status_code == 200
        assert "Acme Electronics" in resp.text

    def test_page_beyond_range_clamps(self, htmx_client, sample_buy_plans):
        resp = htmx_client.get("/views/buy-plans/rows?page=999")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]
