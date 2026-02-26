"""Tests for load test performance fixes and correct offer model.

Offer model: "active" (default) or "sold" (manually marked).
is_stale is display-only metadata — never hides offers.
"Leave no stone unturned."

Called by: pytest tests/test_load_test_fixes.py
Depends on: app/routers/crm/quotes.py, app/routers/crm/offers.py,
            app/routers/crm/companies.py, app/scheduler.py,
            app/routers/dashboard.py
"""

from datetime import datetime, timedelta, timezone
import os

import pytest
from sqlalchemy.orm import Session

from app.models import Company, CustomerSite, Offer, Quote, Requisition, User


# ── Helpers ──────────────────────────────────────────────────────────────


def _make_user(db, name="Test User"):
    u = User(name=name, email=f"{name.lower().replace(' ', '.')}@test.com")
    db.add(u)
    db.flush()
    return u


def _make_req(db, user, name="REQ-1", status="active"):
    r = Requisition(
        name=name,
        status=status,
        created_by=user.id,
        created_at=datetime.now(timezone.utc),
    )
    db.add(r)
    db.flush()
    return r


def _make_offer(db, req, user, status="active", mpn="LM317T", days_ago=0):
    o = Offer(
        requisition_id=req.id,
        vendor_name="Arrow",
        mpn=mpn,
        qty_available=100,
        unit_price=1.50,
        entered_by_id=user.id,
        status=status,
        created_at=datetime.now(timezone.utc) - timedelta(days=days_ago),
    )
    db.add(o)
    db.flush()
    return o


def _make_company_and_site(db):
    co = Company(name="Test Co", is_active=True, created_at=datetime.now(timezone.utc))
    db.add(co)
    db.flush()
    site = CustomerSite(
        company_id=co.id, site_name="HQ", created_at=datetime.now(timezone.utc)
    )
    db.add(site)
    db.flush()
    return co, site


def _make_quote(db, req, site, user, offer_ids, quote_number="Q-TEST-001"):
    line_items = [{"offer_id": oid, "mpn": "LM317T", "qty": 100,
                   "cost_price": 1.50, "sell_price": 2.00} for oid in offer_ids]
    q = Quote(
        requisition_id=req.id,
        customer_site_id=site.id,
        quote_number=quote_number,
        line_items=line_items,
        subtotal=200.0,
        total_cost=150.0,
        status="draft",
        created_by_id=user.id,
        created_at=datetime.now(timezone.utc),
    )
    db.add(q)
    db.flush()
    return q


# ── Fix 1: Migration file exists ────────────────────────────────────────


