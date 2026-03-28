"""Tests for app/services/vendor_scorecard.py — vendor performance metrics.

Called by: pytest
Depends on: conftest fixtures, unittest.mock
"""

from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy.orm import Session

from app.models import Offer, Requisition, User, VendorCard
from app.models.offers import Contact, VendorResponse
from app.models.performance import VendorMetricsSnapshot
from app.models.vendors import VendorReview
from app.services.vendor_scorecard import (
    COLD_START_THRESHOLD,
    _compute_composite,
    compute_all_vendor_scorecards,
    compute_vendor_scorecard,
    get_vendor_scorecard_detail,
    get_vendor_scorecard_list,
)

# ── Helpers ──────────────────────────────────────────────────────────


def _make_vendor(db: Session, **overrides) -> VendorCard:
    defaults = {
        "normalized_name": "test vendor",
        "display_name": "Test Vendor",
        "emails": ["test@vendor.com"],
        "phones": [],
        "sighting_count": 0,
        "domain": "vendor.com",
        "domain_aliases": [],
        "created_at": datetime.now(timezone.utc),
    }
    defaults.update(overrides)
    vc = VendorCard(**defaults)
    db.add(vc)
    db.flush()
    return vc


def _make_user(db: Session) -> User:
    u = User(
        email="scorecard-test@trioscs.com",
        name="SC Test",
        role="buyer",
        azure_id="sc-test-001",
        created_at=datetime.now(timezone.utc),
    )
    db.add(u)
    db.flush()
    return u


def _make_requisition(db: Session, user: User) -> Requisition:
    r = Requisition(
        name="REQ-SC-001",
        customer_name="Test Customer",
        status="active",
        created_by=user.id,
        created_at=datetime.now(timezone.utc),
    )
    db.add(r)
    db.flush()
    return r


# ── _compute_composite tests ─────────────────────────────────────────


class TestComputeComposite:
    def test_all_metrics_present(self):
        result = _compute_composite(0.8, 0.6, 0.4, 0.9)
        # (0.8*0.25 + 0.6*0.25 + 0.4*0.25 + 0.9*0.25) / 1.0 = 0.675
        assert result == pytest.approx(0.675, abs=0.001)

    def test_only_response_rate(self):
        result = _compute_composite(0.5)
        # 0.5*0.25 / 0.25 = 0.5
        assert result == pytest.approx(0.5, abs=0.001)

    def test_no_metrics(self):
        result = _compute_composite(None)
        assert result is None

    def test_values_capped_at_1(self):
        result = _compute_composite(1.5, 2.0)
        # Both capped to 1.0: (1.0*0.25 + 1.0*0.25) / 0.5 = 1.0
        assert result == pytest.approx(1.0, abs=0.001)

    def test_two_metrics(self):
        result = _compute_composite(0.6, 0.4)
        # (0.6*0.25 + 0.4*0.25) / 0.5 = 0.5
        assert result == pytest.approx(0.5, abs=0.001)

    def test_zero_values(self):
        result = _compute_composite(0.0, 0.0, 0.0, 0.0)
        assert result == pytest.approx(0.0, abs=0.001)


# ── compute_vendor_scorecard tests ───────────────────────────────────


