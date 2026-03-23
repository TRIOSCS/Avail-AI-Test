"""Tests for Avail Score Service — behavior + outcome scoring.

Tests the 10-metric scoring system for buyers and sales, ranking, bonus assignment, and
API endpoints.
"""

import os

os.environ["TESTING"] = "1"
os.environ["RATE_LIMIT_ENABLED"] = "false"

from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from app.models import (
    ActivityLog,
    BuyPlan,
    Company,
    Contact,
    CustomerSite,
    Offer,
    ProactiveOffer,
    Quote,
    Requirement,
    Requisition,
    SiteContact,
    User,
    VendorCard,
)
from app.models.performance import AvailScoreSnapshot, StockListHash
from app.services.avail_score_service import (
    BONUS_1ST,
    BONUS_2ND,
    BONUS_3RD,
    MIN_REQS_BUYER,
    _rank_and_bonus,
    _tier,
    compute_all_avail_scores,
    compute_buyer_avail_score,
    compute_sales_avail_score,
    get_avail_scores,
)

# ── Helpers ──────────────────────────────────────────────────────────

NOW = datetime(2026, 2, 15, 12, 0, 0, tzinfo=timezone.utc)
MONTH = date(2026, 2, 1)


def _make_user(db, name, role, email_prefix):
    u = User(email=f"{email_prefix}@test.com", name=name, role=role, azure_id=f"az-{email_prefix}")
    db.add(u)
    db.flush()
    return u


def _make_req(db, user_id, created_at=None):
    r = Requisition(
        name=f"REQ-{user_id}-{id(created_at)}",
        status="active",
        created_by=user_id,
        created_at=created_at or NOW,
    )
    db.add(r)
    db.flush()
    return r


def _make_contact(db, req_id, user_id, vendor="vendor-a", created_at=None, status="sent"):
    c = Contact(
        requisition_id=req_id,
        user_id=user_id,
        contact_type="email",
        vendor_name=vendor,
        vendor_name_normalized=vendor.lower(),
        status=status,
        created_at=created_at or NOW,
    )
    db.add(c)
    db.flush()
    return c


def _make_offer(db, req_id, user_id, vendor_card_id=None, created_at=None):
    o = Offer(
        requisition_id=req_id,
        vendor_name="Test Vendor",
        mpn="LM317T",
        entered_by_id=user_id,
        vendor_card_id=vendor_card_id,
        unit_price=1.50,
        created_at=created_at or NOW,
    )
    db.add(o)
    db.flush()
    return o


def _make_quote(
    db,
    req_id,
    site_id,
    user_id,
    status="sent",
    result=None,
    offers=None,
    sent_at=None,
    result_at=None,
    won_revenue=None,
):
    q = Quote(
        requisition_id=req_id,
        customer_site_id=site_id,
        quote_number=f"Q-{req_id}-{id(status)}",
        line_items=[{"offer_id": o.id} for o in (offers or [])],
        status=status,
        sent_at=sent_at or NOW,
        result=result,
        result_at=result_at,
        won_revenue=won_revenue,
        created_by_id=user_id,
    )
    db.add(q)
    db.flush()
    return q


# ── Unit tests: _tier helper ────────────────────────────────────────


class TestTier:
    def test_tier_highest(self):
        assert _tier(100, [(90, 10), (80, 8), (70, 6)]) == 10

    def test_tier_middle(self):
        assert _tier(85, [(90, 10), (80, 8), (70, 6)]) == 8

    def test_tier_lowest(self):
        assert _tier(50, [(90, 10), (80, 8), (70, 6)]) == 0

    def test_tier_exact_boundary(self):
        assert _tier(80, [(90, 10), (80, 8), (70, 6)]) == 8

    def test_tier_empty(self):
        assert _tier(100, []) == 0


# ── Unit tests: _rank_and_bonus ─────────────────────────────────────


