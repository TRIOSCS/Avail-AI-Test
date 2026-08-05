"""test_resell_close_outcome.py — the ladder's last step: award → outcome (W3.10).

Covers the forward ``excess_lists.outcome`` writer added with the W3 status
collapse (spec §5.3: "outcome field on close — sold / scrapped / withdrawn /
no-bids"; migration 207 only backfilled historical rows):
  • ``close_awarded_list`` — AWARDED → terminal ``closed`` + the owner-picked
    outcome (sold/scrapped/withdrawn); 422 on any other value, 409 off-state,
    403 non-owner; a pre-existing ``close_at`` window stamp is preserved;
  • ``close_list_without_bid`` now stamps ``no_bids``;
  • the route door POST /api/resell/{id}/close-awarded.

Called by: pytest
Depends on: app.services.excess_service, app.models.excess, tests.conftest
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.constants import ExcessListOutcome, ExcessListStatus
from app.models import Company, User
from app.models.excess import ExcessList
from app.services import excess_service
from app.services.excess_mirror import publish_list
from app.services.excess_service import create_excess_list, import_line_items

# ── Fixtures / helpers (test_resell_list_lifecycle idiom) ────────────


@pytest.fixture()
def company(db_session: Session) -> Company:
    co = Company(name="Outcome Surplus Co")
    db_session.add(co)
    db_session.commit()
    db_session.refresh(co)
    return co


@pytest.fixture()
def owner(db_session: Session) -> User:
    user = User(email="oc-owner@trioscs.com", name="Ora Owner", role="trader", azure_id="oc-owner-1")
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture()
def other_user(db_session: Session) -> User:
    user = User(email="oc-other@trioscs.com", name="Nate NonOwner", role="trader", azure_id="oc-other-1")
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


def _awarded_list(db: Session, owner: User, company: Company) -> ExcessList:
    el = create_excess_list(db, title="Awarded excess", company_id=company.id, owner_id=owner.id)
    import_line_items(db, el.id, [{"part_number": "LM358N", "quantity": "100"}])
    publish_list(db, el.id, owner)
    el.status = ExcessListStatus.AWARDED
    db.commit()
    db.refresh(el)
    return el


# ── close_awarded_list ───────────────────────────────────────────────


@pytest.mark.parametrize("outcome", ["sold", "scrapped", "withdrawn"])
def test_close_awarded_records_outcome(db_session, owner, company, outcome):
    el = _awarded_list(db_session, owner, company)

    closed = excess_service.close_awarded_list(db_session, el.id, owner, outcome)

    assert closed.status == ExcessListStatus.CLOSED
    assert closed.outcome == outcome
    assert closed.close_at is not None


def test_close_awarded_preserves_existing_close_at(db_session, owner, company):
    """A posting-window stamp from close_list survives — the outcome close only fills
    close_at when it was never stamped."""
    el = _awarded_list(db_session, owner, company)
    window_end = datetime.now(UTC) - timedelta(days=2)
    el.close_at = window_end
    db_session.commit()

    closed = excess_service.close_awarded_list(db_session, el.id, owner, "sold")

    assert closed.close_at.replace(tzinfo=UTC) == window_end


def test_close_awarded_rejects_unknown_outcome(db_session, owner, company):
    el = _awarded_list(db_session, owner, company)
    with pytest.raises(HTTPException) as exc:
        excess_service.close_awarded_list(db_session, el.id, owner, "vaporized")
    assert exc.value.status_code == 422


def test_close_awarded_rejects_no_bids(db_session, owner, company):
    """An awarded list had a winning bid — no_bids is a contradiction here (it is
    stamped only by the close-without-bid exit)."""
    el = _awarded_list(db_session, owner, company)
    with pytest.raises(HTTPException) as exc:
        excess_service.close_awarded_list(db_session, el.id, owner, "no_bids")
    assert exc.value.status_code == 422


def test_close_awarded_rejects_non_awarded_state(db_session, owner, company):
    el = create_excess_list(db_session, title="Still posted", company_id=company.id, owner_id=owner.id)
    import_line_items(db_session, el.id, [{"part_number": "NE555P", "quantity": "10"}])
    publish_list(db_session, el.id, owner)
    with pytest.raises(HTTPException) as exc:
        excess_service.close_awarded_list(db_session, el.id, owner, "sold")
    assert exc.value.status_code == 409


def test_close_awarded_owner_only(db_session, owner, other_user, company):
    el = _awarded_list(db_session, owner, company)
    with pytest.raises(HTTPException) as exc:
        excess_service.close_awarded_list(db_session, el.id, other_user, "sold")
    assert exc.value.status_code == 403


# ── close_list_without_bid stamps no_bids ────────────────────────────


def test_close_without_bid_stamps_no_bids_outcome(db_session, owner, company):
    el = create_excess_list(db_session, title="No takers", company_id=company.id, owner_id=owner.id)
    import_line_items(db_session, el.id, [{"part_number": "BAT54S", "quantity": "500"}])
    publish_list(db_session, el.id, owner)

    closed = excess_service.close_list_without_bid(db_session, el.id, owner)

    assert closed.status == ExcessListStatus.CLOSED
    assert closed.outcome == ExcessListOutcome.NO_BIDS


# ── route door ───────────────────────────────────────────────────────


def test_close_awarded_route_renders_outcome_badge(client, db_session, owner, company):
    from app.dependencies import require_user
    from app.main import app

    el = _awarded_list(db_session, owner, company)
    el_id = el.id

    # The default client user is not the owner → 403 surfaces from the service.
    assert client.post(f"/api/resell/{el_id}/close-awarded", data={"outcome": "sold"}).status_code == 403

    app.dependency_overrides[require_user] = lambda: owner
    try:
        resp = client.post(f"/api/resell/{el_id}/close-awarded", data={"outcome": "sold"})
    finally:
        app.dependency_overrides.pop(require_user, None)

    assert resp.status_code == 200
    assert 'data-testid="list-outcome"' in resp.text
    db_session.refresh(el)
    assert el.status == ExcessListStatus.CLOSED
    assert el.outcome == ExcessListOutcome.SOLD
