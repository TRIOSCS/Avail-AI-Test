"""test_resell_erp_refs.py — ERP reference columns on CustomerBid / ExcessOffer (C3).

Migration 216 adds two nullable String(100) reference-only columns so a trader can
record the ERP document number tied to a resell transaction, without AVAIL owning
that document's lifecycle (CLAUDE.md "Hard constraints" — nothing integrates with
Acctivate, references only):
- ``CustomerBid.po_number`` — the customer's PO number issued against the accepted
  bid-back (the seller's purchase order, cut in the ERP).
- ``ExcessOffer.sales_order_number`` — the sales order number the accepted inbound
  offer was fulfilled under (cut in the ERP).

Both columns are optional, free-text references only — AVAIL never validates or
syncs them against Acctivate. Task 2 (capture UI) reads/writes them.

Called by: pytest
Depends on: app.models.excess, tests.conftest
"""

from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from app.models import Company, User
from app.models.excess import CustomerBid, ExcessList, ExcessOffer
from tests.conftest import engine

_ = engine  # ensure test DB tables are created


# ── Fixtures ─────────────────────────────────────────────────────────


@pytest.fixture()
def seller_company(db_session: Session) -> Company:
    co = Company(name="ERP Refs Test Co")
    db_session.add(co)
    db_session.commit()
    db_session.refresh(co)
    return co


@pytest.fixture()
def trader(db_session: Session) -> User:
    user = User(
        email="erp-refs-trader@trioscs.com",
        name="ERP Refs Trader",
        role="trader",
        azure_id="erp-refs-trader-1",
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
        title="ERP Refs Excess List",
    )
    db_session.add(el)
    db_session.commit()
    db_session.refresh(el)
    return el


# ── CustomerBid.po_number ───────────────────────────────────────────


class TestCustomerBidPoNumber:
    def test_column_exists_and_defaults_to_none(self, db_session: Session, excess_list: ExcessList, trader: User):
        bid = CustomerBid(excess_list_id=excess_list.id, owner_id=trader.id)
        db_session.add(bid)
        db_session.commit()
        db_session.refresh(bid)
        assert bid.po_number is None

    def test_accepts_and_persists_a_value(self, db_session: Session, excess_list: ExcessList, trader: User):
        bid = CustomerBid(excess_list_id=excess_list.id, owner_id=trader.id, po_number="PO-2026-0042")
        db_session.add(bid)
        db_session.commit()
        bid_id = bid.id
        db_session.expire_all()

        reloaded = db_session.get(CustomerBid, bid_id)
        assert reloaded.po_number == "PO-2026-0042"


# ── ExcessOffer.sales_order_number ──────────────────────────────────


class TestExcessOfferSalesOrderNumber:
    def test_column_exists_and_defaults_to_none(self, db_session: Session, excess_list: ExcessList, trader: User):
        offer = ExcessOffer(
            excess_list_id=excess_list.id,
            submitted_by=trader.id,
            take_all_total_price=Decimal("1000.0000"),
        )
        db_session.add(offer)
        db_session.commit()
        db_session.refresh(offer)
        assert offer.sales_order_number is None

    def test_accepts_and_persists_a_value(self, db_session: Session, excess_list: ExcessList, trader: User):
        offer = ExcessOffer(
            excess_list_id=excess_list.id,
            submitted_by=trader.id,
            sales_order_number="SO-2026-0917",
        )
        db_session.add(offer)
        db_session.commit()
        offer_id = offer.id
        db_session.expire_all()

        reloaded = db_session.get(ExcessOffer, offer_id)
        assert reloaded.sales_order_number == "SO-2026-0917"