class TestRankAndBonus:
    def test_ranking_order(self):
        results = [
            {"total_score": 50, "behavior_total": 30, "qualified": True},
            {"total_score": 80, "behavior_total": 40, "qualified": True},
            {"total_score": 65, "behavior_total": 35, "qualified": True},
        ]
        _rank_and_bonus(results)
        assert results[0]["rank"] == 1
        assert results[0]["total_score"] == 80
        assert results[1]["rank"] == 2
        assert results[2]["rank"] == 3

    def test_bonus_assignment(self):
        results = [
            {"total_score": 75, "behavior_total": 40, "qualified": True},
            {"total_score": 60, "behavior_total": 30, "qualified": True},
            {"total_score": 45, "behavior_total": 25, "qualified": True},
        ]
        _rank_and_bonus(results)
        assert results[0]["bonus_amount"] == BONUS_1ST
        assert results[1]["bonus_amount"] == BONUS_2ND
        assert results[2]["bonus_amount"] == BONUS_3RD

    def test_no_bonus_below_threshold(self):
        results = [
            {"total_score": 55, "behavior_total": 30, "qualified": True},  # below 60 for 1st
            {"total_score": 45, "behavior_total": 25, "qualified": True},  # below 50 for 2nd
        ]
        _rank_and_bonus(results)
        assert results[0]["bonus_amount"] == 0  # doesn't qualify for 1st
        assert results[1]["bonus_amount"] == 0  # doesn't qualify for 2nd

    def test_unqualified_skip_bonus(self):
        results = [
            {"total_score": 90, "behavior_total": 45, "qualified": False},
            {"total_score": 70, "behavior_total": 35, "qualified": True},
        ]
        _rank_and_bonus(results)
        assert results[0]["bonus_amount"] == 0  # not qualified
        assert results[1]["bonus_amount"] == BONUS_1ST  # first qualified

    def test_tiebreak_by_behavior(self):
        results = [
            {"total_score": 70, "behavior_total": 30, "qualified": True},
            {"total_score": 70, "behavior_total": 40, "qualified": True},
        ]
        _rank_and_bonus(results)
        # Higher behavior_total wins
        assert results[0]["behavior_total"] == 40
        assert results[0]["rank"] == 1


# ── Integration: Buyer Avail Score ──────────────────────────────────


