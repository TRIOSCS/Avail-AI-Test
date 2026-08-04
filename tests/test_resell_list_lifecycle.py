"""test_resell_list_lifecycle.py — List close lifecycle + mirror retire (M5).

Covers the M5 rework of the ExcessList posting-window lifecycle:
  • ``close_list`` is guarded to ``open``/``collecting`` only (409 for a draft or an
    already-resolved list) and RETIRES the Sighting mirror on close (a closed posting
    stops advertising its supply as live);
  • ``close_list_without_bid`` → the terminal ``closed`` state (D5);
  • the list-view stage filter offers the ``closed`` / ``expired`` stages.

(The nightly expiry job + ``expire_overdue_lists`` service and their tests were
removed in the W1 simplification per docs/W1_JOB_DISPOSITION.md; git restores.)

Called by: pytest
Depends on: app.services.excess_service, app.services.excess_mirror,
    app.models.excess, app.models.sourcing, tests.conftest
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.constants import ExcessListStatus
from app.models import Company, User
from app.models.excess import ExcessList
from app.models.sourcing import Sighting
from app.services import excess_service
from app.services.excess_mirror import publish_list
from app.services.excess_service import create_excess_list, import_line_items

# ── Fixtures / helpers ───────────────────────────────────────────────


@pytest.fixture()
def company(db_session: Session) -> Company:
    co = Company(name="Wonka Surplus")
    db_session.add(co)
    db_session.commit()
    db_session.refresh(co)
    return co


@pytest.fixture()
def owner(db_session: Session) -> User:
    user = User(email="ll-owner@trioscs.com", name="Lex Owner", role="trader", azure_id="ll-owner-1")
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture()
def other_user(db_session: Session) -> User:
    user = User(email="ll-other@trioscs.com", name="Nia NonOwner", role="trader", azure_id="ll-other-1")
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


def _make_list(db: Session, owner: User, company: Company, parts=("LM358N",)) -> ExcessList:
    """A list with card-resolved lines (so the mirror actually creates Sightings)."""
    el = create_excess_list(db, title="Excess", company_id=company.id, owner_id=owner.id)
    import_line_items(db, el.id, [{"part_number": p, "quantity": "100"} for p in parts])
    return el


def _sightings(db: Session, company_id: int) -> list[Sighting]:
    return (
        db.query(Sighting)
        .filter(Sighting.source_type == "customer_excess", Sighting.source_company_id == company_id)
        .all()
    )


# ── close_list guard ─────────────────────────────────────────────────


def test_close_list_rejects_draft(db_session, owner, company):
    """A draft (never published) list cannot be closed — 409, no mutation."""
    el = _make_list(db_session, owner, company)
    assert el.status == ExcessListStatus.DRAFT
    with pytest.raises(HTTPException) as exc:
        excess_service.close_list(db_session, el.id, owner)
    assert exc.value.status_code == 409
    db_session.refresh(el)
    assert el.status == ExcessListStatus.DRAFT
    assert el.close_at is None


def test_close_list_rejects_already_resolved(db_session, owner, company):
    """An already bid_out list cannot be re-closed — 409."""
    el = _make_list(db_session, owner, company)
    el.status = ExcessListStatus.BID_OUT
    db_session.commit()
    with pytest.raises(HTTPException) as exc:
        excess_service.close_list(db_session, el.id, owner)
    assert exc.value.status_code == 409


def test_close_list_allows_collecting(db_session, owner, company):
    """A collecting list closes → bid_out + close_at stamped."""
    el = _make_list(db_session, owner, company)
    publish_list(db_session, el.id, owner)  # → open
    el.status = ExcessListStatus.COLLECTING
    db_session.commit()

    closed = excess_service.close_list(db_session, el.id, owner)
    assert closed.status == ExcessListStatus.BID_OUT
    assert closed.close_at is not None


def test_end_posting_window_locks_list_before_guard(db_session, owner, company, monkeypatch):
    """Finding #11: ``_end_posting_window`` (close / close-without-bid) takes the SAME
    M9 list+lines lock award/unaward/withdraw/assign use, BEFORE evaluating the
    closeable guard — so a close racing a concurrent award serializes instead of
    clobbering the just-awarded status.

    Spy the lock hook to prove it's wired (``with_for_update`` itself is a no-op on the
    SQLite test engine, so the race is unobservable here — this guards the hook against
    regression, same technique as ``test_withdraw_offer_locks_list_for_serialization``).
    """
    el = _make_list(db_session, owner, company)
    publish_list(db_session, el.id, owner)
    el.status = ExcessListStatus.COLLECTING
    db_session.commit()

    calls: list[int] = []
    real_lock = excess_service._lock_list_and_lines

    def _spy(db, excess_list_id):
        calls.append(excess_list_id)
        return real_lock(db, excess_list_id)

    monkeypatch.setattr(excess_service, "_lock_list_and_lines", _spy)

    excess_service.close_list(db_session, el.id, owner)

    assert calls == [el.id]


def test_end_posting_window_stale_read_cannot_clobber_concurrent_award(db_session, owner, company):
    """Finding R4: the spy-only test above only pins WIRING — it would pass even if the
    closeable guard read PRE-lock state. This repros the actual race: a raw core UPDATE
    (bypassing the ORM, like a second transaction's committed award) flips the list to
    ``awarded`` behind the already-identity-mapped object's back, with no intervening
    commit (so ``expire_on_commit`` never auto-refreshes it). Without the lock's
    ``populate_existing`` refresh, ``close_list`` would read the STALE 'collecting' status
    and clobber the just-awarded list to ``bid_out``; with the fix the closeable guard
    sees 'awarded' post-lock and 409s instead.
    """
    from sqlalchemy import text as sa_text

    el = _make_list(db_session, owner, company)
    publish_list(db_session, el.id, owner)
    el.status = ExcessListStatus.COLLECTING
    db_session.commit()
    assert el.status == ExcessListStatus.COLLECTING

    db_session.execute(sa_text("UPDATE excess_lists SET status = 'awarded' WHERE id = :id").bindparams(id=el.id))
    assert el.status == ExcessListStatus.COLLECTING  # still stale, pre-call

    with pytest.raises(HTTPException) as exc:
        excess_service.close_list(db_session, el.id, owner)
    assert exc.value.status_code == 409
    assert el.status == ExcessListStatus.AWARDED  # the lock refreshed it in place — never clobbered


# ── close retires the Sighting mirror ────────────────────────────────


def test_close_retires_mirror(db_session, owner, company):
    """Closing a posted list retires its live-supply mirror (M5)."""
    el = _make_list(db_session, owner, company)
    publish_list(db_session, el.id, owner)
    assert len(_sightings(db_session, company.id)) == 1  # posted → mirrored

    excess_service.close_list(db_session, el.id, owner)

    assert _sightings(db_session, company.id) == []  # closed → retired


# ── close_list_without_bid → CLOSED terminal state (Task 3, D5) ──────


def test_close_without_bid_on_open_flips_to_closed(db_session, owner, company):
    """Closing an OPEN list without bidding flips it to CLOSED + stamps close_at."""
    el = _make_list(db_session, owner, company)
    publish_list(db_session, el.id, owner)  # → open
    assert el.status == ExcessListStatus.OPEN

    closed = excess_service.close_list_without_bid(db_session, el.id, owner)
    assert closed.status == ExcessListStatus.CLOSED
    assert closed.close_at is not None


def test_close_without_bid_on_collecting_flips_to_closed(db_session, owner, company):
    """A collecting list closes without bidding → CLOSED (distinct from the bid_out
    path)."""
    el = _make_list(db_session, owner, company)
    publish_list(db_session, el.id, owner)
    el.status = ExcessListStatus.COLLECTING
    db_session.commit()

    closed = excess_service.close_list_without_bid(db_session, el.id, owner)
    assert closed.status == ExcessListStatus.CLOSED


def test_close_without_bid_retires_mirror(db_session, owner, company):
    """Closing without bidding retires the live-supply Sighting mirror (terminal)."""
    el = _make_list(db_session, owner, company)
    publish_list(db_session, el.id, owner)
    assert len(_sightings(db_session, company.id)) == 1

    excess_service.close_list_without_bid(db_session, el.id, owner)

    assert _sightings(db_session, company.id) == []


def test_close_without_bid_rejects_draft(db_session, owner, company):
    """A draft can't be closed-without-bid — 409, no mutation (mirrors close_list)."""
    el = _make_list(db_session, owner, company)
    assert el.status == ExcessListStatus.DRAFT
    with pytest.raises(HTTPException) as exc:
        excess_service.close_list_without_bid(db_session, el.id, owner)
    assert exc.value.status_code == 409
    db_session.refresh(el)
    assert el.status == ExcessListStatus.DRAFT


@pytest.mark.parametrize(
    "terminal_status",
    [ExcessListStatus.BID_OUT, ExcessListStatus.AWARDED, ExcessListStatus.CLOSED, ExcessListStatus.EXPIRED],
)
def test_close_without_bid_rejects_terminal(db_session, owner, company, terminal_status):
    """An already-resolved (incl.

    already-CLOSED) list can't be re-closed — 409.
    """
    el = _make_list(db_session, owner, company)
    el.status = terminal_status
    db_session.commit()
    with pytest.raises(HTTPException) as exc:
        excess_service.close_list_without_bid(db_session, el.id, owner)
    assert exc.value.status_code == 409


def test_close_without_bid_non_owner_403(db_session, owner, other_user, company):
    """Only the owner can close a list without bidding — 403 otherwise."""
    el = _make_list(db_session, owner, company)
    publish_list(db_session, el.id, owner)
    with pytest.raises(HTTPException) as exc:
        excess_service.close_list_without_bid(db_session, el.id, other_user)
    assert exc.value.status_code == 403


def test_close_without_bid_route_200_and_forbidden(client, db_session, owner, company):
    """Owner POST closes without bidding → 200 + CLOSED; a non-owner is 403."""
    from app.dependencies import require_user
    from app.main import app

    el = _make_list(db_session, owner, company)
    publish_list(db_session, el.id, owner)
    el.status = ExcessListStatus.COLLECTING
    db_session.commit()
    el_id = el.id

    # The default client user is not the owner → 403.
    assert client.post(f"/api/resell/{el_id}/close-without-bid").status_code == 403

    app.dependency_overrides[require_user] = lambda: owner
    try:
        resp = client.post(f"/api/resell/{el_id}/close-without-bid")
    finally:
        app.dependency_overrides.pop(require_user, None)
    assert resp.status_code == 200
    assert db_session.get(ExcessList, el_id).status == ExcessListStatus.CLOSED


def test_workspace_bid_out_subtitle_is_accurate(client, db_session, owner):
    """The bid_out glance card no longer overstates 'Sent to the customer' (closing ends
    the collection window — the bid-back send is a separate, later action)."""
    from app.dependencies import require_user
    from app.main import app

    app.dependency_overrides[require_user] = lambda: owner
    try:
        body = client.get("/v2/partials/resell/workspace?lens=mine").text
    finally:
        app.dependency_overrides.pop(require_user, None)
    assert "Sent to the customer" not in body


# ── List views/filters consume the terminal states ──────────────────


def test_stage_filter_offers_closed_and_expired(client, db_session, owner):
    """The list-view stage filter now offers the Closed / Expired stages (M5)."""
    from app.dependencies import require_user
    from app.main import app

    app.dependency_overrides[require_user] = lambda: owner
    try:
        resp = client.get("/v2/partials/resell/lists?lens=mine")
        assert resp.status_code == 200
        assert "Expired" in resp.text
        assert "Closed" in resp.text
    finally:
        app.dependency_overrides.pop(require_user, None)
