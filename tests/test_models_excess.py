"""Tests for Excess Inventory (Resell) models.

Verifies ExcessList / ExcessLineItem model creation, defaults, status
validation, and cascade deletes. (The dead app/schemas/excess.py Pydantic
module was deleted — routers use Form fields and services take kwargs — so
the schema tests that exercised it are gone with it.)

Called by: pytest
Depends on: app.constants, app.models.excess, tests.conftest
"""

from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from app.constants import ExcessLineItemStatus
from app.models import Company, User
from app.models.excess import ExcessLineItem, ExcessList, ExcessOffer
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


class TestExcessOfferValidUntilDropped:
    """D6: the dead ``excess_offers.valid_until`` column is dropped (migration 201) — the
    model column goes with it, or the fresh-DB drift gate would emit a remove_column
    diff and fail."""

    def test_model_has_no_valid_until_column(self):
        assert "valid_until" not in ExcessOffer.__table__.columns


class TestExcessLineItemModel:
    def test_create_and_link_to_list(self, line_item: ExcessLineItem, excess_list: ExcessList):
        assert line_item.excess_list_id == excess_list.id
        assert line_item.part_number == "LM358N"

    def test_default_status_is_available(self, line_item: ExcessLineItem):
        assert line_item.status == "available"

    def test_default_condition_is_new(self, line_item: ExcessLineItem):
        assert line_item.condition == "New"

    @pytest.mark.parametrize("bad_status", ["availble", "active", "bogus_status"])
    def test_invalid_status_rejected(self, bad_status: str):
        with pytest.raises(ValueError):
            ExcessLineItem(excess_list_id=1, part_number="LM358N", quantity=1, status=bad_status)

    def test_valid_enum_statuses_accepted(self):
        for member in ExcessLineItemStatus:
            li = ExcessLineItem(excess_list_id=1, part_number="LM358N", quantity=1, status=member)
            assert li.status == member.value


class TestCascadeDelete:
    def test_deleting_list_removes_line_items(
        self, db_session: Session, excess_list: ExcessList, line_item: ExcessLineItem
    ):
        list_id = excess_list.id
        db_session.delete(excess_list)
        db_session.commit()
        remaining = db_session.query(ExcessLineItem).filter_by(excess_list_id=list_id).all()
        assert remaining == []