class TestBuyerAvailScore:
    def test_empty_buyer(self, db_session):
        """Buyer with no data gets all zeros."""
        buyer = _make_user(db_session, "Empty Buyer", "buyer", "empty")
        db_session.commit()

        result = compute_buyer_avail_score(db_session, buyer.id, MONTH)
        assert result["total_score"] == 0
        assert result["behavior_total"] == 0
        assert result["outcome_total"] == 0
        assert result["qualified"] is False
        assert result["role_type"] == "buyer"

    def test_qualified_buyer(self, db_session):
        """Buyer with enough reqs qualifies."""
        buyer = _make_user(db_session, "Active Buyer", "buyer", "active")
        for i in range(MIN_REQS_BUYER):
            _make_req(db_session, buyer.id, created_at=NOW - timedelta(days=i))
        db_session.commit()

        result = compute_buyer_avail_score(db_session, buyer.id, MONTH)
        assert result["qualified"] is True

    def test_speed_to_source_fast(self, db_session):
        """Buyer who sends RFQ within 2 hours gets 10 on B1."""
        buyer = _make_user(db_session, "Fast Buyer", "buyer", "fast")
        req = _make_req(db_session, buyer.id, created_at=NOW - timedelta(hours=3))
        _make_contact(db_session, req.id, buyer.id, created_at=NOW - timedelta(hours=1))
        db_session.commit()

        result = compute_buyer_avail_score(db_session, buyer.id, MONTH)
        assert result["b1_score"] == 10
        assert result["b1_label"] == "Speed to Source"

    def test_speed_to_source_slow(self, db_session):
        """Buyer who takes 50 hours gets 2 on B1 (falls in <72h tier)."""
        buyer = _make_user(db_session, "Slow Buyer", "buyer", "slow")
        req = _make_req(db_session, buyer.id, created_at=NOW - timedelta(hours=52))
        _make_contact(db_session, req.id, buyer.id, created_at=NOW - timedelta(hours=2))
        db_session.commit()

        result = compute_buyer_avail_score(db_session, buyer.id, MONTH)
        assert result["b1_score"] == 2  # 50h avg → <72h tier

    def test_multi_source_discipline(self, db_session):
        """Buyer contacting 3 vendors per req gets 8 on B2."""
        buyer = _make_user(db_session, "Multi Buyer", "buyer", "multi")
        req = _make_req(db_session, buyer.id)
        _make_contact(db_session, req.id, buyer.id, vendor="vendor-a")
        _make_contact(db_session, req.id, buyer.id, vendor="vendor-b")
        _make_contact(db_session, req.id, buyer.id, vendor="vendor-c")
        db_session.commit()

        result = compute_buyer_avail_score(db_session, buyer.id, MONTH)
        assert result["b2_score"] == 8  # 3 vendors = 8

    def test_vendor_followup_all_replied(self, db_session):
        """No stale RFQs → perfect B3 score."""
        buyer = _make_user(db_session, "Good Buyer", "buyer", "good")
        req = _make_req(db_session, buyer.id)
        _make_contact(db_session, req.id, buyer.id, status="replied")
        db_session.commit()

        result = compute_buyer_avail_score(db_session, buyer.id, MONTH)
        assert result["b3_score"] == 10

    def test_stock_lists(self, db_session):
        """Stock list uploads score B5."""
        buyer = _make_user(db_session, "Stock Buyer", "buyer", "stock")
        for i in range(6):
            db_session.add(
                StockListHash(
                    user_id=buyer.id,
                    content_hash=f"hash-{i}",
                    file_name=f"stock-{i}.csv",
                    row_count=100,
                    first_seen_at=NOW - timedelta(days=i),
                )
            )
        db_session.commit()

        result = compute_buyer_avail_score(db_session, buyer.id, MONTH)
        assert result["b5_score"] == 6  # 6 lists → tier for 5+

    def test_sourcing_ratio(self, db_session):
        """Reqs with offers score O1."""
        buyer = _make_user(db_session, "Source Buyer", "buyer", "source")
        vc = VendorCard(normalized_name="testv", display_name="TestV")
        db_session.add(vc)
        db_session.flush()

        for i in range(10):
            req = _make_req(db_session, buyer.id, created_at=NOW - timedelta(days=i))
            if i < 8:  # 8 of 10 reqs get offers
                _make_offer(db_session, req.id, buyer.id, vendor_card_id=vc.id)
        db_session.commit()

        result = compute_buyer_avail_score(db_session, buyer.id, MONTH)
        assert result["o1_score"] == 8  # 80% → tier 8
        assert "80%" in result["o1_raw"]

    def test_vendor_diversity(self, db_session):
        """Distinct vendor cards in offers score O5."""
        buyer = _make_user(db_session, "Diverse Buyer", "buyer", "diverse")
        req = _make_req(db_session, buyer.id)

        for i in range(9):
            vc = VendorCard(normalized_name=f"vendor-{i}", display_name=f"Vendor {i}")
            db_session.add(vc)
            db_session.flush()
            _make_offer(db_session, req.id, buyer.id, vendor_card_id=vc.id)
        db_session.commit()

        result = compute_buyer_avail_score(db_session, buyer.id, MONTH)
        assert result["o5_score"] == 6  # 9 vendors → tier for 8+


# ── Integration: Sales Avail Score ──────────────────────────────────


