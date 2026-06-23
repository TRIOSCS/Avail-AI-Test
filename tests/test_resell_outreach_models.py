"""Tests for the Resell outbound-outreach tracking models (Chunk A).

Verifies ExcessOutreach (the trader→buyer tracking spine) and BuyerScore (the
per-buyer engagement rollup) creation, defaults, nullable line FK, channel/status
validation, and the new ActivityLog.excess_list_id scope.

Called by: pytest
Depends on: app.models.excess, app.models.intelligence, tests.conftest
"""

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import ActivityLog, BuyerScore, Company, ExcessList, ExcessOutreach, User, VendorCard
from app.models.excess import ExcessLineItem
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
        email="outreach-trader@trioscs.com",
        name="Outreach Trader",
        role="trader",
        azure_id="outreach-trader-001",
        m365_connected=True,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture()
def buyer_card(db_session: Session) -> VendorCard:
    vc = VendorCard(normalized_name="buyer one", display_name="Buyer One")
    db_session.add(vc)
    db_session.commit()
    db_session.refresh(vc)
    return vc


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
    li = ExcessLineItem(excess_list_id=excess_list.id, part_number="LM358N", quantity=500)
    db_session.add(li)
    db_session.commit()
    db_session.refresh(li)
    return li


# ── ExcessOutreach ───────────────────────────────────────────────────


class TestExcessOutreachModel:
    def test_create_email_outreach(
        self,
        db_session: Session,
        excess_list: ExcessList,
        line_item: ExcessLineItem,
        buyer_card: VendorCard,
        trader: User,
    ):
        o = ExcessOutreach(
            excess_list_id=excess_list.id,
            excess_line_item_id=line_item.id,
            target_vendor_card_id=buyer_card.id,
            submitted_by=trader.id,
            channel="email",
            graph_message_id="AAMkADEx..",
            graph_conversation_id="conv-1",
            parts_included=[{"part_number": "LM358N", "quantity": 500}],
        )
        db_session.add(o)
        db_session.commit()
        db_session.refresh(o)
        assert o.id is not None
        assert o.status == "sent"  # default
        assert o.channel == "email"
        assert o.parts_included == [{"part_number": "LM358N", "quantity": 500}]

    def test_default_channel_is_email(self, db_session: Session, excess_list: ExcessList, trader: User):
        o = ExcessOutreach(excess_list_id=excess_list.id, submitted_by=trader.id)
        db_session.add(o)
        db_session.commit()
        db_session.refresh(o)
        assert o.channel == "email"
        assert o.status == "sent"

    def test_manual_phone_log_with_null_line(
        self, db_session: Session, excess_list: ExcessList, buyer_card: VendorCard, trader: User
    ):
        # A logged phone touch against the whole list (no specific line).
        o = ExcessOutreach(
            excess_list_id=excess_list.id,
            excess_line_item_id=None,
            target_vendor_card_id=buyer_card.id,
            submitted_by=trader.id,
            channel="phone",
            status="responded",
        )
        db_session.add(o)
        db_session.commit()
        db_session.refresh(o)
        assert o.excess_line_item_id is None
        assert o.channel == "phone"
        assert o.status == "responded"

    def test_invalid_channel_rejected(self):
        with pytest.raises(ValueError, match="Invalid ExcessOutreach channel"):
            ExcessOutreach(excess_list_id=1, submitted_by=1, channel="carrier_pigeon")

    def test_invalid_status_rejected(self):
        with pytest.raises(ValueError, match="Invalid ExcessOutreach status"):
            ExcessOutreach(excess_list_id=1, submitted_by=1, status="ghosted")

    def test_all_channels_accepted(self):
        for ch in ("email", "phone", "teams", "marketplace", "other"):
            assert ExcessOutreach(excess_list_id=1, submitted_by=1, channel=ch).channel == ch

    def test_all_statuses_accepted(self):
        for st in ("sent", "opened", "responded", "bid", "declined", "no_response"):
            assert ExcessOutreach(excess_list_id=1, submitted_by=1, status=st).status == st


# ── BuyerScore ───────────────────────────────────────────────────────


class TestBuyerScoreModel:
    def test_create_with_defaults(self, db_session: Session, buyer_card: VendorCard):
        bs = BuyerScore(vendor_card_id=buyer_card.id)
        db_session.add(bs)
        db_session.commit()
        db_session.refresh(bs)
        assert bs.id is not None
        assert bs.offers_received == 0
        assert bs.wins == 0
        assert bs.avg_bid_pct_of_ask is None
        assert bs.commodity_affinity is None

    def test_vendor_card_unique(self, db_session: Session, buyer_card: VendorCard):
        db_session.add(BuyerScore(vendor_card_id=buyer_card.id))
        db_session.commit()
        db_session.add(BuyerScore(vendor_card_id=buyer_card.id))
        with pytest.raises(IntegrityError):
            db_session.commit()


# ── ActivityLog.excess_list_id scope ─────────────────────────────────


class TestActivityLogExcessScope:
    def test_log_with_excess_list_scope(self, db_session: Session, excess_list: ExcessList, trader: User):
        log = ActivityLog(
            user_id=trader.id,
            activity_type="note",
            channel="manual",
            excess_list_id=excess_list.id,
            subject="Offered Q1 surplus to Buyer One",
        )
        db_session.add(log)
        db_session.commit()
        db_session.refresh(log)
        assert log.excess_list_id == excess_list.id
        assert log.excess_list is not None
        assert log.excess_list.id == excess_list.id
