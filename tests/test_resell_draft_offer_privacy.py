"""test_resell_draft_offer_privacy.py — Draft-privacy regression for the offer funnel.

Guards the resell offer entry points (the submit-offer modal and the offer POST) against
leaking or accepting offers on an UNPUBLISHED (draft) list. A non-owner with ``can_offer``
must get a 404 (existence not revealed) on a draft list — only the owner sees a draft, and
nobody may bid on it until it is posted. A posted (collecting) list still works for the
same non-owner.

Called by: pytest. Depends on: conftest fixtures (client auths as test_user, a buyer),
app.routers.resell, app.services.excess_service.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy.orm import Session

from app.constants import ExcessLineItemStatus, ExcessListStatus, ExcessOfferScope, ExcessOfferStatus
from app.models import Company, User
from app.models.excess import ExcessLineItem, ExcessList, ExcessOffer
from app.utils.normalization import normalize_mpn_key


@pytest.fixture()
def owner_user(db_session: Session) -> User:
    """The list owner — a trader (can_post + can_offer), distinct from the buyer
    client."""
    user = User(
        email="owner-trader@trioscs.com",
        name="Olive Owner",
        role="trader",
        azure_id="test-azure-owner-trader",
        created_at=datetime.now(UTC),
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


def _list_with_line(db_session: Session, owner: User, company: Company, status: str) -> ExcessList:
    el = ExcessList(
        title=f"List ({status})",
        company_id=company.id,
        owner_id=owner.id,
        status=status,
        total_line_items=1,
        created_at=datetime.now(UTC),
    )
    db_session.add(el)
    db_session.flush()
    db_session.add(
        ExcessLineItem(
            excess_list_id=el.id,
            part_number="XCVU9P-2FLGA2104I",
            normalized_part_number=normalize_mpn_key("XCVU9P-2FLGA2104I"),
            quantity=50,
            condition="New",
        )
    )
    db_session.commit()
    db_session.refresh(el)
    return el


@pytest.fixture()
def draft_list(db_session: Session, owner_user: User, test_company: Company) -> ExcessList:
    """A DRAFT (unpublished) list owned by owner_user — invisible to non-owners."""
    return _list_with_line(db_session, owner_user, test_company, ExcessListStatus.DRAFT)


@pytest.fixture()
def posted_list(db_session: Session, owner_user: User, test_company: Company) -> ExcessList:
    """A posted (collecting) list owned by owner_user — open for offers."""
    return _list_with_line(db_session, owner_user, test_company, ExcessListStatus.COLLECTING)


def test_non_owner_offer_form_on_draft_404(client, draft_list, owner_user, test_user):
    """Non-owner GET on a draft list's offer-form modal → 404 (existence not
    revealed)."""
    assert test_user.id != owner_user.id
    resp = client.get(f"/v2/partials/resell/{draft_list.id}/offer-form")
    assert resp.status_code == 404


def test_non_owner_submit_offer_on_draft_404(client, db_session, draft_list, owner_user, test_user):
    """Non-owner POST of an offer on a draft list → 404 and NO offer persisted."""
    assert test_user.id != owner_user.id
    resp = client.post(
        f"/api/resell/{draft_list.id}/offers",
        data={"scope": "per_line", "mpn_raw": "XCVU9P-2FLGA2104I", "quantity": "10", "unit_price": "5.00"},
    )
    assert resp.status_code == 404
    offers = db_session.query(ExcessOffer).filter_by(excess_list_id=draft_list.id).all()
    assert offers == []


def test_non_owner_offer_form_on_posted_200(client, posted_list, owner_user, test_user):
    """The same non-owner CAN open the offer-form on a posted (collecting) list."""
    resp = client.get(f"/v2/partials/resell/{posted_list.id}/offer-form")
    assert resp.status_code == 200


def test_non_owner_submit_offer_on_posted_200(client, db_session, posted_list, owner_user, test_user):
    """The same non-owner CAN submit an offer on a posted list (offer persisted)."""
    resp = client.post(
        f"/api/resell/{posted_list.id}/offers",
        data={"scope": "per_line", "mpn_raw": "XCVU9P-2FLGA2104I", "quantity": "10", "unit_price": "5.00"},
    )
    assert resp.status_code == 200
    offers = db_session.query(ExcessOffer).filter_by(excess_list_id=posted_list.id).all()
    assert len(offers) == 1


def _posted_list_with_best_offer(db_session, owner, company):
    """A posted list whose single line carries a best competing offer price + count."""
    el = _list_with_line(db_session, owner, company, ExcessListStatus.COLLECTING)
    line = db_session.query(ExcessLineItem).filter_by(excess_list_id=el.id).first()
    line.best_offer_unit_price = 12.3456
    line.offer_count = 3
    db_session.commit()
    return el


def test_non_owner_lines_tab_hides_best_offer_price_and_count(client, db_session, owner_user, test_company, test_user):
    """RS-1 (data leak): the Lines tab must NOT show a non-owner broker the current best
    COMPETING offer price or the offer count — that's the same data the Offers tab and
    compare endpoint 403-guard.

    client (test_user, a buyer) is NOT the owner.
    """
    assert test_user.id != owner_user.id
    el = _posted_list_with_best_offer(db_session, owner_user, test_company)

    resp = client.get(f"/v2/partials/resell/{el.id}/lines")
    assert resp.status_code == 200
    body = resp.text
    assert "12.3456" not in body, "best competing offer price leaked to a non-owner broker"
    assert "12.35" not in body
    assert "3 offer" not in body, "competing offer count leaked to a non-owner broker"
    # The line itself (MPN/qty) is still shown — only the offer data is hidden.
    assert "XCVU9P-2FLGA2104I" in body


def test_owner_lines_tab_shows_best_offer_price_and_count(monkeypatch, client, db_session, owner_user, test_company):
    """Control: the OWNER does see the best offer price + count (the gate is
    owner-scoped, not a blanket hide)."""
    el = _posted_list_with_best_offer(db_session, owner_user, test_company)

    # Authenticate the client as the owner for this request.
    from app.dependencies import require_user

    client.app.dependency_overrides[require_user] = lambda: owner_user
    try:
        resp = client.get(f"/v2/partials/resell/{el.id}/lines")
    finally:
        client.app.dependency_overrides.pop(require_user, None)
    assert resp.status_code == 200
    assert "12.3456" in resp.text, "owner must still see the best offer price"
    assert "3 offer" in resp.text, "owner must still see the offer count"


# ── D2: offer-count / coverage / awarded aggregates are OWNER-PRIVATE ──────
# One policy (predicate `can_see_customer`) across every count/coverage/existence
# surface: the header "N offers" chip, the Offers-tab count badge, the open-lens
# coverage meter + amber offer badge, and the "N/M awarded" progress chip. A
# non-owner (offerer) sees none of them — the same class of competitive leak the
# per-line offer badge (RS-1) already hides.


def _posted_list_with_offers(db_session, owner, company, *, n_offers: int) -> ExcessList:
    """A posted (collecting) list carrying ``n_offers`` live OPEN offers."""
    el = _list_with_line(db_session, owner, company, ExcessListStatus.COLLECTING)
    for _ in range(n_offers):
        db_session.add(
            ExcessOffer(
                excess_list_id=el.id,
                submitted_by=owner.id,
                scope=ExcessOfferScope.PER_LINE,
                status=ExcessOfferStatus.OPEN,
            )
        )
    db_session.commit()
    return el


def test_non_owner_detail_hides_offer_count_chip_and_badge(client, db_session, owner_user, test_company, test_user):
    """D2: a non-owner viewing a posted list's DETAIL must not see the "N offers" header
    chip or the Offers-tab count badge — both leak how much competitive interest the
    list has drawn."""
    assert test_user.id != owner_user.id
    el = _posted_list_with_offers(db_session, owner_user, test_company, n_offers=2)

    body = client.get(f"/v2/partials/resell/{el.id}").text
    # Header chip renders "2 offers"; the tab badge's aria-label is "2 offers" — both gone.
    assert "2 offer" not in body, "offer count leaked to a non-owner (header chip or tab badge)"
    # The public line-count chip is unaffected (offerers need the listing size).
    assert "1 line" in body


def test_owner_detail_shows_offer_count_chip_and_badge(client, db_session, owner_user, test_company):
    """Control: the OWNER still sees the offer count (header chip + tab badge)."""
    el = _posted_list_with_offers(db_session, owner_user, test_company, n_offers=2)

    from app.dependencies import require_user

    client.app.dependency_overrides[require_user] = lambda: owner_user
    try:
        body = client.get(f"/v2/partials/resell/{el.id}").text
    finally:
        client.app.dependency_overrides.pop(require_user, None)
    assert "2 offer" in body, "owner must still see the offer count"


def test_non_owner_detail_hides_awarded_chip(client, db_session, owner_user, test_company, test_user):
    """D2 (one policy everywhere): the "N/M awarded" progress chip is owner-private — a
    non-owner watching deal progress is the same leak class as the offer count."""
    assert test_user.id != owner_user.id
    el = _list_with_line(db_session, owner_user, test_company, ExcessListStatus.COLLECTING)
    line = db_session.query(ExcessLineItem).filter_by(excess_list_id=el.id).first()
    line.status = ExcessLineItemStatus.AWARDED
    db_session.commit()

    body = client.get(f"/v2/partials/resell/{el.id}").text
    assert "1/1 awarded" not in body, "awarded-progress chip leaked to a non-owner"


def test_owner_detail_shows_awarded_chip(client, db_session, owner_user, test_company):
    """Control: the OWNER still sees the "N/M awarded" progress chip."""
    el = _list_with_line(db_session, owner_user, test_company, ExcessListStatus.COLLECTING)
    line = db_session.query(ExcessLineItem).filter_by(excess_list_id=el.id).first()
    line.status = ExcessLineItemStatus.AWARDED
    db_session.commit()

    from app.dependencies import require_user

    client.app.dependency_overrides[require_user] = lambda: owner_user
    try:
        body = client.get(f"/v2/partials/resell/{el.id}").text
    finally:
        client.app.dependency_overrides.pop(require_user, None)
    assert "1/1 awarded" in body, "owner must still see the awarded-progress chip"


def test_open_lens_hides_coverage_meter_and_offer_badge(client, db_session, owner_user, test_company, test_user):
    """D2: the open (offerer) lens must not show the per-list coverage meter or the amber
    offer-count badge — they reveal how many lines already have offers and how many bids
    are in. Owner (mine lens) still sees them."""
    assert test_user.id != owner_user.id
    el = _list_with_line(db_session, owner_user, test_company, ExcessListStatus.COLLECTING)
    line = db_session.query(ExcessLineItem).filter_by(excess_list_id=el.id).first()
    line.offer_count = 3  # line reads as "covered" → coverage meter would show 1/1
    db_session.add(
        ExcessOffer(
            excess_list_id=el.id,
            submitted_by=owner_user.id,
            scope=ExcessOfferScope.PER_LINE,
            status=ExcessOfferStatus.OPEN,
        )
    )
    db_session.commit()

    # Non-owner, open lens: the coverage meter (+ its amber-badge sibling, same gate) is gone.
    open_body = client.get("/v2/partials/resell/lists?lens=open").text
    assert "Offer coverage:" not in open_body, "coverage meter leaked to a non-owner (open lens)"

    # Owner, mine lens: the meter is present.
    from app.dependencies import require_user

    client.app.dependency_overrides[require_user] = lambda: owner_user
    try:
        mine_body = client.get("/v2/partials/resell/lists?lens=mine").text
    finally:
        client.app.dependency_overrides.pop(require_user, None)
    assert "Offer coverage:" in mine_body, "owner must still see the coverage meter"