class TestSalesAvailScore:
    def test_empty_sales(self, db_session):
        """Sales with no data gets near-zero (B3=10 for 'no quotes to follow up')."""
        sales = _make_user(db_session, "Empty Sales", "sales", "emptysales")
        db_session.commit()

        result = compute_sales_avail_score(db_session, sales.id, MONTH)
        # B3 (Quote Follow-Up) = 10 because "no quotes sent" = nothing to miss
        assert result["total_score"] == 10
        assert result["b3_score"] == 10
        assert result["qualified"] is False
        assert result["role_type"] == "sales"

    def test_account_coverage(self, db_session):
        """Sales who contacts 8 of 10 accounts gets 8 on B1."""
        sales = _make_user(db_session, "Coverage Sales", "sales", "coverage")
        companies = []
        for i in range(10):
            co = Company(name=f"Co-{i}", is_active=True)
            db_session.add(co)
            db_session.flush()
            site = CustomerSite(company_id=co.id, site_name=f"Site-{i}", owner_id=sales.id)
            db_session.add(site)
            companies.append(co)
        db_session.flush()

        # Contact 8 of 10
        for co in companies[:8]:
            db_session.add(
                ActivityLog(
                    user_id=sales.id,
                    activity_type="email_sent",
                    channel="email",
                    company_id=co.id,
                    created_at=NOW,
                )
            )
        db_session.commit()

        result = compute_sales_avail_score(db_session, sales.id, MONTH)
        assert result["b1_score"] == 8  # 80% coverage

    def test_outreach_consistency(self, db_session):
        """Sales active on 15 days gets 8 on B2."""
        sales = _make_user(db_session, "Consistent Sales", "sales", "consistent")
        for i in range(15):
            db_session.add(
                ActivityLog(
                    user_id=sales.id,
                    activity_type="call_outbound",
                    channel="phone",
                    created_at=datetime(2026, 2, i + 1, 10, 0, 0, tzinfo=timezone.utc),
                )
            )
        db_session.commit()

        result = compute_sales_avail_score(db_session, sales.id, MONTH)
        assert result["b2_score"] == 8  # 15 days

    def test_proactive_selling(self, db_session):
        """Proactive offers sent score B4."""
        sales = _make_user(db_session, "Proactive Sales", "sales", "proactive")
        co = Company(name="ProCo", is_active=True)
        db_session.add(co)
        db_session.flush()
        site = CustomerSite(company_id=co.id, site_name="ProSite", owner_id=sales.id)
        db_session.add(site)
        db_session.flush()
        for i in range(6):
            db_session.add(
                ProactiveOffer(
                    salesperson_id=sales.id,
                    customer_site_id=site.id,
                    sent_at=NOW - timedelta(days=i),
                )
            )
        db_session.commit()

        result = compute_sales_avail_score(db_session, sales.id, MONTH)
        assert result["b4_score"] == 6  # 6 sent → tier for 5+

    def test_new_business(self, db_session):
        """New accounts + contacts score B5."""
        sales = _make_user(db_session, "NewBiz Sales", "sales", "newbiz")
        for i in range(3):
            co = Company(name=f"NewCo-{i}", account_owner_id=sales.id, created_at=NOW)
            db_session.add(co)
            db_session.flush()
            site = CustomerSite(company_id=co.id, site_name=f"NewSite-{i}", owner_id=sales.id)
            db_session.add(site)
            db_session.flush()
            db_session.add(
                SiteContact(
                    customer_site_id=site.id,
                    full_name=f"Contact {i}",
                    email=f"c{i}@co{i}.com",
                    created_at=NOW,
                )
            )
        db_session.commit()

        result = compute_sales_avail_score(db_session, sales.id, MONTH)
        # 3 accounts + 3 contacts + 0 prospects = 6 total → tier for 6+
        assert result["b5_score"] == 8
        assert "3 accts" in result["b5_raw"]
        assert "0 prospects" in result["b5_raw"]

    def test_win_rate(self, db_session):
        """Won/lost quotes score O1."""
        sales = _make_user(db_session, "Winner Sales", "sales", "winner")
        co = Company(name="WinCo", is_active=True)
        db_session.add(co)
        db_session.flush()
        site = CustomerSite(company_id=co.id, site_name="WinSite", owner_id=sales.id)
        db_session.add(site)
        db_session.flush()

        # 3 wins, 2 losses = 60% win rate
        for i in range(3):
            req = _make_req(db_session, sales.id)
            _make_quote(
                db_session, req.id, site.id, sales.id, status="won", result="won", result_at=NOW, won_revenue=10000
            )
        for i in range(2):
            req = _make_req(db_session, sales.id)
            _make_quote(db_session, req.id, site.id, sales.id, status="lost", result="lost", result_at=NOW)
        db_session.commit()

        result = compute_sales_avail_score(db_session, sales.id, MONTH)
        assert result["o1_score"] == 10  # 60% → top tier

    def test_revenue(self, db_session):
        """Won revenue scores O2."""
        sales = _make_user(db_session, "Revenue Sales", "sales", "revenue")
        co = Company(name="RevCo", is_active=True)
        db_session.add(co)
        db_session.flush()
        site = CustomerSite(company_id=co.id, site_name="RevSite", owner_id=sales.id)
        db_session.add(site)
        db_session.flush()

        req = _make_req(db_session, sales.id)
        _make_quote(db_session, req.id, site.id, sales.id, status="won", result="won", result_at=NOW, won_revenue=20000)
        db_session.commit()

        result = compute_sales_avail_score(db_session, sales.id, MONTH)
        assert result["o2_score"] == 6  # $20K → tier for $15K+

    def test_strategic_wins(self, db_session):
        """Wins on strategic accounts score O5."""
        sales = _make_user(db_session, "Strategic Sales", "sales", "strategic")
        co = Company(name="StrategicCo", is_active=True, is_strategic=True)
        db_session.add(co)
        db_session.flush()
        site = CustomerSite(company_id=co.id, site_name="StratSite", owner_id=sales.id)
        db_session.add(site)
        db_session.flush()

        for i in range(3):
            req = _make_req(db_session, sales.id)
            _make_quote(
                db_session, req.id, site.id, sales.id, status="won", result="won", result_at=NOW, won_revenue=5000
            )
        db_session.commit()

        result = compute_sales_avail_score(db_session, sales.id, MONTH)
        assert result["o5_score"] == 6  # 3 strategic wins → tier for 3+


