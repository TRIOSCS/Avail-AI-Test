"""test_excess_service_comprehensive.py — Comprehensive tests for excess_service.py.

Covers: get_excess_stats (offer-based), backfill_normalized_part_numbers,
_parse_price edge cases, _parse_quantity edge cases, _normalize_row edge cases,
_safe_commit IntegrityError.

Called by: pytest
Depends on: app.services.excess_service, app.models.excess, conftest fixtures
"""

from decimal import Decimal
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Company, User
from app.models.excess import ExcessLineItem, ExcessList
from app.services.excess_service import (
    _normalize_row,
    _parse_price,
    _parse_quantity,
    _safe_commit,
    backfill_normalized_part_numbers,
    create_excess_list,
    get_excess_stats,
    submit_offer,
)
from tests.conftest import engine

_ = engine

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_company(db: Session, name: str = "Seller Corp") -> Company:
    co = Company(name=name)
    db.add(co)
    db.commit()
    db.refresh(co)
    return co


def _make_user(db: Session, email: str = "trader@test.com") -> User:
    user = User(
        email=email,
        name="Test Trader",
        role="trader",
        azure_id=f"az-{email}",
        m365_connected=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _make_excess_list(db: Session, company: Company, user: User, title: str = "Test Excess", status: str = "draft"):
    el = create_excess_list(db, title=title, company_id=company.id, owner_id=user.id)
    if status != "draft":
        el.status = status
        db.commit()
        db.refresh(el)
    return el


def _make_line_item(
    db: Session, excess_list: ExcessList, part_number: str = "LM317T", quantity: int = 100, asking_price=1.50
):
    item = ExcessLineItem(
        excess_list_id=excess_list.id,
        part_number=part_number,
        quantity=quantity,
        asking_price=asking_price,
        manufacturer="TI",
        condition="New",
        date_code="2024+",
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


# ---------------------------------------------------------------------------
# _parse_quantity edge cases
# ---------------------------------------------------------------------------


class TestParseQuantity:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            pytest.param(None, None, id="none_returns_none"),
            pytest.param("100", 100, id="valid_int"),
            pytest.param("100.5", 100, id="valid_float_string"),
            pytest.param("1,000", 1000, id="comma_separated"),
            pytest.param("0", None, id="zero_returns_none"),
            pytest.param("-5", None, id="negative_returns_none"),
            pytest.param("abc", None, id="invalid_string"),
            pytest.param("", None, id="empty_string"),
            pytest.param("  50  ", 50, id="whitespace"),
        ],
    )
    def test_parse_quantity(self, raw, expected):
        result = _parse_quantity(raw)
        if expected is None:
            assert result is None
        else:
            assert result == expected


# ---------------------------------------------------------------------------
# _parse_price edge cases
# ---------------------------------------------------------------------------


class TestParsePrice:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            pytest.param(None, None, id="none_returns_none"),
            pytest.param("", None, id="empty_string_returns_none"),
            pytest.param("   ", None, id="whitespace_returns_none"),
            pytest.param("1.25", Decimal("1.25"), id="valid_decimal"),
            pytest.param("$1.25", Decimal("1.25"), id="dollar_sign"),
            pytest.param("$1,234.56", Decimal("1234.56"), id="comma_separated"),
            pytest.param("0", Decimal("0"), id="zero_is_valid"),
            pytest.param("-1.50", None, id="negative_returns_none"),
            pytest.param("abc", None, id="invalid_string"),
            pytest.param(5, Decimal("5"), id="integer_value"),
        ],
    )
    def test_parse_price(self, raw, expected):
        result = _parse_price(raw)
        if expected is None:
            assert result is None
        else:
            assert result == expected


# ---------------------------------------------------------------------------
# _normalize_row
# ---------------------------------------------------------------------------


