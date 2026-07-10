"""test_strategic_vendor_service.py — Tests for strategic_vendor_service.py.

Covers: claim_vendor, drop_vendor, replace_vendor, record_offer, expire_stale,
        get_my_strategic, active_count, get_vendor_owner, get_vendor_status,
        get_open_pool, get_expiring_soon.

Called by: pytest
Depends on: app/services/strategic_vendor_service.py, tests/conftest.py
"""

from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from app.models import User, VendorCard
from app.models.strategic import StrategicVendor
from app.services.strategic_vendor_service import (
    MAX_STRATEGIC_VENDORS,
    TTL_DAYS,
    _ensure_utc,
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
from tests.conftest import engine  # noqa: F401

# ── Helpers ──────────────────────────────────────────────────────────────


def _make_user(db: Session, email: str = "buyer@trioscs.com", role: str = "buyer") -> User:
    u = User(
        email=email,
        name=email.split("@")[0],
        role=role,
        azure_id=f"az-{email}",
        created_at=datetime.now(UTC),
    )
    db.add(u)
    db.flush()
    return u


def _make_vendor(db: Session, name: str = "Acme") -> VendorCard:
    card = VendorCard(
        normalized_name=name.lower(),
        display_name=name,
        emails=[],
        phones=[],
        sighting_count=0,
        created_at=datetime.now(UTC),
    )
    db.add(card)
    db.flush()
    return card


def _make_strategic(
    db: Session,
    user: User,
    vendor: VendorCard,
    *,
    released_at=None,
    expires_at=None,
) -> StrategicVendor:
    now = datetime.now(UTC)
    sv = StrategicVendor(
        user_id=user.id,
        vendor_card_id=vendor.id,
        claimed_at=now,
        expires_at=expires_at or (now + timedelta(days=TTL_DAYS)),
        released_at=released_at,
    )
    db.add(sv)
    db.flush()
    return sv


# ── _ensure_utc ──────────────────────────────────────────────────────────


class TestEnsureUtc:
    def test_none_passes_through(self):
        assert _ensure_utc(None) is None

    def test_naive_datetime_gets_utc(self):
        naive = datetime(2025, 1, 1, 12, 0, 0)
        result = _ensure_utc(naive)
        assert result.tzinfo is not None
        assert result.year == 2025

    def test_aware_datetime_unchanged(self):
        aware = datetime(2025, 1, 1, 12, 0, 0, tzinfo=UTC)
        result = _ensure_utc(aware)
        assert result == aware


# ── get_my_strategic ─────────────────────────────────────────────────────


class TestGetMyStrategic:
    def test_returns_active_vendors_only(self, db_session: Session):
        user = _make_user(db_session)
        v1 = _make_vendor(db_session, "Alpha")
        v2 = _make_vendor(db_session, "Beta")
        _make_strategic(db_session, user, v1)
        _make_strategic(db_session, user, v2, released_at=datetime.now(UTC))
        db_session.commit()

        result = get_my_strategic(db_session, user.id)
        assert len(result) == 1
        assert result[0].vendor_card_id == v1.id

    def test_empty_for_new_user(self, db_session: Session):
        user = _make_user(db_session)
        db_session.commit()

        result = get_my_strategic(db_session, user.id)
        assert result == []


# ── active_count ─────────────────────────────────────────────────────────


class TestActiveCount:
    def test_counts_only_active(self, db_session: Session):
        user = _make_user(db_session)
        v1 = _make_vendor(db_session, "V1")
        v2 = _make_vendor(db_session, "V2")
        _make_strategic(db_session, user, v1)
        _make_strategic(db_session, user, v2, released_at=datetime.now(UTC))
        db_session.commit()

        assert active_count(db_session, user.id) == 1

    def test_zero_for_new_user(self, db_session: Session):
        user = _make_user(db_session)
        db_session.commit()

        assert active_count(db_session, user.id) == 0


# ── get_vendor_owner ─────────────────────────────────────────────────────


class TestGetVendorOwner:
    def test_returns_active_record(self, db_session: Session):
        user = _make_user(db_session)
        vendor = _make_vendor(db_session, "Apex")
        _make_strategic(db_session, user, vendor)
        db_session.commit()

        record = get_vendor_owner(db_session, vendor.id)
        assert record is not None
        assert record.user_id == user.id

    def test_returns_none_for_unclaimed_vendor(self, db_session: Session):
        vendor = _make_vendor(db_session, "Unclaimed")
        db_session.commit()

        assert get_vendor_owner(db_session, vendor.id) is None

    def test_returns_none_after_release(self, db_session: Session):
        user = _make_user(db_session)
        vendor = _make_vendor(db_session, "Released")
        _make_strategic(db_session, user, vendor, released_at=datetime.now(UTC))
        db_session.commit()

        assert get_vendor_owner(db_session, vendor.id) is None


# ── claim_vendor ─────────────────────────────────────────────────────────


class TestClaimVendor:
    def test_successful_claim(self, db_session: Session):
        user = _make_user(db_session)
        vendor = _make_vendor(db_session, "ClaimMe")
        db_session.commit()

        record, err = claim_vendor(db_session, user.id, vendor.id)
        assert err is None
        assert record is not None
        assert record.user_id == user.id
        assert record.vendor_card_id == vendor.id

    def test_claim_nonexistent_vendor_returns_error(self, db_session: Session):
        user = _make_user(db_session)
        db_session.commit()

        record, err = claim_vendor(db_session, user.id, 999999)
        assert record is None
        assert "not found" in err.lower()

    def test_already_claimed_by_same_user_returns_error(self, db_session: Session):
        user = _make_user(db_session)
        vendor = _make_vendor(db_session, "Mine")
        _make_strategic(db_session, user, vendor)
        db_session.commit()

        record, err = claim_vendor(db_session, user.id, vendor.id)
        assert record is None
        assert "already have" in err.lower()

    def test_already_claimed_by_other_user_returns_error(self, db_session: Session):
        user1 = _make_user(db_session, "buyer1@t.com")
        user2 = _make_user(db_session, "buyer2@t.com")
        vendor = _make_vendor(db_session, "Contested")
        _make_strategic(db_session, user1, vendor)
        db_session.commit()

        record, err = claim_vendor(db_session, user2.id, vendor.id)
        assert record is None
        assert "another buyer" in err.lower()

    def test_cap_enforced_at_max(self, db_session: Session):
        user = _make_user(db_session)
        vendors = [_make_vendor(db_session, f"V{i}") for i in range(MAX_STRATEGIC_VENDORS)]
        for v in vendors:
            _make_strategic(db_session, user, v)
        extra = _make_vendor(db_session, "Extra")
        db_session.commit()

        record, err = claim_vendor(db_session, user.id, extra.id)
        assert record is None
        assert str(MAX_STRATEGIC_VENDORS) in err

    def test_claim_with_commit_false_does_flush_not_commit(self, db_session: Session):
        user = _make_user(db_session)
        vendor = _make_vendor(db_session, "FlushOnly")
        db_session.commit()

        record, err = claim_vendor(db_session, user.id, vendor.id, commit=False)
        assert err is None
        assert record is not None
        # Row should be flushed (has ID) but not committed
        assert record.id is not None


# ── drop_vendor ──────────────────────────────────────────────────────────


class TestDropVendor:
    def test_successful_drop(self, db_session: Session):
        user = _make_user(db_session)
        vendor = _make_vendor(db_session, "DropMe")
        _make_strategic(db_session, user, vendor)
        db_session.commit()

        success, err = drop_vendor(db_session, user.id, vendor.id)
        assert success is True
        assert err is None

        # Verify released
        assert get_vendor_owner(db_session, vendor.id) is None

    def test_drop_not_owned_returns_error(self, db_session: Session):
        user = _make_user(db_session)
        vendor = _make_vendor(db_session, "NotMine")
        db_session.commit()

        success, err = drop_vendor(db_session, user.id, vendor.id)
        assert success is False
        assert "not in your" in err.lower()

    def test_drop_commit_false(self, db_session: Session):
        user = _make_user(db_session)
        vendor = _make_vendor(db_session, "FlushDrop")
        sv = _make_strategic(db_session, user, vendor)
        db_session.commit()

        success, err = drop_vendor(db_session, user.id, vendor.id, commit=False)
        assert success is True
        assert err is None
        # released_at should be set even without commit
        db_session.refresh(sv)
        assert sv.released_at is not None


# ── replace_vendor ───────────────────────────────────────────────────────


class TestReplaceVendor:
    def test_same_vendor_id_returns_error(self, db_session: Session):
        user = _make_user(db_session)
        db_session.commit()

        record, err = replace_vendor(db_session, user.id, 1, 1)
        assert record is None
        assert "itself" in err.lower()

    def test_successful_replace(self, db_session: Session):
        user = _make_user(db_session)
        old_vendor = _make_vendor(db_session, "OldVendor")
        new_vendor = _make_vendor(db_session, "NewVendor")
        _make_strategic(db_session, user, old_vendor)
        db_session.commit()

        record, err = replace_vendor(db_session, user.id, old_vendor.id, new_vendor.id)
        assert err is None
        assert record is not None
        assert record.vendor_card_id == new_vendor.id

        # Old vendor should be released
        assert get_vendor_owner(db_session, old_vendor.id) is None

    def test_replace_drop_fails_returns_error(self, db_session: Session):
        user = _make_user(db_session)
        new_vendor = _make_vendor(db_session, "NewOnly")
        db_session.commit()

        # Try to drop a vendor the user doesn't own
        record, err = replace_vendor(db_session, user.id, 999999, new_vendor.id)
        assert record is None
        assert err is not None


# ── record_offer ─────────────────────────────────────────────────────────


class TestRecordOffer:
    def test_resets_ttl_for_strategic_vendor(self, db_session: Session):
        user = _make_user(db_session)
        vendor = _make_vendor(db_session, "ActiveVendor")
        sv = _make_strategic(db_session, user, vendor)
        db_session.commit()

        result = record_offer(db_session, vendor.id)
        assert result is True

        db_session.refresh(sv)
        # expires_at and last_offer_at should be set/extended
        assert sv.last_offer_at is not None
        # expires_at should be approximately TTL_DAYS from now
        now = datetime.now(UTC)
        expires = sv.expires_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=UTC)
        assert expires > now

    def test_no_strategic_record_returns_false(self, db_session: Session):
        vendor = _make_vendor(db_session, "NoStrategic")
        db_session.commit()

        result = record_offer(db_session, vendor.id)
        assert result is False