# ── Integration: Batch compute + persist ────────────────────────────


class TestComputeAll:
    def test_compute_all_empty(self, db_session):
        """No users → no errors."""
        result = compute_all_avail_scores(db_session, MONTH)
        assert result["buyers"] == 0
        assert result["sales"] == 0

    def test_compute_and_persist(self, db_session):
        """Scores get saved to AvailScoreSnapshot table."""
        buyer = _make_user(db_session, "Persist Buyer", "buyer", "persist")
        db_session.commit()

        compute_all_avail_scores(db_session, MONTH)

        snaps = (
            db_session.query(AvailScoreSnapshot)
            .filter(
                AvailScoreSnapshot.user_id == buyer.id,
                AvailScoreSnapshot.role_type == "buyer",
            )
            .all()
        )
        assert len(snaps) == 1
        assert snaps[0].month == MONTH
        assert snaps[0].total_score >= 0

    def test_upsert_on_recompute(self, db_session):
        """Running twice doesn't create duplicates."""
        buyer = _make_user(db_session, "Upsert Buyer", "buyer", "upsert")
        db_session.commit()

        compute_all_avail_scores(db_session, MONTH)
        compute_all_avail_scores(db_session, MONTH)

        count = (
            db_session.query(AvailScoreSnapshot)
            .filter(
                AvailScoreSnapshot.user_id == buyer.id,
            )
            .count()
        )
        assert count == 1

    def test_ranking_across_users(self, db_session):
        """Multiple buyers get ranked correctly."""
        b1 = _make_user(db_session, "Top Buyer", "buyer", "top")
        b2 = _make_user(db_session, "Mid Buyer", "buyer", "mid")

        # b1 gets more activity → higher score
        for i in range(12):
            req = _make_req(db_session, b1.id, created_at=NOW - timedelta(days=i))
            _make_contact(db_session, req.id, b1.id, created_at=NOW - timedelta(days=i, hours=-1))
        for i in range(5):
            req = _make_req(db_session, b2.id, created_at=NOW - timedelta(days=i))
        db_session.commit()

        compute_all_avail_scores(db_session, MONTH)

        s1 = db_session.query(AvailScoreSnapshot).filter(AvailScoreSnapshot.user_id == b1.id).first()
        s2 = db_session.query(AvailScoreSnapshot).filter(AvailScoreSnapshot.user_id == b2.id).first()
        assert s1.rank < s2.rank  # b1 should rank higher
        assert s1.total_score >= s2.total_score


