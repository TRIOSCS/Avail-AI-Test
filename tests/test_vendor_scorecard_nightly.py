"""tests/test_vendor_scorecard_nightly.py — Coverage for uncovered lines in vendor_scorecard.

Targets lines: 111-118, 129-138, 225-228, 239, 260-261, 296-299
Called by: pytest
Depends on: conftest fixtures, vendor_scorecard models
"""

import os

os.environ["TESTING"] = "1"

from datetime import date, datetime, timezone
from unittest.mock import patch

from sqlalchemy.orm import Session

from app.models import Offer, Quote, Requirement, Requisition, User, VendorCard, VendorMetricsSnapshot
from app.services.vendor_scorecard import (
    compute_all_vendor_scorecards,
    compute_vendor_scorecard,
    get_vendor_scorecard_list,
)


def _make_user(db: Session, tag: str = "vs") -> User:
    u = User(
        email=f"{tag}@trioscs.com",
        name=tag,
        role="buyer",
        azure_id=f"az-{tag}-{id(tag)}",
        created_at=datetime.now(timezone.utc),
    )
    db.add(u)
    db.flush()
    return u


def _make_vendor(db: Session, name: str = "test vendor", **kwargs) -> VendorCard:
    defaults = dict(
        normalized_name=name,
        display_name=name.title(),
        emails=[f"{name.replace(' ', '')}@test.com"],
        phones=[],
        sighting_count=0,
        domain_aliases=[],
        created_at=datetime.now(timezone.utc),
    )
    defaults.update(kwargs)
    vc = VendorCard(**defaults)
    db.add(vc)
    db.flush()
    return vc


def _make_req_and_item(db: Session, user: User) -> tuple:
    req = Requisition(
        name="VS-REQ",
        customer_name="C",
        status="active",
        created_by=user.id,
        created_at=datetime.now(timezone.utc),
    )
    db.add(req)
    db.flush()
    item = Requirement(
        requisition_id=req.id,
        primary_mpn="LM317T",
        target_qty=10,
        created_at=datetime.now(timezone.utc),
    )
    db.add(item)
    db.flush()
    return req, item


def _make_offer(db: Session, vendor_card_id: int, user: User) -> Offer:
    req, item = _make_req_and_item(db, user)
    o = Offer(
        requisition_id=req.id,
        requirement_id=item.id,
        vendor_card_id=vendor_card_id,
        vendor_name="Test Vendor",
        mpn="LM317T",
        normalized_mpn="LM317T",
        unit_price=0.50,
        qty_available=1000,
        status="active",
        created_at=datetime.now(timezone.utc),
    )
    db.add(o)
    db.flush()
    return o


