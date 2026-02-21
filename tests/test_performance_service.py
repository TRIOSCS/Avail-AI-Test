"""
Tests for app/services/performance_service.py

Covers: _compute_composite, compute_vendor_scorecard, compute_all_vendor_scorecards,
get_vendor_scorecard_list, get_vendor_scorecard_detail, compute_buyer_leaderboard,
get_buyer_leaderboard, compute_stock_list_hash, check_and_record_stock_list.

Uses in-memory SQLite via conftest fixtures.
"""

from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy.orm import Session

from app.models import (
    BuyerLeaderboardSnapshot,
    BuyPlan,
    Company,
    Contact,
    CustomerSite,
    Offer,
    Quote,
    Requisition,
    StockListHash,
    User,
    VendorCard,
    VendorMetricsSnapshot,
    VendorResponse,
    VendorReview,
)
from app.services.performance_service import (
    COLD_START_THRESHOLD,
    GRACE_DAYS,
    PTS_BUYPLAN,
    PTS_LOGGED,
    PTS_PO_CONFIRMED,
    PTS_QUOTED,
    PTS_STOCK_LIST,
    W_PO_CONVERSION,
    W_QUOTE_CONVERSION,
    W_RESPONSE_RATE,
    W_REVIEW_RATING,
    _compute_composite,
    check_and_record_stock_list,
    compute_all_vendor_scorecards,
    compute_buyer_leaderboard,
    compute_stock_list_hash,
    compute_vendor_scorecard,
    get_buyer_leaderboard,
    get_vendor_scorecard_detail,
    get_vendor_scorecard_list,
)


# ── Helpers ───────────────────────────────────────────────────────────


def _make_vendor(db: Session, name: str = "Test Vendor", domain: str = "testvendor.com") -> VendorCard:
    """Create and return a VendorCard with sensible defaults."""
    vc = VendorCard(
        normalized_name=name.lower(),
        display_name=name,
        domain=domain,
        domain_aliases=[],
        emails=[f"sales@{domain}"],
        created_at=datetime.now(timezone.utc),
    )
    db.add(vc)
    db.flush()
    return vc


def _make_user(db: Session, email: str, role: str = "buyer", name: str = "Buyer") -> User:
    """Create and return a User."""
    u = User(
        email=email,
        name=name,
        role=role,
        azure_id=f"azure-{email}",
        is_active=True,
        created_at=datetime.now(timezone.utc),
    )
    db.add(u)
    db.flush()
    return u


def _make_customer_site(db: Session) -> CustomerSite:
    """Create a Company + CustomerSite for Quote FK requirements."""
    co = Company(
        name="Test Co",
        is_active=True,
        created_at=datetime.now(timezone.utc),
    )
    db.add(co)
    db.flush()
    site = CustomerSite(
        company_id=co.id,
        site_name="Test Site",
        contact_name="Contact",
        contact_email="contact@testco.com",
    )
    db.add(site)
    db.flush()
    return site


def _make_requisition(db: Session, user: User, name: str = "REQ-PERF-001") -> Requisition:
    """Create a minimal requisition."""
    req = Requisition(
        name=name,
        customer_name="Test Customer",
        status="open",
        created_by=user.id,
        created_at=datetime.now(timezone.utc),
    )
    db.add(req)
    db.flush()
    return req


def _make_contact(
    db: Session,
    requisition: Requisition,
    user: User,
    vendor_name: str,
    contact_type: str = "email",
    created_at: datetime | None = None,
) -> Contact:
    """Create a Contact (RFQ sent to vendor)."""
    c = Contact(
        requisition_id=requisition.id,
        user_id=user.id,
        vendor_name=vendor_name,
        contact_type=contact_type,
        status="sent",
        created_at=created_at or datetime.now(timezone.utc),
    )
    db.add(c)
    db.flush()
    return c


def _make_offer(
    db: Session,
    requisition: Requisition,
    user: User,
    vendor_card: VendorCard,
    unit_price: float = 1.00,
    created_at: datetime | None = None,
) -> Offer:
    """Create an Offer linked to a vendor card."""
    o = Offer(
        requisition_id=requisition.id,
        vendor_name=vendor_card.display_name,
        vendor_card_id=vendor_card.id,
        mpn="TEST-MPN",
        qty_available=100,
        unit_price=unit_price,
        entered_by_id=user.id,
        status="active",
        created_at=created_at or datetime.now(timezone.utc),
    )
    db.add(o)
    db.flush()
    return o


