"""test_excess_crud.py — Tests for excess inventory service layer.

Covers CRUD operations on ExcessList and bulk import of line items
with flexible header detection.

Called by: pytest
Depends on: app.services.excess_service, app.models.excess, conftest fixtures
"""

import pytest
from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models import Company, User
from app.models.excess import ExcessLineItem, ExcessList
from app.services.excess_service import (
    confirm_import,
    create_excess_list,
    delete_excess_list,
    get_excess_list,
    import_line_items,
    list_excess_lists,
    preview_import,
    update_excess_list,
)
from tests.conftest import engine

_ = engine  # Ensure test DB tables are created


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


def _make_excess_list(db: Session, company: Company, user: User, title: str = "Test Excess") -> ExcessList:
    return create_excess_list(db, title=title, company_id=company.id, owner_id=user.id)


@pytest.fixture()
def company(db_session: Session) -> Company:
    return _make_company(db_session)


@pytest.fixture()
def trader(db_session: Session) -> User:
    return _make_user(db_session)


# ---------------------------------------------------------------------------
# TestCreateExcessList
# ---------------------------------------------------------------------------


class TestCreateExcessList:
    def test_create_with_required_fields(self, db_session: Session):
        company = _make_company(db_session)
        user = _make_user(db_session)

        el = create_excess_list(
            db_session,
            title="Q1 Excess",
            company_id=company.id,
            owner_id=user.id,
        )

        assert el.id is not None
        assert el.title == "Q1 Excess"
        assert el.company_id == company.id
        assert el.owner_id == user.id
        assert el.status == "draft"
        assert el.total_line_items == 0

    def test_create_with_optional_fields(self, db_session: Session):
        company = _make_company(db_session)
        user = _make_user(db_session)

        el = create_excess_list(
            db_session,
            title="Full Excess",
            company_id=company.id,
            owner_id=user.id,
            notes="Some notes",
            source_filename="excess.csv",
        )

        assert el.notes == "Some notes"
        assert el.source_filename == "excess.csv"

    def test_invalid_company_raises_404(self, db_session: Session):
        user = _make_user(db_session)

        with pytest.raises(HTTPException) as exc_info:
            create_excess_list(
                db_session,
                title="Bad Company",
                company_id=99999,
                owner_id=user.id,
            )
        assert exc_info.value.status_code == 404


# ---------------------------------------------------------------------------
# TestGetExcessList
# ---------------------------------------------------------------------------


class TestGetExcessList:
    def test_get_existing(self, db_session: Session):
        company = _make_company(db_session)
        user = _make_user(db_session)
        el = _make_excess_list(db_session, company, user)

        fetched = get_excess_list(db_session, el.id)
        assert fetched.id == el.id
        assert fetched.title == "Test Excess"

    def test_get_not_found_raises_404(self, db_session: Session):
        with pytest.raises(HTTPException) as exc_info:
            get_excess_list(db_session, 99999)
        assert exc_info.value.status_code == 404


# ---------------------------------------------------------------------------
# TestListExcessLists
# ---------------------------------------------------------------------------


class TestListExcessLists:
    def test_returns_paginated(self, db_session: Session):
        company = _make_company(db_session)
        user = _make_user(db_session)

        for i in range(3):
            _make_excess_list(db_session, company, user, title=f"List {i}")

        result = list_excess_lists(db_session, limit=2, offset=0)

        assert result["total"] == 3
        assert len(result["items"]) == 2
        assert result["limit"] == 2
        assert result["offset"] == 0

    def test_search_filter(self, db_session: Session):
        company = _make_company(db_session)
        user = _make_user(db_session)

        _make_excess_list(db_session, company, user, title="Alpha Batch")
        _make_excess_list(db_session, company, user, title="Beta Batch")
        _make_excess_list(db_session, company, user, title="Gamma Set")

        result = list_excess_lists(db_session, q="batch")
        assert result["total"] == 2

    def test_status_filter(self, db_session: Session):
        company = _make_company(db_session)
        user = _make_user(db_session)

        el = _make_excess_list(db_session, company, user, title="Active One")
        update_excess_list(db_session, el.id, status="active")
        _make_excess_list(db_session, company, user, title="Draft One")

        result = list_excess_lists(db_session, status="active")
        assert result["total"] == 1
        assert result["items"][0].title == "Active One"