# ── expire_stale ─────────────────────────────────────────────────────────


class TestExpireStale:
    def test_expires_past_ttl_records(self, db_session: Session):
        user = _make_user(db_session)
        vendor = _make_vendor(db_session, "StaleVendor")
        sv = _make_strategic(
            db_session,
            user,
            vendor,
            expires_at=datetime.now(UTC) - timedelta(days=1),
        )
        db_session.commit()

        count = expire_stale(db_session)
        assert count == 1

        db_session.refresh(sv)
        assert sv.released_at is not None
        assert sv.release_reason == "expired"

    def test_does_not_expire_active_records(self, db_session: Session):
        user = _make_user(db_session)
        vendor = _make_vendor(db_session, "FreshVendor")
        _make_strategic(
            db_session,
            user,
            vendor,
            expires_at=datetime.now(UTC) + timedelta(days=30),
        )
        db_session.commit()

        count = expire_stale(db_session)
        assert count == 0

    def test_empty_db_returns_zero(self, db_session: Session):
        assert expire_stale(db_session) == 0


# ── get_expiring_soon ────────────────────────────────────────────────────


class TestGetExpiringSoon:
    def test_returns_vendors_expiring_within_window(self, db_session: Session):
        user = _make_user(db_session)
        v1 = _make_vendor(db_session, "ExpiringSoon")
        v2 = _make_vendor(db_session, "ExpiringLater")
        _make_strategic(db_session, user, v1, expires_at=datetime.now(UTC) + timedelta(days=3))
        _make_strategic(db_session, user, v2, expires_at=datetime.now(UTC) + timedelta(days=30))
        db_session.commit()

        results = get_expiring_soon(db_session, days=7)
        assert len(results) == 1
        assert results[0].vendor_card_id == v1.id

    def test_empty_when_none_expiring_soon(self, db_session: Session):
        user = _make_user(db_session)
        vendor = _make_vendor(db_session, "FarFuture")
        _make_strategic(db_session, user, vendor, expires_at=datetime.now(UTC) + timedelta(days=60))
        db_session.commit()

        results = get_expiring_soon(db_session, days=7)
        assert results == []