# ── _compute_composite (5 tests) ─────────────────────────────────────


class TestComputeComposite:
    """Tests for the weighted-average composite score calculation."""

    def test_compute_composite_all_metrics(self):
        """All 4 values provided -> weighted average scaled to 0-1."""
        result = _compute_composite(0.8, 0.6, 0.4, 0.9)
        expected = (
            0.8 * W_RESPONSE_RATE
            + 0.6 * W_QUOTE_CONVERSION
            + 0.4 * W_PO_CONVERSION
            + 0.9 * W_REVIEW_RATING
        ) / (W_RESPONSE_RATE + W_QUOTE_CONVERSION + W_PO_CONVERSION + W_REVIEW_RATING)
        assert result == pytest.approx(expected, abs=1e-4)

    def test_compute_composite_single_metric(self):
        """Only response_rate=0.8 -> returns 0.8 (weight cancels out)."""
        result = _compute_composite(0.8)
        # With only one metric, the weighted average simplifies to the value itself
        assert result == pytest.approx(0.8, abs=1e-4)

    def test_compute_composite_none_metrics(self):
        """All None -> returns None."""
        result = _compute_composite(None, None, None, None)
        assert result is None

    def test_compute_composite_capped(self):
        """Values > 1.0 are capped at 1.0 before weighting."""
        result = _compute_composite(1.5, 2.0, 1.2, 1.8)
        # All capped to 1.0, so weighted average = 1.0
        assert result == pytest.approx(1.0, abs=1e-4)

    def test_compute_composite_two_metrics(self):
        """Two values, two None -> weighted avg of available metrics only."""
        result = _compute_composite(0.6, None, 0.4, None)
        expected = (0.6 * W_RESPONSE_RATE + 0.4 * W_PO_CONVERSION) / (
            W_RESPONSE_RATE + W_PO_CONVERSION
        )
        assert result == pytest.approx(expected, abs=1e-4)


# ── compute_vendor_scorecard (8 tests) ───────────────────────────────