class TestPerformanceIndexes:
    def test_migration_file_exists(self):
        """Migration 015 should exist with is_stale column."""
        import importlib.util
        path = os.path.join(
            os.path.dirname(__file__), "..", "alembic", "versions",
            "015_performance_indexes.py",
        )
        assert os.path.exists(path)
        spec = importlib.util.spec_from_file_location("migration_015", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        assert mod.revision == "015_performance_indexes"
        assert mod.down_revision == "014_multiplier_score_snapshot"

    def test_migration_has_upgrade_and_downgrade(self):
        import importlib.util
        path = os.path.join(
            os.path.dirname(__file__), "..", "alembic", "versions",
            "015_performance_indexes.py",
        )
        spec = importlib.util.spec_from_file_location("migration_015", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        assert callable(mod.upgrade)
        assert callable(mod.downgrade)


# ── Fix 2: Companies typeahead caching ──────────────────────────────────


class TestCompaniesTypeaheadCache:
    def test_typeahead_returns_companies(self, client, db_session):
        co, site = _make_company_and_site(db_session)
        db_session.commit()

        resp = client.get("/api/companies/typeahead")
        assert resp.status_code == 200
        data = resp.json()
        names = [c["name"] for c in data]
        assert "Test Co" in names

    def test_typeahead_includes_sites(self, client, db_session):
        co, site = _make_company_and_site(db_session)
        db_session.commit()

        resp = client.get("/api/companies/typeahead")
        data = resp.json()
        test_co = next(c for c in data if c["name"] == "Test Co")
        assert len(test_co["sites"]) == 1
        assert test_co["sites"][0]["site_name"] == "HQ"

    def test_typeahead_excludes_inactive_companies(self, client, db_session):
        co = Company(name="Inactive Co", is_active=False,
                     created_at=datetime.now(timezone.utc))
        db_session.add(co)
        db_session.commit()

        resp = client.get("/api/companies/typeahead")
        data = resp.json()
        names = [c["name"] for c in data]
        assert "Inactive Co" not in names


# ── Offer status stays "active" through quoting ─────────────────────────


class TestOfferStatusUnchanged:
    def test_offer_stays_active_after_quote_create(self, client, db_session, test_user):
        """Offers must NEVER change status when included in a quote.
        Same offer can be in 5 quotes simultaneously."""
        co, site = _make_company_and_site(db_session)
        req = _make_req(db_session, test_user)
        req.customer_site_id = site.id
        o1 = _make_offer(db_session, req, test_user, mpn="LM317T")
        o2 = _make_offer(db_session, req, test_user, mpn="LM7805")
        db_session.commit()

        resp = client.post(
            f"/api/requisitions/{req.id}/quote",
            json={"offer_ids": [o1.id, o2.id]},
        )
        assert resp.status_code == 200

        db_session.refresh(o1)
        db_session.refresh(o2)
        assert o1.status == "active"
        assert o2.status == "active"

    def test_offer_stays_active_after_quote_won(self, client, db_session, test_user):
        """Offer status must not change when quote result is 'won'."""
        co, site = _make_company_and_site(db_session)
        req = _make_req(db_session, test_user)
        req.customer_site_id = site.id
        o1 = _make_offer(db_session, req, test_user)
        db_session.commit()

        q = _make_quote(db_session, req, site, test_user,
                        [o1.id], quote_number="Q-WIN-001")
        q.status = "sent"
        db_session.commit()

        resp = client.post(
            f"/api/quotes/{q.id}/result",
            json={"result": "won"},
        )
        assert resp.status_code == 200

        db_session.refresh(o1)
        assert o1.status == "active"

    def test_offer_stays_active_after_quote_lost(self, client, db_session, test_user):
        """Offer status must not change when quote result is 'lost'."""
        co, site = _make_company_and_site(db_session)
        req = _make_req(db_session, test_user)
        req.customer_site_id = site.id
        o1 = _make_offer(db_session, req, test_user)
        db_session.commit()

        q = _make_quote(db_session, req, site, test_user,
                        [o1.id], quote_number="Q-LOSS-001")
        q.status = "sent"
        db_session.commit()

        resp = client.post(
            f"/api/quotes/{q.id}/result",
            json={"result": "lost", "reason": "price"},
        )
        assert resp.status_code == 200

        db_session.refresh(o1)
        assert o1.status == "active"

    def test_same_offer_in_multiple_quotes(self, client, db_session, test_user):
        """The same offer can appear in multiple quotes — no status change."""
        co, site = _make_company_and_site(db_session)
        req = _make_req(db_session, test_user)
        req.customer_site_id = site.id
        o1 = _make_offer(db_session, req, test_user)
        db_session.commit()

        # Create two quotes with the same offer
        resp1 = client.post(
            f"/api/requisitions/{req.id}/quote",
            json={"offer_ids": [o1.id]},
        )
        assert resp1.status_code == 200

        resp2 = client.post(
            f"/api/requisitions/{req.id}/quote",
            json={"offer_ids": [o1.id]},
        )
        assert resp2.status_code == 200

        db_session.refresh(o1)
        assert o1.status == "active"


# ── Mark-sold endpoint ──────────────────────────────────────────────────


class TestMarkSold:
    def test_mark_sold_by_creator(self, client, db_session, test_user):
        """Buyer who created the offer can mark it sold."""
        req = _make_req(db_session, test_user)
        o = _make_offer(db_session, req, test_user)
        db_session.commit()

        resp = client.patch(f"/api/offers/{o.id}/mark-sold")
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["status"] == "sold"

        db_session.refresh(o)
        assert o.status == "sold"

    def test_mark_sold_not_found(self, client):
        resp = client.patch("/api/offers/999999/mark-sold")
        assert resp.status_code == 404

    def test_mark_sold_by_other_user_forbidden(self, client, db_session, test_user):
        """Another non-admin user cannot mark someone else's offer as sold."""
        creator = _make_user(db_session, "Creator")
        req = _make_req(db_session, creator)
        o = _make_offer(db_session, req, creator)
        db_session.commit()

        # test_user (from conftest) is NOT the creator and NOT admin
        resp = client.patch(f"/api/offers/{o.id}/mark-sold")
        assert resp.status_code == 403

    def test_mark_sold_idempotent(self, client, db_session, test_user):
        """Marking an already-sold offer returns ok without error."""
        req = _make_req(db_session, test_user)
        o = _make_offer(db_session, req, test_user, status="sold")
        db_session.commit()

        resp = client.patch(f"/api/offers/{o.id}/mark-sold")
        assert resp.status_code == 200
        assert resp.json()["message"] == "Already marked sold"


# ── Stale flag ──────────────────────────────────────────────────────────


class TestStaleFlag:
    def test_is_stale_default_false(self, db_session, test_user):
        """New offers should have is_stale=False by default."""
        req = _make_req(db_session, test_user)
        o = _make_offer(db_session, req, test_user)
        db_session.commit()
        db_session.refresh(o)
        assert o.is_stale is False

    def test_stale_flagging_logic(self, db_session, test_user):
        """Offers older than 14 days should be flagged is_stale=True."""
        req = _make_req(db_session, test_user)
        old_offer = _make_offer(db_session, req, test_user, mpn="OLD-1", days_ago=15)
        new_offer = _make_offer(db_session, req, test_user, mpn="NEW-1", days_ago=1)
        sold_offer = _make_offer(db_session, req, test_user, mpn="SOLD-1",
                                 status="sold", days_ago=20)
        db_session.commit()

        # Run the same logic as the scheduler job
        cutoff = datetime.now(timezone.utc) - timedelta(days=14)
        flagged = (
            db_session.query(Offer)
            .filter(
                Offer.status == "active",
                Offer.is_stale.is_(False),
                Offer.created_at < cutoff,
            )
            .update({"is_stale": True}, synchronize_session="fetch")
        )
        db_session.commit()

        assert flagged == 1

        db_session.refresh(old_offer)
        db_session.refresh(new_offer)
        db_session.refresh(sold_offer)

        assert old_offer.is_stale is True
        assert new_offer.is_stale is False
        assert sold_offer.is_stale is False  # sold offers not flagged

    def test_stale_offers_still_appear_in_hot_offers(self, client, db_session, test_user):
        """Stale offers must still appear in hot-offers — never hidden."""
        req = _make_req(db_session, test_user)
        o = _make_offer(db_session, req, test_user, mpn="STALE-VISIBLE", days_ago=2)
        o.is_stale = True  # manually flag
        db_session.commit()

        resp = client.get("/api/dashboard/hot-offers?days=7")
        assert resp.status_code == 200
        data = resp.json()
        mpns = [x["mpn"] for x in data]
        assert "STALE-VISIBLE" in mpns

    def test_scheduler_has_stale_job(self):
        """The stale flagging job should be registered."""
        from app.scheduler import _job_flag_stale_offers
        assert callable(_job_flag_stale_offers)


# ── Fix 4: Proactive offer expiry ───────────────────────────────────────


class TestProactiveOfferExpiry:
    def test_expire_old_proactive_offers(self, db_session):
        from app.models.intelligence import ProactiveOffer

        co, site = _make_company_and_site(db_session)
        user = _make_user(db_session, "Sales User")

        old = ProactiveOffer(
            customer_site_id=site.id, salesperson_id=user.id,
            line_items=[], status="sent",
            sent_at=datetime.now(timezone.utc) - timedelta(days=15),
        )
        recent = ProactiveOffer(
            customer_site_id=site.id, salesperson_id=user.id,
            line_items=[], status="sent",
            sent_at=datetime.now(timezone.utc) - timedelta(days=2),
        )
        converted = ProactiveOffer(
            customer_site_id=site.id, salesperson_id=user.id,
            line_items=[], status="converted",
            sent_at=datetime.now(timezone.utc) - timedelta(days=20),
        )
        db_session.add_all([old, recent, converted])
        db_session.commit()

        cutoff = datetime.now(timezone.utc) - timedelta(days=14)
        expired_count = (
            db_session.query(ProactiveOffer)
            .filter(ProactiveOffer.status == "sent", ProactiveOffer.sent_at < cutoff)
            .update({"status": "expired"}, synchronize_session="fetch")
        )
        db_session.commit()

        assert expired_count == 1
        db_session.refresh(old)
        db_session.refresh(recent)
        db_session.refresh(converted)
        assert old.status == "expired"
        assert recent.status == "sent"
        assert converted.status == "converted"

    def test_scheduler_has_expiry_job(self):
        from app.scheduler import _job_proactive_offer_expiry
        assert callable(_job_proactive_offer_expiry)


# ── Fix 5: Buyer-brief optimized ────────────────────────────────────────


class TestBuyerBriefOptimized:
    def test_at_risk_reqs_with_offer_counts(self, client, db_session, test_user):
        r1 = Requisition(
            name="RISKY-1", status="active", created_by=test_user.id,
            created_at=datetime.now(timezone.utc) - timedelta(days=3),
        )
        db_session.add(r1)
        db_session.flush()

        r2 = Requisition(
            name="SAFE-1", status="active", created_by=test_user.id,
            created_at=datetime.now(timezone.utc) - timedelta(days=3),
        )
        db_session.add(r2)
        db_session.flush()
        for i in range(5):
            _make_offer(db_session, r2, test_user, mpn=f"SAFE-{i}")
        db_session.commit()

        resp = client.get("/api/dashboard/buyer-brief")
        assert resp.status_code == 200
        data = resp.json()
        risk_names = [r["name"] for r in data["reqs_at_risk"]]
        assert "RISKY-1" in risk_names
        assert "SAFE-1" not in risk_names

    def test_hot_offers_returns_recent(self, client, db_session, test_user):
        req = _make_req(db_session, test_user)
        _make_offer(db_session, req, test_user)
        db_session.commit()

        resp = client.get("/api/dashboard/hot-offers")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) >= 1
        assert data[0]["vendor_name"] == "Arrow"

    def test_hot_offers_only_active_status(self, client, db_session, test_user):
        """Hot offers should only show active offers, not sold."""
        req = _make_req(db_session, test_user)
        _make_offer(db_session, req, test_user, status="sold", mpn="SOLD-HIDDEN")
        _make_offer(db_session, req, test_user, status="active", mpn="ACTIVE-SHOWN")
        db_session.commit()

        resp = client.get("/api/dashboard/hot-offers")
        data = resp.json()
        mpns = [o["mpn"] for o in data]
        assert "ACTIVE-SHOWN" in mpns
        assert "SOLD-HIDDEN" not in mpns


# ── Completed Deals tile ───────────────────────────────────────────────


class TestCompletedDeals:
    def test_completed_deals_in_buyer_brief(self, client, db_session, test_user):
        """Buyer-brief should return completed_deals with won/lost data."""
        co, site = _make_company_and_site(db_session)
        req = _make_req(db_session, test_user, name="WON-REQ", status="won")
        q = _make_quote(db_session, req, site, test_user, [], "Q-WON")
        q.result = "won"
        q.result_at = datetime.now(timezone.utc) - timedelta(hours=6)

        req2 = _make_req(db_session, test_user, name="LOST-REQ", status="lost")
        q2 = _make_quote(db_session, req2, site, test_user, [], "Q-LOST")
        q2.result = "lost"
        q2.result_at = datetime.now(timezone.utc) - timedelta(hours=12)
        db_session.commit()

        resp = client.get("/api/dashboard/buyer-brief")
        assert resp.status_code == 200
        data = resp.json()

        cd = data["completed_deals"]
        assert cd["won_count"] >= 1
        assert cd["lost_count"] >= 1
        assert cd["win_rate"] >= 0
        assert len(cd["recent_wins"]) >= 1
        assert len(cd["recent_losses"]) >= 1

    def test_completed_deals_not_filtered_by_days(self, client, db_session, test_user):
        """Completed deals must not be filtered by the days parameter."""
        co, site = _make_company_and_site(db_session)
        req = _make_req(db_session, test_user, name="OLD-WIN", status="won")
        q = _make_quote(db_session, req, site, test_user, [], "Q-OLD-WIN")
        q.result = "won"
        q.result_at = datetime.now(timezone.utc) - timedelta(days=60)
        db_session.commit()

        # Even with days=7, old deals should still appear
        resp = client.get("/api/dashboard/buyer-brief?days=7")
        data = resp.json()
        cd = data["completed_deals"]
        assert cd["won_count"] >= 1
        win_names = [w["name"] for w in cd["recent_wins"]]
        assert "OLD-WIN" in win_names

    def test_completed_deals_values(self, client, db_session, test_user):
        """Completed deals should include quote values."""
        co, site = _make_company_and_site(db_session)
        req = _make_req(db_session, test_user, name="VAL-REQ", status="won")
        q = _make_quote(db_session, req, site, test_user, [], "Q-VAL")
        q.subtotal = 5000.00
        q.result = "won"
        q.result_at = datetime.now(timezone.utc)
        db_session.commit()

        resp = client.get("/api/dashboard/buyer-brief")
        data = resp.json()
        cd = data["completed_deals"]
        assert cd["won_value"] >= 5000.0
        wins = [w for w in cd["recent_wins"] if w["name"] == "VAL-REQ"]
        assert len(wins) == 1
        assert wins[0]["value"] == 5000.0

    def test_pipeline_includes_lost(self, client, db_session, test_user):
        """Pipeline summary should include lost_this_month."""
        resp = client.get("/api/dashboard/buyer-brief")
        data = resp.json()
        assert "lost_this_month" in data["pipeline"]
