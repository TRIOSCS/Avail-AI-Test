"""Tests for GET /api/dashboard/buyer-brief and _age_label helper."""

from datetime import datetime, timedelta, timezone

import pytest

from app.models import (
    Contact,
    Offer,
    Quote,
    Requisition,
)


class TestBuyerBrief:
    """Tests for the /api/dashboard/buyer-brief endpoint."""

    @pytest.fixture(autouse=True)
    def _skip_if_dashboard_router_disabled(self, client):
        has_route = any(getattr(route, "path", "") == "/api/dashboard/buyer-brief" for route in client.app.routes)
        if not has_route:
            pytest.skip("Dashboard router disabled in MVP mode")

    def _make_req(self, db, user, name="REQ-1", status="active", deadline=None, days_ago=0):
        r = Requisition(
            name=name,
            status=status,
            created_by=user.id,
            deadline=deadline,
            created_at=datetime.now(timezone.utc) - timedelta(days=days_ago),
        )
        db.add(r)
        db.flush()
        return r

    def _make_offer(self, db, req, user, status="active", days_ago=0, attribution="active"):
        o = Offer(
            requisition_id=req.id,
            vendor_name="Arrow",
            mpn="LM317T",
            qty_available=100,
            unit_price=1.50,
            entered_by_id=user.id,
            status=status,
            attribution_status=attribution,
            created_at=datetime.now(timezone.utc) - timedelta(days=days_ago),
        )
        db.add(o)
        db.flush()
        return o

    def _make_contact(self, db, req, user, status="sent", days_ago=0):
        c = Contact(
            requisition_id=req.id,
            user_id=user.id,
            contact_type="RFQ",
            vendor_name="Mouser",
            status=status,
            created_at=datetime.now(timezone.utc) - timedelta(days=days_ago),
        )
        db.add(c)
        db.flush()
        return c

    # 1. Empty DB returns zeros (incl team_kpis)
    def test_empty_db(self, client):
        resp = client.get("/api/dashboard/buyer-brief")
        assert resp.status_code == 200
        data = resp.json()
        assert data["kpis"]["sourcing_ratio"] == 0
        assert data["kpis"]["offer_quote_rate"] == 0
        assert data["kpis"]["quote_win_rate"] == 0
        assert data["kpis"]["buyplan_po_rate"] == 0
        # team_kpis present with all 4 rate fields
        tk = data["team_kpis"]
        assert tk["sourcing_ratio"] == 0
        assert tk["offer_quote_rate"] == 0
        assert tk["quote_win_rate"] == 0
        assert tk["buyplan_po_rate"] == 0
        assert data["new_requirements"] == []
        assert data["offers_to_review"] == []
        assert data["reqs_at_risk"] == []
        assert data["quotes_due_soon"] == []
        assert data["pipeline"]["active_reqs"] == 0

    # 2. Sourcing ratio KPI
    def test_sourcing_ratio(self, client, db_session, test_user):
        r1 = self._make_req(db_session, test_user, name="REQ-SOURCED")
        self._make_offer(db_session, r1, test_user)
        self._make_req(db_session, test_user, name="REQ-UNSOURCED")
        db_session.commit()

        resp = client.get("/api/dashboard/buyer-brief")
        data = resp.json()
        assert data["kpis"]["sourcing_ratio"] == 50
        assert data["kpis"]["total_reqs"] == 2
        assert data["kpis"]["sourced_reqs"] == 1

    # 3. Offer→Quote rate KPI
    def test_offer_quote_rate(self, client, db_session, test_user):
        r1 = self._make_req(db_session, test_user)
        self._make_offer(db_session, r1, test_user, attribution="converted")
        self._make_offer(db_session, r1, test_user, attribution="active")
        self._make_offer(db_session, r1, test_user, attribution="active")
        db_session.commit()

        resp = client.get("/api/dashboard/buyer-brief")
        data = resp.json()
        assert data["kpis"]["offer_quote_rate"] == 33  # 1/3 rounded

    # 4. Quote win rate KPI
    def test_quote_win_rate(self, client, db_session, test_user):
        from app.models import Company, CustomerSite

        co = Company(name="Test Co", is_active=True, created_at=datetime.now(timezone.utc))
        db_session.add(co)
        db_session.flush()
        site = CustomerSite(company_id=co.id, site_name="HQ", created_at=datetime.now(timezone.utc))
        db_session.add(site)
        db_session.flush()

        r1 = self._make_req(db_session, test_user)
        for i, result in enumerate(["won", "won", "lost"]):
            q = Quote(
                requisition_id=r1.id,
                customer_site_id=site.id,
                quote_number=f"Q-{result}-{i}",
                status="sent",
                result=result,
                result_at=datetime.now(timezone.utc),
                created_by_id=test_user.id,
                created_at=datetime.now(timezone.utc),
            )
            db_session.add(q)
        db_session.commit()

        resp = client.get("/api/dashboard/buyer-brief")
        data = resp.json()
        assert data["kpis"]["quote_win_rate"] == 67  # 2/(2+1) rounded

    # 5. New requirements tile
    def test_new_requirements(self, client, db_session, test_user):
        r1 = self._make_req(db_session, test_user, name="NEW-REQ")
        self._make_offer(db_session, r1, test_user)
        self._make_req(db_session, test_user, name="BARE-REQ")
        db_session.commit()

        resp = client.get("/api/dashboard/buyer-brief?days=7")
        data = resp.json()
        reqs = data["new_requirements"]
        assert len(reqs) == 2
        names = {r["name"] for r in reqs}
        assert "NEW-REQ" in names
        assert "BARE-REQ" in names
        sourced = [r for r in reqs if r["name"] == "NEW-REQ"][0]
        assert sourced["has_offers"] is True
        bare = [r for r in reqs if r["name"] == "BARE-REQ"][0]
        assert bare["has_offers"] is False

    # 6. Offers to review tile
    def test_offers_to_review(self, client, db_session, test_user):
        r1 = self._make_req(db_session, test_user)
        self._make_offer(db_session, r1, test_user, status="needs_review")
        self._make_offer(db_session, r1, test_user, status="active")
        db_session.commit()

        resp = client.get("/api/dashboard/buyer-brief")
        data = resp.json()
        assert len(data["offers_to_review"]) == 1
        assert data["offers_to_review"][0]["vendor_name"] == "Arrow"

    # 7b. Reqs at Risk — no offers after 48h
    def test_reqs_at_risk_no_offers(self, client, db_session, test_user):
        # Old req with no offers → at risk
        self._make_req(db_session, test_user, name="STALLED", days_ago=3)
        # Fresh req with no offers → NOT at risk (too new)
        self._make_req(db_session, test_user, name="FRESH", days_ago=0)
        # Old req WITH offers → NOT at risk
        r3 = self._make_req(db_session, test_user, name="SOURCED", days_ago=3)
        self._make_offer(db_session, r3, test_user)
        self._make_offer(db_session, r3, test_user)
        db_session.commit()

        resp = client.get("/api/dashboard/buyer-brief?days=7")
        data = resp.json()
        names = [r["name"] for r in data["reqs_at_risk"]]
        assert "STALLED" in names
        assert "FRESH" not in names
        assert "SOURCED" not in names

    # 7c. Reqs at Risk — deadline approaching with no offers
    def test_reqs_at_risk_deadline(self, client, db_session, test_user):
        from datetime import date

        soon = (date.today() + timedelta(days=2)).isoformat()
        # Deadline soon + no offers → critical
        self._make_req(db_session, test_user, name="URGENT-BARE", deadline=soon, days_ago=0)
        # Deadline soon + has offers → NOT at risk
        r2 = self._make_req(db_session, test_user, name="URGENT-OK", deadline=soon, days_ago=0)
        self._make_offer(db_session, r2, test_user)
        self._make_offer(db_session, r2, test_user)
        db_session.commit()

        resp = client.get("/api/dashboard/buyer-brief?days=7")
        data = resp.json()
        names = [r["name"] for r in data["reqs_at_risk"]]
        assert "URGENT-BARE" in names
        urgent = [r for r in data["reqs_at_risk"] if r["name"] == "URGENT-BARE"][0]
        assert urgent["urgency"] == "critical"
        assert "URGENT-OK" not in names

    # 7d. Reqs at Risk — ASAP deadline with no offers
    def test_reqs_at_risk_asap(self, client, db_session, test_user):
        self._make_req(db_session, test_user, name="ASAP-BARE", deadline="ASAP", days_ago=0)
        db_session.commit()

        resp = client.get("/api/dashboard/buyer-brief?days=7")
        data = resp.json()
        names = [r["name"] for r in data["reqs_at_risk"]]
        assert "ASAP-BARE" in names
        asap = [r for r in data["reqs_at_risk"] if r["name"] == "ASAP-BARE"][0]
        assert asap["urgency"] == "critical"

    # 7e. Reqs at Risk — only 1 offer after 72h
    def test_reqs_at_risk_single_offer(self, client, db_session, test_user):
        r1 = self._make_req(db_session, test_user, name="LONELY", days_ago=4)
        self._make_offer(db_session, r1, test_user)
        db_session.commit()

        resp = client.get("/api/dashboard/buyer-brief?days=7")
        data = resp.json()
        names = [r["name"] for r in data["reqs_at_risk"]]
        assert "LONELY" in names
        lonely = [r for r in data["reqs_at_risk"] if r["name"] == "LONELY"][0]
        assert "only 1 offer" in lonely["risk"]

    # 8. Quotes due soon tile with ASAP deadline
    def test_quotes_due_soon_asap(self, client, db_session, test_user):
        self._make_req(db_session, test_user, name="URGENT", deadline="ASAP")
        db_session.commit()

        resp = client.get("/api/dashboard/buyer-brief")
        data = resp.json()
        assert len(data["quotes_due_soon"]) == 1
        assert data["quotes_due_soon"][0]["urgency"] == "critical"
        assert data["quotes_due_soon"][0]["days_left"] == 0

    # 9. Quotes due soon with date deadline
    def test_quotes_due_soon_date(self, client, db_session, test_user):
        from datetime import date

        soon = (date.today() + timedelta(days=2)).isoformat()
        far = (date.today() + timedelta(days=10)).isoformat()
        self._make_req(db_session, test_user, name="SOON", deadline=soon)
        self._make_req(db_session, test_user, name="FAR", deadline=far)
        db_session.commit()

        resp = client.get("/api/dashboard/buyer-brief")
        data = resp.json()
        due = data["quotes_due_soon"]
        assert len(due) == 2
        assert due[0]["name"] == "SOON"
        assert due[0]["urgency"] == "warning"
        assert due[1]["name"] == "FAR"
        assert due[1]["urgency"] == "normal"

    # 10. Overdue deadline
    def test_quotes_due_overdue(self, client, db_session, test_user):
        from datetime import date

        past = (date.today() - timedelta(days=3)).isoformat()
        self._make_req(db_session, test_user, name="OVERDUE", deadline=past)
        db_session.commit()

        resp = client.get("/api/dashboard/buyer-brief")
        data = resp.json()
        assert len(data["quotes_due_soon"]) == 1
        assert data["quotes_due_soon"][0]["urgency"] == "critical"

    # 11. Invalid deadline skipped
    def test_invalid_deadline_skipped(self, client, db_session, test_user):
        self._make_req(db_session, test_user, name="BAD-DL", deadline="not-a-date")
        db_session.commit()

        resp = client.get("/api/dashboard/buyer-brief")
        data = resp.json()
        assert data["quotes_due_soon"] == []

    # 12. Personal KPIs scoped to user; team_kpis reflect all users
    def test_team_kpis_include_all_users(self, client, db_session, test_user, sales_user):
        # Both users create reqs; only test_user sources one
        r1 = self._make_req(db_session, test_user, name="BUYER-REQ")
        self._make_offer(db_session, r1, test_user)
        self._make_req(db_session, sales_user, name="SALES-REQ")
        db_session.commit()

        resp = client.get("/api/dashboard/buyer-brief?scope=my")
        data = resp.json()

        # Personal KPIs: 1 req sourced out of 1 = 100%
        assert data["kpis"]["sourcing_ratio"] == 100
        assert data["kpis"]["total_reqs"] == 1

        # Team KPIs: 1 sourced out of 2 total = 50%
        assert data["team_kpis"]["sourcing_ratio"] == 50

    # 13. Pipeline summary counts
    def test_pipeline_summary(self, client, db_session, test_user):
        self._make_req(db_session, test_user, name="ACTIVE-1")
        self._make_req(db_session, test_user, name="ACTIVE-2", status="sourcing")
        self._make_req(db_session, test_user, name="CLOSED", status="closed")
        db_session.commit()

        resp = client.get("/api/dashboard/buyer-brief")
        data = resp.json()
        assert data["pipeline"]["active_reqs"] == 2

    # 14. Old requirements excluded by days filter
    def test_days_filter(self, client, db_session, test_user):
        self._make_req(db_session, test_user, name="RECENT", days_ago=3)
        self._make_req(db_session, test_user, name="OLD", days_ago=20)
        db_session.commit()

        resp = client.get("/api/dashboard/buyer-brief?days=7")
        data = resp.json()
        names = [r["name"] for r in data["new_requirements"]]
        assert "RECENT" in names
        assert "OLD" not in names

    # 16. Datetime deadline with T
    def test_datetime_deadline(self, client, db_session, test_user):
        soon = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        self._make_req(db_session, test_user, name="DT-DL", deadline=soon)
        db_session.commit()

        resp = client.get("/api/dashboard/buyer-brief")
        data = resp.json()
        assert len(data["quotes_due_soon"]) == 1
        assert data["quotes_due_soon"][0]["urgency"] == "warning"


class TestAgeLabel:
    """Tests for the _age_label helper function."""

    def test_just_now(self):
        from app.routers.dashboard import _age_label

        assert _age_label(0.5) == "just now"

    def test_hours(self):
        from app.routers.dashboard import _age_label

        assert _age_label(3) == "3h ago"

    def test_days(self):
        from app.routers.dashboard import _age_label

        assert _age_label(48) == "2d ago"