# ── get_vendor_status ────────────────────────────────────────────────────


class TestGetVendorStatus:
    def test_returns_none_for_unclaimed_vendor(self, db_session: Session):
        vendor = _make_vendor(db_session, "OpenVendor")
        db_session.commit()

        assert get_vendor_status(db_session, vendor.id) is None

    def test_returns_status_dict_for_claimed_vendor(self, db_session: Session):
        user = _make_user(db_session)
        vendor = _make_vendor(db_session, "ClaimedVendor")
        _make_strategic(db_session, user, vendor)
        db_session.commit()

        status = get_vendor_status(db_session, vendor.id)
        assert status is not None
        assert status["vendor_card_id"] == vendor.id
        assert status["owner_user_id"] == user.id
        assert "days_remaining" in status
        assert status["days_remaining"] >= 0


# ── get_open_pool ────────────────────────────────────────────────────────


class TestGetOpenPool:
    def test_returns_unclaimed_vendors(self, db_session: Session):
        user = _make_user(db_session)
        v_claimed = _make_vendor(db_session, "Claimed")
        v_open1 = _make_vendor(db_session, "Open Alpha")
        v_open2 = _make_vendor(db_session, "Open Beta")
        _make_strategic(db_session, user, v_claimed)
        db_session.commit()

        vendors, total = get_open_pool(db_session)
        ids = {v.id for v in vendors}
        assert v_claimed.id not in ids
        assert v_open1.id in ids
        assert v_open2.id in ids
        assert total == 2

    def test_search_filters_by_name(self, db_session: Session):
        _make_vendor(db_session, "Alpha Elec")
        _make_vendor(db_session, "Beta Comp")
        db_session.commit()

        vendors, total = get_open_pool(db_session, search="alpha")
        assert total == 1
        assert vendors[0].display_name == "Alpha Elec"

    def test_empty_pool(self, db_session: Session):
        vendors, total = get_open_pool(db_session)
        assert vendors == []
        assert total == 0