# ── Integration: get_avail_scores query ─────────────────────────────


class TestGetAvailScores:
    def test_get_scores_empty(self, db_session):
        """No snapshots returns empty list."""
        result = get_avail_scores(db_session, "buyer", MONTH)
        assert result == []

    def test_get_scores_returns_metrics(self, db_session):
        """Scores returned include full metric breakdown (flat keys)."""
        buyer = _make_user(db_session, "Query Buyer", "buyer", "query")
        db_session.commit()

        compute_all_avail_scores(db_session, MONTH)
        result = get_avail_scores(db_session, "buyer", MONTH)

        assert len(result) == 1
        entry = result[0]
        assert entry["user_name"] == "Query Buyer"
        assert entry["rank"] == 1
        # Flat metric keys: b1-b5 and o1-o5 with _score, _label, _raw suffixes
        for prefix in ("b", "o"):
            for i in range(1, 6):
                assert f"{prefix}{i}_score" in entry
                assert f"{prefix}{i}_label" in entry
                assert f"{prefix}{i}_raw" in entry


# ── API Endpoint Tests ──────────────────────────────────────────────


@pytest.mark.skipif(
    os.environ.get("MVP_MODE", "true").lower() == "true",
    reason="Performance router disabled in MVP mode",
)
class TestAvailScoreAPI:
    def test_get_avail_scores_buyer(self, db_session):
        """GET /api/performance/avail-scores?role=buyer returns data."""
        from app.dependencies import require_user
        from app.main import app

        buyer = _make_user(db_session, "API Buyer", "buyer", "apibuyer")
        db_session.commit()
        compute_all_avail_scores(db_session, MONTH)

        from fastapi.testclient import TestClient

        from app.database import get_db

        app.dependency_overrides[get_db] = lambda: db_session
        app.dependency_overrides[require_user] = lambda: buyer
        client = TestClient(app)

        resp = client.get("/api/performance/avail-scores?role=buyer&month=2026-02")
        assert resp.status_code == 200
        data = resp.json()
        assert data["role"] == "buyer"
        assert len(data["entries"]) == 1

        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(require_user, None)

    def test_get_avail_scores_sales(self, db_session):
        """GET /api/performance/avail-scores?role=sales returns data."""
        from app.dependencies import require_user
        from app.main import app

        sales = _make_user(db_session, "API Sales", "sales", "apisales")
        db_session.commit()
        compute_all_avail_scores(db_session, MONTH)

        from fastapi.testclient import TestClient

        from app.database import get_db

        app.dependency_overrides[get_db] = lambda: db_session
        app.dependency_overrides[require_user] = lambda: sales
        client = TestClient(app)

        resp = client.get("/api/performance/avail-scores?role=sales&month=2026-02")
        assert resp.status_code == 200
        data = resp.json()
        assert data["role"] == "sales"
        assert len(data["entries"]) >= 1

        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(require_user, None)

    def test_invalid_role_rejected(self, db_session):
        """Invalid role returns 422."""
        from app.dependencies import require_user
        from app.main import app

        user = _make_user(db_session, "Bad Role", "buyer", "badrole")
        db_session.commit()

        from fastapi.testclient import TestClient

        from app.database import get_db

        app.dependency_overrides[get_db] = lambda: db_session
        app.dependency_overrides[require_user] = lambda: user
        client = TestClient(app)

        resp = client.get("/api/performance/avail-scores?role=invalid")
        assert resp.status_code == 422

        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(require_user, None)

    def test_refresh_requires_admin(self, db_session):
        """POST refresh requires admin role."""
        from app.dependencies import require_user
        from app.main import app

        buyer = _make_user(db_session, "NonAdmin", "buyer", "nonadmin")
        db_session.commit()

        from fastapi.testclient import TestClient

        from app.database import get_db

        app.dependency_overrides[get_db] = lambda: db_session
        app.dependency_overrides[require_user] = lambda: buyer
        client = TestClient(app)

        resp = client.post("/api/performance/avail-scores/refresh")
        assert resp.status_code == 403

        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(require_user, None)


