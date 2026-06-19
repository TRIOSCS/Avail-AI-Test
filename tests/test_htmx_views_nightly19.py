"""tests/test_htmx_views_nightly19.py — Coverage for dashboard, insights, and buy plan
routes.

Targets:
  - dashboard_partial
  - requisition/vendor/company/pipeline insights (GET + refresh)
  - buy_plans_list_partial
  - buy_plan_detail_partial

Called by: pytest autodiscovery
Depends on: conftest.py fixtures, app.routers.htmx_views
"""

import os

os.environ["TESTING"] = "1"

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import Requisition, VendorCard
from app.models.buy_plan import BuyPlan

# ── Helpers ───────────────────────────────────────────────────────────────


def _make_company(db: Session, name: str):
    from app.models import Company

    company = Company(name=name, is_active=True)
    db.add(company)
    db.commit()
    return company


def _make_buy_plan(db: Session, req: Requisition, **kw) -> BuyPlan:
    import uuid

    from app.constants import BuyPlanStatus, SOVerificationStatus
    from app.models.quotes import Quote

    quote = Quote(
        requisition_id=req.id,
        quote_number=f"Q-{uuid.uuid4().hex[:8]}",
        status="draft",
    )
    db.add(quote)
    db.flush()

    defaults = dict(
        quote_id=quote.id,
        requisition_id=req.id,
        status=BuyPlanStatus.DRAFT,
        so_status=SOVerificationStatus.PENDING,
    )
    defaults.update(kw)
    bp = BuyPlan(**defaults)
    db.add(bp)
    db.commit()
    db.refresh(bp)
    return bp


# ── Dashboard ─────────────────────────────────────────────────────────────


class TestDashboardPartial:
    def test_dashboard_loads(self, client: TestClient):
        resp = client.get("/v2/partials/dashboard")
        assert resp.status_code == 200


# ── Insights ──────────────────────────────────────────────────────────────


class TestInsights:
    def test_requisition_insights_panel(self, client: TestClient, test_requisition: Requisition):
        resp = client.get(f"/v2/partials/requisitions/{test_requisition.id}/insights")
        assert resp.status_code == 200

    def test_requisition_insights_refresh(self, client: TestClient, test_requisition: Requisition):
        resp = client.post(f"/v2/partials/requisitions/{test_requisition.id}/insights/refresh")
        assert resp.status_code == 200

    def test_vendor_insights_panel(self, client: TestClient, test_vendor_card: VendorCard):
        resp = client.get(f"/v2/partials/vendors/{test_vendor_card.id}/insights")
        assert resp.status_code == 200

    def test_vendor_insights_refresh(self, client: TestClient, test_vendor_card: VendorCard):
        resp = client.post(f"/v2/partials/vendors/{test_vendor_card.id}/insights/refresh")
        assert resp.status_code == 200

    def test_company_insights_panel(self, client: TestClient, db_session: Session):
        company = _make_company(db_session, "InsightCo")
        resp = client.get(f"/v2/partials/customers/{company.id}/insights")
        assert resp.status_code == 200

    def test_company_insights_refresh(self, client: TestClient, db_session: Session):
        company = _make_company(db_session, "InsightCo2")
        resp = client.post(f"/v2/partials/customers/{company.id}/insights/refresh")
        assert resp.status_code == 200

    def test_pipeline_insights_panel(self, client: TestClient):
        resp = client.get("/v2/partials/dashboard/pipeline-insights")
        assert resp.status_code == 200

    def test_pipeline_insights_refresh(self, client: TestClient):
        resp = client.post("/v2/partials/dashboard/pipeline-insights/refresh")
        assert resp.status_code == 200


# ── Buy Plans List ────────────────────────────────────────────────────────


class TestBuyPlansListPartial:
    @pytest.mark.parametrize(
        "url",
        [
            # No-param → role-derived default lens; explicit lenses per the real contract.
            pytest.param("/v2/partials/buy-plans", id="default_lens"),
            pytest.param("/v2/partials/buy-plans?lens=deals", id="lens_deals"),
            pytest.param("/v2/partials/buy-plans?lens=orders", id="lens_orders"),
            pytest.param("/v2/partials/buy-plans?lens=supervise", id="lens_supervise"),
        ],
    )
    def test_list_loads(self, client: TestClient, url: str):
        """The hub shell renders: lens switcher + lazy #bp-hub-body with its hx-target."""
        resp = client.get(url)
        assert resp.status_code == 200
        body = resp.text
        # Lens switcher present (one button per lens).
        assert "My Deals" in body
        assert "My Orders" in body
        # Lazy body container carries its own explicit hx-target (cards-vanish guard).
        assert 'id="bp-hub-body"' in body
        assert 'hx-target="#bp-hub-body"' in body

    def test_list_with_plan(self, client: TestClient, db_session: Session, test_requisition: Requisition):
        _make_buy_plan(db_session, test_requisition)
        resp = client.get("/v2/partials/buy-plans")
        assert resp.status_code == 200
