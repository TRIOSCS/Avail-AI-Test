"""test_buyer_leaderboard.py — Tests for buyer_leaderboard.py.

Covers: compute_buyer_leaderboard — no-buyers, single buyer with offers,
        grace period logic, stock upload counting, ranking, snapshot upsert.

Called by: pytest
Depends on: app/services/buyer_leaderboard.py, tests/conftest.py
"""

from datetime import date, datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.models import BuyerLeaderboardSnapshot, Offer, StockListHash, User
from app.services.buyer_leaderboard import (
    PTS_LOGGED,
    PTS_STOCK_LIST,
    compute_buyer_leaderboard,
)
from tests.conftest import engine  # noqa: F401

# ── Helpers ──────────────────────────────────────────────────────────────


def _make_buyer(db: Session, email: str = "buyer@trioscs.com", role: str = "buyer") -> User:
    u = User(
        email=email,
        name=email.split("@")[0],
        role=role,
        azure_id=f"az-{email}",
        created_at=datetime.now(timezone.utc),
    )
    db.add(u)
    db.flush()
    return u


def _make_requisition(db: Session, user: User):
    from app.models import Requisition

    r = Requisition(
        name="LB-REQ",
        customer_name="Test Customer",
        status="open",
        created_by=user.id,
        created_at=datetime.now(timezone.utc),
    )
    db.add(r)
    db.flush()
    return r


def _make_offer(db: Session, user: User, created_at: datetime) -> Offer:
    req = _make_requisition(db, user)
    o = Offer(
        requisition_id=req.id,
        vendor_name="Test Vendor",
        mpn="LM317T",
        qty_available=1000,
        unit_price=0.5,
        entered_by_id=user.id,
        created_at=created_at,
    )
    db.add(o)
    db.flush()
    return o


def _make_stock_hash(db: Session, user: User, first_seen_at: datetime) -> StockListHash:
    slh = StockListHash(
        user_id=user.id,
        content_hash=f"hash-{user.id}-{first_seen_at.timestamp()}",
        first_seen_at=first_seen_at,
        last_seen_at=first_seen_at,
    )
    db.add(slh)
    db.flush()
    return slh


# ── Tests ─────────────────────────────────────────────────────────────────


class TestComputeBuyerLeaderboardNoBuyers:
    def test_no_buyers_returns_empty_entries(self, db_session: Session):
        """No buyer/trader users → returns entries=0."""
        result = compute_buyer_leaderboard(db_session, date(2026, 1, 1))
        assert result["entries"] == 0
        assert result["month"] == "2026-01-01"

    def test_normalizes_to_first_of_month(self, db_session: Session):
        """Any day in the month normalizes to the 1st."""
        result = compute_buyer_leaderboard(db_session, date(2026, 5, 17))
        assert result["month"] == "2026-05-01"

    def test_december_month_end_correct(self, db_session: Session):
        """December month_end should wrap to January of next year (no crash)."""
        result = compute_buyer_leaderboard(db_session, date(2025, 12, 1))
        assert result["month"] == "2025-12-01"


class TestComputeBuyerLeaderboardSingleBuyer:
    def test_buyer_with_no_offers_gets_zero_points(self, db_session: Session):
        _make_buyer(db_session, "buyer@trioscs.com")
        db_session.commit()

        result = compute_buyer_leaderboard(db_session, date(2026, 5, 1))
        assert result["entries"] == 1

        snap = db_session.query(BuyerLeaderboardSnapshot).filter_by(month=date(2026, 5, 1)).first()
        assert snap is not None
        assert snap.total_points == 0
        assert snap.rank == 1

    def test_buyer_with_month_offers_earns_pts_logged(self, db_session: Session):
        buyer = _make_buyer(db_session, "buyer_offers@trioscs.com")
        month_start = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
        _make_offer(db_session, buyer, month_start)
        _make_offer(db_session, buyer, month_start + timedelta(days=5))
        db_session.commit()

        compute_buyer_leaderboard(db_session, date(2026, 5, 1))

        snap = db_session.query(BuyerLeaderboardSnapshot).filter_by(month=date(2026, 5, 1)).first()
        assert snap.offers_logged == 2
        assert snap.points_offers == 2 * PTS_LOGGED

    def test_stock_list_uploads_earn_pts_stock(self, db_session: Session):
        buyer = _make_buyer(db_session, "stock_buyer@trioscs.com")
        may_start = datetime(2026, 5, 1, tzinfo=timezone.utc)
        _make_stock_hash(db_session, buyer, may_start + timedelta(days=1))
        _make_stock_hash(db_session, buyer, may_start + timedelta(days=5))
        db_session.commit()

        compute_buyer_leaderboard(db_session, date(2026, 5, 1))

        snap = db_session.query(BuyerLeaderboardSnapshot).filter_by(month=date(2026, 5, 1)).first()
        assert snap.stock_lists_uploaded == 2
        assert snap.points_stock == 2 * PTS_STOCK_LIST

    def test_stock_outside_month_not_counted(self, db_session: Session):
        buyer = _make_buyer(db_session, "stock_out@trioscs.com")
        # April stock (before May)
        _make_stock_hash(db_session, buyer, datetime(2026, 4, 20, tzinfo=timezone.utc))
        # June stock (after May)
        _make_stock_hash(db_session, buyer, datetime(2026, 6, 1, tzinfo=timezone.utc))
        db_session.commit()

        compute_buyer_leaderboard(db_session, date(2026, 5, 1))

        snap = db_session.query(BuyerLeaderboardSnapshot).filter_by(month=date(2026, 5, 1)).first()
        assert snap.stock_lists_uploaded == 0


