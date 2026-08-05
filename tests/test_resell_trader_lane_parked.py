"""test_resell_trader_lane_parked.py — W2.3 parked-off state pins (spec §5.3).

Solo-operator mode parks two resell lanes until a second trader user exists:

1. The internal-trader offer lane — the "Open to Me" lens is coerced to the owner
   ("mine") lens, the submit-offer modal + offer POST routes are UNREGISTERED, and
   the detail context hard-Falses ``can_offer`` so the Submit-offer button never
   renders.
2. The buyer-intelligence DISPLAY layer — the not-yet-strip route (nudge + its auto
   My-Day task writes) is UNREGISTERED, and the offer-to-buyers panel renders with
   EMPTY ranked-suggestion / no-contact sections (manual add keeps kernel outreach
   working).

Implementations stay in place (functions, templates, services) — these tests pin
exactly the parked-off surface so an accidental re-registration or lens revival
fails loudly. Comeback trigger: second trader user exists.

Called by: pytest
Depends on: conftest.py (client auths as test_user, a buyer), app.routers.resell,
            app.services.excess_service.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy.orm import Session

from app.constants import ExcessListStatus
from app.models import Company, User
from app.models.excess import ExcessLineItem, ExcessList, ExcessOffer
from app.utils.normalization import normalize_mpn_key


@pytest.fixture()
def owner_trader(db_session: Session) -> User:
    """The list owner — a trader (can_post + can_offer)."""
    user = User(
        email="parked-owner@trioscs.com",
        name="Pat Parked",
        role="trader",
        azure_id="test-azure-parked-owner",
        created_at=datetime.now(UTC),
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture()
def posted_list(db_session: Session, owner_trader: User, test_company: Company) -> ExcessList:
    """A posted (collecting) list owned by the trader, with one line."""
    el = ExcessList(
        title="Parked-lane surplus",
        company_id=test_company.id,
        owner_id=owner_trader.id,
        status=ExcessListStatus.BIDDING,
        total_line_items=1,
        created_at=datetime.now(UTC),
    )
    db_session.add(el)
    db_session.flush()
    db_session.add(
        ExcessLineItem(
            excess_list_id=el.id,
            part_number="LM358N",
            normalized_part_number=normalize_mpn_key("LM358N"),
            quantity=100,
            condition="New",
        )
    )
    db_session.commit()
    db_session.refresh(el)
    return el


def _as_owner(client, owner):
    """Override require_user to the owner; returns a cleanup callable."""
    from app.dependencies import require_user
    from app.main import app

    app.dependency_overrides[require_user] = lambda: owner
    return lambda: app.dependency_overrides.pop(require_user, None)


# ── 1. Trader offer lane ─────────────────────────────────────────────


def test_offer_form_route_unregistered(client, posted_list, owner_trader, test_user):
    """The submit-offer modal route 404s — even for the previously-eligible non-owner
    buyer on a posted list (was 200 before the park)."""
    assert test_user.id != owner_trader.id
    resp = client.get(f"/v2/partials/resell/{posted_list.id}/offer-form")
    assert resp.status_code == 404


def test_submit_offer_route_unregistered(client, db_session, posted_list, owner_trader, test_user):
    """The offer POST 404s and persists nothing — the lane's only submit door."""
    resp = client.post(
        f"/api/resell/{posted_list.id}/offers",
        data={"scope": "per_line", "mpn_raw": "LM358N", "quantity": "10", "unit_price": "5.00"},
    )
    assert resp.status_code == 404
    assert db_session.query(ExcessOffer).filter_by(excess_list_id=posted_list.id).count() == 0


def test_workspace_has_no_open_lens_pill(client, owner_trader, posted_list):
    """The workspace renders only the owner lens pill — no "Open to Me"."""
    restore = _as_owner(client, owner_trader)
    try:
        resp = client.get("/v2/partials/resell/workspace")
    finally:
        restore()
    assert resp.status_code == 200
    assert "My Lists" in resp.text
    assert "Open to Me" not in resp.text


def test_owner_lens_open_coerced_to_mine(client, owner_trader, posted_list):
    """A crafted ``lens=open`` URL serves the MINE lens: the owner sees their own titled
    list (the open lens would exclude own lists and anonymize titles)."""
    restore = _as_owner(client, owner_trader)
    try:
        body = client.get("/v2/partials/resell/lists?lens=open").text
    finally:
        restore()
    assert posted_list.title in body  # own list, real title → mine lens served
    assert f"Excess listing #{posted_list.id}" not in body  # no anonymized labels


def test_detail_never_renders_submit_offer_button(client, posted_list, owner_trader, test_user):
    """can_offer is hard-False: the eligible non-owner buyer sees no Submit-offer
    button on a posted list's detail."""
    assert test_user.id != owner_trader.id
    resp = client.get(f"/v2/partials/resell/{posted_list.id}")
    assert resp.status_code == 200
    assert "Submit offer" not in resp.text
    assert "offer-form" not in resp.text


# ── 2. Buyer-intelligence display layer ──────────────────────────────


def test_not_yet_strip_route_unregistered(client, posted_list, owner_trader):
    """The nudge strip (and its auto My-Day task writes) 404s even for the owner."""
    restore = _as_owner(client, owner_trader)
    try:
        resp = client.get(f"/v2/partials/resell/{posted_list.id}/not-yet-strip")
    finally:
        restore()
    assert resp.status_code == 404


def test_detail_outreach_tab_has_no_nudge_embed(client, posted_list, owner_trader):
    """The owner detail no longer lazy-loads the parked not-yet-strip."""
    restore = _as_owner(client, owner_trader)
    try:
        resp = client.get(f"/v2/partials/resell/{posted_list.id}")
    finally:
        restore()
    assert resp.status_code == 200
    assert "not-yet-strip" not in resp.text


def test_buyer_panel_parked_empty_with_manual_add(client, monkeypatch, posted_list, owner_trader):
    """The offer-to-buyers panel (kernel outreach door) still renders, but with the
    buyer-intelligence sections PARKED: ranking is never computed, the empty state
    shows, and the manual-add path + finding-#6 setChannel wiring stay intact."""
    import app.routers.resell as resell_router

    def _boom(*args, **kwargs):
        raise AssertionError("buyer-intelligence computed while parked (W2.3)")

    # If the park regresses and either helper is called, the render errors.
    monkeypatch.setattr(resell_router, "_suggestion_rows", _boom)
    monkeypatch.setattr(resell_router, "_no_contact_buyers", _boom)

    restore = _as_owner(client, owner_trader)
    try:
        resp = client.get(f"/v2/partials/resell/{posted_list.id}/offer-buyers-form")
    finally:
        restore()
    assert resp.status_code == 200
    body = resp.text
    assert "Offer to buyers" in body
    assert "Suggested buyers (0)" in body
    assert "No ranked buyers yet" in body  # existing empty state
    assert "Add a buyer by name" in body  # manual add keeps outreach working
    assert "setChannel(" in body  # finding-#6 purge wiring intact