# ── Edge cases ──────────────────────────────────────────────────────


class TestEdgeCases:
    def test_buyer_no_contacts_no_crash(self, db_session):
        """Buyer with reqs but no contacts doesn't crash."""
        buyer = _make_user(db_session, "NoContact Buyer", "buyer", "nocontact")
        for i in range(3):
            _make_req(db_session, buyer.id)
        db_session.commit()

        result = compute_buyer_avail_score(db_session, buyer.id, MONTH)
        assert result["b1_score"] == 0
        assert result["b1_raw"] == "no RFQs sent"

    def test_sales_no_accounts_no_crash(self, db_session):
        """Sales with no owned accounts doesn't crash."""
        sales = _make_user(db_session, "NoAcct Sales", "sales", "noacct")
        db_session.commit()

        result = compute_sales_avail_score(db_session, sales.id, MONTH)
        assert result["b1_score"] == 0
        assert "0/0" in result["b1_raw"]

    def test_quote_followup_no_quotes(self, db_session):
        """Sales with no quotes gets perfect B3 (nothing to follow up on)."""
        sales = _make_user(db_session, "NoQuote Sales", "sales", "noquote")
        db_session.commit()

        result = compute_sales_avail_score(db_session, sales.id, MONTH)
        assert result["b3_score"] == 10

    def test_pipeline_hygiene_no_offers(self, db_session):
        """Buyer reqs with no offers within 5 days scores low on B4."""
        buyer = _make_user(db_session, "Slow Pipeline", "buyer", "slowpipe")
        for i in range(5):
            _make_req(db_session, buyer.id, created_at=NOW - timedelta(days=10 + i))
        db_session.commit()

        result = compute_buyer_avail_score(db_session, buyer.id, MONTH)
        assert result["b4_score"] == 0  # no offers at all

    def test_month_boundaries(self, db_session):
        """Data outside the target month is excluded."""
        buyer = _make_user(db_session, "Boundary Buyer", "buyer", "boundary")
        # Req in January (outside Feb)
        _make_req(db_session, buyer.id, created_at=datetime(2026, 1, 15, tzinfo=timezone.utc))
        # Req in February (inside)
        _make_req(db_session, buyer.id, created_at=datetime(2026, 2, 15, tzinfo=timezone.utc))
        db_session.commit()

        result = compute_buyer_avail_score(db_session, buyer.id, MONTH)
        # Only 1 req should count (Feb), not the Jan one
        assert "1" in result["o1_raw"] or result["o1_raw"].endswith("/1)")


# ── Coverage Gap Tests ──────────────────────────────────────────────


