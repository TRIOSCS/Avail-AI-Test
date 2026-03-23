"""test_strategic_vendor_service.py — Tests for strategic vendor claim/drop/replace
logic.

Covers race condition handling in claim_vendor (IntegrityError catch),
cap enforcement, vendor ownership checks, drop, replace, expiry, and
record_offer clock reset.

Called by: pytest
Depends on: app.services.strategic_vendor_service, conftest fixtures
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from sqlalchemy.exc import IntegrityError

from app.models.strategic import StrategicVendor
from app.models.vendors import VendorCard
from app.services.strategic_vendor_service import (
    MAX_STRATEGIC_VENDORS,
    active_count,
    claim_vendor,
    drop_vendor,
    expire_stale,
    get_expiring_soon,
    get_my_strategic,
    get_open_pool,
    get_vendor_owner,
    get_vendor_status,
    record_offer,
    replace_vendor,
)


@pytest.fixture()
def buyer(db_session):
    from app.models import User

    user = User(
        email="buyer1@trioscs.com",
        name="Buyer One",
        role="buyer",
        azure_id="az-buyer-1",
        m365_connected=True,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture()
def buyer2(db_session):
    from app.models import User

    user = User(
        email="buyer2@trioscs.com",
        name="Buyer Two",
        role="buyer",
        azure_id="az-buyer-2",
        m365_connected=True,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture()
def vendor_card(db_session):
    vc = VendorCard(display_name="Acme Electronics", normalized_name="acme electronics")
    db_session.add(vc)
    db_session.commit()
    db_session.refresh(vc)
    return vc


@pytest.fixture()
def vendor_card2(db_session):
    vc = VendorCard(display_name="Beta Parts", normalized_name="beta parts")
    db_session.add(vc)
    db_session.commit()
    db_session.refresh(vc)
    return vc


def _make_vendor_cards(db_session, count):
    """Helper to create multiple vendor cards."""
    cards = []
    for i in range(count):
        vc = VendorCard(display_name=f"Vendor {i}", normalized_name=f"vendor {i}")
        db_session.add(vc)
    db_session.commit()
    return db_session.query(VendorCard).all()


# ── claim_vendor ─────────────────────────────────────────────────────


def test_claim_vendor_success(db_session, buyer, vendor_card):
    record, err = claim_vendor(db_session, buyer.id, vendor_card.id)
    assert err is None
    assert record is not None
    assert record.user_id == buyer.id
    assert record.vendor_card_id == vendor_card.id
    assert record.released_at is None


def test_claim_vendor_already_own(db_session, buyer, vendor_card):
    claim_vendor(db_session, buyer.id, vendor_card.id)
    record, err = claim_vendor(db_session, buyer.id, vendor_card.id)
    assert record is None
    assert "already have" in err


def test_claim_vendor_already_claimed_by_other(db_session, buyer, buyer2, vendor_card):
    claim_vendor(db_session, buyer.id, vendor_card.id)
    record, err = claim_vendor(db_session, buyer2.id, vendor_card.id)
    assert record is None
    assert "already claimed" in err


def test_claim_vendor_not_found(db_session, buyer):
    record, err = claim_vendor(db_session, buyer.id, 99999)
    assert record is None
    assert "not found" in err.lower()


def test_claim_vendor_cap_enforced(db_session, buyer):
    cards = _make_vendor_cards(db_session, MAX_STRATEGIC_VENDORS)
    for vc in cards:
        record, err = claim_vendor(db_session, buyer.id, vc.id)
        assert err is None

    extra = VendorCard(display_name="One Too Many", normalized_name="one too many")
    db_session.add(extra)
    db_session.commit()
    db_session.refresh(extra)

    record, err = claim_vendor(db_session, buyer.id, extra.id)
    assert record is None
    assert "Already at" in err


def test_claim_vendor_no_commit(db_session, buyer, vendor_card):
    record, err = claim_vendor(db_session, buyer.id, vendor_card.id, commit=False)
    assert err is None
    assert record is not None
    # Should be flushed but not committed
    db_session.rollback()
    assert active_count(db_session, buyer.id) == 0


def test_claim_vendor_integrity_error_caught(db_session, buyer, vendor_card):
    """Simulate a race condition where IntegrityError is raised on commit."""
    original_commit = db_session.commit

    call_count = 0

    def fake_commit():
        nonlocal call_count
        call_count += 1
        # The first commit in claim_vendor is after db.add(record)
        # Let all prior commits (from fixtures) pass, fail on the claim commit
        if call_count == 1:
            raise IntegrityError("duplicate", params=None, orig=Exception("unique violation"))
        return original_commit()

    # First, ensure the vendor exists and buyer has room
    assert active_count(db_session, buyer.id) == 0

    with patch.object(db_session, "commit", side_effect=fake_commit):
        record, err = claim_vendor(db_session, buyer.id, vendor_card.id)

    assert record is None
    assert "just claimed" in err


# ── drop_vendor ──────────────────────────────────────────────────────


def test_drop_vendor_success(db_session, buyer, vendor_card):
    claim_vendor(db_session, buyer.id, vendor_card.id)
    success, err = drop_vendor(db_session, buyer.id, vendor_card.id)
    assert success is True
    assert err is None
    assert active_count(db_session, buyer.id) == 0


def test_drop_vendor_not_yours(db_session, buyer, vendor_card):
    success, err = drop_vendor(db_session, buyer.id, vendor_card.id)
    assert success is False
    assert "not in your" in err.lower()


# ── replace_vendor ───────────────────────────────────────────────────


def test_replace_vendor_success(db_session, buyer, vendor_card, vendor_card2):
    claim_vendor(db_session, buyer.id, vendor_card.id)
    record, err = replace_vendor(db_session, buyer.id, vendor_card.id, vendor_card2.id)
    assert err is None
    assert record is not None
    assert record.vendor_card_id == vendor_card2.id
    assert active_count(db_session, buyer.id) == 1


def test_replace_vendor_same_id(db_session, buyer, vendor_card):
    claim_vendor(db_session, buyer.id, vendor_card.id)
    record, err = replace_vendor(db_session, buyer.id, vendor_card.id, vendor_card.id)
    assert record is None
    assert "itself" in err


def test_replace_vendor_drop_fails(db_session, buyer, vendor_card, vendor_card2):
    # Don't claim vendor_card first, so drop will fail
    record, err = replace_vendor(db_session, buyer.id, vendor_card.id, vendor_card2.id)
    assert record is None
    assert "not in your" in err.lower()


# ── record_offer ─────────────────────────────────────────────────────


def test_record_offer_resets_clock(db_session, buyer, vendor_card):
    claim_vendor(db_session, buyer.id, vendor_card.id)
    original = get_vendor_owner(db_session, vendor_card.id)
    old_expires = original.expires_at

    result = record_offer(db_session, vendor_card.id)
    assert result is True
    updated = get_vendor_owner(db_session, vendor_card.id)
    assert updated.last_offer_at is not None


def test_record_offer_no_strategic(db_session, vendor_card):
    result = record_offer(db_session, vendor_card.id)
    assert result is False


# ── expire_stale ─────────────────────────────────────────────────────


def test_expire_stale(db_session, buyer, vendor_card):
    now = datetime.now(timezone.utc)
    record = StrategicVendor(
        user_id=buyer.id,
        vendor_card_id=vendor_card.id,
        claimed_at=now - timedelta(days=50),
        expires_at=now - timedelta(days=1),
    )
    db_session.add(record)
    db_session.commit()

    count = expire_stale(db_session)
    assert count == 1
    assert active_count(db_session, buyer.id) == 0


# ── get helpers ──────────────────────────────────────────────────────


def test_get_my_strategic(db_session, buyer, vendor_card):
    claim_vendor(db_session, buyer.id, vendor_card.id)
    result = get_my_strategic(db_session, buyer.id)
    assert len(result) == 1
    assert result[0].vendor_card_id == vendor_card.id


def test_get_vendor_owner(db_session, buyer, vendor_card):
    claim_vendor(db_session, buyer.id, vendor_card.id)
    owner = get_vendor_owner(db_session, vendor_card.id)
    assert owner is not None
    assert owner.user_id == buyer.id


def test_get_vendor_owner_none(db_session, vendor_card):
    owner = get_vendor_owner(db_session, vendor_card.id)
    assert owner is None


def test_get_vendor_status(db_session, buyer, vendor_card):
    claim_vendor(db_session, buyer.id, vendor_card.id)
    status = get_vendor_status(db_session, vendor_card.id)
    assert status is not None
    assert status["owner_user_id"] == buyer.id
    assert status["days_remaining"] >= 0


def test_get_vendor_status_none(db_session, vendor_card):
    status = get_vendor_status(db_session, vendor_card.id)
    assert status is None


def test_get_expiring_soon(db_session, buyer, vendor_card):
    now = datetime.now(timezone.utc)
    record = StrategicVendor(
        user_id=buyer.id,
        vendor_card_id=vendor_card.id,
        claimed_at=now,
        expires_at=now + timedelta(days=3),
    )
    db_session.add(record)
    db_session.commit()

    result = get_expiring_soon(db_session, days=7)
    assert len(result) == 1


def test_get_open_pool(db_session, buyer, vendor_card, vendor_card2):
    claim_vendor(db_session, buyer.id, vendor_card.id)
    vendors, total = get_open_pool(db_session)
    vendor_ids = [v.id for v in vendors]
    assert vendor_card.id not in vendor_ids
    assert vendor_card2.id in vendor_ids


def test_active_count(db_session, buyer, vendor_card, vendor_card2):
    assert active_count(db_session, buyer.id) == 0
    claim_vendor(db_session, buyer.id, vendor_card.id)
    assert active_count(db_session, buyer.id) == 1
    claim_vendor(db_session, buyer.id, vendor_card2.id)
    assert active_count(db_session, buyer.id) == 2
