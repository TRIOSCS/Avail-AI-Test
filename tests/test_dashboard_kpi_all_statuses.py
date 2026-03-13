"""Tests verifying dashboard KPIs count all requisition statuses.

KPI queries must count won/lost/active/sourcing reqs equally — completed
deals should never disappear from performance metrics.  Action-item tiles
(reqs_at_risk, quotes_due_soon) should still filter to active work only.

Called by: pytest tests/test_dashboard_kpi_all_statuses.py
Depends on: app/routers/dashboard.py, app/models
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.models import Company, CustomerSite, Offer, Quote, Requisition, User

# ── Helpers ──────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _skip_if_dashboard_router_disabled(client):
    has_route = any(getattr(route, "path", "") == "/api/dashboard/buyer-brief" for route in client.app.routes)
    if not has_route:
        pytest.skip("Dashboard router disabled in MVP mode")


def _make_user(db, name="KPI User"):
    u = User(name=name, email=f"{name.lower().replace(' ', '.')}@test.com")
    db.add(u)
    db.flush()
    return u


def _make_req(db, user, name="REQ-1", status="active", days_ago=0):
    r = Requisition(
        name=name,
        status=status,
        created_by=user.id,
        created_at=datetime.now(timezone.utc) - timedelta(days=days_ago),
    )
    db.add(r)
    db.flush()
    return r


def _make_offer(db, req, user, mpn="LM317T"):
    o = Offer(
        requisition_id=req.id,
        vendor_name="Arrow",
        mpn=mpn,
        qty_available=100,
        unit_price=1.50,
        entered_by_id=user.id,
        status="active",
        created_at=datetime.now(timezone.utc),
    )
    db.add(o)
    db.flush()
    return o


def _make_company_and_site(db):
    co = Company(name="KPI Co", is_active=True, created_at=datetime.now(timezone.utc))
    db.add(co)
    db.flush()
    site = CustomerSite(company_id=co.id, site_name="HQ", created_at=datetime.now(timezone.utc))
    db.add(site)
    db.flush()
    return co, site


def _make_quote(db, req, site, user, result=None, number="Q-001"):
    q = Quote(
        requisition_id=req.id,
        customer_site_id=site.id,
        quote_number=number,
        line_items=[],
        subtotal=1000.0,
        total_cost=750.0,
        status="sent",
        created_by_id=user.id,
        created_at=datetime.now(timezone.utc),
    )
    if result:
        q.result = result
        q.result_at = datetime.now(timezone.utc)
    db.add(q)
    db.flush()
    return q


# ── KPI: Sourcing Ratio includes won reqs ─────────────────────────────


class TestSourcingRatioAllStatuses:
    def test_sourcing_ratio_counts_won_reqs(self, client, db_session, test_user):
        """A won req with offers should count toward sourcing ratio."""
        won_req = _make_req(db_session, test_user, name="WON-SOURCED", status="won")
        _make_offer(db_session, won_req, test_user, mpn="WON-PART-1")
        _make_offer(db_session, won_req, test_user, mpn="WON-PART-2")
        _make_offer(db_session, won_req, test_user, mpn="WON-PART-3")
        db_session.commit()

        resp = client.get("/api/dashboard/buyer-brief")
        assert resp.status_code == 200
        data = resp.json()
        kpis = data["kpis"]

        # Won req should be counted in total_reqs and sourced_reqs
        assert kpis["total_reqs"] >= 1
        assert kpis["sourced_reqs"] >= 1
        assert kpis["sourcing_ratio"] > 0

    def test_sourcing_ratio_counts_lost_reqs(self, client, db_session, test_user):
        """A lost req with offers should count toward sourcing ratio."""
        lost_req = _make_req(db_session, test_user, name="LOST-SOURCED", status="lost")
        _make_offer(db_session, lost_req, test_user, mpn="LOST-PART-1")
        db_session.commit()

        resp = client.get("/api/dashboard/buyer-brief")
        assert resp.status_code == 200
        data = resp.json()
        kpis = data["kpis"]

        assert kpis["total_reqs"] >= 1
        assert kpis["sourced_reqs"] >= 1


# ── KPI: total_reqs includes all statuses ──────────────────────────────


class TestTotalReqsAllStatuses:
    def test_total_reqs_includes_all_statuses(self, client, db_session, test_user):
        """total_reqs should count won + lost + active + sourcing reqs."""
        _make_req(db_session, test_user, name="ACTIVE-R", status="active")
        _make_req(db_session, test_user, name="WON-R", status="won")
        _make_req(db_session, test_user, name="LOST-R", status="lost")
        _make_req(db_session, test_user, name="SOURCING-R", status="sourcing")
        db_session.commit()

        resp = client.get("/api/dashboard/buyer-brief")
        assert resp.status_code == 200
        data = resp.json()
        # All 4 reqs should be counted
        assert data["kpis"]["total_reqs"] >= 4


# ── KPI: Offers counted regardless of req status ──────────────────────


class TestOffersAllStatuses:
    def test_offers_counted_regardless_of_req_status(self, client, db_session, test_user):
        """Offer→Quote KPI should count offers on won/lost reqs too."""
        won_req = _make_req(db_session, test_user, name="WON-OFFERS", status="won")
        _make_offer(db_session, won_req, test_user, mpn="WON-O-1")
        _make_offer(db_session, won_req, test_user, mpn="WON-O-2")

        lost_req = _make_req(db_session, test_user, name="LOST-OFFERS", status="lost")
        _make_offer(db_session, lost_req, test_user, mpn="LOST-O-1")
        db_session.commit()

        resp = client.get("/api/dashboard/buyer-brief")
        assert resp.status_code == 200
        data = resp.json()
        # All 3 offers should be in total_offers
        assert data["kpis"]["total_offers"] >= 3


# ── KPI: BuyPlans counted regardless of req status ────────────────────


class TestBuyPlansAllStatuses:
    def test_buyplans_count_includes_won_reqs(self, client, db_session, test_user):
        """Buy plan KPI should count plans on won reqs."""
        from app.models.quotes import BuyPlan

        co, site = _make_company_and_site(db_session)
        won_req = _make_req(db_session, test_user, name="WON-BP", status="won")
        quote = _make_quote(db_session, won_req, site, test_user, result="won", number="Q-BP")
        bp = BuyPlan(
            requisition_id=won_req.id,
            quote_id=quote.id,
            submitted_by_id=test_user.id,
            submitted_at=datetime.now(timezone.utc),
            status="approved",
            line_items=[],
        )
        db_session.add(bp)
        db_session.commit()

        resp = client.get("/api/dashboard/buyer-brief")
        assert resp.status_code == 200
        data = resp.json()
        assert data["kpis"]["total_buyplans"] >= 1
        assert data["kpis"]["buyplan_po_rate"] >= 0


# ── Action tiles: reqs_at_risk ONLY shows active reqs ─────────────────


class TestActionTilesStillFiltered:
    def test_reqs_at_risk_excludes_won_reqs(self, client, db_session, test_user):
        """Won reqs with 0 offers should NOT appear in reqs_at_risk."""
        won_req = _make_req(db_session, test_user, name="WON-NORISK", status="won", days_ago=3)
        # Active req with 0 offers SHOULD appear
        active_req = _make_req(db_session, test_user, name="ACTIVE-ATRISK", status="active", days_ago=3)
        db_session.commit()

        resp = client.get("/api/dashboard/buyer-brief")
        assert resp.status_code == 200
        data = resp.json()
        risk_names = [r["name"] for r in data["reqs_at_risk"]]
        assert "WON-NORISK" not in risk_names
        assert "ACTIVE-ATRISK" in risk_names

    def test_quotes_due_soon_excludes_won_reqs(self, client, db_session, test_user):
        """Won reqs with deadlines should NOT appear in quotes_due_soon."""

        tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).date().isoformat()
        won_req = _make_req(db_session, test_user, name="WON-DL", status="won")
        won_req.deadline = tomorrow

        active_req = _make_req(db_session, test_user, name="ACTIVE-DL", status="active")
        active_req.deadline = tomorrow
        db_session.commit()

        resp = client.get("/api/dashboard/buyer-brief")
        assert resp.status_code == 200
        data = resp.json()
        due_names = [r["name"] for r in data["quotes_due_soon"]]
        assert "WON-DL" not in due_names
        assert "ACTIVE-DL" in due_names