# ---------------------------------------------------------------------------
# TestUpdateExcessList
# ---------------------------------------------------------------------------


class TestUpdateExcessList:
    def test_updates_title(self, db_session: Session):
        company = _make_company(db_session)
        user = _make_user(db_session)
        el = _make_excess_list(db_session, company, user)

        updated = update_excess_list(db_session, el.id, title="New Title")
        assert updated.title == "New Title"

    def test_not_found_raises_404(self, db_session: Session):
        with pytest.raises(HTTPException) as exc_info:
            update_excess_list(db_session, 99999, title="Nope")
        assert exc_info.value.status_code == 404

    def test_ignores_none_values(self, db_session: Session):
        company = _make_company(db_session)
        user = _make_user(db_session)
        el = _make_excess_list(db_session, company, user, title="Original")

        updated = update_excess_list(db_session, el.id, title=None, notes="Added")
        assert updated.title == "Original"
        assert updated.notes == "Added"


# ---------------------------------------------------------------------------
# TestDeleteExcessList
# ---------------------------------------------------------------------------


class TestDeleteExcessList:
    def test_hard_deletes(self, db_session: Session):
        company = _make_company(db_session)
        user = _make_user(db_session)
        el = _make_excess_list(db_session, company, user)
        list_id = el.id

        delete_excess_list(db_session, list_id)

        with pytest.raises(HTTPException) as exc_info:
            get_excess_list(db_session, list_id)
        assert exc_info.value.status_code == 404

    def test_delete_not_found_raises_404(self, db_session: Session):
        with pytest.raises(HTTPException) as exc_info:
            delete_excess_list(db_session, 99999)
        assert exc_info.value.status_code == 404


# ---------------------------------------------------------------------------
# TestImportLineItems
# ---------------------------------------------------------------------------


class TestImportLineItems:
    def test_imports_valid_rows(self, db_session: Session):
        company = _make_company(db_session)
        user = _make_user(db_session)
        el = _make_excess_list(db_session, company, user)

        rows = [
            {"part_number": "LM317T", "quantity": "100", "manufacturer": "TI", "asking_price": "0.50"},
            {"part_number": "NE555P", "quantity": "200", "manufacturer": "TI"},
        ]

        result = import_line_items(db_session, el.id, rows)

        assert result["imported"] == 2
        assert result["skipped"] == 0
        assert result["errors"] == []

        # Verify counter updated
        db_session.refresh(el)
        assert el.total_line_items == 2

        # Verify items in DB
        items = db_session.query(ExcessLineItem).filter_by(excess_list_id=el.id).all()
        assert len(items) == 2

    def test_flexible_headers(self, db_session: Session):
        """Accepts mpn/qty/price/mfr/dc/cond aliases."""
        company = _make_company(db_session)
        user = _make_user(db_session)
        el = _make_excess_list(db_session, company, user)

        rows = [
            {"mpn": "LM317T", "qty": "50", "mfr": "Texas Instruments", "price": "$1.25", "dc": "2024+", "cond": "New"},
        ]

        result = import_line_items(db_session, el.id, rows)

        assert result["imported"] == 1

        item = db_session.query(ExcessLineItem).filter_by(excess_list_id=el.id).first()
        assert item.part_number == "LM317T"
        assert item.quantity == 50
        assert item.manufacturer == "Texas Instruments"
        assert item.date_code == "2024+"
        assert item.condition == "New"
        assert float(item.asking_price) == 1.25

    def test_skips_blank_part_number(self, db_session: Session):
        company = _make_company(db_session)
        user = _make_user(db_session)
        el = _make_excess_list(db_session, company, user)

        rows = [
            {"part_number": "", "quantity": "100"},
            {"part_number": "  ", "quantity": "100"},
            {"part_number": "LM317T", "quantity": "100"},
        ]

        result = import_line_items(db_session, el.id, rows)

        assert result["imported"] == 1
        assert result["skipped"] == 2
        assert len(result["errors"]) == 2

    def test_skips_invalid_quantity(self, db_session: Session):
        company = _make_company(db_session)
        user = _make_user(db_session)
        el = _make_excess_list(db_session, company, user)

        rows = [
            {"part_number": "LM317T", "quantity": "abc"},
            {"part_number": "NE555P", "quantity": "-5"},
            {"part_number": "LM7805", "quantity": "0"},
            {"part_number": "AD620", "quantity": "50"},
        ]

        result = import_line_items(db_session, el.id, rows)

        assert result["imported"] == 1
        assert result["skipped"] == 3

    def test_updates_total_counter(self, db_session: Session):
        company = _make_company(db_session)
        user = _make_user(db_session)
        el = _make_excess_list(db_session, company, user)

        # First import
        import_line_items(db_session, el.id, [{"part_number": "A", "quantity": "1"}])
        db_session.refresh(el)
        assert el.total_line_items == 1

        # Second import adds to counter
        import_line_items(
            db_session, el.id, [{"part_number": "B", "quantity": "2"}, {"part_number": "C", "quantity": "3"}]
        )
        db_session.refresh(el)
        assert el.total_line_items == 3

    def test_not_found_list_raises_404(self, db_session: Session):
        with pytest.raises(HTTPException) as exc_info:
            import_line_items(db_session, 99999, [{"part_number": "X", "quantity": "1"}])
        assert exc_info.value.status_code == 404