class TestComputeVendorScorecard:
    def test_nonexistent_vendor(self, db_session):
        result = compute_vendor_scorecard(db_session, 99999)
        assert result == {}

    def test_vendor_no_activity(self, db_session):
        vc = _make_vendor(db_session)
        result = compute_vendor_scorecard(db_session, vc.id)

        assert result["vendor_card_id"] == vc.id
        assert result["response_rate"] is None
        assert result["quote_conversion"] is None
        assert result["po_conversion"] is None
        assert result["interaction_count"] == 0
        assert result["is_sufficient_data"] is False
        assert result["composite_score"] is None

    def test_cold_start_below_threshold(self, db_session):
        vc = _make_vendor(db_session)
        user = _make_user(db_session)
        req = _make_requisition(db_session, user)

        # Add fewer than COLD_START_THRESHOLD contacts
        for i in range(COLD_START_THRESHOLD - 1):
            c = Contact(
                requisition_id=req.id,
                user_id=user.id,
                contact_type="email",
                vendor_name=vc.display_name,
                vendor_name_normalized=vc.normalized_name,
                created_at=datetime.now(timezone.utc),
            )
            db_session.add(c)
        db_session.flush()

        result = compute_vendor_scorecard(db_session, vc.id)
        assert result["is_sufficient_data"] is False
        assert result["composite_score"] is None

    def test_sufficient_data_with_contacts(self, db_session):
        vc = _make_vendor(db_session)
        user = _make_user(db_session)
        req = _make_requisition(db_session, user)

        # Add enough contacts to cross threshold
        for i in range(COLD_START_THRESHOLD + 1):
            c = Contact(
                requisition_id=req.id,
                user_id=user.id,
                contact_type="email",
                vendor_name=vc.display_name,
                vendor_name_normalized=vc.normalized_name,
                created_at=datetime.now(timezone.utc),
            )
            db_session.add(c)
        db_session.flush()

        result = compute_vendor_scorecard(db_session, vc.id)
        assert result["is_sufficient_data"] is True
        assert result["rfqs_sent"] == COLD_START_THRESHOLD + 1

    def test_response_rate_calculation(self, db_session):
        vc = _make_vendor(db_session, domain="vendor.com")
        user = _make_user(db_session)
        req = _make_requisition(db_session, user)

        # 4 RFQs sent
        for i in range(4):
            c = Contact(
                requisition_id=req.id,
                user_id=user.id,
                contact_type="email",
                vendor_name=vc.display_name,
                vendor_name_normalized=vc.normalized_name,
                created_at=datetime.now(timezone.utc),
            )
            db_session.add(c)

        # 2 responses from vendor domain
        for i in range(2):
            vr = VendorResponse(
                vendor_email=f"sales{i}@vendor.com",
                status="parsed",
                received_at=datetime.now(timezone.utc),
            )
            db_session.add(vr)
        db_session.flush()

        result = compute_vendor_scorecard(db_session, vc.id)
        assert result["rfqs_sent"] == 4
        assert result["rfqs_answered"] == 2
        assert result["response_rate"] == pytest.approx(0.5, abs=0.01)

    def test_quote_conversion_with_preloaded_ids(self, db_session):
        vc = _make_vendor(db_session)
        user = _make_user(db_session)
        req = _make_requisition(db_session, user)

        # Create offers
        offers = []
        for i in range(4):
            o = Offer(
                requisition_id=req.id,
                vendor_name=vc.display_name,
                vendor_card_id=vc.id,
                mpn="LM317T",
                qty_available=100,
                unit_price=0.50,
                entered_by_id=user.id,
                status="active",
                created_at=datetime.now(timezone.utc),
            )
            db_session.add(o)
            db_session.flush()
            offers.append(o)

        # Preload: 2 of 4 offers are in quotes
        quoted_offer_ids = {offers[0].id, offers[1].id}

        result = compute_vendor_scorecard(db_session, vc.id, quoted_offer_ids=quoted_offer_ids, po_offer_ids=set())
        assert result["quote_conversion"] == pytest.approx(0.5, abs=0.01)

    def test_po_conversion_with_preloaded_ids(self, db_session):
        vc = _make_vendor(db_session)
        user = _make_user(db_session)
        req = _make_requisition(db_session, user)

        offers = []
        for i in range(5):
            o = Offer(
                requisition_id=req.id,
                vendor_name=vc.display_name,
                vendor_card_id=vc.id,
                mpn="LM317T",
                qty_available=100,
                unit_price=0.50,
                entered_by_id=user.id,
                status="active",
                created_at=datetime.now(timezone.utc),
            )
            db_session.add(o)
            db_session.flush()
            offers.append(o)

        po_offer_ids = {offers[0].id}
        result = compute_vendor_scorecard(db_session, vc.id, quoted_offer_ids=set(), po_offer_ids=po_offer_ids)
        assert result["po_conversion"] == pytest.approx(0.2, abs=0.01)

    def test_avg_review_rating(self, db_session):
        vc = _make_vendor(db_session)
        user = _make_user(db_session)

        # Two reviews: rating 4 and 5 => avg 4.5 => normalized 0.9
        for rating in [4, 5]:
            r = VendorReview(
                vendor_card_id=vc.id,
                user_id=user.id,
                rating=rating,
                created_at=datetime.now(timezone.utc),
            )
            db_session.add(r)
        db_session.flush()

        result = compute_vendor_scorecard(db_session, vc.id, quoted_offer_ids=set(), po_offer_ids=set())
        assert result["avg_review_rating"] == pytest.approx(0.9, abs=0.01)

    def test_noise_responses_excluded(self, db_session):
        vc = _make_vendor(db_session, domain="vendor.com")
        user = _make_user(db_session)
        req = _make_requisition(db_session, user)

        # 2 RFQs sent
        for _ in range(2):
            c = Contact(
                requisition_id=req.id,
                user_id=user.id,
                contact_type="email",
                vendor_name=vc.display_name,
                vendor_name_normalized=vc.normalized_name,
                created_at=datetime.now(timezone.utc),
            )
            db_session.add(c)

        # 1 noise response (should be excluded)
        vr = VendorResponse(
            vendor_email="auto@vendor.com",
            status="noise",
            received_at=datetime.now(timezone.utc),
        )
        db_session.add(vr)
        db_session.flush()

        result = compute_vendor_scorecard(db_session, vc.id)
        assert result["rfqs_answered"] == 0

    def test_domain_aliases_counted(self, db_session):
        vc = _make_vendor(db_session, domain="vendor.com", domain_aliases=["vendor.co.uk"])
        user = _make_user(db_session)
        req = _make_requisition(db_session, user)

        c = Contact(
            requisition_id=req.id,
            user_id=user.id,
            contact_type="email",
            vendor_name=vc.display_name,
            vendor_name_normalized=vc.normalized_name,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(c)

        # Response from alias domain
        vr = VendorResponse(
            vendor_email="reply@vendor.co.uk",
            status="parsed",
            received_at=datetime.now(timezone.utc),
        )
        db_session.add(vr)
        db_session.flush()

        result = compute_vendor_scorecard(db_session, vc.id)
        assert result["rfqs_answered"] == 1


# ── compute_all_vendor_scorecards tests ──────────────────────────────


class TestComputeAllVendorScorecards:
    def test_empty_database(self, db_session):
        result = compute_all_vendor_scorecards(db_session)
        assert result["updated"] == 0
        assert result["skipped_cold_start"] == 0

    def test_creates_snapshot(self, db_session):
        vc = _make_vendor(db_session)
        user = _make_user(db_session)
        req = _make_requisition(db_session, user)

        # Create enough interactions to cross cold-start threshold
        for i in range(COLD_START_THRESHOLD + 1):
            c = Contact(
                requisition_id=req.id,
                user_id=user.id,
                contact_type="email",
                vendor_name=vc.display_name,
                vendor_name_normalized=vc.normalized_name,
                created_at=datetime.now(timezone.utc),
            )
            db_session.add(c)
        db_session.flush()

        result = compute_all_vendor_scorecards(db_session)
        assert result["updated"] == 1

        snap = db_session.query(VendorMetricsSnapshot).filter_by(vendor_card_id=vc.id).first()
        assert snap is not None
        assert snap.snapshot_date == date.today()

    def test_cold_start_vendor_skipped(self, db_session):
        _make_vendor(db_session)  # No activity at all
        result = compute_all_vendor_scorecards(db_session)
        assert result["skipped_cold_start"] == 1
        assert result["updated"] == 0

    def test_upserts_existing_snapshot(self, db_session):
        vc = _make_vendor(db_session)
        user = _make_user(db_session)
        req = _make_requisition(db_session, user)

        for i in range(COLD_START_THRESHOLD + 1):
            c = Contact(
                requisition_id=req.id,
                user_id=user.id,
                contact_type="email",
                vendor_name=vc.display_name,
                vendor_name_normalized=vc.normalized_name,
                created_at=datetime.now(timezone.utc),
            )
            db_session.add(c)
        db_session.flush()

        # Run twice — should upsert, not duplicate
        compute_all_vendor_scorecards(db_session)
        compute_all_vendor_scorecards(db_session)

        count = (
            db_session.query(VendorMetricsSnapshot).filter_by(vendor_card_id=vc.id, snapshot_date=date.today()).count()
        )
        assert count == 1


# ── get_vendor_scorecard_list tests ──────────────────────────────────


class TestGetVendorScorecardList:
    def _seed_snapshot(self, db_session, vc, **overrides):
        defaults = {
            "vendor_card_id": vc.id,
            "snapshot_date": date.today(),
            "composite_score": 0.75,
            "response_rate": 0.8,
            "interaction_count": 10,
            "is_sufficient_data": True,
        }
        defaults.update(overrides)
        snap = VendorMetricsSnapshot(**defaults)
        db_session.add(snap)
        db_session.flush()
        return snap

    def test_empty_result(self, db_session):
        result = get_vendor_scorecard_list(db_session)
        assert result["items"] == []
        assert result["total"] == 0

    def test_returns_items(self, db_session):
        vc = _make_vendor(db_session)
        self._seed_snapshot(db_session, vc)

        result = get_vendor_scorecard_list(db_session)
        assert result["total"] == 1
        assert result["items"][0]["vendor_card_id"] == vc.id
        assert result["items"][0]["vendor_name"] == vc.display_name

    def test_sort_by_response_rate(self, db_session):
        vc1 = _make_vendor(db_session, normalized_name="vendor a", display_name="Vendor A", domain="a.com")
        vc2 = _make_vendor(db_session, normalized_name="vendor b", display_name="Vendor B", domain="b.com")
        self._seed_snapshot(db_session, vc1, response_rate=0.3)
        self._seed_snapshot(db_session, vc2, response_rate=0.9)

        result = get_vendor_scorecard_list(db_session, sort_by="response_rate", order="desc")
        assert result["items"][0]["vendor_card_id"] == vc2.id

    def test_invalid_sort_column_defaults(self, db_session):
        vc = _make_vendor(db_session)
        self._seed_snapshot(db_session, vc)

        # Should not crash, defaults to composite_score
        result = get_vendor_scorecard_list(db_session, sort_by="invalid_column")
        assert result["total"] == 1

    def test_search_filter(self, db_session):
        vc1 = _make_vendor(db_session, normalized_name="arrow", display_name="Arrow Electronics", domain="arrow.com")
        vc2 = _make_vendor(db_session, normalized_name="mouser", display_name="Mouser Electronics", domain="mouser.com")
        self._seed_snapshot(db_session, vc1)
        self._seed_snapshot(db_session, vc2)

        result = get_vendor_scorecard_list(db_session, search="Arrow")
        assert result["total"] == 1
        assert result["items"][0]["vendor_name"] == "Arrow Electronics"

    def test_pagination(self, db_session):
        for i in range(5):
            vc = _make_vendor(
                db_session,
                normalized_name=f"vendor_{i}",
                display_name=f"Vendor {i}",
                domain=f"v{i}.com",
            )
            self._seed_snapshot(db_session, vc, composite_score=0.5 + i * 0.05)

        result = get_vendor_scorecard_list(db_session, limit=2, offset=0)
        assert result["total"] == 5
        assert len(result["items"]) == 2

    def test_asc_order(self, db_session):
        vc1 = _make_vendor(db_session, normalized_name="vendor_lo", display_name="Low", domain="lo.com")
        vc2 = _make_vendor(db_session, normalized_name="vendor_hi", display_name="High", domain="hi.com")
        self._seed_snapshot(db_session, vc1, composite_score=0.2)
        self._seed_snapshot(db_session, vc2, composite_score=0.9)

        result = get_vendor_scorecard_list(db_session, order="asc")
        assert result["items"][0]["composite_score"] == pytest.approx(0.2)

    def test_invalid_order_defaults_to_desc(self, db_session):
        vc = _make_vendor(db_session)
        self._seed_snapshot(db_session, vc)
        result = get_vendor_scorecard_list(db_session, order="invalid")
        assert result["total"] == 1


# ── get_vendor_scorecard_detail tests ────────────────────────────────


class TestGetVendorScorecardDetail:
    def test_nonexistent_vendor(self, db_session):
        result = get_vendor_scorecard_detail(db_session, 99999)
        assert result == {}

    def test_no_snapshots(self, db_session):
        vc = _make_vendor(db_session)
        result = get_vendor_scorecard_detail(db_session, vc.id)
        assert result["vendor_card_id"] == vc.id
        assert result["latest"] is None
        assert result["trend"] == []

    def test_with_snapshots(self, db_session):
        vc = _make_vendor(db_session)

        # Create a few snapshots
        for i in range(3):
            snap = VendorMetricsSnapshot(
                vendor_card_id=vc.id,
                snapshot_date=date.today() - timedelta(days=i),
                composite_score=0.5 + i * 0.1,
                response_rate=0.6 + i * 0.1,
                interaction_count=10 + i,
                is_sufficient_data=True,
            )
            db_session.add(snap)
        db_session.flush()

        result = get_vendor_scorecard_detail(db_session, vc.id)
        assert result["vendor_name"] == vc.display_name
        assert result["latest"] is not None
        # Latest should be today (most recent)
        assert result["latest"]["snapshot_date"] == date.today().isoformat()
        # Trend oldest first
        assert len(result["trend"]) == 3
        assert result["trend"][0]["date"] <= result["trend"][-1]["date"]

    def test_old_snapshots_excluded(self, db_session):
        vc = _make_vendor(db_session)

        # Snapshot from 120 days ago (beyond 90-day window)
        old_snap = VendorMetricsSnapshot(
            vendor_card_id=vc.id,
            snapshot_date=date.today() - timedelta(days=120),
            composite_score=0.5,
        )
        db_session.add(old_snap)
        db_session.flush()

        result = get_vendor_scorecard_detail(db_session, vc.id)
        assert result["latest"] is None
        assert result["trend"] == []
