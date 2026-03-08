"""
test_strategic_vendors.py — Tests for Strategic Vendor assignments.

Tests the 10-cap, 39-day TTL, claim/drop/replace flows, offer clock
reset, expiry logic, and API endpoints.

Depends on: conftest.py fixtures, app/services/strategic_vendor_service.py,
            app/routers/strategic.py
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.orm import Session

from app.models import User, VendorCard
from app.models.strategic import StrategicVendor
from app.services import strategic_vendor_service as svc


def _utcnow_naive():
    """Return current UTC time without timezone info (matches SQLite storage)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ── Helpers ──────────────────────────────────────────────────────────

def _make_vendor(db: Session, name: str) -> VendorCard:
    """Create a vendor card with the given name."""
    v = VendorCard(
        normalized_name=name.lower(),
        display_name=name,
        created_at=datetime.now(timezone.utc),
    )
    db.add(v)
    db.commit()
    db.refresh(v)
    return v


def _make_user(db: Session, email: str) -> User:
    """Create a buyer user."""
    u = User(
        email=email,
        name=email.split("@")[0],
        role="buyer",
        azure_id=f"azure-{email}",
        created_at=datetime.now(timezone.utc),
    )
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


# ── Service layer tests ─────────────────────────────────────────────

class TestClaimVendor:
    def test_claim_success(self, db_session: Session, test_user: User, test_vendor_card: VendorCard):
        record, err = svc.claim_vendor(db_session, test_user.id, test_vendor_card.id)
        assert record is not None
        assert err is None
        assert record.user_id == test_user.id
        assert record.vendor_card_id == test_vendor_card.id
        assert record.released_at is None
        assert record.expires_at > _utcnow_naive()

    def test_claim_sets_39_day_ttl(self, db_session: Session, test_user: User, test_vendor_card: VendorCard):
        record, _ = svc.claim_vendor(db_session, test_user.id, test_vendor_card.id)
        delta = record.expires_at - record.claimed_at
        assert 38 <= delta.days <= 39

    def test_claim_already_owned_by_self(self, db_session: Session, test_user: User, test_vendor_card: VendorCard):
        svc.claim_vendor(db_session, test_user.id, test_vendor_card.id)
        record, err = svc.claim_vendor(db_session, test_user.id, test_vendor_card.id)
        assert record is None
        assert "already have" in err

    def test_claim_already_owned_by_other(self, db_session: Session, test_user: User, test_vendor_card: VendorCard):
        other = _make_user(db_session, "other@trioscs.com")
        svc.claim_vendor(db_session, test_user.id, test_vendor_card.id)
        record, err = svc.claim_vendor(db_session, other.id, test_vendor_card.id)
        assert record is None
        assert "already claimed" in err

    def test_claim_nonexistent_vendor(self, db_session: Session, test_user: User):
        record, err = svc.claim_vendor(db_session, test_user.id, 99999)
        assert record is None
        assert "not found" in err

    def test_claim_at_cap_fails(self, db_session: Session, test_user: User):
        for i in range(10):
            v = _make_vendor(db_session, f"Vendor {i}")
            svc.claim_vendor(db_session, test_user.id, v.id)
        extra = _make_vendor(db_session, "Vendor Extra")
        record, err = svc.claim_vendor(db_session, test_user.id, extra.id)
        assert record is None
        assert "10" in err


class TestDropVendor:
    def test_drop_success(self, db_session: Session, test_user: User, test_vendor_card: VendorCard):
        svc.claim_vendor(db_session, test_user.id, test_vendor_card.id)
        ok, err = svc.drop_vendor(db_session, test_user.id, test_vendor_card.id)
        assert ok is True
        assert err is None
        # Verify released
        record = db_session.query(StrategicVendor).filter_by(
            user_id=test_user.id, vendor_card_id=test_vendor_card.id
        ).first()
        assert record.released_at is not None
        assert record.release_reason == "dropped"

    def test_drop_not_in_list(self, db_session: Session, test_user: User, test_vendor_card: VendorCard):
        ok, err = svc.drop_vendor(db_session, test_user.id, test_vendor_card.id)
        assert ok is False
        assert "not in your" in err

    def test_drop_frees_slot(self, db_session: Session, test_user: User):
        vendors = [_make_vendor(db_session, f"V{i}") for i in range(10)]
        for v in vendors:
            svc.claim_vendor(db_session, test_user.id, v.id)
        assert svc.active_count(db_session, test_user.id) == 10
        svc.drop_vendor(db_session, test_user.id, vendors[0].id)
        assert svc.active_count(db_session, test_user.id) == 9
        new_v = _make_vendor(db_session, "New Vendor")
        record, err = svc.claim_vendor(db_session, test_user.id, new_v.id)
        assert record is not None


