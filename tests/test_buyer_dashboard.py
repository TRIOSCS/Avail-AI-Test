"""Tests for GET /api/dashboard/buyer-brief and _age_label helper."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.orm import Session

from app.models import (
    Contact,
    Offer,
    Quote,
    Requisition,
    User,
)


class TestBuyerBrief:
    """Tests for the /api/dashboard/buyer-brief endpoint."""

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

    # 1. Empty DB returns zeros
    def test_empty_db(self, client):
        resp = client.get("/api/dashboard/buyer-brief")
        assert resp.status_code == 200
        data = resp.json()
        assert data["kpis"]["sourcing_ratio"] == 0
        assert data["kpis"]["offer_quote_rate"] == 0
        assert data["kpis"]["quote_win_rate"] == 0
        assert data["kpis"]["buyplan_po_rate"] == 0
        assert data["new_requirements"] == []
        assert data["offers_to_review"] == []
        assert data["awaiting_vendor"] == []
        assert data["quotes_due_soon"] == []
        assert data["top_vendors"] == []
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
        from app.models import CustomerSite, Company

        co = Company(name="Test Co", is_active=True, created_at=datetime.now(timezone.utc))
        db_session.add(co)
        db_session.flush()
        site = CustomerSite(company_id=co.id, site_name="HQ", created_at=datetime.now(timezone.utc))
        db_session.add(site)
        db_session.flush()

        r1 = self._make_req(db_session, test_user)
        for i, result in enumerate(["won", "won", "lost"]):
            q = Quote(
                requisition_id=r1.id, customer_site_id=site.id,
                quote_number=f"Q-{result}-{i}",
                status="sent", result=result,
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

    # 7. Awaiting vendor response tile
    def test_awaiting_vendor(self, client, db_session, test_user):
        r1 = self._make_req(db_session, test_user)
        self._make_contact(db_session, r1, test_user, status="sent", days_ago=2)
        self._make_contact(db_session, r1, test_user, status="responded", days_ago=1)
        db_session.commit()

        resp = client.get("/api/dashboard/buyer-brief?days=7")
        data = resp.json()
        assert len(data["awaiting_vendor"]) == 1
        assert data["awaiting_vendor"][0]["vendor_name"] == "Mouser"

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

    # 12. Team scope shows all users
    def test_team_scope(self, client, db_session, test_user, sales_user):
        self._make_req(db_session, test_user, name="BUYER-REQ")
        self._make_req(db_session, sales_user, name="SALES-REQ")
        db_session.commit()

        # My scope: only buyer's
        resp = client.get("/api/dashboard/buyer-brief?scope=my")
        data = resp.json()
        names = [r["name"] for r in data["new_requirements"]]
        assert "BUYER-REQ" in names
        assert "SALES-REQ" not in names

        # Team scope: both
        resp = client.get("/api/dashboard/buyer-brief?scope=team")
        data = resp.json()
        names = [r["name"] for r in data["new_requirements"]]
        assert "BUYER-REQ" in names
        assert "SALES-REQ" in names

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

    # 15. Top vendors tile
    def test_top_vendors(self, client, db_session, test_user):
        r1 = self._make_req(db_session, test_user)
        # Arrow: 3 offers, Mouser: 1 offer
        for _ in range(3):
            o = Offer(
                requisition_id=r1.id, vendor_name="Arrow", mpn="LM317T",
                qty_available=100, unit_price=1.50, entered_by_id=test_user.id,
                status="active", attribution_status="active",
                created_at=datetime.now(timezone.utc),
            )
            db_session.add(o)
        o2 = Offer(
            requisition_id=r1.id, vendor_name="Mouser", mpn="NE555",
            qty_available=50, unit_price=0.75, entered_by_id=test_user.id,
            status="approved", attribution_status="active",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(o2)
        db_session.commit()

        resp = client.get("/api/dashboard/buyer-brief")
        data = resp.json()
        tv = data["top_vendors"]
        assert len(tv) == 2
        assert tv[0]["vendor_name"] == "Arrow"
        assert tv[0]["offer_count"] == 3
        assert tv[1]["vendor_name"] == "Mouser"
        assert tv[1]["offer_count"] == 1

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
