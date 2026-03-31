"""test_coverage_self_repair.py — Tests for app/services/self_repair_service.py.

Called by: pytest
Depends on: conftest.py fixtures, app.services.self_repair_service
"""

import os
import uuid

os.environ["TESTING"] = "1"

from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.models import Requirement, Requisition, User, VendorCard
from app.models.offers import Offer


def _uid() -> str:
    return uuid.uuid4().hex[:8]


def _make_user(db_session: Session) -> User:
    u = User(
        email=f"repair-{_uid()}@test.com",
        name="Test User",
        role="buyer",
        azure_id=f"azure-{_uid()}",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(u)
    db_session.flush()
    return u


def _make_vendor(db_session: Session) -> VendorCard:
    name = f"repair-vendor-{_uid()}"
    vc = VendorCard(
        normalized_name=name,
        display_name=name.title(),
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(vc)
    db_session.flush()
    return vc


def _make_req_item(db_session: Session, user_id: int) -> tuple:
    req = Requisition(
        name=f"REQ-{_uid()}",
        status="open",
        created_by=user_id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(req)
    db_session.flush()
    item = Requirement(
        requisition_id=req.id,
        primary_mpn="TEST-MPN",
        target_qty=100,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(item)
    db_session.flush()
    return req, item


def _make_offer(
    db_session: Session,
    vc: VendorCard,
    req_id: int,
    rq_id: int,
    mpn: str,
    status: str = "active",
    attribution_status: str = "active",
    unit_price: float = 1.0,
    expires_at=None,
) -> Offer:
    offer = Offer(
        requisition_id=req_id,
        requirement_id=rq_id,
        vendor_card_id=vc.id,
        vendor_name=vc.display_name,
        vendor_name_normalized=vc.normalized_name,
        mpn=mpn,
        status=status,
        attribution_status=attribution_status,
        unit_price=unit_price,
        qty_available=100,
        expires_at=expires_at,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(offer)
    db_session.flush()
    return offer


class TestExpireStaleOffers:
    def test_no_offers_returns_zero(self, db_session: Session):
        from app.services.self_repair_service import expire_stale_offers

        result = expire_stale_offers(db_session)
        assert result == 0

    def test_active_expired_offer_gets_expired(self, db_session: Session):
        from app.services.self_repair_service import expire_stale_offers

        user = _make_user(db_session)
        vc = _make_vendor(db_session)
        req, item = _make_req_item(db_session, user.id)
        past = datetime.now(timezone.utc) - timedelta(days=30)
        _make_offer(db_session, vc, req.id, item.id, "EXPIRE-001", expires_at=past)
        db_session.commit()

        result = expire_stale_offers(db_session)
        assert result == 1

    def test_future_offer_not_expired(self, db_session: Session):
        from app.services.self_repair_service import expire_stale_offers

        user = _make_user(db_session)
        vc = _make_vendor(db_session)
        req, item = _make_req_item(db_session, user.id)
        future = datetime.now(timezone.utc) + timedelta(days=30)
        _make_offer(db_session, vc, req.id, item.id, "FUTURE-001", expires_at=future)
        db_session.commit()

        result = expire_stale_offers(db_session)
        assert result == 0

    def test_no_expires_at_not_expired(self, db_session: Session):
        from app.services.self_repair_service import expire_stale_offers

        user = _make_user(db_session)
        vc = _make_vendor(db_session)
        req, item = _make_req_item(db_session, user.id)
        _make_offer(db_session, vc, req.id, item.id, "NOEXP-001", expires_at=None)
        db_session.commit()

        result = expire_stale_offers(db_session)
        assert result == 0

    def test_already_expired_attribution_not_double_counted(self, db_session: Session):
        from app.services.self_repair_service import expire_stale_offers

        user = _make_user(db_session)
        vc = _make_vendor(db_session)
        req, item = _make_req_item(db_session, user.id)
        past = datetime.now(timezone.utc) - timedelta(days=30)
        _make_offer(
            db_session,
            vc,
            req.id,
            item.id,
            "ALREADY-EXP",
            attribution_status="expired",
            expires_at=past,
        )
        db_session.commit()

        result = expire_stale_offers(db_session)
        assert result == 0


class TestFixZeroQtyRequirements:
    def test_no_requirements_returns_zero(self, db_session: Session):
        from app.services.self_repair_service import fix_zero_qty_requirements

        result = fix_zero_qty_requirements(db_session)
        assert result == 0

    def test_zero_qty_requirement_fixed(self, db_session: Session, test_user):
        from app.services.self_repair_service import fix_zero_qty_requirements

        req = Requisition(
            name="REQ-ZEROQTY",
            status="open",
            created_by=test_user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(req)
        db_session.flush()

        item = Requirement(
            requisition_id=req.id,
            primary_mpn="ZERO-001",
            target_qty=0,
            sourcing_status="open",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(item)
        db_session.commit()

        result = fix_zero_qty_requirements(db_session)
        assert result == 1

        db_session.refresh(item)
        assert item.target_qty == 1

    def test_lost_requirement_not_fixed(self, db_session: Session, test_user):
        from app.services.self_repair_service import fix_zero_qty_requirements

        req = Requisition(
            name="REQ-LOST-ZERO",
            status="lost",
            created_by=test_user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(req)
        db_session.flush()

        item = Requirement(
            requisition_id=req.id,
            primary_mpn="LOST-ZERO-001",
            target_qty=0,
            sourcing_status="lost",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(item)
        db_session.commit()

        result = fix_zero_qty_requirements(db_session)
        assert result == 0

    def test_positive_qty_not_changed(self, db_session: Session, test_user):
        from app.services.self_repair_service import fix_zero_qty_requirements

        req = Requisition(
            name="REQ-POS-QTY",
            status="open",
            created_by=test_user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(req)
        db_session.flush()

        item = Requirement(
            requisition_id=req.id,
            primary_mpn="POS-QTY-001",
            target_qty=500,
            sourcing_status="open",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(item)
        db_session.commit()

        result = fix_zero_qty_requirements(db_session)
        assert result == 0


class TestFixZeroPriceOffers:
    def test_no_offers_returns_zero(self, db_session: Session):
        from app.services.self_repair_service import fix_zero_price_offers

        result = fix_zero_price_offers(db_session)
        assert result == 0

    def test_zero_price_active_offer_expired(self, db_session: Session):
        from app.services.self_repair_service import fix_zero_price_offers

        user = _make_user(db_session)
        vc = _make_vendor(db_session)
        req, item = _make_req_item(db_session, user.id)
        _make_offer(db_session, vc, req.id, item.id, "ZERO-PRICE", unit_price=0.0)
        db_session.commit()

        result = fix_zero_price_offers(db_session)
        assert result == 1

    def test_positive_price_offer_not_touched(self, db_session: Session):
        from app.services.self_repair_service import fix_zero_price_offers

        user = _make_user(db_session)
        vc = _make_vendor(db_session)
        req, item = _make_req_item(db_session, user.id)
        _make_offer(db_session, vc, req.id, item.id, "GOOD-PRICE", unit_price=1.50)
        db_session.commit()

        result = fix_zero_price_offers(db_session)
        assert result == 0

    def test_negative_price_offer_expired(self, db_session: Session):
        from app.services.self_repair_service import fix_zero_price_offers

        user = _make_user(db_session)
        vc = _make_vendor(db_session)
        req, item = _make_req_item(db_session, user.id)
        _make_offer(db_session, vc, req.id, item.id, "NEG-PRICE", unit_price=-0.5)
        db_session.commit()

        result = fix_zero_price_offers(db_session)
        assert result == 1


class TestDeduplicateVendorNames:
    def test_no_duplicates_returns_zero(self, db_session: Session):
        from app.services.self_repair_service import deduplicate_vendor_names

        result = deduplicate_vendor_names(db_session)
        assert result == 0

    def test_unique_vendors_not_merged(self, db_session: Session):
        from app.services.self_repair_service import deduplicate_vendor_names

        _make_vendor(db_session)
        _make_vendor(db_session)
        db_session.commit()

        result = deduplicate_vendor_names(db_session)
        assert result == 0


class TestRunFullRepair:
    def test_returns_dict_with_expected_keys(self, db_session: Session):
        from app.services.self_repair_service import run_full_repair

        result = run_full_repair(db_session)
        assert "stale_offers_expired" in result
        assert "zero_qty_fixed" in result
        assert "zero_price_expired" in result
        assert "vendor_dupes_merged" in result
        assert "ran_at" in result

    def test_all_values_are_numbers(self, db_session: Session):
        from app.services.self_repair_service import run_full_repair

        result = run_full_repair(db_session)
        for key in ["stale_offers_expired", "zero_qty_fixed", "zero_price_expired", "vendor_dupes_merged"]:
            assert isinstance(result[key], int), f"{key} should be int"

    def test_ran_at_is_iso_string(self, db_session: Session):
        from app.services.self_repair_service import run_full_repair

        result = run_full_repair(db_session)
        ran_at = result["ran_at"]
        assert isinstance(ran_at, str)
        datetime.fromisoformat(ran_at.replace("Z", "+00:00"))

    def test_idempotent_on_empty_db(self, db_session: Session):
        from app.services.self_repair_service import run_full_repair

        r1 = run_full_repair(db_session)
        r2 = run_full_repair(db_session)
        assert r1["stale_offers_expired"] == 0
        assert r2["stale_offers_expired"] == 0

    def test_repairs_zero_qty_requirement(self, db_session: Session, test_user):
        from app.services.self_repair_service import run_full_repair

        req = Requisition(
            name="REQ-FULL-REPAIR",
            status="open",
            created_by=test_user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(req)
        db_session.flush()

        item = Requirement(
            requisition_id=req.id,
            primary_mpn="FR-001",
            target_qty=0,
            sourcing_status="open",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(item)
        db_session.commit()

        result = run_full_repair(db_session)
        assert result["zero_qty_fixed"] == 1

    def test_repairs_stale_offers(self, db_session: Session):
        from app.services.self_repair_service import run_full_repair

        user = _make_user(db_session)
        vc = _make_vendor(db_session)
        req, item = _make_req_item(db_session, user.id)
        past = datetime.now(timezone.utc) - timedelta(days=30)
        _make_offer(db_session, vc, req.id, item.id, "STALE-REPAIR", expires_at=past)
        db_session.commit()

        result = run_full_repair(db_session)
        assert result["stale_offers_expired"] == 1

    def test_repairs_zero_price_offers(self, db_session: Session):
        from app.services.self_repair_service import run_full_repair

        user = _make_user(db_session)
        vc = _make_vendor(db_session)
        req, item = _make_req_item(db_session, user.id)
        _make_offer(db_session, vc, req.id, item.id, "ZERO-P-REPAIR", unit_price=0.0)
        db_session.commit()

        result = run_full_repair(db_session)
        assert result["zero_price_expired"] == 1
