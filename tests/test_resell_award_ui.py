"""test_resell_award_ui.py — rendered-HTML checks for the RS-3 award picker.

Complements the behavioural award/unaward tests (test_resell_award.py) by asserting the
wiring is actually visible in the served partials: the Award button on the Offers tab and
the per-line Compare modal, the Awarded pill on the Lines tab, the N/M-awarded header
chip, and — the privacy boundary — that a non-owner never sees any award affordance.

Called by: pytest
Depends on: app.routers.resell, app.services.excess_service, tests.conftest
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from app.constants import ExcessLineItemStatus, ExcessListStatus, ExcessOfferStatus, OfferLineMatchStatus
from app.models import Company, User, VendorCard
from app.models.excess import ExcessLineItem, ExcessList, ExcessOffer, ExcessOfferLine
from app.models.intelligence import MaterialCard
from tests.conftest import engine

_ = engine


# ── Fixtures ─────────────────────────────────────────────────────────


@pytest.fixture()
def owner(db_session: Session) -> User:
    u = User(email="ui-owner@trioscs.com", name="UI Owner", role="trader", azure_id="ui-owner-1")
    db_session.add(u)
    db_session.commit()
    db_session.refresh(u)
    return u


@pytest.fixture()
def non_owner(db_session: Session) -> User:
    u = User(email="ui-broker@trioscs.com", name="UI Broker", role="buyer", azure_id="ui-broker-1")
    db_session.add(u)
    db_session.commit()
    db_session.refresh(u)
    return u


@pytest.fixture()
def posted_list(db_session: Session, owner: User) -> ExcessList:
    co = Company(name="UI Seller Co")
    db_session.add(co)
    db_session.flush()
    el = ExcessList(company_id=co.id, owner_id=owner.id, title="UI Award List", status=ExcessListStatus.COLLECTING)
    db_session.add(el)
    db_session.commit()
    db_session.refresh(el)
    return el


def _line(db: Session, el: ExcessList, pn: str) -> ExcessLineItem:
    mc = MaterialCard(normalized_mpn=pn.lower(), display_mpn=pn, category="capacitors")
    db.add(mc)
    db.flush()
    li = ExcessLineItem(
        excess_list_id=el.id, part_number=pn, quantity=100, material_card_id=mc.id, asking_price=Decimal("1.00")
    )
    db.add(li)
    db.flush()
    return li


def _open_offer(db: Session, el: ExcessList, submitter: User, line: ExcessLineItem) -> ExcessOffer:
    vc = VendorCard(normalized_name=f"buyer-{line.id}", display_name=f"Buyer {line.id}")
    db.add(vc)
    db.flush()
    offer = ExcessOffer(
        excess_list_id=el.id,
        submitted_by=submitter.id,
        offerer_vendor_card_id=vc.id,
        scope="per_line",
        status=ExcessOfferStatus.OPEN,
    )
    db.add(offer)
    db.flush()
    db.add(
        ExcessOfferLine(
            offer_id=offer.id,
            excess_line_item_id=line.id,
            mpn_raw=line.part_number,
            quantity=line.quantity,
            unit_price=Decimal("0.80"),
            match_status=OfferLineMatchStatus.MATCHED,
        )
    )
    db.commit()
    return offer


def _as(user: User):
    """Override require_user for the duration of one request."""
    from app.dependencies import require_user
    from app.main import app

    app.dependency_overrides[require_user] = lambda: user
    return require_user


def _unset(dep):
    from app.main import app

    app.dependency_overrides.pop(dep, None)


# ── Tests ────────────────────────────────────────────────────────────


def test_offers_tab_shows_award_button_for_open_offer(
    client, db_session: Session, posted_list: ExcessList, owner: User, non_owner: User
):
    line = _line(db_session, posted_list, "OFFERTAB-A")
    offer = _open_offer(db_session, posted_list, non_owner, line)

    dep = _as(owner)
    try:
        resp = client.get(f"/v2/partials/resell/{posted_list.id}/offers")
    finally:
        _unset(dep)

    assert resp.status_code == 200
    assert f"/offers/{offer.id}/award" in resp.text
    assert ">Award</button>" in resp.text


def test_offer_compare_shows_award_button(
    client, db_session: Session, posted_list: ExcessList, owner: User, non_owner: User
):
    line = _line(db_session, posted_list, "COMPARE-A")
    offer = _open_offer(db_session, posted_list, non_owner, line)

    dep = _as(owner)
    try:
        resp = client.get(f"/v2/partials/resell/{posted_list.id}/lines/{line.id}/offers")
    finally:
        _unset(dep)

    assert resp.status_code == 200
    assert f"/offers/{offer.id}/award" in resp.text
    assert "Action" in resp.text


def test_lines_tab_shows_awarded_pill(client, db_session: Session, posted_list: ExcessList, owner: User):
    line = _line(db_session, posted_list, "PILL-A")
    line.status = ExcessLineItemStatus.AWARDED
    db_session.commit()

    dep = _as(owner)
    try:
        resp = client.get(f"/v2/partials/resell/{posted_list.id}/lines")
    finally:
        _unset(dep)

    assert resp.status_code == 200
    assert "Awarded" in resp.text


def test_header_shows_awarded_chip(client, db_session: Session, posted_list: ExcessList, owner: User):
    a = _line(db_session, posted_list, "CHIP-A")
    _line(db_session, posted_list, "CHIP-B")
    a.status = ExcessLineItemStatus.AWARDED
    db_session.commit()

    dep = _as(owner)
    try:
        resp = client.get(f"/v2/partials/resell/{posted_list.id}")
    finally:
        _unset(dep)

    assert resp.status_code == 200
    assert "1/2 awarded" in resp.text


def test_non_owner_never_sees_award_affordance(
    client, db_session: Session, posted_list: ExcessList, owner: User, non_owner: User
):
    """A non-owner's Offers tab is the private state — no award/unaward wiring at
    all."""
    line = _line(db_session, posted_list, "PRIVACY-A")
    _open_offer(db_session, posted_list, non_owner, line)

    dep = _as(non_owner)
    try:
        resp = client.get(f"/v2/partials/resell/{posted_list.id}/offers")
    finally:
        _unset(dep)

    assert resp.status_code == 200
    assert "/award" not in resp.text
    assert "/unaward" not in resp.text
