"""Tests for requisition_service — date normalization, validation, error mapping."""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from app.services.requisition_service import (
    clone_requisition,
    parse_date_field,
    parse_positive_int,
    safe_commit,
    to_utc,
)

# ---------------------------------------------------------------------------
# to_utc()
# ---------------------------------------------------------------------------


class TestToUtc:
    def test_none_returns_none(self):
        assert to_utc(None) is None

    def test_naive_datetime_gets_utc(self):
        naive = datetime(2026, 3, 11, 12, 0, 0)
        result = to_utc(naive)
        assert result is not None
        assert result.tzinfo == timezone.utc
        assert result.year == 2026
        assert result.hour == 12

    def test_utc_datetime_unchanged(self):
        aware = datetime(2026, 3, 11, 12, 0, 0, tzinfo=timezone.utc)
        result = to_utc(aware)
        assert result == aware

    def test_non_utc_aware_converted(self):
        eastern = timezone(timedelta(hours=-5))
        aware = datetime(2026, 3, 11, 12, 0, 0, tzinfo=eastern)
        result = to_utc(aware)
        assert result is not None
        assert result.tzinfo == timezone.utc
        assert result.hour == 17  # 12 EST = 17 UTC


# ---------------------------------------------------------------------------
# parse_date_field()
# ---------------------------------------------------------------------------


class TestParseDateField:
    def test_valid_iso_string(self):
        result = parse_date_field("2026-03-11T10:00:00")
        assert result.year == 2026
        assert result.tzinfo == timezone.utc

    def test_valid_iso_with_tz(self):
        result = parse_date_field("2026-03-11T10:00:00+00:00")
        assert result.tzinfo == timezone.utc

    def test_invalid_string_raises_400(self):
        with pytest.raises(HTTPException) as exc_info:
            parse_date_field("not-a-date", field_name="deadline")
        assert exc_info.value.status_code == 400
        assert "deadline" in exc_info.value.detail

    def test_empty_string_raises_400(self):
        with pytest.raises(HTTPException) as exc_info:
            parse_date_field("")
        assert exc_info.value.status_code == 400


# ---------------------------------------------------------------------------
# parse_positive_int()
# ---------------------------------------------------------------------------


class TestParsePositiveInt:
    def test_valid_int(self):
        assert parse_positive_int(5) == 5

    def test_valid_string(self):
        assert parse_positive_int("42") == 42

    def test_zero_raises_400(self):
        with pytest.raises(HTTPException) as exc_info:
            parse_positive_int(0, field_name="qty")
        assert exc_info.value.status_code == 400
        assert "qty" in exc_info.value.detail

    def test_negative_raises_400(self):
        with pytest.raises(HTTPException) as exc_info:
            parse_positive_int(-1)
        assert exc_info.value.status_code == 400

    def test_non_numeric_raises_400(self):
        with pytest.raises(HTTPException) as exc_info:
            parse_positive_int("abc", field_name="target_qty")
        assert exc_info.value.status_code == 400
        assert "target_qty" in exc_info.value.detail

    def test_none_raises_400(self):
        with pytest.raises(HTTPException) as exc_info:
            parse_positive_int(None)  # type: ignore[arg-type]
        assert exc_info.value.status_code == 400


# ---------------------------------------------------------------------------
# safe_commit()
# ---------------------------------------------------------------------------


class TestSafeCommit:
    def test_successful_commit(self):
        db = MagicMock()
        safe_commit(db, entity="test")
        db.commit.assert_called_once()

    def test_integrity_error_raises_409(self):
        from sqlalchemy.exc import IntegrityError

        db = MagicMock()
        db.commit.side_effect = IntegrityError("dup", {}, Exception("unique"))
        with pytest.raises(HTTPException) as exc_info:
            safe_commit(db, entity="requisition")
        assert exc_info.value.status_code == 409
        assert "requisition" in exc_info.value.detail
        db.rollback.assert_called_once()


def test_clone_requisition_duplicate_mpn_preserves_offer_mapping(db_session, test_user):
    """Clone keeps offers mapped to distinct cloned requirement rows even with duplicate MPNs."""
    from app.models import Offer, Requirement, Requisition

    src = Requisition(
        name="SRC-REQ",
        customer_name="Acme",
        status="active",
        created_by=test_user.id,
    )
    db_session.add(src)
    db_session.flush()

    r1 = Requirement(requisition_id=src.id, primary_mpn="LM317T", target_qty=10)
    r2 = Requirement(requisition_id=src.id, primary_mpn="LM317T", target_qty=20)
    db_session.add_all([r1, r2])
    db_session.flush()

    o1 = Offer(requisition_id=src.id, requirement_id=r1.id, vendor_name="V1", mpn="LM317T", status="active")
    o2 = Offer(requisition_id=src.id, requirement_id=r2.id, vendor_name="V2", mpn="LM317T", status="active")
    db_session.add_all([o1, o2])
    db_session.commit()

    cloned = clone_requisition(db_session, src, test_user.id)
    cloned_offers = db_session.query(Offer).filter(Offer.requisition_id == cloned.id).all()

    assert len(cloned_offers) == 2
    assert len({o.requirement_id for o in cloned_offers}) == 2