class TestComputeBuyerLeaderboardGracePeriod:
    def test_grace_offers_not_advanced_are_excluded(self, db_session: Session):
        """Offers from grace window that never advanced don't count."""
        buyer = _make_buyer(db_session, "grace_buyer@trioscs.com")
        # Offer created in last 7 days of April (grace window for May)
        grace_time = datetime(2026, 4, 28, 12, 0, tzinfo=timezone.utc)
        _make_offer(db_session, buyer, grace_time)
        db_session.commit()

        compute_buyer_leaderboard(db_session, date(2026, 5, 1))

        snap = db_session.query(BuyerLeaderboardSnapshot).filter_by(month=date(2026, 5, 1)).first()
        # Grace offer didn't advance (not in quote/buyplan) → excluded
        assert snap.offers_logged == 0


class TestComputeBuyerLeaderboardRanking:
    def test_two_buyers_ranked_by_points(self, db_session: Session):
        b1 = _make_buyer(db_session, "high_scorer@trioscs.com")
        b2 = _make_buyer(db_session, "low_scorer@trioscs.com")
        may_start = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)

        # b1 has 3 offers, b2 has 1
        for _ in range(3):
            _make_offer(db_session, b1, may_start)
        _make_offer(db_session, b2, may_start)
        db_session.commit()

        compute_buyer_leaderboard(db_session, date(2026, 5, 1))

        snap1 = db_session.query(BuyerLeaderboardSnapshot).filter_by(user_id=b1.id, month=date(2026, 5, 1)).first()
        snap2 = db_session.query(BuyerLeaderboardSnapshot).filter_by(user_id=b2.id, month=date(2026, 5, 1)).first()
        assert snap1.rank < snap2.rank  # Higher points → lower rank number


class TestComputeBuyerLeaderboardUpsert:
    def test_second_run_updates_existing_snapshot(self, db_session: Session):
        """Running the leaderboard twice upserts the snapshot (not duplicates)."""
        buyer = _make_buyer(db_session, "upsert_buyer@trioscs.com")
        db_session.commit()

        compute_buyer_leaderboard(db_session, date(2026, 5, 1))
        compute_buyer_leaderboard(db_session, date(2026, 5, 1))

        # Should still be exactly 1 snapshot
        count = db_session.query(BuyerLeaderboardSnapshot).filter_by(user_id=buyer.id, month=date(2026, 5, 1)).count()
        assert count == 1

    def test_trader_role_included(self, db_session: Session):
        """Traders also appear in the leaderboard."""
        _make_buyer(db_session, "trader@trioscs.com", role="trader")
        db_session.commit()

        result = compute_buyer_leaderboard(db_session, date(2026, 5, 1))
        assert result["entries"] == 1


class TestComputeBuyerLeaderboardQuoteAndBuyPlan:
    def test_offer_in_quote_earns_pts_quoted(self, db_session: Session):
        """Lines 54-57: Quote with line_items containing offer_id → quoted_offer_ids populated."""
        from app.models import Quote

        buyer = _make_buyer(db_session, "quote_buyer@trioscs.com")

        # Create a requisition for the offer and quote
        req = _make_requisition(db_session, buyer)

        # Create an offer in May
        may_start = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
        offer = _make_offer(db_session, buyer, may_start)
        offer.requisition_id = req.id
        db_session.flush()

        # Create a Quote with line_items referencing the offer
        quote = Quote(
            requisition_id=req.id,
            quote_number="QT-LB-001",
            line_items=[{"offer_id": offer.id, "mpn": "LM317T", "qty": 100}],
            status="sent",
        )
        db_session.add(quote)
        db_session.commit()

        compute_buyer_leaderboard(db_session, date(2026, 5, 1))

        snap = db_session.query(BuyerLeaderboardSnapshot).filter_by(user_id=buyer.id, month=date(2026, 5, 1)).first()
        assert snap is not None
        # offer_id is in quoted_offer_ids → quoted count >= 1
        assert snap.offers_quoted >= 1
        assert snap.points_quoted >= 3  # PTS_QUOTED = 3

    def test_offer_in_completed_buyplan_earns_pts_po(self, db_session: Session):
        """Lines 69-71: BuyPlan with completed status + BuyPlanLine → po_confirmed_offer_ids."""
        from app.models import BuyPlan, BuyPlanLine, Quote

        buyer = _make_buyer(db_session, "po_buyer@trioscs.com")
        req = _make_requisition(db_session, buyer)

        may_start = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
        offer = _make_offer(db_session, buyer, may_start)
        offer.requisition_id = req.id
        db_session.flush()

        # Need a Quote for BuyPlan FK
        quote = Quote(
            requisition_id=req.id,
            quote_number="QT-LB-002",
            line_items=[],
            status="won",
        )
        db_session.add(quote)
        db_session.flush()

        # Create a completed BuyPlan
        bp = BuyPlan(
            quote_id=quote.id,
            requisition_id=req.id,
            status="completed",
        )
        db_session.add(bp)
        db_session.flush()

        # Create a BuyPlanLine linking the offer
        bpl = BuyPlanLine(
            buy_plan_id=bp.id,
            offer_id=offer.id,
            quantity=100,
            unit_cost=0.5,
        )
        db_session.add(bpl)
        db_session.commit()

        compute_buyer_leaderboard(db_session, date(2026, 5, 1))

        snap = db_session.query(BuyerLeaderboardSnapshot).filter_by(user_id=buyer.id, month=date(2026, 5, 1)).first()
        assert snap is not None
        # offer is in po_confirmed_offer_ids → po_confirmed >= 1
        assert snap.offers_po_confirmed >= 1
        assert snap.points_po >= 8  # PTS_PO_CONFIRMED = 8
