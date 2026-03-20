"""test_self_repair.py — Tests for the Self-Repair Toolkit.

Verifies each repair function detects and fixes the target problem.

Called by: pytest tests/ux_mega/test_self_repair.py
Depends on: conftest.py fixtures, app.services.self_repair_service
"""

from datetime import datetime, timedelta, timezone

from app.models import Offer, Requirement
from app.services.self_repair_service import (
    expire_stale_offers,
    fix_zero_price_offers,
    fix_zero_qty_requirements,
    run_full_repair,
)


class TestExpireStaleOffers:
    def test_expires_past_due_offers(self, db_session, test_requisition):
        req = db_session.query(Requirement).filter_by(requisition_id=test_requisition.id).first()
        offer = Offer(
            requisition_id=test_requisition.id,
            requirement_id=req.id if req else None,
            vendor_name="StaleVendor",
            mpn="OLD123",
            qty_available=100,
            unit_price=1.00,
            status="active",
            attribution_status="active",
            expires_at=datetime.now(timezone.utc) - timedelta(days=30),
            created_at=datetime.now(timezone.utc) - timedelta(days=60),
        )
        db_session.add(offer)
        db_session.flush()

        count = expire_stale_offers(db_session)
        assert count >= 1

        db_session.refresh(offer)
        assert offer.attribution_status == "expired"

    def test_skips_non_expired_offers(self, db_session, test_requisition):
        req = db_session.query(Requirement).filter_by(requisition_id=test_requisition.id).first()
        offer = Offer(
            requisition_id=test_requisition.id,
            requirement_id=req.id if req else None,
            vendor_name="FreshVendor",
            mpn="NEW123",
            qty_available=100,
            unit_price=1.00,
            status="active",
            attribution_status="active",
            expires_at=datetime.now(timezone.utc) + timedelta(days=30),
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(offer)
        db_session.flush()

        expire_stale_offers(db_session)
        db_session.refresh(offer)
        assert offer.attribution_status == "active"


class TestFixZeroQty:
    def test_fixes_zero_qty(self, db_session, test_requisition):
        req = Requirement(
            requisition_id=test_requisition.id,
            primary_mpn="ZEROQTY",
            normalized_mpn="zeroqty",
            target_qty=0,
        )
        db_session.add(req)
        db_session.flush()

        count = fix_zero_qty_requirements(db_session)
        assert count >= 1

        db_session.refresh(req)
        assert req.target_qty == 1


class TestFixZeroPrice:
    def test_expires_zero_price_offers(self, db_session, test_requisition):
        req = db_session.query(Requirement).filter_by(requisition_id=test_requisition.id).first()
        offer = Offer(
            requisition_id=test_requisition.id,
            requirement_id=req.id if req else None,
            vendor_name="FreeVendor",
            mpn="FREE123",
            qty_available=100,
            unit_price=0.0,
            status="active",
            attribution_status="active",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(offer)
        db_session.flush()

        count = fix_zero_price_offers(db_session)
        assert count >= 1

        db_session.refresh(offer)
        assert offer.attribution_status == "expired"


class TestFullRepair:
    def test_full_repair_runs_without_error(self, db_session):
        report = run_full_repair(db_session)
        assert "ran_at" in report
        assert "stale_offers_expired" in report
        assert "zero_qty_fixed" in report
        assert "zero_price_expired" in report
        assert "vendor_dupes_merged" in report
