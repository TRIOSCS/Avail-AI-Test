"""Tests for GET /api/dashboard/attention-feed and team-leaderboard user_role.

Tests the unified attention feed endpoint (urgency sorting, item types,
scope filtering) and verifies team_leaderboard includes user_role field.

Called by: pytest
Depends on: app/routers/dashboard.py, conftest fixtures
"""

from datetime import date, datetime, timedelta, timezone

from app.models import (
    ActivityLog,
    Company,
    CustomerSite,
    Offer,
    Quote,
    Requisition,
    User,
)
from app.models.performance import AvailScoreSnapshot


class TestAttentionFeed:
    """Tests for /api/dashboard/attention-feed endpoint."""

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

    def _make_offer(self, db, req, user, status="active", days_ago=0):
        o = Offer(
            requisition_id=req.id,
            vendor_name="Arrow",
            mpn="LM317T",
            qty_available=100,
            unit_price=1.50,
            entered_by_id=user.id,
            status=status,
            created_at=datetime.now(timezone.utc) - timedelta(days=days_ago),
        )
        db.add(o)
        db.flush()
        return o

    def _make_company_with_site(self, db, user, name="Acme", days_since=60):
        co = Company(name=name, is_active=True, created_at=datetime.now(timezone.utc))
        db.add(co)
        db.flush()
        site = CustomerSite(
            company_id=co.id,
            owner_id=user.id,
            site_name="HQ",
            created_at=datetime.now(timezone.utc),
        )
        db.add(site)
        db.flush()
        if days_since < 999:
            act = ActivityLog(
                user_id=user.id,
                company_id=co.id,
                activity_type="email_sent",
                channel="email",
                created_at=datetime.now(timezone.utc) - timedelta(days=days_since),
            )
            db.add(act)
            db.flush()
        return co

    # 1. Empty DB returns empty list
    def test_empty_db(self, client):
        resp = client.get("/api/dashboard/attention-feed")
        assert resp.status_code == 200
        data = resp.json()
        assert data == []

    # 2. Stale account appears in feed
    def test_stale_account(self, client, db_session, test_user):
        co = self._make_company_with_site(db_session, test_user, "Stale Corp", days_since=45)
        db_session.commit()
        resp = client.get("/api/dashboard/attention-feed?days=30")
        assert resp.status_code == 200
        items = resp.json()
        stale = [i for i in items if i["type"] == "stale_account"]
        assert len(stale) >= 1
        assert stale[0]["title"] == "Stale Corp"
        assert stale[0]["link_type"] == "company"
        assert stale[0]["link_id"] == co.id

    # 3. Req at risk (ASAP deadline, no offers)
    def test_req_at_risk_asap(self, client, db_session, test_user):
        self._make_req(db_session, test_user, name="URGENT-REQ", deadline="ASAP", days_ago=1)
        db_session.commit()
        resp = client.get("/api/dashboard/attention-feed")
        assert resp.status_code == 200
        items = resp.json()
        risk = [i for i in items if i["type"] == "req_at_risk"]
        assert len(risk) >= 1
        assert risk[0]["urgency"] == "critical"
        assert "ASAP" in risk[0]["detail"]

    # 4. Req at risk (overdue deadline, no offers)
    def test_req_at_risk_overdue(self, client, db_session, test_user):
        past_date = (datetime.now(timezone.utc) - timedelta(days=3)).strftime("%Y-%m-%d")
        self._make_req(db_session, test_user, name="OVERDUE-REQ", deadline=past_date, days_ago=5)
        db_session.commit()
        resp = client.get("/api/dashboard/attention-feed")
        assert resp.status_code == 200
        items = resp.json()
        risk = [i for i in items if i["type"] == "req_at_risk"]
        assert len(risk) >= 1
        assert risk[0]["urgency"] == "critical"

    # 5. Needs quote (has offers, no quote sent)
    def test_needs_quote(self, client, db_session, test_user):
        req = self._make_req(db_session, test_user, name="NEEDS-QUOTE-REQ")
        self._make_offer(db_session, req, test_user)
        db_session.commit()
        resp = client.get("/api/dashboard/attention-feed")
        assert resp.status_code == 200
        items = resp.json()
        nq = [i for i in items if i["type"] == "needs_quote"]
        assert len(nq) >= 1
        assert "offer" in nq[0]["detail"]
        assert nq[0]["link_type"] == "requisition"

    # 6. Expiring quote
    def test_expiring_quote(self, client, db_session, test_user):
        req = self._make_req(db_session, test_user, name="EXPIRING-REQ")
        # Quote requires a customer_site
        co = Company(name="QuoteCo", is_active=True, created_at=datetime.now(timezone.utc))
        db_session.add(co)
        db_session.flush()
        site = CustomerSite(
            company_id=co.id,
            owner_id=test_user.id,
            site_name="HQ",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(site)
        db_session.flush()
        q = Quote(
            requisition_id=req.id,
            customer_site_id=site.id,
            quote_number="Q-001",
            status="sent",
            subtotal=5000.0,
            validity_days=3,
            sent_at=datetime.now(timezone.utc) - timedelta(days=2),
            created_by_id=test_user.id,
            created_at=datetime.now(timezone.utc) - timedelta(days=2),
        )
        db_session.add(q)
        db_session.commit()
        resp = client.get("/api/dashboard/attention-feed")
        assert resp.status_code == 200
        items = resp.json()
        exp = [i for i in items if i["type"] == "expiring_quote"]
        assert len(exp) >= 1
        assert "Q-001" in exp[0]["detail"]

    # 7. Urgency sorting (critical before warning before info)
    def test_urgency_sorting(self, client, db_session, test_user):
        # Create a critical item (ASAP req)
        self._make_req(db_session, test_user, name="CRITICAL-REQ", deadline="ASAP", days_ago=1)
        # Create a warning item (old req with no offers)
        self._make_req(db_session, test_user, name="WARNING-REQ", days_ago=3)
        # Create a needs_quote (warning)
        req = self._make_req(db_session, test_user, name="QUOTE-REQ")
        self._make_offer(db_session, req, test_user)
        db_session.commit()
        resp = client.get("/api/dashboard/attention-feed")
        assert resp.status_code == 200
        items = resp.json()
        if len(items) >= 2:
            urgencies = [i["urgency"] for i in items]
            crit_idx = [i for i, u in enumerate(urgencies) if u == "critical"]
            warn_idx = [i for i, u in enumerate(urgencies) if u == "warning"]
            info_idx = [i for i, u in enumerate(urgencies) if u == "info"]
            if crit_idx and warn_idx:
                assert max(crit_idx) < min(warn_idx), "Critical items should come before warning"
            if warn_idx and info_idx:
                assert max(warn_idx) < min(info_idx), "Warning items should come before info"

    # 8. Max 12 items returned
    def test_max_12_items(self, client, db_session, test_user):
        for i in range(15):
            self._make_req(db_session, test_user, name=f"REQ-{i}", deadline="ASAP", days_ago=1)
        db_session.commit()
        resp = client.get("/api/dashboard/attention-feed")
        assert resp.status_code == 200
        items = resp.json()
        assert len(items) <= 12

    # 9. Scope filtering (team returns items from other users too)
    def test_scope_my(self, client, db_session, test_user):
        self._make_req(db_session, test_user, name="MY-REQ", deadline="ASAP")
        db_session.commit()
        resp = client.get("/api/dashboard/attention-feed?scope=my")
        assert resp.status_code == 200
        items = resp.json()
        risk = [i for i in items if i["type"] == "req_at_risk"]
        assert len(risk) >= 1

    # 10. Each item has required fields
    def test_item_structure(self, client, db_session, test_user):
        self._make_req(db_session, test_user, name="STRUCT-REQ", deadline="ASAP")
        db_session.commit()
        resp = client.get("/api/dashboard/attention-feed")
        items = resp.json()
        assert len(items) >= 1
        item = items[0]
        assert "type" in item
        assert "urgency" in item
        assert "title" in item
        assert "detail" in item
        assert "link_type" in item
        assert "link_id" in item


class TestTeamLeaderboardUserRole:
    """Tests for user_role field in /api/dashboard/team-leaderboard."""

    def test_user_role_included(self, client, db_session, test_user):
        """team-leaderboard entries include user_role field."""
        current_month = date.today().replace(day=1)
        snap = AvailScoreSnapshot(
            user_id=test_user.id,
            month=current_month,
            role_type="buyer",
            total_score=75.0,
            behavior_total=40.0,
            outcome_total=35.0,
            rank=1,
            qualified=True,
            bonus_amount=500,
        )
        db_session.add(snap)
        db_session.commit()
        resp = client.get("/api/dashboard/team-leaderboard?role=buyer")
        assert resp.status_code == 200
        data = resp.json()
        entries = data["entries"]
        assert len(entries) >= 1
        assert "user_role" in entries[0]
        assert entries[0]["user_role"] == "buyer"

    def test_trader_role_shown(self, client, db_session, test_user):
        """Trader users show user_role='trader' in leaderboard."""
        test_user.role = "trader"
        db_session.commit()
        current_month = date.today().replace(day=1)
        snap = AvailScoreSnapshot(
            user_id=test_user.id,
            month=current_month,
            role_type="buyer",
            total_score=60.0,
            behavior_total=30.0,
            outcome_total=30.0,
            rank=1,
            qualified=True,
        )
        db_session.add(snap)
        db_session.commit()
        resp = client.get("/api/dashboard/team-leaderboard?role=buyer")
        assert resp.status_code == 200
        data = resp.json()
        entries = data["entries"]
        assert len(entries) >= 1
        assert entries[0]["user_role"] == "trader"

    def test_multiple_users_with_roles(self, client, db_session, test_user):
        """Multiple users show correct roles in leaderboard."""
        trader = User(
            email="trader@trioscs.com",
            name="Test Trader",
            role="trader",
            azure_id="trader-001",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(trader)
        db_session.flush()

        current_month = date.today().replace(day=1)
        for u, score in [(test_user, 80.0), (trader, 70.0)]:
            snap = AvailScoreSnapshot(
                user_id=u.id,
                month=current_month,
                role_type="buyer",
                total_score=score,
                behavior_total=score / 2,
                outcome_total=score / 2,
                rank=None,
                qualified=True,
            )
            db_session.add(snap)
        db_session.commit()

        resp = client.get("/api/dashboard/team-leaderboard?role=buyer")
        assert resp.status_code == 200
        data = resp.json()
        entries = data["entries"]
        roles = {e["user_name"]: e["user_role"] for e in entries}
        assert roles.get("Test Buyer") == "buyer"
        assert roles.get("Test Trader") == "trader"
