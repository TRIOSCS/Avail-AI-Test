"""Tests for Multiplier Score Service — non-stacking offer points + bonus determination.

Tests the competitive scoring system: buyer pipeline progression, sales
quote/proactive points, bonus winner qualification, and API endpoints.
"""

import os

os.environ["TESTING"] = "1"
os.environ["RATE_LIMIT_ENABLED"] = "false"

from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy.orm import Session

from app.models import (
    BuyPlan,
    Company,
    Contact,
    CustomerSite,
    Offer,
    ProactiveOffer,
    Quote,
    Requisition,
    User,
    VendorCard,
)
from app.models.performance import AvailScoreSnapshot, MultiplierScoreSnapshot, StockListHash
from app.services.multiplier_score_service import (
    BONUS_1ST,
    BONUS_2ND,
    MIN_OFFERS_BUYER,
    PTS_NEW_ACCOUNT,
    PTS_OFFER_BASE,
    PTS_OFFER_BUYPLAN,
    PTS_OFFER_PO,
    PTS_OFFER_QUOTED,
    PTS_PROACTIVE_CONVERTED,
    PTS_PROACTIVE_SENT,
    PTS_QUOTE_SENT,
    PTS_QUOTE_WON,
    PTS_RFQ_SENT,
    PTS_STOCK_LIST,
    QUALIFY_SCORE_1ST,
    QUALIFY_SCORE_2ND,
    _attach_avail_scores_and_rank,
    compute_all_multiplier_scores,
    compute_buyer_multiplier,
    compute_sales_multiplier,
    determine_bonus_winners,
    get_multiplier_scores,
)
from tests.conftest import engine


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


def _make_offer(db, req_id, user_id, created_at=None):
    o = Offer(
        requisition_id=req_id,
        vendor_name="Test Vendor",
        mpn="LM317T",
        entered_by_id=user_id,
        unit_price=1.50,
        created_at=created_at or NOW,
    )
    db.add(o)
    db.flush()
    return o


