"""Tests for Excess Inventory (Resell) models and schemas.

Verifies ExcessList / ExcessLineItem model creation, defaults, cascade deletes,
and Pydantic validation.

Called by: pytest
Depends on: app.models.excess, app.schemas.excess, tests.conftest
"""

from decimal import Decimal

import pytest
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.models import Company, User
from app.models.excess import ExcessLineItem, ExcessList
from app.schemas.excess import (
    ExcessLineItemCreate,
    ExcessListCreate,
    ExcessListResponse,
    ExcessListUpdate,
)
from tests.conftest import engine

# Re-create tables for this test module (conftest handles it globally,
# but import engine to satisfy the project convention).
_ = engine


# ── Fixtures ─────────────────────────────────────────────────────────


@pytest.fixture()
def seller_company(db_session: Session) -> Company:
    co = Company(name="Acme Corp")
    db_session.add(co)
    db_session.commit()
    db_session.refresh(co)
    return co


@pytest.fixture()
def trader(db_session: Session) -> User:
    user = User(
        email="excess-trader@trioscs.com",
        name="Excess Trader",
        role="trader",
        azure_id="excess-trader-001",
        m365_connected=True,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture()
def excess_list(db_session: Session, seller_company: Company, trader: User) -> ExcessList:
    el = ExcessList(
        company_id=seller_company.id,
        owner_id=trader.id,
        title="Q1 2026 Excess - Acme",
    )
    db_session.add(el)
    db_session.commit()
    db_session.refresh(el)
    return el


@pytest.fixture()
def line_item(db_session: Session, excess_list: ExcessList) -> ExcessLineItem:
    li = ExcessLineItem(
        excess_list_id=excess_list.id,
        part_number="LM358N",
        quantity=500,
        asking_price=Decimal("0.4500"),
    )
    db_session.add(li)
    db_session.commit()
    db_session.refresh(li)
    return li


# ── Model Tests ──────────────────────────────────────────────────────


class TestExcessListModel:
    def test_create_with_required_fields(self, excess_list: ExcessList):
        assert excess_list.id is not None
        assert excess_list.title == "Q1 2026 Excess - Acme"

    def test_default_status_is_draft(self, excess_list: ExcessList):
        assert excess_list.status == "draft"

    def test_default_total_line_items_is_zero(self, excess_list: ExcessList):
        assert excess_list.total_line_items == 0


class TestExcessLineItemModel:
    def test_create_and_link_to_list(self, line_item: ExcessLineItem, excess_list: ExcessList):
        assert line_item.excess_list_id == excess_list.id
        assert line_item.part_number == "LM358N"

    def test_default_status_is_available(self, line_item: ExcessLineItem):
        assert line_item.status == "available"

    def test_default_condition_is_new(self, line_item: ExcessLineItem):
        assert line_item.condition == "New"


class TestCascadeDelete:
    def test_deleting_list_removes_line_items(
        self, db_session: Session, excess_list: ExcessList, line_item: ExcessLineItem
    ):
        list_id = excess_list.id
        db_session.delete(excess_list)
        db_session.commit()
        remaining = db_session.query(ExcessLineItem).filter_by(excess_list_id=list_id).all()
        assert remaining == []


# ── Schema Tests ─────────────────────────────────────────────────────


class TestExcessListSchemas:
    def test_create_valid(self):
        schema = ExcessListCreate(title="Test List", company_id=1)
        assert schema.title == "Test List"

    def test_create_blank_title_rejected(self):
        with pytest.raises(ValidationError):
            ExcessListCreate(title="   ", company_id=1)

    def test_update_with_valid_status(self):
        schema = ExcessListUpdate(status="active")
        assert schema.status == "active"

    def test_update_with_invalid_status_rejected(self):
        with pytest.raises(ValidationError):
            ExcessListUpdate(status="invalid_status")

    def test_response_schema(self):
        resp = ExcessListResponse(id=1, company_id=1, owner_id=1, title="Test", status="draft")
        assert resp.total_line_items == 0


class TestExcessLineItemSchemas:
    def test_create_valid(self):
        schema = ExcessLineItemCreate(part_number="LM358N", quantity=100)
        assert schema.part_number == "LM358N"

    @pytest.mark.parametrize(
        "kwargs",
        [
            pytest.param({"part_number": "  ", "quantity": 100}, id="blank_part_number"),
            pytest.param({"part_number": "LM358N", "quantity": 0}, id="zero_quantity"),
            pytest.param({"part_number": "LM358N", "quantity": 1, "asking_price": -1.0}, id="negative_price"),
        ],
    )
    def test_create_invalid_rejected(self, kwargs):
        with pytest.raises(ValidationError):
            ExcessLineItemCreate(**kwargs)

    def test_create_defaults(self):
        schema = ExcessLineItemCreate(part_number="SN74HC595N", quantity=1)
        assert schema.condition == "New"
        assert schema.asking_price is None
