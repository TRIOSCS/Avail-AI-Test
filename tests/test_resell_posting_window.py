"""test_resell_posting_window.py — owner posting-window deadline + resolved-list chip.

Covers Phase-5 Task 1 (finding #8, decision D1): the posting-deadline subsystem finally
gets a real owner entry point, and resolved lists stop rendering a red "Overdue" chip.

Hard correctness points:
  - ``create_excess_list(close_at=...)`` persists a FUTURE, tz-aware deadline on the draft
    and rejects a naive or past one with a 400 (an owner cannot schedule a window in the
    past, and a naive wall-clock is ambiguous).
  - ``publish_list`` PRESERVES a future ``close_at`` (was: unconditionally nulled it) and
    only clears a stale/past one — so a real, create-set deadline survives publishing and
    the nightly ``expire_overdue_lists`` finally has live rows to act on.
  - The resell chip context exposes ``is_live`` (open/collecting only) + ``close_at_display``
    so the countdown chip renders ONLY while live; a resolved (bid_out/closed/awarded/
    expired) list yields ``is_live=False`` and never a red "Overdue".

Called by: pytest
Depends on: app.services.excess_service, app.services.excess_mirror, app.routers.resell,
    app.models.excess, tests.conftest
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.constants import ExcessListStatus
from app.models import Company, User
from app.models.excess import ExcessList
from app.routers.resell import _close_at_display, _detail_context, _is_live, _list_cards
from app.services.excess_mirror import publish_list
from app.services.excess_service import create_excess_list, import_line_items
from tests.conftest import engine

_ = engine  # Ensure test DB tables are created


# ── Helpers ──────────────────────────────────────────────────────────


def _make_company(db: Session, name: str = "Window Seller") -> Company:
    co = Company(name=name)
    db.add(co)
    db.commit()
    db.refresh(co)
    return co


def _make_user(db: Session, *, email: str = "win-owner@test.com") -> User:
    user = User(email=email, name=email.split("@")[0], role="trader", azure_id=f"az-{email}")
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


# ── create_excess_list(close_at=...) validation + persistence ────────


def test_create_persists_future_close_at(db_session: Session):
    company = _make_company(db_session)
    owner = _make_user(db_session)
    close = datetime.now(UTC) + timedelta(days=3)

    el = create_excess_list(db_session, title="X", company_id=company.id, owner_id=owner.id, close_at=close)

    assert el.close_at is not None
    assert abs((_aware(el.close_at) - close).total_seconds()) < 2


def test_create_without_close_at_leaves_it_none(db_session: Session):
    company = _make_company(db_session)
    owner = _make_user(db_session)

    el = create_excess_list(db_session, title="X", company_id=company.id, owner_id=owner.id)

    assert el.close_at is None


def test_create_rejects_past_close_at(db_session: Session):
    company = _make_company(db_session)
    owner = _make_user(db_session)

    with pytest.raises(HTTPException) as exc:
        create_excess_list(
            db_session,
            title="X",
            company_id=company.id,
            owner_id=owner.id,
            close_at=datetime.now(UTC) - timedelta(hours=1),
        )
    assert exc.value.status_code == 400


def test_create_rejects_naive_close_at(db_session: Session):
    company = _make_company(db_session)
    owner = _make_user(db_session)

    with pytest.raises(HTTPException) as exc:
        create_excess_list(
            db_session,
            title="X",
            company_id=company.id,
            owner_id=owner.id,
            close_at=datetime.now() + timedelta(days=1),  # deliberately naive (no tzinfo)
        )
    assert exc.value.status_code == 400


# ── publish_list preserves a future deadline, nulls a stale one ──────


def test_publish_preserves_future_close_at(db_session: Session):
    """A create-set FUTURE deadline survives publishing (the T1 fix — was nulled)."""
    company = _make_company(db_session)
    owner = _make_user(db_session)
    close = datetime.now(UTC) + timedelta(days=5)
    el = create_excess_list(db_session, title="X", company_id=company.id, owner_id=owner.id, close_at=close)
    import_line_items(db_session, el.id, [{"part_number": "LM358N", "quantity": "100"}])

    publish_list(db_session, el.id, owner)

    db_session.refresh(el)
    assert el.status == ExcessListStatus.OPEN
    assert el.close_at is not None
    assert abs((_aware(el.close_at) - close).total_seconds()) < 2
    assert el.open_at is not None


def test_publish_nulls_stale_close_at(db_session: Session):
    """A leftover PAST/stale deadline is cleared on publish — an open posting must not
    advertise a lapsed close time."""
    company = _make_company(db_session)
    owner = _make_user(db_session)
    el = create_excess_list(db_session, title="X", company_id=company.id, owner_id=owner.id)
    import_line_items(db_session, el.id, [{"part_number": "LM358N", "quantity": "100"}])
    # A draft that somehow carries a stale close_at (past) — publish must null it.
    el.close_at = datetime.now(UTC) - timedelta(hours=2)
    db_session.commit()

    publish_list(db_session, el.id, owner)

    db_session.refresh(el)
    assert el.status == ExcessListStatus.OPEN
    assert el.close_at is None
    assert el.open_at is not None


# ── update_excess_list close_at (sentinel: untouched / clear / validate) ──


def test_update_without_close_at_leaves_deadline_untouched(db_session: Session):
    """A header edit that carries no deadline input must NOT wipe a stored close_at (the
    draft-edit form has no deadline field)."""
    from app.services.excess_service import update_excess_list

    company = _make_company(db_session)
    owner = _make_user(db_session)
    close = datetime.now(UTC) + timedelta(days=3)
    el = create_excess_list(db_session, title="X", company_id=company.id, owner_id=owner.id, close_at=close)

    update_excess_list(db_session, el.id, owner, title="Renamed", notes="n", company_id=company.id)

    db_session.refresh(el)
    assert el.title == "Renamed"
    assert el.close_at is not None  # untouched


def test_update_with_none_close_at_clears_deadline(db_session: Session):
    from app.services.excess_service import update_excess_list

    company = _make_company(db_session)
    owner = _make_user(db_session)
    close = datetime.now(UTC) + timedelta(days=3)
    el = create_excess_list(db_session, title="X", company_id=company.id, owner_id=owner.id, close_at=close)

    update_excess_list(db_session, el.id, owner, title="X", company_id=company.id, close_at=None)

    db_session.refresh(el)
    assert el.close_at is None


def test_update_rejects_past_close_at(db_session: Session):
    from app.services.excess_service import update_excess_list

    company = _make_company(db_session)
    owner = _make_user(db_session)
    el = create_excess_list(db_session, title="X", company_id=company.id, owner_id=owner.id)

    with pytest.raises(HTTPException) as exc:
        update_excess_list(
            db_session,
            el.id,
            owner,
            title="X",
            company_id=company.id,
            close_at=datetime.now(UTC) - timedelta(hours=1),
        )
    assert exc.value.status_code == 400


# ── chip context: is_live + close_at_display ─────────────────────────


@pytest.mark.parametrize(
    ("status", "expected_live"),
    [
        (ExcessListStatus.DRAFT, False),
        (ExcessListStatus.OPEN, True),
        (ExcessListStatus.COLLECTING, True),
        (ExcessListStatus.BID_OUT, False),
        (ExcessListStatus.AWARDED, False),
        (ExcessListStatus.CLOSED, False),
        (ExcessListStatus.EXPIRED, False),
    ],
)
def test_is_live_only_open_collecting(db_session: Session, status, expected_live):
    company = _make_company(db_session)
    owner = _make_user(db_session)
    el = ExcessList(company_id=company.id, owner_id=owner.id, title="X", status=status)
    db_session.add(el)
    db_session.commit()
    assert _is_live(el) is expected_live


def test_close_at_display_formats_and_handles_none():
    assert _close_at_display(None) is None
    label = _close_at_display(datetime(2026, 7, 20, 15, 30, tzinfo=UTC))
    assert label == "Jul 20"


def test_resolved_list_card_is_not_live_no_overdue(db_session: Session):
    """A resolved (bid_out) list with a PAST close_at → is_live False, a muted
    close_at_display, and NO red 'Overdue' countdown in the card context."""
    company = _make_company(db_session)
    owner = _make_user(db_session)
    el = ExcessList(
        company_id=company.id,
        owner_id=owner.id,
        title="X",
        status=ExcessListStatus.BID_OUT,
        close_at=datetime.now(UTC) - timedelta(days=1),
    )
    db_session.add(el)
    db_session.commit()

    cards = _list_cards(db_session, [el], can_see_customer=True)
    card = cards[0]
    assert card["is_live"] is False
    assert card["close_at_display"] is not None


def test_live_list_card_is_live(db_session: Session):
    company = _make_company(db_session)
    owner = _make_user(db_session)
    el = ExcessList(
        company_id=company.id,
        owner_id=owner.id,
        title="X",
        status=ExcessListStatus.OPEN,
        close_at=datetime.now(UTC) + timedelta(days=2),
    )
    db_session.add(el)
    db_session.commit()

    card = _list_cards(db_session, [el], can_see_customer=True)[0]
    assert card["is_live"] is True


def test_detail_context_exposes_is_live_and_display(db_session: Session):
    from fastapi import Request

    company = _make_company(db_session)
    owner = _make_user(db_session)
    el = ExcessList(
        company_id=company.id,
        owner_id=owner.id,
        title="X",
        status=ExcessListStatus.CLOSED,
        close_at=datetime.now(UTC) - timedelta(days=3),
    )
    db_session.add(el)
    db_session.commit()

    req = Request({"type": "http", "headers": [], "method": "GET", "query_string": b"", "path": "/"})
    ctx = _detail_context(req, db_session, el, owner)
    assert ctx["is_live"] is False
    assert ctx["close_at_display"] is not None


# ── rendered HTML: no red 'Overdue' on a resolved list (headless) ────


def test_resolved_list_renders_no_overdue(client, db_session: Session):
    """The left-list partial must NOT render 'Overdue' for a resolved (bid_out) list
    past its close_at — the finding-#8 regression."""
    from app.dependencies import require_user
    from app.main import app

    company = _make_company(db_session)
    owner = _make_user(db_session, email="win-render@test.com")
    el = ExcessList(
        company_id=company.id,
        owner_id=owner.id,
        title="Resolved Deal",
        status=ExcessListStatus.BID_OUT,
        close_at=datetime.now(UTC) - timedelta(days=2),
    )
    db_session.add(el)
    db_session.commit()

    app.dependency_overrides[require_user] = lambda: owner
    try:
        resp = client.get("/v2/partials/resell/lists?lens=mine")
    finally:
        app.dependency_overrides.pop(require_user, None)
    assert resp.status_code == 200
    assert "Overdue" not in resp.text
    assert "closed" in resp.text.lower()