# ---------------------------------------------------------------------------
# TestPreviewImport
# ---------------------------------------------------------------------------


class TestPreviewImport:
    def test_parses_valid_rows(self, db_session, company, trader):
        el = create_excess_list(db_session, title="Preview", company_id=company.id, owner_id=trader.id)
        rows = [
            {"part_number": "LM358N", "quantity": "500", "asking_price": "0.45"},
            {"mpn": "NE555P", "qty": "1000", "manufacturer": "TI"},
        ]
        result = preview_import(rows)
        assert result["valid_count"] == 2
        assert result["error_count"] == 0
        assert len(result["preview_rows"]) == 2
        assert result["preview_rows"][0]["part_number"] == "LM358N"

    def test_flags_invalid_rows(self):
        rows = [
            {"part_number": "", "quantity": "500"},
            {"part_number": "LM358N", "quantity": "abc"},
            {"part_number": "NE555P", "quantity": "100"},
        ]
        result = preview_import(rows)
        assert result["valid_count"] == 1
        assert result["error_count"] == 2
        assert len(result["errors"]) == 2
        assert "Row 1" in result["errors"][0]

    def test_detects_column_mapping(self):
        rows = [{"mpn": "LM358N", "qty": "100", "cost": "0.50"}]
        result = preview_import(rows)
        mapping = result["column_mapping"]
        assert mapping["mpn"] == "part_number"
        assert mapping["qty"] == "quantity"
        assert mapping["cost"] == "asking_price"

    def test_limits_preview_to_10_rows(self):
        rows = [{"part_number": f"PART{i}", "quantity": "1"} for i in range(25)]
        result = preview_import(rows)
        assert len(result["preview_rows"]) == 10
        assert result["valid_count"] == 25


# ---------------------------------------------------------------------------
# TestConfirmImport
# ---------------------------------------------------------------------------


class TestConfirmImport:
    def test_imports_validated_rows(self, db_session, company, trader):
        el = create_excess_list(db_session, title="Confirm", company_id=company.id, owner_id=trader.id)
        validated_rows = [
            {"part_number": "LM358N", "quantity": 500, "asking_price": 0.45},
            {"part_number": "NE555P", "quantity": 1000, "manufacturer": "TI"},
        ]
        result = confirm_import(db_session, el.id, validated_rows)
        assert result["imported"] == 2
        db_session.refresh(el)
        assert el.total_line_items == 2

    def test_rejects_empty_rows(self, db_session, company, trader):
        el = create_excess_list(db_session, title="Empty", company_id=company.id, owner_id=trader.id)
        result = confirm_import(db_session, el.id, [])
        assert result["imported"] == 0