def _make_quote(db, req_id, site_id, user_id, offers=None, status="sent",
                result=None, sent_at=None, result_at=None, won_revenue=None):
    q = Quote(
        requisition_id=req_id,
        customer_site_id=site_id,
        quote_number=f"Q-{req_id}-{id(status)}-{id(result)}",
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


def _make_buyplan(db, req_id, quote_id, user_id, offers=None, status="approved"):
    bp = BuyPlan(
        requisition_id=req_id,
        quote_id=quote_id,
        line_items=[{"offer_id": o.id} for o in (offers or [])],
        status=status,
        submitted_by_id=user_id,
    )
    db.add(bp)
    db.flush()
    return bp


def _make_avail_snapshot(db, user_id, role_type, total_score, month=MONTH):
    """Helper to seed an AvailScoreSnapshot for qualification checks."""
    snap = AvailScoreSnapshot(
        user_id=user_id,
        month=month,
        role_type=role_type,
        total_score=total_score,
        behavior_total=total_score / 2,
        outcome_total=total_score / 2,
    )
    db.add(snap)
    db.flush()
    return snap


# ══════════════════════════════════════════════════════════════════════
#  BUYER MULTIPLIER — NON-STACKING LOGIC
# ══════════════════════════════════════════════════════════════════════


class TestBuyerMultiplierNonStacking:
    def test_all_base_offers(self, db_session):
        """Offers with no quotes/BP get base points only (1 pt each)."""
        buyer = _make_user(db_session, "Base Buyer", "buyer", "base")
        req = _make_req(db_session, buyer.id)
        for _ in range(5):
            _make_offer(db_session, req.id, buyer.id)
        db_session.commit()

        result = compute_buyer_multiplier(db_session, buyer.id, MONTH)
        assert result["offers_total"] == 5
        assert result["offers_base_count"] == 5
        assert result["offers_base_pts"] == 5 * PTS_OFFER_BASE
        assert result["offer_points"] == 5
        assert result["offers_quoted_count"] == 0
        assert result["offers_bp_count"] == 0
        assert result["offers_po_count"] == 0

    def test_non_stacking_quoted(self, db_session):
        """Quoted offers earn 3 pts (not 1+3=4). Non-stacking."""
        buyer = _make_user(db_session, "Quoted Buyer", "buyer", "quoted")
        co = Company(name="QCo", is_active=True)
        db_session.add(co)
        db_session.flush()
        site = CustomerSite(company_id=co.id, site_name="QSite", owner_id=buyer.id)
        db_session.add(site)
        db_session.flush()

        req = _make_req(db_session, buyer.id)
        o1 = _make_offer(db_session, req.id, buyer.id)
        o2 = _make_offer(db_session, req.id, buyer.id)
        o3 = _make_offer(db_session, req.id, buyer.id)

        # Put o1 and o2 in a quote — they should get 3 pts each, not 1+3=4
        _make_quote(db_session, req.id, site.id, buyer.id, offers=[o1, o2])
        db_session.commit()

        result = compute_buyer_multiplier(db_session, buyer.id, MONTH)
        assert result["offers_total"] == 3
        assert result["offers_quoted_count"] == 2  # o1, o2
        assert result["offers_base_count"] == 1    # o3
        # Non-stacking: 2×3 + 1×1 = 7, NOT 3×1 + 2×3 = 9
        assert result["offer_points"] == 2 * PTS_OFFER_QUOTED + 1 * PTS_OFFER_BASE

    def test_non_stacking_full_pipeline(self, db_session):
        """Example from plan: 50 offers, 20 quoted, 8 BP, 3 PO confirmed."""
        buyer = _make_user(db_session, "Full Pipeline", "buyer", "fullpipe")
        co = Company(name="FPCo", is_active=True)
        db_session.add(co)
        db_session.flush()
        site = CustomerSite(company_id=co.id, site_name="FPSite", owner_id=buyer.id)
        db_session.add(site)
        db_session.flush()

        req = _make_req(db_session, buyer.id)
        offers = [_make_offer(db_session, req.id, buyer.id) for _ in range(50)]

        # 20 offers in quote
        quoted = offers[:20]
        _make_quote(db_session, req.id, site.id, buyer.id, offers=quoted)

        # 8 of the quoted offers in buy plan
        bp_offers = quoted[:8]
        q = db_session.query(Quote).first()
        bp = _make_buyplan(db_session, req.id, q.id, buyer.id, offers=bp_offers, status="approved")

        # 3 of the BP offers reach PO confirmed
        po_offers = bp_offers[:3]
        bp2 = _make_buyplan(db_session, req.id, q.id, buyer.id, offers=po_offers, status="po_confirmed")
        db_session.commit()

        result = compute_buyer_multiplier(db_session, buyer.id, MONTH)
        assert result["offers_total"] == 50
        assert result["offers_po_count"] == 3
        assert result["offers_bp_count"] == 5    # 8 - 3 = 5 (BP but not PO)
        assert result["offers_quoted_count"] == 12  # 20 - 8 = 12 (quoted but not BP)
        assert result["offers_base_count"] == 30  # 50 - 20 = 30

        expected = (30 * PTS_OFFER_BASE + 12 * PTS_OFFER_QUOTED
                    + 5 * PTS_OFFER_BUYPLAN + 3 * PTS_OFFER_PO)
        assert result["offer_points"] == expected

    def test_rfq_bonus_points(self, db_session):
        """RFQ emails sent earn 0.25 pts each."""
        buyer = _make_user(db_session, "RFQ Buyer", "buyer", "rfq")
        req = _make_req(db_session, buyer.id)
        for i in range(10):
            db_session.add(Contact(
                requisition_id=req.id,
                user_id=buyer.id,
                contact_type="email",
                vendor_name=f"Vendor-{i}",
                vendor_name_normalized=f"vendor-{i}",
                status="sent",
                created_at=NOW,
            ))
        db_session.commit()

        result = compute_buyer_multiplier(db_session, buyer.id, MONTH)
        assert result["rfqs_sent_count"] == 10
        assert result["rfqs_sent_pts"] == 10 * PTS_RFQ_SENT
        assert result["bonus_points"] == 10 * PTS_RFQ_SENT

    def test_stock_list_bonus_points(self, db_session):
        """Stock list uploads earn 2 pts each."""
        buyer = _make_user(db_session, "Stock Buyer", "buyer", "stockmult")
        for i in range(3):
            db_session.add(StockListHash(
                user_id=buyer.id,
                content_hash=f"hash-mult-{i}",
                file_name=f"stock-{i}.csv",
                row_count=100,
                first_seen_at=NOW,
            ))
        db_session.commit()

        result = compute_buyer_multiplier(db_session, buyer.id, MONTH)
        assert result["stock_lists_count"] == 3
        assert result["stock_lists_pts"] == 3 * PTS_STOCK_LIST

    def test_grace_period_offers(self, db_session):
        """Offers from last 7 days of prev month count if they advanced."""
        buyer = _make_user(db_session, "Grace Buyer", "buyer", "grace")
        co = Company(name="GCo", is_active=True)
        db_session.add(co)
        db_session.flush()
        site = CustomerSite(company_id=co.id, site_name="GSite", owner_id=buyer.id)
        db_session.add(site)
        db_session.flush()

        req = _make_req(db_session, buyer.id)
        # Offer from Jan 28 (within 7-day grace window)
        grace_offer = _make_offer(db_session, req.id, buyer.id,
                                  created_at=datetime(2026, 1, 28, tzinfo=timezone.utc))
        # Offer from Jan 20 (outside grace window)
        old_offer = _make_offer(db_session, req.id, buyer.id,
                                created_at=datetime(2026, 1, 20, tzinfo=timezone.utc))
        # Put grace_offer in a quote (it advanced)
        _make_quote(db_session, req.id, site.id, buyer.id, offers=[grace_offer])
        db_session.commit()

        result = compute_buyer_multiplier(db_session, buyer.id, MONTH)
        # grace_offer should count (advanced to quote), old_offer should not
        assert result["offers_total"] == 1
        assert result["offers_quoted_count"] == 1

    def test_empty_buyer(self, db_session):
        """Buyer with no data gets all zeros."""
        buyer = _make_user(db_session, "Empty Buyer", "buyer", "emptymult")
        db_session.commit()

        result = compute_buyer_multiplier(db_session, buyer.id, MONTH)
        assert result["total_points"] == 0
        assert result["offer_points"] == 0
        assert result["bonus_points"] == 0


# ══════════════════════════════════════════════════════════════════════
#  SALES MULTIPLIER — NON-STACKING LOGIC
# ══════════════════════════════════════════════════════════════════════


class TestSalesMultiplier:
    def test_quotes_sent_only(self, db_session):
        """Quotes sent but not won earn 2 pts each."""
        sales = _make_user(db_session, "Sent Sales", "sales", "sentsales")
        co = Company(name="SCo", is_active=True)
        db_session.add(co)
        db_session.flush()
        site = CustomerSite(company_id=co.id, site_name="SSite", owner_id=sales.id)
        db_session.add(site)
        db_session.flush()

        for i in range(5):
            req = _make_req(db_session, sales.id)
            _make_quote(db_session, req.id, site.id, sales.id, status="sent")
        db_session.commit()

        result = compute_sales_multiplier(db_session, sales.id, MONTH)
        assert result["quotes_sent_count"] == 5
        assert result["quotes_sent_pts"] == 5 * PTS_QUOTE_SENT
        assert result["quotes_won_count"] == 0

    def test_non_stacking_quotes_won(self, db_session):
        """Won quotes earn 8 pts (not 2+8=10). Non-stacking."""
        sales = _make_user(db_session, "Won Sales", "sales", "wonsales")
        co = Company(name="WCo", is_active=True)
        db_session.add(co)
        db_session.flush()
        site = CustomerSite(company_id=co.id, site_name="WSite", owner_id=sales.id)
        db_session.add(site)
        db_session.flush()

        # 5 quotes sent, 2 won
        for i in range(3):
            req = _make_req(db_session, sales.id)
            _make_quote(db_session, req.id, site.id, sales.id, status="sent")
        for i in range(2):
            req = _make_req(db_session, sales.id)
            _make_quote(db_session, req.id, site.id, sales.id, status="won",
                        result="won", result_at=NOW, won_revenue=5000)
        db_session.commit()

        result = compute_sales_multiplier(db_session, sales.id, MONTH)
        assert result["quotes_won_count"] == 2
        assert result["quotes_sent_count"] == 3  # 5 total - 2 won = 3 sent-only
        # Non-stacking: 3×2 + 2×8 = 22, NOT 5×2 + 2×8 = 26
        expected = 3 * PTS_QUOTE_SENT + 2 * PTS_QUOTE_WON
        assert result["offer_points"] == expected

    def test_proactive_non_stacking(self, db_session):
        """Converted proactive offers earn 4 pts (not 1+4=5)."""
        sales = _make_user(db_session, "Proactive Sales", "sales", "proactivemult")
        co = Company(name="PCo", is_active=True)
        db_session.add(co)
        db_session.flush()
        site = CustomerSite(company_id=co.id, site_name="PSite", owner_id=sales.id)
        db_session.add(site)
        db_session.flush()

        # 5 proactive offers, 2 converted
        for i in range(5):
            status = "converted" if i < 2 else "sent"
            db_session.add(ProactiveOffer(
                salesperson_id=sales.id,
                customer_site_id=site.id,
                sent_at=NOW,
                status=status,
                converted_at=NOW if status == "converted" else None,
            ))
        db_session.commit()

        result = compute_sales_multiplier(db_session, sales.id, MONTH)
        assert result["proactive_converted_count"] == 2
        assert result["proactive_sent_count"] == 3
        expected = 3 * PTS_PROACTIVE_SENT + 2 * PTS_PROACTIVE_CONVERTED
        assert result["proactive_sent_pts"] + result["proactive_converted_pts"] == expected

    def test_new_accounts(self, db_session):
        """New accounts earn 3 pts each."""
        sales = _make_user(db_session, "NewAcct Sales", "sales", "newacctmult")
        for i in range(4):
            db_session.add(Company(
                name=f"NewCo-mult-{i}",
                account_owner_id=sales.id,
                created_at=NOW,
            ))
        db_session.commit()

        result = compute_sales_multiplier(db_session, sales.id, MONTH)
        assert result["new_accounts_count"] == 4
        assert result["new_accounts_pts"] == 4 * PTS_NEW_ACCOUNT
        assert result["bonus_points"] == 4 * PTS_NEW_ACCOUNT

    def test_empty_sales(self, db_session):
        """Sales with no data gets all zeros."""
        sales = _make_user(db_session, "Empty Sales", "sales", "emptysalesmult")
        db_session.commit()

        result = compute_sales_multiplier(db_session, sales.id, MONTH)
        assert result["total_points"] == 0


# ══════════════════════════════════════════════════════════════════════
#  RANKING + BONUS QUALIFICATION
# ══════════════════════════════════════════════════════════════════════


class TestRankingAndBonus:
    def test_ranking_by_points(self, db_session):
        """Users ranked by total_points descending."""
        results = [
            {"user_id": 1, "total_points": 30, "offers_total": 15},
            {"user_id": 2, "total_points": 80, "offers_total": 40},
            {"user_id": 3, "total_points": 50, "offers_total": 25},
        ]

        # Seed avail scores for qualification
        b1 = _make_user(db_session, "R1", "buyer", "r1")
        b2 = _make_user(db_session, "R2", "buyer", "r2")
        b3 = _make_user(db_session, "R3", "buyer", "r3")
        results[0]["user_id"] = b1.id
        results[1]["user_id"] = b2.id
        results[2]["user_id"] = b3.id
        _make_avail_snapshot(db_session, b1.id, "buyer", 70)
        _make_avail_snapshot(db_session, b2.id, "buyer", 80)
        _make_avail_snapshot(db_session, b3.id, "buyer", 60)
        db_session.commit()

        _attach_avail_scores_and_rank(db_session, results, MONTH, "buyer")
        assert results[0]["user_id"] == b2.id  # 80 pts
        assert results[0]["rank"] == 1
        assert results[1]["user_id"] == b3.id  # 50 pts
        assert results[1]["rank"] == 2

    def test_bonus_requires_avail_score(self, db_session):
        """1st place needs avail score >=60, 2nd needs >=50."""
        b1 = _make_user(db_session, "High", "buyer", "high")
        b2 = _make_user(db_session, "Low", "buyer", "low")
        _make_avail_snapshot(db_session, b1.id, "buyer", 45)  # below 50
        _make_avail_snapshot(db_session, b2.id, "buyer", 55)  # 50-60 range
        db_session.commit()

        results = [
            {"user_id": b1.id, "total_points": 100, "offers_total": 20},
            {"user_id": b2.id, "total_points": 80, "offers_total": 15},
        ]
        _attach_avail_scores_and_rank(db_session, results, MONTH, "buyer")

        # b1: not qualified (avail<50), b2: qualified for 2nd but not 1st (50<=avail<60)
        assert results[0]["bonus_amount"] == 0  # b1 has most points but avail too low
        assert results[1]["bonus_amount"] == 0  # b2 qualified but can't get 1st (avail<60)

    def test_bonus_full_qualification(self, db_session):
        """Two qualified users with sufficient scores get $500 and $250."""
        b1 = _make_user(db_session, "First", "buyer", "first")
        b2 = _make_user(db_session, "Second", "buyer", "second")
        _make_avail_snapshot(db_session, b1.id, "buyer", 75)
        _make_avail_snapshot(db_session, b2.id, "buyer", 55)
        db_session.commit()

        results = [
            {"user_id": b1.id, "total_points": 100, "offers_total": 20},
            {"user_id": b2.id, "total_points": 80, "offers_total": 15},
        ]
        _attach_avail_scores_and_rank(db_session, results, MONTH, "buyer")

        assert results[0]["bonus_amount"] == BONUS_1ST
        assert results[1]["bonus_amount"] == BONUS_2ND

    def test_tiebreak_by_avail_score(self, db_session):
        """Equal points → higher avail score wins."""
        b1 = _make_user(db_session, "Tie1", "buyer", "tie1")
        b2 = _make_user(db_session, "Tie2", "buyer", "tie2")
        _make_avail_snapshot(db_session, b1.id, "buyer", 60)
        _make_avail_snapshot(db_session, b2.id, "buyer", 80)
        db_session.commit()

        results = [
            {"user_id": b1.id, "total_points": 50, "offers_total": 20},
            {"user_id": b2.id, "total_points": 50, "offers_total": 20},
        ]
        _attach_avail_scores_and_rank(db_session, results, MONTH, "buyer")

        # b2 should rank first (same points, higher avail score)
        assert results[0]["user_id"] == b2.id
        assert results[0]["rank"] == 1

    def test_nobody_qualifies(self, db_session):
        """No bonus if nobody meets thresholds."""
        b1 = _make_user(db_session, "Unq1", "buyer", "unq1")
        _make_avail_snapshot(db_session, b1.id, "buyer", 30)
        db_session.commit()

        results = [{"user_id": b1.id, "total_points": 100, "offers_total": 20}]
        _attach_avail_scores_and_rank(db_session, results, MONTH, "buyer")
        assert results[0]["bonus_amount"] == 0

    def test_min_offers_required(self, db_session):
        """Buyer needs >=10 offers to qualify even with high avail score."""
        b1 = _make_user(db_session, "FewOffers", "buyer", "fewoffers")
        _make_avail_snapshot(db_session, b1.id, "buyer", 90)
        db_session.commit()

        results = [{"user_id": b1.id, "total_points": 100, "offers_total": 5}]
        _attach_avail_scores_and_rank(db_session, results, MONTH, "buyer")
        assert results[0]["qualified"] is False
        assert results[0]["bonus_amount"] == 0


# ══════════════════════════════════════════════════════════════════════
#  BATCH COMPUTE + PERSIST
# ══════════════════════════════════════════════════════════════════════


class TestComputeAll:
    def test_compute_all_empty(self, db_session):
        """No users → no errors."""
        result = compute_all_multiplier_scores(db_session, MONTH)
        assert result["buyers"] == 0
        assert result["sales"] == 0

    def test_compute_and_persist(self, db_session):
        """Scores get saved to MultiplierScoreSnapshot table."""
        buyer = _make_user(db_session, "Persist Buyer", "buyer", "persistmult")
        req = _make_req(db_session, buyer.id)
        _make_offer(db_session, req.id, buyer.id)
        db_session.commit()

        compute_all_multiplier_scores(db_session, MONTH)

        snaps = db_session.query(MultiplierScoreSnapshot).filter(
            MultiplierScoreSnapshot.user_id == buyer.id,
            MultiplierScoreSnapshot.role_type == "buyer",
        ).all()
        assert len(snaps) == 1
        assert snaps[0].total_points >= 0
        assert snaps[0].offers_base_count is not None

    def test_upsert_on_recompute(self, db_session):
        """Running twice doesn't create duplicates."""
        buyer = _make_user(db_session, "Upsert Buyer", "buyer", "upsertmult")
        db_session.commit()

        compute_all_multiplier_scores(db_session, MONTH)
        compute_all_multiplier_scores(db_session, MONTH)

        count = db_session.query(MultiplierScoreSnapshot).filter(
            MultiplierScoreSnapshot.user_id == buyer.id,
        ).count()
        assert count == 1


# ══════════════════════════════════════════════════════════════════════
#  BONUS WINNER QUERY
# ══════════════════════════════════════════════════════════════════════


class TestBonusWinners:
    def test_winners_from_persisted(self, db_session):
        """determine_bonus_winners reads from persisted snapshots."""
        b1 = _make_user(db_session, "Winner1", "buyer", "winner1")
        b2 = _make_user(db_session, "Winner2", "buyer", "winner2")

        # Seed avail scores
        _make_avail_snapshot(db_session, b1.id, "buyer", 75)
        _make_avail_snapshot(db_session, b2.id, "buyer", 55)

        # Seed multiplier snapshots directly
        db_session.add(MultiplierScoreSnapshot(
            user_id=b1.id, month=MONTH, role_type="buyer",
            total_points=100, offer_points=90, bonus_points=10,
            avail_score=75, qualified=True, rank=1, bonus_amount=BONUS_1ST,
        ))
        db_session.add(MultiplierScoreSnapshot(
            user_id=b2.id, month=MONTH, role_type="buyer",
            total_points=60, offer_points=50, bonus_points=10,
            avail_score=55, qualified=True, rank=2, bonus_amount=BONUS_2ND,
        ))
        db_session.commit()

        winners = determine_bonus_winners(db_session, "buyer", MONTH)
        assert len(winners) == 2
        assert winners[0]["bonus_amount"] == BONUS_1ST
        assert winners[0]["user_name"] == "Winner1"
        assert winners[1]["bonus_amount"] == BONUS_2ND

    def test_no_winners(self, db_session):
        """No qualified users → empty winners list."""
        winners = determine_bonus_winners(db_session, "buyer", MONTH)
        assert winners == []


# ══════════════════════════════════════════════════════════════════════
#  API QUERY
# ══════════════════════════════════════════════════════════════════════


class TestGetMultiplierScores:
    def test_get_scores_empty(self, db_session):
        """No snapshots returns empty list."""
        result = get_multiplier_scores(db_session, "buyer", MONTH)
        assert result == []

    def test_get_scores_returns_breakdown(self, db_session):
        """Scores returned include role-specific breakdown."""
        buyer = _make_user(db_session, "Query Buyer", "buyer", "querymult")
        req = _make_req(db_session, buyer.id)
        _make_offer(db_session, req.id, buyer.id)
        db_session.commit()

        compute_all_multiplier_scores(db_session, MONTH)
        result = get_multiplier_scores(db_session, "buyer", MONTH)

        assert len(result) == 1
        entry = result[0]
        assert entry["user_name"] == "Query Buyer"
        assert "breakdown" in entry
        assert "offers_total" in entry["breakdown"]


# ══════════════════════════════════════════════════════════════════════
#  API ENDPOINT TESTS
# ══════════════════════════════════════════════════════════════════════


class TestMultiplierAPI:
    def test_get_multiplier_scores_buyer(self, db_session):
        """GET /api/performance/multiplier-scores?role=buyer returns data."""
        from app.database import get_db
        from app.dependencies import require_user
        from app.main import app
        from fastapi.testclient import TestClient

        buyer = _make_user(db_session, "API Buyer", "buyer", "apibuyermult")
        db_session.commit()
        compute_all_multiplier_scores(db_session, MONTH)

        app.dependency_overrides[get_db] = lambda: db_session
        app.dependency_overrides[require_user] = lambda: buyer
        client = TestClient(app)

        resp = client.get("/api/performance/multiplier-scores?role=buyer&month=2026-02")
        assert resp.status_code == 200
        data = resp.json()
        assert data["role"] == "buyer"
        assert len(data["entries"]) >= 1

        app.dependency_overrides.clear()

    def test_get_bonus_winners(self, db_session):
        """GET /api/performance/bonus-winners returns data."""
        from app.database import get_db
        from app.dependencies import require_user
        from app.main import app
        from fastapi.testclient import TestClient

        buyer = _make_user(db_session, "BW Buyer", "buyer", "bwbuyer")
        db_session.commit()

        app.dependency_overrides[get_db] = lambda: db_session
        app.dependency_overrides[require_user] = lambda: buyer
        client = TestClient(app)

        resp = client.get("/api/performance/bonus-winners?role=buyer&month=2026-02")
        assert resp.status_code == 200
        data = resp.json()
        assert "winners" in data

        app.dependency_overrides.clear()

    def test_invalid_role_rejected(self, db_session):
        """Invalid role returns 422."""
        from app.database import get_db
        from app.dependencies import require_user
        from app.main import app
        from fastapi.testclient import TestClient

        user = _make_user(db_session, "Bad Role", "buyer", "badrolemult")
        db_session.commit()

        app.dependency_overrides[get_db] = lambda: db_session
        app.dependency_overrides[require_user] = lambda: user
        client = TestClient(app)

        resp = client.get("/api/performance/multiplier-scores?role=invalid")
        assert resp.status_code == 422

        app.dependency_overrides.clear()

    def test_refresh_requires_admin(self, db_session):
        """POST refresh requires admin role."""
        from app.database import get_db
        from app.dependencies import require_user
        from app.main import app
        from fastapi.testclient import TestClient

        buyer = _make_user(db_session, "NonAdmin", "buyer", "nonadminmult")
        db_session.commit()

        app.dependency_overrides[get_db] = lambda: db_session
        app.dependency_overrides[require_user] = lambda: buyer
        client = TestClient(app)

        resp = client.post("/api/performance/multiplier-scores/refresh")
        assert resp.status_code == 403

        app.dependency_overrides.clear()


# ══════════════════════════════════════════════════════════════════════
#  EDGE CASES
# ══════════════════════════════════════════════════════════════════════


class TestEdgeCases:
    def test_offer_in_multiple_quotes_highest_tier_wins(self, db_session):
        """Same offer in multiple quotes → still only counts once at highest tier."""
        buyer = _make_user(db_session, "MultiQ Buyer", "buyer", "multiq")
        co = Company(name="MQCo", is_active=True)
        db_session.add(co)
        db_session.flush()
        site = CustomerSite(company_id=co.id, site_name="MQSite", owner_id=buyer.id)
        db_session.add(site)
        db_session.flush()

        req = _make_req(db_session, buyer.id)
        offer = _make_offer(db_session, req.id, buyer.id)

        # Same offer in two quotes
        _make_quote(db_session, req.id, site.id, buyer.id, offers=[offer], status="sent")
        req2 = _make_req(db_session, buyer.id)
        _make_quote(db_session, req2.id, site.id, buyer.id, offers=[offer], status="won",
                    result="won", result_at=NOW)
        db_session.commit()

        result = compute_buyer_multiplier(db_session, buyer.id, MONTH)
        # Only 1 offer total — should count at quoted tier (highest)
        assert result["offers_total"] == 1
        assert result["offers_quoted_count"] == 1
        assert result["offers_base_count"] == 0

    def test_month_boundary_exclusion(self, db_session):
        """Offers outside the target month are excluded."""
        buyer = _make_user(db_session, "Boundary Buyer", "buyer", "boundarymult")
        req = _make_req(db_session, buyer.id)
        # Jan offer (outside Feb)
        _make_offer(db_session, req.id, buyer.id,
                    created_at=datetime(2026, 1, 10, tzinfo=timezone.utc))
        # Feb offer (inside)
        _make_offer(db_session, req.id, buyer.id,
                    created_at=datetime(2026, 2, 10, tzinfo=timezone.utc))
        db_session.commit()

        result = compute_buyer_multiplier(db_session, buyer.id, MONTH)
        # Only the Feb offer counts (Jan one is outside grace period too)
        assert result["offers_total"] == 1