class TestReplaceVendor:
    def test_replace_success(self, db_session: Session, test_user: User):
        v1 = _make_vendor(db_session, "Old Vendor")
        v2 = _make_vendor(db_session, "New Vendor")
        svc.claim_vendor(db_session, test_user.id, v1.id)
        record, err = svc.replace_vendor(db_session, test_user.id, v1.id, v2.id)
        assert record is not None
        assert record.vendor_card_id == v2.id
        assert svc.active_count(db_session, test_user.id) == 1

    def test_replace_same_vendor_fails(self, db_session: Session, test_user: User):
        v = _make_vendor(db_session, "Same Vendor")
        svc.claim_vendor(db_session, test_user.id, v.id)
        record, err = svc.replace_vendor(db_session, test_user.id, v.id, v.id)
        assert record is None
        assert "itself" in err


class TestRecordOffer:
    def test_offer_resets_clock(self, db_session: Session, test_user: User, test_vendor_card: VendorCard):
        record, _ = svc.claim_vendor(db_session, test_user.id, test_vendor_card.id)
        # Simulate time passing by backdating expires_at
        record.expires_at = _utcnow_naive() + timedelta(days=5)
        db_session.commit()
        updated = svc.record_offer(db_session, test_vendor_card.id)
        assert updated is True
        db_session.refresh(record)
        # New expires_at should be ~39 days from now
        delta = record.expires_at - _utcnow_naive()
        assert delta.days >= 38
        assert record.last_offer_at is not None

    def test_offer_non_strategic_returns_false(self, db_session: Session, test_vendor_card: VendorCard):
        assert svc.record_offer(db_session, test_vendor_card.id) is False


class TestExpireStale:
    def test_expire_past_ttl(self, db_session: Session, test_user: User, test_vendor_card: VendorCard):
        record, _ = svc.claim_vendor(db_session, test_user.id, test_vendor_card.id)
        record.expires_at = datetime.now(timezone.utc) - timedelta(hours=1)
        db_session.commit()
        count = svc.expire_stale(db_session)
        assert count == 1
        db_session.refresh(record)
        assert record.released_at is not None
        assert record.release_reason == "expired"

    def test_expire_skips_active(self, db_session: Session, test_user: User, test_vendor_card: VendorCard):
        svc.claim_vendor(db_session, test_user.id, test_vendor_card.id)
        count = svc.expire_stale(db_session)
        assert count == 0


