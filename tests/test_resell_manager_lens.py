"""test_resell_manager_lens.py — manager "all" lens, identity still hidden per card
(Phase-3 resell Task 5, C4, Decision G).

``lens=all`` is a NEW manager/admin-only oversight lens over every POSTED excess list
tenant-wide (own + everyone else's, in ONE render) — gated on
``is_manager_or_admin`` (app.dependencies). It does NOT change what identity a manager
may see: ``can_see_customer`` is computed PER CARD (``el.owner_id == user.id``), so a
manager's own listings render full data while every foreign listing in the SAME render
stays anonymized exactly like the existing "Open to Me" lens (seller/title hidden,
coverage/offer_count/needs hidden, collecting/open merged to a neutral "Open" badge).
A non-manager requesting ``lens=all`` silently falls back to ``open`` — no error, no
leak of rows they aren't entitled to.

Called by: pytest.
Depends on: conftest fixtures (client auths as test_user; sales_user is non-manager),
    app.routers.resell, app.models.excess.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy.orm import Session

from app.constants import ExcessListStatus, ExcessOfferScope, ExcessOfferStatus
from app.dependencies import require_user
from app.models import Company, User
from app.models.excess import ExcessLineItem, ExcessList, ExcessOffer
from app.utils.normalization import normalize_mpn_key


@pytest.fixture()
def manager_user(db_session: Session) -> User:
    """A manager — the only role (besides admin) entitled to lens=all."""
    user = User(
        email="mgrlens-manager@trioscs.com",
        name="Mona Manager",
        role="manager",
        azure_id="test-azure-mgrlens-manager",
        created_at=datetime.now(UTC),
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture()
def foreign_owner(db_session: Session) -> User:
    """A trader who owns a list the manager does NOT own."""
    user = User(
        email="mgrlens-foreign@trioscs.com",
        name="Fiona Foreign",
        role="trader",
        azure_id="test-azure-mgrlens-foreign",
        created_at=datetime.now(UTC),
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture()
def foreign_company(db_session: Session) -> Company:
    """Owner-identity Company for the foreign list — must never leak to the manager."""
    co = Company(name="Foreign Customer Corp", is_active=True, created_at=datetime.now(UTC))
    db_session.add(co)
    db_session.commit()
    db_session.refresh(co)
    return co


def _posted_list(
    db_session: Session,
    owner: User,
    company: Company,
    *,
    title: str,
    status: str = ExcessListStatus.COLLECTING,
) -> ExcessList:
    el = ExcessList(
        title=title,
        company_id=company.id,
        owner_id=owner.id,
        status=status,
        created_at=datetime.now(UTC),
    )
    db_session.add(el)
    db_session.flush()
    db_session.add(
        ExcessLineItem(
            excess_list_id=el.id,
            part_number="XCVU9P-2FLGA2104I",
            normalized_part_number=normalize_mpn_key("XCVU9P-2FLGA2104I"),
            manufacturer="Xilinx",
            condition="New",
            quantity=10,
        )
    )
    db_session.commit()
    db_session.refresh(el)
    return el


def _as_user(client, user: User):
    client.app.dependency_overrides[require_user] = lambda: user
    return client


def _clear_override(client):
    client.app.dependency_overrides.pop(require_user, None)


# ── (a) manager lens=all sees a foreign list AND their own ─────────────────


def test_manager_all_lens_sees_own_and_foreign_lists(
    client, db_session, manager_user, foreign_owner, foreign_company, test_company
):
    foreign_el = _posted_list(db_session, foreign_owner, foreign_company, title="Foreign — do not leak")
    own_el = _posted_list(db_session, manager_user, test_company, title="Manager owned posting")

    _as_user(client, manager_user)
    try:
        resp = client.get("/v2/partials/resell/list-rows?lens=all")
    finally:
        _clear_override(client)

    assert resp.status_code == 200
    body = resp.text
    assert f"Excess listing #{foreign_el.id}" in body
    assert own_el.title in body


# ── (b) foreign card anonymized, own card shows identity, SAME render ──────


def test_manager_all_lens_anonymizes_foreign_card_but_not_own(
    client, db_session, manager_user, foreign_owner, foreign_company, test_company
):
    foreign_el = _posted_list(db_session, foreign_owner, foreign_company, title="Foreign — do not leak")
    db_session.add(
        ExcessOffer(
            excess_list_id=foreign_el.id,
            submitted_by=manager_user.id,
            scope=ExcessOfferScope.PER_LINE,
            status=ExcessOfferStatus.OPEN,
        )
    )
    own_el = _posted_list(db_session, manager_user, test_company, title="Manager owned posting")
    db_session.commit()

    _as_user(client, manager_user)
    try:
        resp = client.get("/v2/partials/resell/list-rows?lens=all")
    finally:
        _clear_override(client)

    body = resp.text
    # Foreign card: anonymized title + row-signal summary, no seller identity.
    assert f"Excess listing #{foreign_el.id}" in body
    assert "Xilinx" in body  # R4 non-identifying content signal
    assert foreign_company.name not in body
    assert "Foreign — do not leak" not in body
    # Own card: real title + company, in the SAME render.
    assert own_el.title in body
    assert test_company.name in body
    # D2: coverage/offer data renders ONLY for the can_see_customer card (own) — the
    # foreign card carries a live offer but must not expose the meter/badge for it.
    assert body.count("Offer coverage:") == 1


# ── (c) non-manager requesting lens=all falls back to open (no leak, no 500) ──


def test_non_manager_all_lens_falls_back_to_open(
    client, db_session, sales_user, foreign_owner, foreign_company, test_company
):
    own_el = _posted_list(db_session, sales_user, test_company, title="Sales own posting")
    other_el = _posted_list(db_session, foreign_owner, foreign_company, title="Other trader posting")

    _as_user(client, sales_user)
    try:
        resp = client.get("/v2/partials/resell/list-rows?lens=all")
    finally:
        _clear_override(client)

    assert resp.status_code == 200
    body = resp.text
    # Fell back to "open": the requester's OWN list is excluded entirely (proves this
    # is not a real "all" render — the open lens never shows the caller's own rows).
    assert own_el.title not in body
    assert f"Excess listing #{own_el.id}" not in body
    # The other trader's posting is visible but anonymized, same as any open-lens hit.
    assert f"Excess listing #{other_el.id}" in body
    assert foreign_company.name not in body


# ── (d) manager still 403s on a foreign list's mutation route ──────────────


def test_manager_still_403_on_foreign_list_mutation(client, db_session, manager_user, foreign_owner, foreign_company):
    foreign_el = _posted_list(db_session, foreign_owner, foreign_company, title="Foreign — do not leak")

    _as_user(client, manager_user)
    try:
        resp = client.get(f"/v2/partials/resell/{foreign_el.id}/build-bid")
    finally:
        _clear_override(client)

    assert resp.status_code == 403


# ── (e) collecting-vs-open merges to a neutral badge on a foreign card ─────


def test_manager_all_lens_merges_collecting_badge_on_foreign_card(
    client, db_session, manager_user, foreign_owner, foreign_company
):
    _posted_list(
        db_session, foreign_owner, foreign_company, title="Foreign — collecting", status=ExcessListStatus.COLLECTING
    )

    _as_user(client, manager_user)
    try:
        resp = client.get("/v2/partials/resell/list-rows?lens=all")
    finally:
        _clear_override(client)

    body = resp.text
    # Finding #13 (D2): the collecting badge is an offer-existence signal, so a foreign
    # card renders the SAME neutral "Open" badge open would show — never "Collecting".
    assert "Collecting" not in body
    assert "Open" in body
