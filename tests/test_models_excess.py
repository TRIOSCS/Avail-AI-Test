"""Tests for Excess Inventory & Bid Collection models and schemas.

Verifies model creation, defaults, cascade deletes, and Pydantic validation.

Called by: pytest
Depends on: app.models.excess, app.schemas.excess, tests.conftest
"""

from decimal import Decimal

import pytest
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.models import Company, User
from app.models.excess import Bid, BidSolicitation, ExcessLineItem, ExcessList
from app.schemas.excess import (
    BidCreate,
    BidSolicitationCreate,
    BidUpdate,
    ExcessLineItemCreate,
    ExcessLineItemImportRow,
    ExcessLineItemUpdate,
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


class TestBidSolicitationModel:
    def test_create_solicitation(self, db_session: Session, line_item: ExcessLineItem, trader: User):
        sol = BidSolicitation(
            excess_line_item_id=line_item.id,
            contact_id=99,
            sent_by=trader.id,
        )
        db_session.add(sol)
        db_session.commit()
        db_session.refresh(sol)
        assert sol.id is not None
        assert sol.status == "pending"


class TestBidModel:
    def test_create_bid(self, db_session: Session, line_item: ExcessLineItem, trader: User):
        bid = Bid(
            excess_line_item_id=line_item.id,
            unit_price=Decimal("0.3500"),
            quantity_wanted=200,
            created_by=trader.id,
        )
        db_session.add(bid)
        db_session.commit()
        db_session.refresh(bid)
        assert bid.id is not None
        assert bid.status == "pending"
        assert bid.source == "manual"

    def test_bid_links_to_line_item(self, db_session: Session, line_item: ExcessLineItem, trader: User):
        bid = Bid(
            excess_line_item_id=line_item.id,
            unit_price=Decimal("0.5000"),
            quantity_wanted=100,
            created_by=trader.id,
        )
        db_session.add(bid)
        db_session.commit()
        db_session.refresh(bid)
        assert bid.excess_line_item_id == line_item.id


class TestCascadeDelete:
    def test_deleting_list_removes_line_items(
        self, db_session: Session, excess_list: ExcessList, line_item: ExcessLineItem
    ):
        list_id = excess_list.id
        db_session.delete(excess_list)
        db_session.commit()
        remaining = db_session.query(ExcessLineItem).filter_by(excess_list_id=list_id).all()
        assert remaining == []

    def test_deleting_line_item_removes_bids(self, db_session: Session, line_item: ExcessLineItem, trader: User):
        bid = Bid(
            excess_line_item_id=line_item.id,
            unit_price=Decimal("1.0000"),
            quantity_wanted=50,
            created_by=trader.id,
        )
        db_session.add(bid)
        db_session.commit()
        li_id = line_item.id
        db_session.delete(line_item)
        db_session.commit()
        remaining = db_session.query(Bid).filter_by(excess_line_item_id=li_id).all()
        assert remaining == []

    def test_deleting_line_item_removes_solicitations(
        self, db_session: Session, line_item: ExcessLineItem, trader: User
    ):
        sol = BidSolicitation(
            excess_line_item_id=line_item.id,
            contact_id=42,
            sent_by=trader.id,
        )
        db_session.add(sol)
        db_session.commit()
        li_id = line_item.id
        db_session.delete(line_item)
        db_session.commit()
        remaining = db_session.query(BidSolicitation).filter_by(excess_line_item_id=li_id).all()
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

    def test_create_blank_part_number_rejected(self):
        with pytest.raises(ValidationError):
            ExcessLineItemCreate(part_number="  ", quantity=100)

    def test_create_zero_quantity_rejected(self):
        with pytest.raises(ValidationError):
            ExcessLineItemCreate(part_number="LM358N", quantity=0)

    def test_create_negative_price_rejected(self):
        with pytest.raises(ValidationError):
            ExcessLineItemCreate(part_number="LM358N", quantity=1, asking_price=-1.0)

    def test_import_row_valid(self):
        row = ExcessLineItemImportRow(
            part_number="SN74HC595N",
            manufacturer="TI",
            quantity=1000,
            date_code="2024",
            asking_price=0.25,
        )
        assert row.condition == "New"

    def test_import_row_defaults(self):
        row = ExcessLineItemImportRow(part_number="ABC123")
        assert row.quantity == 1
        assert row.condition == "New"

    def test_update_with_valid_status(self):
        schema = ExcessLineItemUpdate(status="withdrawn")
        assert schema.status == "withdrawn"


class TestBidSchemas:
    def test_create_valid(self):
        schema = BidCreate(excess_line_item_id=1, unit_price=0.50, quantity_wanted=100)
        assert schema.source == "manual"

    def test_create_missing_unit_price_rejected(self):
        with pytest.raises(ValidationError):
            BidCreate(excess_line_item_id=1, quantity_wanted=100)

    def test_create_missing_quantity_rejected(self):
        with pytest.raises(ValidationError):
            BidCreate(excess_line_item_id=1, unit_price=0.50)

    def test_update_valid(self):
        schema = BidUpdate(status="accepted", unit_price=0.60)
        assert schema.status == "accepted"

    def test_update_invalid_status_rejected(self):
        with pytest.raises(ValidationError):
            BidUpdate(status="bogus")


class TestBidSolicitationSchemas:
    def test_create_valid(self):
        schema = BidSolicitationCreate(excess_line_item_id=1, contact_id=42)
        assert schema.contact_id == 42