class TestQueries:
    def test_get_my_strategic(self, db_session: Session, test_user: User):
        v1 = _make_vendor(db_session, "Alpha")
        v2 = _make_vendor(db_session, "Beta")
        svc.claim_vendor(db_session, test_user.id, v1.id)
        svc.claim_vendor(db_session, test_user.id, v2.id)
        result = svc.get_my_strategic(db_session, test_user.id)
        assert len(result) == 2

    def test_get_my_strategic_excludes_released(self, db_session: Session, test_user: User):
        v = _make_vendor(db_session, "Released Vendor")
        svc.claim_vendor(db_session, test_user.id, v.id)
        svc.drop_vendor(db_session, test_user.id, v.id)
        result = svc.get_my_strategic(db_session, test_user.id)
        assert len(result) == 0

    def test_get_vendor_status(self, db_session: Session, test_user: User, test_vendor_card: VendorCard):
        svc.claim_vendor(db_session, test_user.id, test_vendor_card.id)
        status = svc.get_vendor_status(db_session, test_vendor_card.id)
        assert status is not None
        assert status["owner_user_id"] == test_user.id
        assert status["days_remaining"] > 0

    def test_get_vendor_status_open_pool(self, db_session: Session, test_vendor_card: VendorCard):
        assert svc.get_vendor_status(db_session, test_vendor_card.id) is None

    def test_get_expiring_soon(self, db_session: Session, test_user: User):
        v = _make_vendor(db_session, "Expiring Soon")
        record, _ = svc.claim_vendor(db_session, test_user.id, v.id)
        record.expires_at = datetime.now(timezone.utc) + timedelta(days=3)
        db_session.commit()
        expiring = svc.get_expiring_soon(db_session, days=7)
        assert len(expiring) == 1
        assert expiring[0].vendor_card_id == v.id

    def test_open_pool(self, db_session: Session, test_user: User, test_vendor_card: VendorCard):
        v2 = _make_vendor(db_session, "Unclaimed")
        svc.claim_vendor(db_session, test_user.id, test_vendor_card.id)
        vendors, total = svc.get_open_pool(db_session)
        vendor_ids = [v.id for v in vendors]
        assert test_vendor_card.id not in vendor_ids
        assert v2.id in vendor_ids

    def test_open_pool_search(self, db_session: Session):
        _make_vendor(db_session, "Acme Corp")
        _make_vendor(db_session, "Beta Inc")
        vendors, total = svc.get_open_pool(db_session, search="Acme")
        assert total == 1
        assert vendors[0].display_name == "Acme Corp"


# ── API endpoint tests ──────────────────────────────────────────────

class TestStrategicAPI:
    def test_get_mine(self, client, db_session, test_user, test_vendor_card):
        svc.claim_vendor(db_session, test_user.id, test_vendor_card.id)
        resp = client.get("/api/strategic-vendors/mine")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["vendors"]) == 1
        assert data["vendors"][0]["vendor_card_id"] == test_vendor_card.id

    def test_claim_endpoint(self, client, db_session, test_user, test_vendor_card):
        resp = client.post(f"/api/strategic-vendors/claim/{test_vendor_card.id}")
        assert resp.status_code == 200
        assert resp.json()["vendor_card_id"] == test_vendor_card.id

    def test_claim_at_cap_returns_409(self, client, db_session, test_user):
        for i in range(10):
            v = _make_vendor(db_session, f"Cap Vendor {i}")
            svc.claim_vendor(db_session, test_user.id, v.id)
        extra = _make_vendor(db_session, "Over Cap")
        resp = client.post(f"/api/strategic-vendors/claim/{extra.id}")
        assert resp.status_code == 409

    def test_drop_endpoint(self, client, db_session, test_user, test_vendor_card):
        svc.claim_vendor(db_session, test_user.id, test_vendor_card.id)
        resp = client.delete(f"/api/strategic-vendors/drop/{test_vendor_card.id}")
        assert resp.status_code == 200

    def test_replace_endpoint(self, client, db_session, test_user):
        v1 = _make_vendor(db_session, "Replace Old")
        v2 = _make_vendor(db_session, "Replace New")
        svc.claim_vendor(db_session, test_user.id, v1.id)
        resp = client.post("/api/strategic-vendors/replace", json={
            "drop_vendor_card_id": v1.id,
            "claim_vendor_card_id": v2.id,
        })
        assert resp.status_code == 200
        assert resp.json()["vendor_card_id"] == v2.id

    def test_status_endpoint(self, client, db_session, test_user, test_vendor_card):
        svc.claim_vendor(db_session, test_user.id, test_vendor_card.id)
        resp = client.get(f"/api/strategic-vendors/status/{test_vendor_card.id}")
        assert resp.status_code == 200
        assert resp.json()["owner_user_id"] == test_user.id

    def test_open_pool_endpoint(self, client, db_session, test_vendor_card):
        resp = client.get("/api/strategic-vendors/open-pool")
        assert resp.status_code == 200
        assert resp.json()["total"] >= 1
