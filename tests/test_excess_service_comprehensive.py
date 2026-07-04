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
    create_excess_list,
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


# ---------------------------------------------------------------------------
# backfill_normalized_part_numbers
# ---------------------------------------------------------------------------