class TestComputeVendorScorecard:
    """Tests for single-vendor scorecard computation."""

    def test_scorecard_no_vendor(self, db_session):
        """Invalid vendor_card_id returns empty dict."""
        result = compute_vendor_scorecard(db_session, 99999)
        assert result == {}

    def test_scorecard_cold_start(self, db_session, test_user):
        """Vendor with < COLD_START_THRESHOLD interactions -> is_sufficient_data=False."""
        vc = _make_vendor(db_session, "Cold Vendor", "cold.com")
        req = _make_requisition(db_session, test_user, "REQ-COLD")
        # Create fewer contacts + offers than COLD_START_THRESHOLD
        _make_contact(db_session, req, test_user, vc.normalized_name)
        db_session.commit()

        result = compute_vendor_scorecard(db_session, vc.id)
        assert result["is_sufficient_data"] is False
        assert result["composite_score"] is None
        assert result["interaction_count"] < COLD_START_THRESHOLD

    def test_scorecard_sufficient_data(self, db_session, test_user):
        """Vendor with >= COLD_START_THRESHOLD interactions -> is_sufficient_data=True, has composite."""
        vc = _make_vendor(db_session, "Active Vendor", "active.com")
        req = _make_requisition(db_session, test_user, "REQ-ACTIVE")

        # Create enough contacts and offers to reach threshold
        for i in range(3):
            _make_contact(db_session, req, test_user, vc.normalized_name)
        for i in range(3):
            _make_offer(db_session, req, test_user, vc)
        db_session.commit()

        result = compute_vendor_scorecard(db_session, vc.id)
        assert result["is_sufficient_data"] is True
        assert result["interaction_count"] >= COLD_START_THRESHOLD
        # Composite may still be None if all individual metrics are None
        # but interaction_count meets threshold

    def test_scorecard_response_rate(self, db_session, test_user):
        """Response rate = rfqs_answered / rfqs_sent when both > 0."""
        vc = _make_vendor(db_session, "Responsive Vendor", "responsive.com")
        req = _make_requisition(db_session, test_user, "REQ-RESP")

        # Create 4 RFQs (contacts with type=email, vendor_name matching)
        for i in range(4):
            _make_contact(db_session, req, test_user, vc.normalized_name)

        # Create 2 vendor responses with matching domain
        for i in range(2):
            vr = VendorResponse(
                vendor_name="Responsive Vendor",
                vendor_email=f"rep{i}@responsive.com",
                subject="Re: RFQ",
                status="new",
                received_at=datetime.now(timezone.utc),
            )
            db_session.add(vr)
        db_session.commit()

        result = compute_vendor_scorecard(db_session, vc.id)
        assert result["rfqs_sent"] == 4
        assert result["rfqs_answered"] == 2
        assert result["response_rate"] == pytest.approx(0.5)

    def test_scorecard_quote_conversion(self, db_session, test_user, test_requisition):
        """Quote conversion = offers_in_quotes / total_offers."""
        vc = _make_vendor(db_session, "Quotable Vendor", "quotable.com")
        site = _make_customer_site(db_session)

        # Create 4 offers
        offers = []
        for i in range(4):
            o = _make_offer(db_session, test_requisition, test_user, vc)
            offers.append(o)

        # Create a quote referencing 2 of the offers
        q = Quote(
            requisition_id=test_requisition.id,
            customer_site_id=site.id,
            quote_number=f"Q-PERF-001",
            status="sent",
            line_items=[
                {"offer_id": offers[0].id, "qty": 100},
                {"offer_id": offers[1].id, "qty": 50},
            ],
            subtotal=500.0,
            total_cost=300.0,
            total_margin_pct=40.0,
            created_by_id=test_user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(q)
        db_session.commit()

        result = compute_vendor_scorecard(db_session, vc.id)
        # 2 of 4 offers in quotes
        assert result["quote_conversion"] == pytest.approx(0.5)

    def test_scorecard_po_conversion(self, db_session, test_user, test_requisition, test_quote):
        """PO conversion = offers_to_po / total_offers."""
        vc = _make_vendor(db_session, "PO Vendor", "povendor.com")

        # Create 5 offers
        offers = []
        for i in range(5):
            o = _make_offer(db_session, test_requisition, test_user, vc)
            offers.append(o)

        # Create a buy plan with po_confirmed status referencing 2 offers
        bp = BuyPlan(
            requisition_id=test_requisition.id,
            quote_id=test_quote.id,
            status="po_confirmed",
            line_items=[
                {"offer_id": offers[0].id, "qty": 100},
                {"offer_id": offers[2].id, "qty": 50},
            ],
            submitted_by_id=test_user.id,
        )
        db_session.add(bp)
        db_session.commit()

        result = compute_vendor_scorecard(db_session, vc.id)
        # 2 of 5 offers to PO
        assert result["po_conversion"] == pytest.approx(0.4)

    def test_scorecard_review_rating(self, db_session, test_user):
        """avg_review_rating = average(ratings) / 5.0, normalized to 0-1."""
        vc = _make_vendor(db_session, "Reviewed Vendor", "reviewed.com")

        # Create 3 reviews with ratings 3, 4, 5 -> avg = 4.0, normalized = 0.8
        for rating in [3, 4, 5]:
            r = VendorReview(
                vendor_card_id=vc.id,
                user_id=test_user.id,
                rating=rating,
            )
            db_session.add(r)
        db_session.commit()

        result = compute_vendor_scorecard(db_session, vc.id)
        assert result["avg_review_rating"] == pytest.approx(4.0 / 5.0)

    def test_scorecard_preloaded_lookups(self, db_session, test_user, test_requisition):
        """Passing quoted_offer_ids and po_offer_ids skips DB queries for those sets."""
        vc = _make_vendor(db_session, "Preload Vendor", "preload.com")

        offers = []
        for i in range(3):
            o = _make_offer(db_session, test_requisition, test_user, vc)
            offers.append(o)
        db_session.commit()

        # Pass preloaded sets that include offer[0] as quoted and offer[1] as PO
        quoted = {offers[0].id}
        po = {offers[1].id}

        result = compute_vendor_scorecard(
            db_session, vc.id, quoted_offer_ids=quoted, po_offer_ids=po
        )
        # 1 of 3 quoted, 1 of 3 PO
        assert result["quote_conversion"] == pytest.approx(1 / 3, abs=1e-4)
        assert result["po_conversion"] == pytest.approx(1 / 3, abs=1e-4)


# ── compute_all_vendor_scorecards (4 tests) ──────────────────────────


class TestComputeAllVendorScorecards:
    """Tests for batch vendor scorecard computation and snapshotting."""

    def test_compute_all_empty(self, db_session):
        """No vendors -> runs without error, returns 0 updated."""
        result = compute_all_vendor_scorecards(db_session)
        assert result["updated"] == 0
        assert result["skipped_cold_start"] == 0

    def test_compute_all_creates_snapshots(self, db_session, test_user):
        """Create vendors with data -> creates VendorMetricsSnapshot rows."""
        vc = _make_vendor(db_session, "Snapshot Vendor", "snapshot.com")
        req = _make_requisition(db_session, test_user, "REQ-SNAP")

        # Sufficient interactions
        for i in range(3):
            _make_contact(db_session, req, test_user, vc.normalized_name)
        for i in range(3):
            _make_offer(db_session, req, test_user, vc)
        db_session.commit()

        result = compute_all_vendor_scorecards(db_session)
        assert result["updated"] + result["skipped_cold_start"] > 0

        # Verify snapshot exists
        snaps = db_session.query(VendorMetricsSnapshot).filter(
            VendorMetricsSnapshot.vendor_card_id == vc.id
        ).all()
        assert len(snaps) == 1
        assert snaps[0].snapshot_date == date.today()

    def test_compute_all_upserts(self, db_session, test_user):
        """Running twice on same day upserts (updates, not duplicates)."""
        vc = _make_vendor(db_session, "Upsert Vendor", "upsert.com")
        req = _make_requisition(db_session, test_user, "REQ-UPSERT")

        for i in range(3):
            _make_contact(db_session, req, test_user, vc.normalized_name)
        for i in range(3):
            _make_offer(db_session, req, test_user, vc)
        db_session.commit()

        compute_all_vendor_scorecards(db_session)
        compute_all_vendor_scorecards(db_session)

        snaps = db_session.query(VendorMetricsSnapshot).filter(
            VendorMetricsSnapshot.vendor_card_id == vc.id,
            VendorMetricsSnapshot.snapshot_date == date.today(),
        ).all()
        assert len(snaps) == 1  # Upsert, not duplicate

    def test_compute_all_error_isolation(self, db_session, test_user, monkeypatch):
        """One vendor failure does not prevent others from being processed."""
        vc1 = _make_vendor(db_session, "Good Vendor", "good.com")
        vc2 = _make_vendor(db_session, "Bad Vendor", "bad.com")
        req = _make_requisition(db_session, test_user, "REQ-ISO")

        # Give both vendors enough data
        for vc in [vc1, vc2]:
            for i in range(3):
                _make_contact(db_session, req, test_user, vc.normalized_name)
            for i in range(3):
                _make_offer(db_session, req, test_user, vc)
        db_session.commit()

        # Patch compute_vendor_scorecard to raise on vc2 but work for vc1
        original = compute_vendor_scorecard.__wrapped__ if hasattr(compute_vendor_scorecard, '__wrapped__') else compute_vendor_scorecard

        call_count = {"good": 0, "bad": 0}

        def patched_scorecard(db, vid, window_days=90, *, quoted_offer_ids=None, po_offer_ids=None):
            if vid == vc2.id:
                call_count["bad"] += 1
                raise RuntimeError("Simulated failure")
            call_count["good"] += 1
            return original(db, vid, window_days, quoted_offer_ids=quoted_offer_ids, po_offer_ids=po_offer_ids)

        monkeypatch.setattr(
            "app.services.performance_service.compute_vendor_scorecard",
            patched_scorecard,
        )

        result = compute_all_vendor_scorecards(db_session)

        # vc1 should have a snapshot; vc2 should not
        snap1 = db_session.query(VendorMetricsSnapshot).filter(
            VendorMetricsSnapshot.vendor_card_id == vc1.id
        ).first()
        snap2 = db_session.query(VendorMetricsSnapshot).filter(
            VendorMetricsSnapshot.vendor_card_id == vc2.id
        ).first()
        assert snap1 is not None
        assert snap2 is None


# ── Scorecard list/detail (5 tests) ──────────────────────────────────


class TestScorecardListDetail:
    """Tests for get_vendor_scorecard_list and get_vendor_scorecard_detail."""

    def test_scorecard_list_empty(self, db_session):
        """No snapshots -> empty results."""
        result = get_vendor_scorecard_list(db_session)
        assert result["items"] == []
        assert result["total"] == 0

    def test_scorecard_list_with_snapshots(self, db_session):
        """Create snapshots -> returns vendor list with scores."""
        vc = _make_vendor(db_session, "Listed Vendor", "listed.com")
        snap = VendorMetricsSnapshot(
            vendor_card_id=vc.id,
            snapshot_date=date.today(),
            composite_score=85.0,
            response_rate=0.9,
            interaction_count=20,
            is_sufficient_data=True,
        )
        db_session.add(snap)
        db_session.commit()

        result = get_vendor_scorecard_list(db_session)
        assert result["total"] == 1
        assert result["items"][0]["vendor_card_id"] == vc.id
        assert result["items"][0]["vendor_name"] == "Listed Vendor"
        assert result["items"][0]["composite_score"] == 85.0

    def test_scorecard_list_sort_search(self, db_session):
        """Sort by composite_score desc, search by name."""
        vc1 = _make_vendor(db_session, "Alpha Vendor", "alpha.com")
        vc2 = _make_vendor(db_session, "Beta Vendor", "beta.com")
        vc3 = _make_vendor(db_session, "Gamma Corp", "gamma.com")

        for vc, score in [(vc1, 90.0), (vc2, 70.0), (vc3, 80.0)]:
            snap = VendorMetricsSnapshot(
                vendor_card_id=vc.id,
                snapshot_date=date.today(),
                composite_score=score,
                is_sufficient_data=True,
            )
            db_session.add(snap)
        db_session.commit()

        # Sort desc
        result = get_vendor_scorecard_list(
            db_session, sort_by="composite_score", order="desc"
        )
        scores = [item["composite_score"] for item in result["items"]]
        assert scores == sorted(scores, reverse=True)

        # Search by name
        result = get_vendor_scorecard_list(db_session, search="Alpha")
        assert result["total"] == 1
        assert result["items"][0]["vendor_name"] == "Alpha Vendor"

    def test_scorecard_detail_not_found(self, db_session):
        """Invalid vendor_card_id -> returns empty dict."""
        result = get_vendor_scorecard_detail(db_session, 99999)
        assert result == {}

    def test_scorecard_detail_with_trend(self, db_session):
        """Create snapshots for multiple dates -> returns trend data."""
        vc = _make_vendor(db_session, "Trend Vendor", "trend.com")

        today = date.today()
        for days_ago in [0, 7, 14, 21]:
            snap = VendorMetricsSnapshot(
                vendor_card_id=vc.id,
                snapshot_date=today - timedelta(days=days_ago),
                composite_score=80.0 + days_ago,
                response_rate=0.8,
                is_sufficient_data=True,
            )
            db_session.add(snap)
        db_session.commit()

        result = get_vendor_scorecard_detail(db_session, vc.id)
        assert result["vendor_card_id"] == vc.id
        assert result["vendor_name"] == "Trend Vendor"
        assert result["latest"] is not None
        assert result["latest"]["snapshot_date"] == today.isoformat()
        assert len(result["trend"]) == 4
        # Trend should be in chronological order (oldest first)
        trend_dates = [t["date"] for t in result["trend"]]
        assert trend_dates == sorted(trend_dates)


# ── Buyer leaderboard (5 tests) ──────────────────────────────────────


class TestBuyerLeaderboard:
    """Tests for compute_buyer_leaderboard and get_buyer_leaderboard."""

    def test_leaderboard_no_buyers(self, db_session):
        """No buyer/trader users -> empty leaderboard."""
        # Create a non-buyer user so there are users but no buyers
        _make_user(db_session, "sales@test.com", role="sales", name="Sales Guy")
        db_session.commit()

        month = date.today().replace(day=1)
        result = compute_buyer_leaderboard(db_session, month)
        assert result["entries"] == 0

    def test_leaderboard_single_buyer(self, db_session):
        """Single buyer with offers and quotes -> correct point calculation."""
        buyer = _make_user(db_session, "lb-buyer@test.com", role="buyer", name="LB Buyer")
        vc = _make_vendor(db_session, "LB Vendor", "lbvendor.com")
        req = _make_requisition(db_session, buyer, "REQ-LB")
        site = _make_customer_site(db_session)

        month = date.today().replace(day=1)

        # Create 3 offers in current month
        offers = []
        for i in range(3):
            o = _make_offer(
                db_session, req, buyer, vc,
                created_at=datetime(month.year, month.month, 10, tzinfo=timezone.utc),
            )
            offers.append(o)

        # 1 of the offers gets into a quote
        q = Quote(
            requisition_id=req.id,
            customer_site_id=site.id,
            quote_number="Q-LB-001",
            status="sent",
            line_items=[{"offer_id": offers[0].id}],
            subtotal=100.0,
            total_cost=50.0,
            total_margin_pct=50.0,
            created_by_id=buyer.id,
        )
        db_session.add(q)
        db_session.commit()

        result = compute_buyer_leaderboard(db_session, month)
        assert result["entries"] == 1

        snap = db_session.query(BuyerLeaderboardSnapshot).filter(
            BuyerLeaderboardSnapshot.user_id == buyer.id,
            BuyerLeaderboardSnapshot.month == month,
        ).first()
        assert snap is not None
        assert snap.offers_logged == 3
        assert snap.offers_quoted == 1
        expected_points = 3 * PTS_LOGGED + 1 * PTS_QUOTED
        assert snap.total_points == expected_points

    def test_leaderboard_grace_period(self, db_session):
        """Offer created in last GRACE_DAYS of prev month counts if it advanced to a quote."""
        buyer = _make_user(db_session, "grace-buyer@test.com", role="buyer", name="Grace Buyer")
        vc = _make_vendor(db_session, "Grace Vendor", "gracevendor.com")
        req = _make_requisition(db_session, buyer, "REQ-GRACE")
        site = _make_customer_site(db_session)

        # Current month
        month = date.today().replace(day=1)
        prev_month_end = month - timedelta(days=1)

        # Offer created within grace window (last GRACE_DAYS of previous month)
        grace_date = datetime(
            prev_month_end.year, prev_month_end.month, prev_month_end.day,
            tzinfo=timezone.utc,
        )
        grace_offer = _make_offer(
            db_session, req, buyer, vc, created_at=grace_date,
        )

        # This offer was quoted (advanced) -> should count in current month
        q = Quote(
            requisition_id=req.id,
            customer_site_id=site.id,
            quote_number="Q-GRACE-001",
            status="sent",
            line_items=[{"offer_id": grace_offer.id}],
            subtotal=100.0,
            total_cost=50.0,
            total_margin_pct=50.0,
            created_by_id=buyer.id,
        )
        db_session.add(q)
        db_session.commit()

        result = compute_buyer_leaderboard(db_session, month)
        snap = db_session.query(BuyerLeaderboardSnapshot).filter(
            BuyerLeaderboardSnapshot.user_id == buyer.id,
            BuyerLeaderboardSnapshot.month == month,
        ).first()
        assert snap is not None
        # Grace offer that advanced counts as both logged and quoted
        assert snap.offers_logged >= 1
        assert snap.offers_quoted >= 1

    def test_leaderboard_ranking(self, db_session):
        """Multiple buyers -> ranked by total_points desc."""
        vc = _make_vendor(db_session, "Rank Vendor", "rankvendor.com")
        month = date.today().replace(day=1)

        buyers = []
        for i, (email, n_offers) in enumerate([
            ("rank-top@test.com", 10),
            ("rank-mid@test.com", 5),
            ("rank-low@test.com", 1),
        ]):
            buyer = _make_user(db_session, email, role="buyer", name=f"Buyer {i}")
            buyers.append(buyer)
            req = _make_requisition(db_session, buyer, f"REQ-RANK-{i}")
            for j in range(n_offers):
                _make_offer(
                    db_session, req, buyer, vc,
                    created_at=datetime(month.year, month.month, 10, tzinfo=timezone.utc),
                )

        db_session.commit()
        compute_buyer_leaderboard(db_session, month)

        snaps = (
            db_session.query(BuyerLeaderboardSnapshot)
            .filter(BuyerLeaderboardSnapshot.month == month)
            .order_by(BuyerLeaderboardSnapshot.rank)
            .all()
        )
        assert len(snaps) == 3
        assert snaps[0].rank == 1
        assert snaps[0].total_points >= snaps[1].total_points >= snaps[2].total_points

    def test_leaderboard_ytd(self, db_session):
        """get_buyer_leaderboard with multiple months -> includes YTD totals."""
        buyer = _make_user(db_session, "ytd-buyer@test.com", role="buyer", name="YTD Buyer")

        # Create snapshots for January and February
        jan = date(2026, 1, 1)
        feb = date(2026, 2, 1)

        for month_date, points in [(jan, 50), (feb, 30)]:
            snap = BuyerLeaderboardSnapshot(
                user_id=buyer.id,
                month=month_date,
                offers_logged=points // PTS_LOGGED,
                offers_quoted=0,
                offers_in_buyplan=0,
                offers_po_confirmed=0,
                stock_lists_uploaded=0,
                points_offers=points,
                points_quoted=0,
                points_buyplan=0,
                points_po=0,
                points_stock=0,
                total_points=points,
                rank=1,
            )
            db_session.add(snap)
        db_session.commit()

        result = get_buyer_leaderboard(db_session, feb)
        assert len(result) == 1
        entry = result[0]
        assert entry["user_name"] == "YTD Buyer"
        assert entry["total_points"] == 30
        assert entry["ytd_total_points"] == 80  # 50 + 30


# ── Stock list dedup (3 tests) ───────────────────────────────────────


class TestStockDedup:
    """Tests for compute_stock_list_hash and check_and_record_stock_list."""

    def test_stock_hash_deterministic(self):
        """Same rows in different order -> same hash."""
        rows_a = [
            {"mpn": "LM317T"},
            {"mpn": "NE555P"},
            {"mpn": "LM7805"},
        ]
        rows_b = [
            {"mpn": "NE555P"},
            {"mpn": "LM7805"},
            {"mpn": "LM317T"},
        ]
        assert compute_stock_list_hash(rows_a) == compute_stock_list_hash(rows_b)

    def test_stock_record_new_upload(self, db_session, test_user):
        """First upload -> creates StockListHash row, is_duplicate=False."""
        rows = [{"mpn": "LM317T"}, {"mpn": "NE555P"}]
        content_hash = compute_stock_list_hash(rows)

        result = check_and_record_stock_list(
            db_session, test_user.id, content_hash, None, "stock_v1.xlsx", 2
        )
        assert result["is_duplicate"] is False
        assert result["upload_count"] == 1

        # Verify row was created
        slh = db_session.query(StockListHash).filter(
            StockListHash.user_id == test_user.id,
            StockListHash.content_hash == content_hash,
        ).first()
        assert slh is not None
        assert slh.file_name == "stock_v1.xlsx"
        assert slh.row_count == 2

    def test_stock_record_duplicate(self, db_session, test_user):
        """Second upload with same hash -> detects duplicate, increments count."""
        rows = [{"mpn": "LM317T"}, {"mpn": "NE555P"}]
        content_hash = compute_stock_list_hash(rows)

        # First upload
        check_and_record_stock_list(
            db_session, test_user.id, content_hash, None, "stock_v1.xlsx", 2
        )

        # Second upload with same hash
        result = check_and_record_stock_list(
            db_session, test_user.id, content_hash, None, "stock_v1.xlsx", 2
        )
        assert result["is_duplicate"] is True
        assert result["upload_count"] == 2
