"""Tests for GET /api/dashboard/needs-attention endpoint."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.orm import Session

from app.models import (
    ActivityLog,
    Company,
    CustomerSite,
    Quote,
    Requisition,
    User,
)


class TestNeedsAttention:
    """10 test cases covering the needs-attention endpoint."""

    def _make_company(self, db, owner, name="Acme Corp", **kw):
        c = Company(
            name=name,
            is_active=kw.pop("is_active", True),
            is_strategic=kw.pop("is_strategic", False),
            account_owner_id=owner.id,
            created_at=datetime.now(timezone.utc),
            **kw,
        )
        db.add(c)
        db.flush()
        return c

    def _make_site(self, db, company, name="HQ"):
        s = CustomerSite(
            company_id=company.id,
            site_name=name,
            created_at=datetime.now(timezone.utc),
        )
        db.add(s)
        db.flush()
        return s

    def _make_activity(self, db, user, company, days_ago=0, activity_type="email_sent"):
        a = ActivityLog(
            user_id=user.id,
            company_id=company.id,
            activity_type=activity_type,
            channel="email" if "email" in activity_type else "phone",
            created_at=datetime.now(timezone.utc) - timedelta(days=days_ago),
        )
        db.add(a)
        db.flush()
        return a

    # 1. No companies → empty list
    def test_no_companies_returns_empty(self, client):
        resp = client.get("/api/dashboard/needs-attention")
        assert resp.status_code == 200
        assert resp.json() == []

    # 2. User with 1 stale company → returned
    def test_stale_company_returned(self, client, db_session, test_user):
        c = self._make_company(db_session, test_user)
        self._make_activity(db_session, test_user, c, days_ago=10)
        db_session.commit()

        resp = client.get("/api/dashboard/needs-attention?days=7")
        data = resp.json()
        assert len(data) == 1
        assert data[0]["company_name"] == "Acme Corp"
        assert data[0]["days_since_contact"] == 10

    # 3. User with 1 recently-contacted company → not returned
    def test_active_company_not_returned(self, client, db_session, test_user):
        c = self._make_company(db_session, test_user)
        self._make_activity(db_session, test_user, c, days_ago=2)
        db_session.commit()

        resp = client.get("/api/dashboard/needs-attention?days=7")
        assert resp.json() == []

    # 4. days=30 → only companies inactive 31+ days
    def test_custom_days_parameter(self, client, db_session, test_user):
        c1 = self._make_company(db_session, test_user, name="Stale Corp")
        self._make_activity(db_session, test_user, c1, days_ago=35)

        c2 = self._make_company(db_session, test_user, name="Active Corp")
        self._make_activity(db_session, test_user, c2, days_ago=15)
        db_session.commit()

        resp = client.get("/api/dashboard/needs-attention?days=30")
        data = resp.json()
        assert len(data) == 1
        assert data[0]["company_name"] == "Stale Corp"

    # 5. Deactivated company excluded
    def test_inactive_company_excluded(self, client, db_session, test_user):
        self._make_company(db_session, test_user, name="Dead Corp", is_active=False)
        db_session.commit()

        resp = client.get("/api/dashboard/needs-attention")
        assert resp.json() == []

    # 6. Other user's companies not shown
    def test_other_users_companies_hidden(self, client, db_session, test_user, sales_user):
        self._make_company(db_session, sales_user, name="Their Corp")
        db_session.commit()

        resp = client.get("/api/dashboard/needs-attention")
        assert resp.json() == []

    # 7. Strategic flag included correctly
    def test_strategic_flag(self, client, db_session, test_user):
        self._make_company(db_session, test_user, name="VIP Corp", is_strategic=True)
        db_session.commit()

        resp = client.get("/api/dashboard/needs-attention")
        data = resp.json()
        assert len(data) == 1
        assert data[0]["is_strategic"] is True

    # 8. Open req count aggregation
    def test_open_req_count(self, client, db_session, test_user):
        c = self._make_company(db_session, test_user)
        site = self._make_site(db_session, c)

        for i in range(2):
            req = Requisition(
                name=f"REQ-{i}",
                customer_site_id=site.id,
                status="active",
                created_by=test_user.id,
                created_at=datetime.now(timezone.utc),
            )
            db_session.add(req)
        db_session.commit()

        resp = client.get("/api/dashboard/needs-attention")
        data = resp.json()
        assert len(data) == 1
        assert data[0]["open_req_count"] == 2

    # 9. Open quote value aggregation
    def test_open_quote_value(self, client, db_session, test_user):
        c = self._make_company(db_session, test_user)
        site = self._make_site(db_session, c)

        req = Requisition(
            name="REQ-Q",
            customer_site_id=site.id,
            status="active",
            created_by=test_user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(req)
        db_session.flush()

        q = Quote(
            requisition_id=req.id,
            customer_site_id=site.id,
            quote_number="Q-001",
            status="sent",
            subtotal=12500.00,
            sent_at=datetime.now(timezone.utc) - timedelta(days=3),
            created_by_id=test_user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(q)
        db_session.commit()

        resp = client.get("/api/dashboard/needs-attention")
        data = resp.json()
        assert len(data) == 1
        assert data[0]["open_quote_value"] == 12500.00

    # 10. Sort order: most stale first
    def test_sort_most_stale_first(self, client, db_session, test_user):
        c1 = self._make_company(db_session, test_user, name="Very Stale")
        self._make_activity(db_session, test_user, c1, days_ago=30)

        c2 = self._make_company(db_session, test_user, name="Somewhat Stale")
        self._make_activity(db_session, test_user, c2, days_ago=10)
        db_session.commit()

        resp = client.get("/api/dashboard/needs-attention?days=7")
        data = resp.json()
        assert len(data) == 2
        assert data[0]["company_name"] == "Very Stale"
        assert data[1]["company_name"] == "Somewhat Stale"

    # 11. Received-only activity doesn't count as outreach
    def test_received_activity_not_counted(self, client, db_session, test_user):
        c = self._make_company(db_session, test_user)
        self._make_activity(db_session, test_user, c, days_ago=1, activity_type="email_received")
        db_session.commit()

        resp = client.get("/api/dashboard/needs-attention?days=7")
        data = resp.json()
        assert len(data) == 1
        assert data[0]["company_name"] == "Acme Corp"
