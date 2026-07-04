"""Tests for the additive Resell (resell-brokerage) schema foundation.

Covers the inbound-offer models (ExcessOffer / ExcessOfferLine), the
additive columns on the kept models (ExcessLineItem rollup + material_card_id,
ExcessList.version), the StrEnum constants, and the Pydantic schemas. The kept
ExcessList / ExcessLineItem models stay covered by tests/test_models_excess.py.

Called by: pytest
Depends on: app.constants, app.models.excess, app.schemas.excess, tests.conftest
"""

from decimal import Decimal

import pytest
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.constants import ExcessListStatus, ExcessOfferScope, ExcessOfferStatus, OfferLineMatchStatus
from app.models import Company, User
from app.models.excess import ExcessLineItem, ExcessList, ExcessOffer, ExcessOfferLine
from app.schemas.excess import (
    ExcessOfferCreate,
    ExcessOfferLineCreate,
    ExcessOfferLineResponse,
    ExcessOfferResponse,
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
def owner(db_session: Session) -> User:
    user = User(
        email="trading-owner@trioscs.com",
        name="Trading Owner",
        role="trader",
        azure_id="trading-owner-001",
        m365_connected=True,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture()
def offerer(db_session: Session) -> User:
    user = User(
        email="trading-offerer@trioscs.com",
        name="Trading Offerer",
        role="buyer",
        azure_id="trading-offerer-001",
        m365_connected=True,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture()
def excess_list(db_session: Session, seller_company: Company, owner: User) -> ExcessList:
    el = ExcessList(
        company_id=seller_company.id,
        owner_id=owner.id,
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
    )
    db_session.add(li)
    db_session.commit()
    db_session.refresh(li)
    return li


# ── Constants ────────────────────────────────────────────────────────


class TestTradingConstants:
    def test_offer_status_members(self):
        assert {e.value for e in ExcessOfferStatus} == {
            "open",
            "won",
            "lost",
            "withdrawn",
            "late",
        }

    def test_offer_scope_members(self):
        assert {e.value for e in ExcessOfferScope} == {"per_line", "take_all"}

    def test_match_status_members(self):
        assert {e.value for e in OfferLineMatchStatus} == {"matched", "unmatched", "ambiguous"}

    def test_excess_list_status_extended_additively(self):
        values = {e.value for e in ExcessListStatus}
        # New lifecycle members the Trading spec uses.
        assert {"draft", "open", "collecting", "bid_out", "awarded", "closed", "expired"} <= values
        # Backward-compat: pre-existing members are NOT removed in this additive chunk.
        assert {"active", "bidding"} <= values


# ── ExcessOffer (header) ─────────────────────────────────────────────


class TestExcessOfferModel:
    def test_take_all_offer_no_lines(self, db_session: Session, excess_list: ExcessList, offerer: User):
        offer = ExcessOffer(
            excess_list_id=excess_list.id,
            submitted_by=offerer.id,
            scope=ExcessOfferScope.TAKE_ALL,
            take_all_total_price=Decimal("12500.0000"),
        )
        db_session.add(offer)
        db_session.commit()
        db_session.refresh(offer)
        assert offer.id is not None
        assert offer.scope == "take_all"
        assert offer.take_all_total_price == Decimal("12500.0000")
        assert offer.status == "open"  # default
        assert offer.lines == []

    def test_per_line_offer_matched_and_unmatched_lines(
        self, db_session: Session, excess_list: ExcessList, offerer: User, line_item: ExcessLineItem
    ):
        offer = ExcessOffer(
            excess_list_id=excess_list.id,
            submitted_by=offerer.id,
            scope=ExcessOfferScope.PER_LINE,
        )
        matched = ExcessOfferLine(
            excess_line_item_id=line_item.id,
            mpn_raw="LM358N",
            quantity=200,
            unit_price=Decimal("0.3500"),
            match_status=OfferLineMatchStatus.MATCHED,
        )
        unmatched = ExcessOfferLine(
            excess_line_item_id=None,  # nullable — held for manual resolution
            mpn_raw="MYSTERY-PART-99",
            quantity=10,
            match_status=OfferLineMatchStatus.UNMATCHED,
        )
        offer.lines = [matched, unmatched]
        db_session.add(offer)
        db_session.commit()
        db_session.refresh(offer)

        assert len(offer.lines) == 2
        by_status = {ln.match_status: ln for ln in offer.lines}
        assert by_status["matched"].excess_line_item_id == line_item.id
        assert by_status["unmatched"].excess_line_item_id is None
        assert by_status["unmatched"].mpn_raw == "MYSTERY-PART-99"
        # default match_status is unmatched (column default applies on flush)
        bare = ExcessOfferLine(offer_id=offer.id, mpn_raw="X", quantity=1)
        db_session.add(bare)
        db_session.commit()
        db_session.refresh(bare)
        assert bare.match_status == "unmatched"

    def test_offer_line_unit_price_nullable(self, db_session: Session, excess_list: ExcessList, offerer: User):
        offer = ExcessOffer(
            excess_list_id=excess_list.id,
            submitted_by=offerer.id,
            scope=ExcessOfferScope.PER_LINE,
        )
        offer.lines = [ExcessOfferLine(mpn_raw="LM358N", quantity=5, unit_price=None)]
        db_session.add(offer)
        db_session.commit()
        db_session.refresh(offer)
        assert offer.lines[0].unit_price is None

    def test_offer_line_quantity_must_be_positive(self):
        with pytest.raises(ValueError):
            ExcessOfferLine(mpn_raw="LM358N", quantity=0)

    def test_invalid_scope_rejected(self):
        with pytest.raises(ValueError):
            ExcessOffer(excess_list_id=1, submitted_by=1, scope="bogus_scope")

    def test_invalid_status_rejected(self):
        with pytest.raises(ValueError):
            ExcessOffer(excess_list_id=1, submitted_by=1, status="bogus_status")

    def test_invalid_match_status_rejected(self):
        with pytest.raises(ValueError):
            ExcessOfferLine(mpn_raw="LM358N", quantity=1, match_status="bogus")

    def test_deleting_offer_cascades_lines(self, db_session: Session, excess_list: ExcessList, offerer: User):
        offer = ExcessOffer(
            excess_list_id=excess_list.id,
            submitted_by=offerer.id,
            scope=ExcessOfferScope.PER_LINE,
        )
        offer.lines = [ExcessOfferLine(mpn_raw="LM358N", quantity=5)]
        db_session.add(offer)
        db_session.commit()
        offer_id = offer.id
        db_session.delete(offer)
        db_session.commit()
        remaining = db_session.query(ExcessOfferLine).filter_by(offer_id=offer_id).all()
        assert remaining == []


# ── Additive columns on kept models ──────────────────────────────────


class TestAdditiveColumns:
    def test_line_item_rollup_defaults(self, line_item: ExcessLineItem):
        assert line_item.material_card_id is None
        assert line_item.best_offer_unit_price is None
        assert line_item.best_offer_id is None
        assert line_item.offer_count == 0

    def test_excess_list_version_defaults_to_one(self, excess_list: ExcessList):
        assert excess_list.version == 1


# ── Schemas ──────────────────────────────────────────────────────────


class TestTradingSchemas:
    def test_offer_line_create_valid(self):
        schema = ExcessOfferLineCreate(mpn_raw="LM358N", quantity=100)
        assert schema.mpn_raw == "LM358N"
        assert schema.unit_price is None  # optional

    def test_offer_line_create_quantity_must_be_positive(self):
        with pytest.raises(ValidationError):
            ExcessOfferLineCreate(mpn_raw="LM358N", quantity=0)

    def test_offer_create_per_line_carries_lines(self):
        schema = ExcessOfferCreate(
            scope="per_line",
            lines=[ExcessOfferLineCreate(mpn_raw="LM358N", quantity=10, unit_price=0.35)],
        )
        assert schema.scope == "per_line"
        assert len(schema.lines) == 1

    def test_offer_create_take_all_carries_total(self):
        schema = ExcessOfferCreate(scope="take_all", take_all_total_price=12500.0)
        assert schema.scope == "take_all"
        assert schema.take_all_total_price == 12500.0

    def test_offer_create_invalid_scope_rejected(self):
        with pytest.raises(ValidationError):
            ExcessOfferCreate(scope="bogus")

    def test_offer_response_from_attributes(self):
        resp = ExcessOfferResponse(
            id=1,
            excess_list_id=1,
            submitted_by=1,
            scope="per_line",
            status="open",
        )
        assert resp.id == 1

    def test_offer_line_response_from_attributes(self):
        resp = ExcessOfferLineResponse(
            id=1,
            offer_id=1,
            mpn_raw="LM358N",
            quantity=10,
            match_status="matched",
        )
        assert resp.match_status == "matched"