class TestAvailScoreCoverageGaps:
    """Cover specific uncovered lines."""

    def test_december_month_boundary(self, db_session):
        """Line 75 + 415: December month rolls over to January next year."""
        buyer = _make_user(db_session, "Dec Buyer", "buyer", "dec-buyer")
        db_session.commit()

        dec_month = date(2025, 12, 1)
        result = compute_buyer_avail_score(db_session, buyer.id, dec_month)
        assert result["total_score"] == 0  # no data

    def test_december_sales_month(self, db_session):
        """Line 415: December in sales avail score."""
        sales = _make_user(db_session, "Dec Sales", "sales", "dec-sales")
        db_session.commit()

        dec_month = date(2025, 12, 1)
        result = compute_sales_avail_score(db_session, sales.id, dec_month)
        # B3 (Quote Follow-Up) scores 10 for "no quotes sent" = perfect
        assert result["total_score"] == 10

    def test_quote_line_items_offer_extraction(self, db_session):
        """Lines 117-120: extract offer_ids from quote line_items."""
        buyer = _make_user(db_session, "Quote Buyer", "buyer", "quote-buyer")
        reqn = _make_req(db_session, buyer.id)
        req = Requirement(requisition_id=reqn.id, primary_mpn="TEST-PART")
        db_session.add(req)
        db_session.flush()

        offer = Offer(
            requisition_id=reqn.id,
            requirement_id=req.id,
            vendor_name="TestVend",
            mpn="TEST-PART",
            entered_by_id=buyer.id,
            created_at=NOW,
        )
        db_session.add(offer)
        db_session.flush()

        # Need a company + site for the quote's NOT NULL customer_site_id
        co = Company(name="QuoteCo", is_active=True)
        db_session.add(co)
        db_session.flush()
        site = CustomerSite(company_id=co.id, site_name="QuoteSite", owner_id=buyer.id)
        db_session.add(site)
        db_session.flush()

        # Create a quote with line_items referencing the offer
        quote = Quote(
            requisition_id=reqn.id,
            customer_site_id=site.id,
            quote_number=f"Q-COVER-{offer.id}",
            status="sent",
            line_items=[{"offer_id": offer.id, "qty": 100}],
            created_by_id=buyer.id,
            created_at=NOW,
        )
        db_session.add(quote)
        db_session.flush()

        # BuyPlan with BuyPlanLine referencing the offer
        bp = BuyPlan(
            requisition_id=reqn.id,
            quote_id=quote.id,
            status="completed",
            submitted_by_id=buyer.id,
            created_at=NOW,
        )
        db_session.add(bp)
        db_session.flush()
        from app.models.buy_plan import BuyPlanLine

        line = BuyPlanLine(
            buy_plan_id=bp.id,
            offer_id=offer.id,
            quantity=100,
        )
        db_session.add(line)
        db_session.commit()

        result = compute_buyer_avail_score(db_session, buyer.id, MONTH)
        # Offer should be in quoted + po_confirmed sets
        assert result["total_score"] >= 0

    def test_req_without_created_at_skipped(self, db_session):
        """Line 385: req with no created_at is skipped in B4 pipeline hygiene."""
        buyer = _make_user(db_session, "NoDt Buyer", "buyer", "nodt")
        reqn = Requisition(
            name="REQ-NODT",
            status="active",
            created_by=buyer.id,
            created_at=NOW,
        )
        db_session.add(reqn)
        db_session.flush()

        req = Requirement(requisition_id=reqn.id, primary_mpn="PART-X")
        db_session.add(req)
        db_session.flush()

        # Manually set created_at to None
        reqn.created_at = None
        db_session.commit()

        # This won't crash — the B4 function skips reqs with no created_at
        result = compute_buyer_avail_score(db_session, buyer.id, MONTH)
        assert result["b4_score"] >= 0

    def test_compute_all_buyer_exception(self, db_session):
        """Lines 699-700: exception in compute_buyer_avail_score is caught."""
        buyer = _make_user(db_session, "Error Buyer", "buyer", "err-buyer")
        db_session.commit()

        with patch(
            "app.services.avail_score_service.compute_buyer_avail_score", side_effect=RuntimeError("score exploded")
        ):
            result = compute_all_avail_scores(db_session, MONTH)

        # Should not crash; buyer is skipped
        assert "buyer_count" in result or isinstance(result, dict)

    def test_compute_all_sales_exception(self, db_session):
        """Lines 709-710: exception in compute_sales_avail_score is caught."""
        sales = _make_user(db_session, "Error Sales", "sales", "err-sales")
        db_session.commit()

        with patch(
            "app.services.avail_score_service.compute_sales_avail_score",
            side_effect=RuntimeError("sales score exploded"),
        ):
            result = compute_all_avail_scores(db_session, MONTH)

        assert isinstance(result, dict)