class TestComputeVendorScorecardLazyLookups:
    def test_quoted_offer_ids_loaded_on_demand_when_none(self, db_session: Session):
        """Lines 111-118 — quoted_offer_ids=None auto-loads from Quote when offers exist."""
        user = _make_user(db_session, "lazy")
        vc = _make_vendor(db_session, "lazy vendor")
        offer = _make_offer(db_session, vc.id, user)
        db_session.commit()

        # Put offer in a sent quote so it gets counted
        q = Quote(
            requisition_id=offer.requisition_id,
            quote_number="Q-LAZ-001",
            revision=1,
            line_items=[{"offer_id": offer.id, "mpn": "LM317T", "qty": 10}],
            status="sent",
            created_by_id=user.id,
        )
        db_session.add(q)
        db_session.commit()

        # quoted_offer_ids=None → auto-loaded
        result = compute_vendor_scorecard(db_session, vc.id, quoted_offer_ids=None)
        # quote_conversion should be 1.0 (1 of 1 offer in a sent quote)
        assert result.get("quote_conversion") == 1.0

    def test_po_offer_ids_loaded_on_demand_when_none(self, db_session: Session):
        """Lines 129-138 — po_offer_ids=None auto-loads from BuyPlanLine when offers exist."""
        from app.models import BuyPlan, BuyPlanLine

        user = _make_user(db_session, "po")
        vc = _make_vendor(db_session, "po vendor")
        offer = _make_offer(db_session, vc.id, user)
        db_session.commit()

        # Need a Quote for BuyPlan.quote_id (NOT NULL)
        q = Quote(
            requisition_id=offer.requisition_id,
            quote_number="Q-PO-001",
            revision=1,
            line_items=[],
            status="won",
            created_by_id=user.id,
        )
        db_session.add(q)
        db_session.flush()

        bp = BuyPlan(
            quote_id=q.id,
            requisition_id=offer.requisition_id,
            status="completed",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(bp)
        db_session.flush()

        bpl = BuyPlanLine(
            buy_plan_id=bp.id,
            offer_id=offer.id,
            quantity=10,
        )
        db_session.add(bpl)
        db_session.commit()

        # po_offer_ids=None → auto-loaded
        result = compute_vendor_scorecard(db_session, vc.id, po_offer_ids=None)
        assert result.get("po_conversion") == 1.0


class TestComputeAllVendorScorecards:
    def test_empty_scorecard_result_triggers_savepoint_rollback(self, db_session: Session):
        """Lines 225-228 — compute_vendor_scorecard returns {} → savepoint.rollback(), continue."""
        _make_vendor(db_session, "cold vendor")
        db_session.commit()

        with patch(
            "app.services.vendor_scorecard.compute_vendor_scorecard",
            return_value={},  # empty dict → falsy → savepoint.rollback()
        ):
            result = compute_all_vendor_scorecards(db_session)

        assert result["updated"] == 0

    def test_existing_snapshot_is_updated_not_duplicated(self, db_session: Session):
        """Line 239 — existing snapshot found → updated in-place (not new row)."""
        vc = _make_vendor(db_session, "snap vendor")
        db_session.commit()

        today = date.today()
        existing = VendorMetricsSnapshot(
            vendor_card_id=vc.id,
            snapshot_date=today,
            composite_score=0.5,
            interaction_count=10,
            is_sufficient_data=True,
            rfqs_sent=10,
            rfqs_answered=5,
        )
        db_session.add(existing)
        db_session.commit()

        mock_result = {
            "response_rate": 0.7,
            "quote_conversion": 0.3,
            "po_conversion": 0.2,
            "avg_review_rating": None,
            "composite_score": 0.6,
            "interaction_count": 15,
            "is_sufficient_data": True,
            "rfqs_sent": 15,
            "rfqs_answered": 10,
        }
        with patch(
            "app.services.vendor_scorecard.compute_vendor_scorecard",
            return_value=mock_result,
        ):
            result = compute_all_vendor_scorecards(db_session)

        assert result["updated"] >= 1
        # Only one snapshot for today (no duplicate)
        count = (
            db_session.query(VendorMetricsSnapshot)
            .filter(
                VendorMetricsSnapshot.vendor_card_id == vc.id,
                VendorMetricsSnapshot.snapshot_date == today,
            )
            .count()
        )
        assert count == 1

    def test_exception_in_scorecard_triggers_savepoint_rollback_and_continues(self, db_session: Session):
        """Lines 260-261 — exception during compute → savepoint.rollback(), next vendor."""
        vc1 = _make_vendor(db_session, "err vendor 1")
        vc2 = _make_vendor(db_session, "ok vendor 2")
        db_session.commit()

        call_count = {"n": 0}

        def mock_scorecard(db, vid, **kwargs):
            call_count["n"] += 1
            if vid == vc1.id:
                raise RuntimeError("compute failed")
            return {
                "response_rate": None,
                "quote_conversion": None,
                "po_conversion": None,
                "avg_review_rating": None,
                "composite_score": None,
                "interaction_count": 2,
                "is_sufficient_data": False,
                "rfqs_sent": 2,
                "rfqs_answered": 0,
            }

        with patch(
            "app.services.vendor_scorecard.compute_vendor_scorecard",
            side_effect=mock_scorecard,
        ):
            result = compute_all_vendor_scorecards(db_session)

        assert call_count["n"] == 2  # both vendors processed despite first error


class TestGetVendorScorecardListSearch:
    def test_search_filter_returns_matching_vendors(self, db_session: Session):
        """Lines 296-299 — search param → ILIKE filter applied, non-matching excluded."""
        vc1 = _make_vendor(db_session, "arrow electronics")
        vc2 = _make_vendor(db_session, "mouser electronics")
        db_session.commit()

        today = date.today()
        snap1 = VendorMetricsSnapshot(
            vendor_card_id=vc1.id,
            snapshot_date=today,
            composite_score=0.8,
        )
        snap2 = VendorMetricsSnapshot(
            vendor_card_id=vc2.id,
            snapshot_date=today,
            composite_score=0.6,
        )
        db_session.add_all([snap1, snap2])
        db_session.commit()

        result = get_vendor_scorecard_list(db_session, search="arrow")
        names = [r["vendor_name"].lower() for r in result["items"]]
        assert any("arrow" in n for n in names)
        assert all("mouser" not in n for n in names)

    def test_invalid_sort_column_falls_back_to_composite_score(self, db_session: Session):
        """Lines 296-299 — invalid sort_by column → falls back, no AttributeError."""
        vc = _make_vendor(db_session, "sort fallback vendor")
        db_session.commit()

        result = get_vendor_scorecard_list(db_session, sort_by="__invalid_col__")
        assert "items" in result