class TestNormalizeRow:
    def test_maps_aliases(self):
        raw = {"mpn": "LM317T", "qty": "100", "price": "$1.50"}
        result = _normalize_row(raw)
        assert result["part_number"] == "LM317T"
        assert result["quantity"] == "100"
        assert result["asking_price"] == "$1.50"

    def test_first_match_wins(self):
        """If multiple keys map to same canonical, first one wins."""
        raw = {"part_number": "FIRST", "mpn": "SECOND"}
        result = _normalize_row(raw)
        assert result["part_number"] == "FIRST"

    def test_unknown_keys_ignored(self):
        raw = {"unknown_key": "value", "mpn": "LM317T"}
        result = _normalize_row(raw)
        assert "unknown_key" not in result
        assert result["part_number"] == "LM317T"

    def test_whitespace_in_keys(self):
        raw = {" Part Number ": "LM317T"}
        result = _normalize_row(raw)
        assert result["part_number"] == "LM317T"

    def test_manufacturer_aliases(self):
        raw = {"mfr": "Texas Instruments"}
        result = _normalize_row(raw)
        assert result["manufacturer"] == "Texas Instruments"

    def test_date_code_aliases(self):
        raw = {"dc": "2024+"}
        result = _normalize_row(raw)
        assert result["date_code"] == "2024+"

    def test_condition_aliases(self):
        raw = {"cond": "New"}
        result = _normalize_row(raw)
        assert result["condition"] == "New"


# ---------------------------------------------------------------------------
# _safe_commit
# ---------------------------------------------------------------------------


class TestSafeCommit:
    def test_integrity_error_raises_409(self):
        mock_db = MagicMock()
        mock_db.commit.side_effect = IntegrityError("dup", {}, None)
        with pytest.raises(HTTPException) as exc_info:
            _safe_commit(mock_db, entity="test")
        assert exc_info.value.status_code == 409
        mock_db.rollback.assert_called_once()


# ---------------------------------------------------------------------------
# get_excess_stats
# ---------------------------------------------------------------------------


class TestGetExcessStats:
    def test_empty_db(self, db_session: Session):
        stats = get_excess_stats(db_session)
        assert stats["total_lists"] == 0
        assert stats["total_line_items"] == 0
        assert stats["open_offers"] == 0
        assert stats["total_offers"] == 0
        assert stats["matched_items"] == 0
        assert stats["awarded_items"] == 0

    def test_with_data(self, db_session: Session):
        company = _make_company(db_session)
        owner = _make_user(db_session)
        offerer = _make_user(db_session, email="broker@test.com")
        el = _make_excess_list(db_session, company, owner)
        item = _make_line_item(db_session, el)

        # An inbound broker offer (an OPEN ExcessOffer with a matched line) — the
        # Trading replacement for the old per-line bid.
        submit_offer(
            db_session,
            list_id=el.id,
            user=offerer,
            scope="per_line",
            lines=[{"mpn_raw": item.part_number, "quantity": 50, "unit_price": 1.0}],
        )

        stats = get_excess_stats(db_session)
        assert stats["total_lists"] == 1
        assert stats["total_line_items"] == 1
        assert stats["open_offers"] == 1
        assert stats["total_offers"] == 1
        # offer_count rollup made the line "matched" (has >=1 offer).
        assert stats["matched_items"] == 1
        assert stats["awarded_items"] == 0

    def test_awarded_items(self, db_session: Session):
        company = _make_company(db_session)
        user = _make_user(db_session)
        el = _make_excess_list(db_session, company, user)
        item = _make_line_item(db_session, el)
        item.status = "awarded"
        db_session.commit()

        stats = get_excess_stats(db_session)
        assert stats["awarded_items"] == 1


# ---------------------------------------------------------------------------
# backfill_normalized_part_numbers
# ---------------------------------------------------------------------------


class TestBackfillNormalizedPartNumbers:
    def test_backfills_missing(self, db_session: Session):
        company = _make_company(db_session)
        user = _make_user(db_session)
        el = _make_excess_list(db_session, company, user)
        item = _make_line_item(db_session, el, part_number="LM-317T")
        item.normalized_part_number = None
        db_session.commit()

        count = backfill_normalized_part_numbers(db_session)
        assert count == 1

        db_session.refresh(item)
        assert item.normalized_part_number is not None

    def test_no_items_to_backfill(self, db_session: Session):
        count = backfill_normalized_part_numbers(db_session)
        assert count == 0
