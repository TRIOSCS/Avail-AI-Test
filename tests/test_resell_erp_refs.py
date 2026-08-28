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
from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.constants import CustomerBidStatus, ExcessListStatus, ExcessOfferScope, ExcessOfferStatus
from app.models import Company, User
from app.models.excess import CustomerBid, ExcessLineItem, ExcessList, ExcessOffer, ExcessOfferLine
from app.services import bid_back_service, excess_service
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


# ── Task 2 (C3) fixtures: a POSTED list with an owner + an outsider ────


@pytest.fixture()
def outsider(db_session: Session) -> User:
    """A teammate who does NOT own the list — the non-owner gate target."""
    user = User(
        email="erp-refs-outsider@trioscs.com",
        name="ERP Refs Outsider",
        role="trader",
        azure_id="erp-refs-outsider-1",
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture()
def posted_list(db_session: Session, seller_company: Company, trader: User) -> ExcessList:
    """A posted (non-draft) list owned by *trader*, carrying one line item — the
    ``_get_list_for_user`` non-owner branch needs a posted status to reach
    ``_require_owner`` instead of 404-masking."""
    el = ExcessList(
        company_id=seller_company.id,
        owner_id=trader.id,
        title="ERP Refs Posted List",
        status=ExcessListStatus.COLLECTING,
    )
    db_session.add(el)
    db_session.flush()
    db_session.add(ExcessLineItem(excess_list_id=el.id, part_number="ERP1", quantity=100))
    db_session.commit()
    db_session.refresh(el)
    return el


def _own(app, user):
    """Override require_user to *user*; returns a cleanup callable."""
    from app.dependencies import require_user

    app.dependency_overrides[require_user] = lambda: user
    return lambda: app.dependency_overrides.pop(require_user, None)


def _posted_line(db: Session, el: ExcessList) -> ExcessLineItem:
    return db.query(ExcessLineItem).filter_by(excess_list_id=el.id).order_by(ExcessLineItem.id).first()


def _sent_bid(db: Session, el: ExcessList, owner: User) -> CustomerBid:
    line = _posted_line(db, el)
    bid = bid_back_service.build_bid_back(db, list_id=el.id, owner=owner, selections=[{"excess_line_item_id": line.id}])
    bid.status = CustomerBidStatus.SENT
    db.commit()
    db.refresh(bid)
    return bid


def _accepted_bid(db: Session, el: ExcessList, owner: User) -> CustomerBid:
    bid = _sent_bid(db, el, owner)
    bid.status = CustomerBidStatus.ACCEPTED
    db.commit()
    db.refresh(bid)
    return bid


def _won_offer(db: Session, el: ExcessList, submitter: User) -> tuple[ExcessOffer, ExcessLineItem]:
    """A per-line WON offer covering the list's single posted line."""
    line = _posted_line(db, el)
    offer = ExcessOffer(
        excess_list_id=el.id,
        submitted_by=submitter.id,
        scope=ExcessOfferScope.PER_LINE,
        status=ExcessOfferStatus.WON,
    )
    db.add(offer)
    db.flush()
    db.add(
        ExcessOfferLine(
            offer_id=offer.id,
            excess_line_item_id=line.id,
            mpn_raw=line.part_number,
            quantity=line.quantity,
            unit_price=Decimal("0.5000"),
        )
    )
    db.commit()
    db.refresh(offer)
    return offer, line


# ── save_bid_reference (service) ────────────────────────────────────


class TestSaveBidReferenceService:
    def test_persists_value(self, db_session: Session, posted_list: ExcessList, trader: User):
        bid = _accepted_bid(db_session, posted_list, trader)
        result = bid_back_service.save_bid_reference(
            db_session, list_id=posted_list.id, bid_id=bid.id, po_number="PO-2026-0042"
        )
        assert result.po_number == "PO-2026-0042"
        db_session.refresh(bid)
        assert bid.po_number == "PO-2026-0042"

    def test_blank_clears_a_saved_value(self, db_session: Session, posted_list: ExcessList, trader: User):
        bid = _accepted_bid(db_session, posted_list, trader)
        bid_back_service.save_bid_reference(db_session, list_id=posted_list.id, bid_id=bid.id, po_number="PO-1")
        result = bid_back_service.save_bid_reference(db_session, list_id=posted_list.id, bid_id=bid.id, po_number="  ")
        assert result.po_number is None

    def test_404_bid_not_on_list(self, db_session: Session, posted_list: ExcessList, trader: User):
        bid = _accepted_bid(db_session, posted_list, trader)
        with pytest.raises(HTTPException) as exc:
            bid_back_service.save_bid_reference(db_session, list_id=posted_list.id + 999, bid_id=bid.id, po_number="x")
        assert exc.value.status_code == 404


# ── save_offer_reference (service) ──────────────────────────────────


class TestSaveOfferReferenceService:
    def test_persists_value(self, db_session: Session, posted_list: ExcessList, trader: User):
        offer, _line = _won_offer(db_session, posted_list, trader)
        result = excess_service.save_offer_reference(db_session, offer_id=offer.id, sales_order_number="SO-2026-0917")
        assert result.sales_order_number == "SO-2026-0917"
        db_session.refresh(offer)
        assert offer.sales_order_number == "SO-2026-0917"

    def test_blank_clears_a_saved_value(self, db_session: Session, posted_list: ExcessList, trader: User):
        offer, _line = _won_offer(db_session, posted_list, trader)
        excess_service.save_offer_reference(db_session, offer_id=offer.id, sales_order_number="SO-1")
        result = excess_service.save_offer_reference(db_session, offer_id=offer.id, sales_order_number=" ")
        assert result.sales_order_number is None

    def test_404_missing_offer(self, db_session: Session):
        with pytest.raises(HTTPException) as exc:
            excess_service.save_offer_reference(db_session, offer_id=999999, sales_order_number="SO-1")
        assert exc.value.status_code == 404


# ── Route: POST …/bid/{id}/reference ────────────────────────────────


class TestBidReferenceRoute:
    def test_owner_saves_and_rerenders(self, client, db_session: Session, posted_list: ExcessList, trader: User):
        from app.main import app

        bid = _accepted_bid(db_session, posted_list, trader)
        restore = _own(app, trader)
        try:
            resp = client.post(
                f"/api/resell/{posted_list.id}/bid/{bid.id}/reference", data={"po_number": "PO-2026-0042"}
            )
            assert resp.status_code == 200
            assert "PO-2026-0042" in resp.text
        finally:
            restore()
        db_session.refresh(bid)
        assert bid.po_number == "PO-2026-0042"

    def test_non_owner_forbidden(self, client, db_session: Session, posted_list: ExcessList, trader: User):
        """The default client user (test_user) is not the list owner → 403."""
        bid = _accepted_bid(db_session, posted_list, trader)
        resp = client.post(f"/api/resell/{posted_list.id}/bid/{bid.id}/reference", data={"po_number": "PO-X"})
        assert resp.status_code == 403
        db_session.refresh(bid)
        assert bid.po_number is None

    def test_over_100_chars_is_truncated_to_100(
        self, client, db_session: Session, posted_list: ExcessList, trader: User
    ):
        """``CustomerBid.po_number`` is ``String(100)``.

        SQLite (this test's DB) doesn't
        enforce that length, so an over-long value only DataErrors on real PG — this
        exercises the server-side ``[:100]`` truncation in ``save_bid_reference`` that
        guards against it directly, rather than depending on the column constraint.
        """
        from app.main import app

        bid = _accepted_bid(db_session, posted_list, trader)
        long_value = "P" * 150
        expected = long_value[:100]
        restore = _own(app, trader)
        try:
            resp = client.post(f"/api/resell/{posted_list.id}/bid/{bid.id}/reference", data={"po_number": long_value})
        finally:
            restore()
        assert resp.status_code == 200
        assert expected in resp.text
        assert long_value not in resp.text
        db_session.refresh(bid)
        assert bid.po_number == expected
        assert len(bid.po_number) == 100


# ── Route: POST …/offers/{id}/reference ─────────────────────────────


class TestOfferReferenceRoute:
    def test_owner_saves_and_rerenders(self, client, db_session: Session, posted_list: ExcessList, trader: User):
        from app.main import app

        offer, _line = _won_offer(db_session, posted_list, trader)
        restore = _own(app, trader)
        try:
            resp = client.post(
                f"/api/resell/{posted_list.id}/offers/{offer.id}/reference",
                data={"sales_order_number": "SO-2026-0917"},
            )
            assert resp.status_code == 200
            assert "SO-2026-0917" in resp.text
        finally:
            restore()
        db_session.refresh(offer)
        assert offer.sales_order_number == "SO-2026-0917"

    def test_over_100_chars_is_truncated_to_100(
        self, client, db_session: Session, posted_list: ExcessList, trader: User
    ):
        """``ExcessOffer.sales_order_number`` is ``String(100)``.

        Mirrors the bid-route
        truncation test above — same server-side ``[:100]`` cap in
        ``save_offer_reference``, exercised directly rather than depending on PG's
        column constraint (SQLite, this test's DB, doesn't enforce it).
        """
        from app.main import app

        offer, _line = _won_offer(db_session, posted_list, trader)
        long_value = "S" * 150
        expected = long_value[:100]
        restore = _own(app, trader)
        try:
            resp = client.post(
                f"/api/resell/{posted_list.id}/offers/{offer.id}/reference",
                data={"sales_order_number": long_value},
            )
        finally:
            restore()
        assert resp.status_code == 200
        assert expected in resp.text
        assert long_value not in resp.text
        db_session.refresh(offer)
        assert offer.sales_order_number == expected
        assert len(offer.sales_order_number) == 100

    def test_non_owner_forbidden(self, client, db_session: Session, posted_list: ExcessList, trader: User):
        """The default client user (test_user) is not the list owner → 403."""
        offer, _line = _won_offer(db_session, posted_list, trader)
        resp = client.post(
            f"/api/resell/{posted_list.id}/offers/{offer.id}/reference", data={"sales_order_number": "SO-X"}
        )
        assert resp.status_code == 403
        db_session.refresh(offer)
        assert offer.sales_order_number is None

    def test_cross_list_offer_id_404s_and_leaves_value_unchanged(
        self, client, db_session: Session, posted_list: ExcessList, seller_company: Company, trader: User
    ):
        """Finding #32 pattern (mirrors ``resell_award_offer``/``resell_unaward_offer``,
        see test_resell_award.py::test_award_route_wrong_list_id_404_nothing_mutated /
        ::test_unaward_route_wrong_list_id_404_nothing_mutated): the owner of list A
        posting to A's URL with an offer id that actually belongs to list B must 404 —
        existence not revealed across lists, and the offer must not be mutated under the
        wrong list's URL. This is the offer route's own inline cross-list guard (``offer
        is None or offer.excess_list_id != list_id`` in ``resell_save_offer_reference``);
        the bid-side twin lives in ``save_bid_reference`` and is already covered by
        ``TestSaveBidReferenceService::test_404_bid_not_on_list``.
        """
        from app.main import app

        other_list = ExcessList(
            company_id=seller_company.id,
            owner_id=trader.id,
            title="ERP Refs Other List (cross-list target)",
            status=ExcessListStatus.COLLECTING,
        )
        db_session.add(other_list)
        db_session.flush()
        db_session.add(ExcessLineItem(excess_list_id=other_list.id, part_number="ERP-OTHER", quantity=25))
        db_session.commit()
        db_session.refresh(other_list)

        offer_on_b, _line_b = _won_offer(db_session, other_list, trader)
        restore = _own(app, trader)
        try:
            resp = client.post(
                f"/api/resell/{posted_list.id}/offers/{offer_on_b.id}/reference",
                data={"sales_order_number": "SO-SHOULD-NOT-SAVE"},
            )
        finally:
            restore()

        assert resp.status_code == 404
        db_session.refresh(offer_on_b)
        assert offer_on_b.sales_order_number is None


# ── Template: accepted-branch capture UI + next-step checklist ─────


class TestBuildBidAcceptedBranch:
    def test_shows_capture_input_when_empty(self, client, db_session: Session, posted_list: ExcessList, trader: User):
        from app.main import app

        _accepted_bid(db_session, posted_list, trader)
        restore = _own(app, trader)
        try:
            resp = client.get(f"/v2/partials/resell/{posted_list.id}/build-bid")
        finally:
            restore()
        assert resp.status_code == 200
        assert 'name="po_number"' in resp.text
        assert "Cut the customer" in resp.text  # next-step checklist

    def test_shows_saved_value_not_input(self, client, db_session: Session, posted_list: ExcessList, trader: User):
        from app.main import app

        bid = _accepted_bid(db_session, posted_list, trader)
        bid.po_number = "PO-2026-0099"
        db_session.commit()
        restore = _own(app, trader)
        try:
            resp = client.get(f"/v2/partials/resell/{posted_list.id}/build-bid")
        finally:
            restore()
        assert resp.status_code == 200
        assert "PO-2026-0099" in resp.text
        assert 'name="po_number"' not in resp.text


# ── Template: won offer row carries the sales_order_number field ───


class TestOffersWonRow:
    def test_won_row_shows_capture_input_when_empty(
        self, client, db_session: Session, posted_list: ExcessList, trader: User
    ):
        from app.main import app

        _won_offer(db_session, posted_list, trader)
        restore = _own(app, trader)
        try:
            resp = client.get(f"/v2/partials/resell/{posted_list.id}/offers")
        finally:
            restore()
        assert resp.status_code == 200
        assert 'name="sales_order_number"' in resp.text

    def test_won_row_shows_saved_value(self, client, db_session: Session, posted_list: ExcessList, trader: User):
        from app.main import app

        offer, _line = _won_offer(db_session, posted_list, trader)
        offer.sales_order_number = "SO-2026-0500"
        db_session.commit()
        restore = _own(app, trader)
        try:
            resp = client.get(f"/v2/partials/resell/{posted_list.id}/offers")
        finally:
            restore()
        assert resp.status_code == 200
        assert "SO-2026-0500" in resp.text
        assert 'name="sales_order_number"' not in resp.text
