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

from datetime import datetime, timezone

import pytest
from sqlalchemy.orm import Session

from app.constants import ExcessListStatus
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
        created_at=datetime.now(timezone.utc),
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
        created_at=datetime.now(timezone.utc),
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
