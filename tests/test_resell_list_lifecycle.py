"""test_resell_list_lifecycle.py — List close/expire lifecycle + mirror retire (M5).

Covers the M5 rework of the ExcessList posting-window lifecycle:
  • ``close_list`` is guarded to ``open``/``collecting`` only (409 for a draft or an
    already-resolved list) and RETIRES the Sighting mirror on close (a closed posting
    stops advertising its supply as live);
  • ``expire_overdue_lists`` flips past-``close_at`` unresolved (open/collecting) lists to
    ``expired`` + retires their mirror, skips current / already-resolved ones, and is
    idempotent;
  • the nightly ``_job_expire_resell_lists`` job delegates to that service;
  • ``register_resell_jobs`` registers the expiry job;
  • the list-view stage filter now offers the ``closed`` / ``expired`` stages.

Called by: pytest
Depends on: app.services.excess_service, app.services.excess_mirror, app.jobs.resell_jobs,
    app.models.excess, app.models.sourcing, tests.conftest
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

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


# ── close retires the Sighting mirror ────────────────────────────────


def test_close_retires_mirror(db_session, owner, company):
    """Closing a posted list retires its live-supply mirror (M5)."""
    el = _make_list(db_session, owner, company)
    publish_list(db_session, el.id, owner)
    assert len(_sightings(db_session, company.id)) == 1  # posted → mirrored

    excess_service.close_list(db_session, el.id, owner)

    assert _sightings(db_session, company.id) == []  # closed → retired


# ── expire_overdue_lists (the nightly service) ───────────────────────


def _overdue_open_list(db: Session, owner: User, company: Company) -> ExcessList:
    """A published (open) list whose close_at deadline is already in the past."""
    el = _make_list(db, owner, company)
    publish_list(db, el.id, owner)  # → open, mirrored
    el.close_at = datetime.now(UTC) - timedelta(hours=1)
    db.commit()
    return el


def test_expire_overdue_flips_and_retires(db_session, owner, company):
    """A past-close_at open list expires and its mirror is retired."""
    el = _overdue_open_list(db_session, owner, company)
    assert len(_sightings(db_session, company.id)) == 1

    n = excess_service.expire_overdue_lists(db_session)

    assert n == 1
    db_session.refresh(el)
    assert el.status == ExcessListStatus.EXPIRED
    assert _sightings(db_session, company.id) == []


def test_expire_skips_future_and_null_close_at(db_session, owner, company):
    """A future deadline and a null close_at are both left alone."""
    future = _make_list(db_session, owner, company, parts=("MAX232",))
    publish_list(db_session, future.id, owner)
    future.close_at = datetime.now(UTC) + timedelta(days=3)
    no_deadline = _make_list(db_session, owner, company, parts=("NE555P",))
    publish_list(db_session, no_deadline.id, owner)  # close_at stays None
    db_session.commit()

    assert excess_service.expire_overdue_lists(db_session) == 0
    db_session.refresh(future)
    db_session.refresh(no_deadline)
    assert future.status == ExcessListStatus.OPEN
    assert no_deadline.status == ExcessListStatus.OPEN


def test_expire_skips_resolved_lists(db_session, owner, company):
    """A bid_out list past close_at is NOT expired (only open/collecting are
    eligible)."""
    el = _make_list(db_session, owner, company)
    el.status = ExcessListStatus.BID_OUT
    el.close_at = datetime.now(UTC) - timedelta(days=1)
    db_session.commit()

    assert excess_service.expire_overdue_lists(db_session) == 0
    db_session.refresh(el)
    assert el.status == ExcessListStatus.BID_OUT


def test_expire_is_idempotent(db_session, owner, company):
    """A second run finds nothing left to expire."""
    _overdue_open_list(db_session, owner, company)
    assert excess_service.expire_overdue_lists(db_session) == 1
    assert excess_service.expire_overdue_lists(db_session) == 0


# ── Nightly job + registration ───────────────────────────────────────


async def test_nightly_job_expires_overdue(db_session, owner, company):
    """The job runs expire_overdue_lists against a fresh session (SessionLocal
    patched)."""
    el = _overdue_open_list(db_session, owner, company)
    from app.jobs.resell_jobs import _job_expire_resell_lists

    list_id = el.id
    with patch("app.database.SessionLocal", return_value=db_session):
        await _job_expire_resell_lists()

    # The job closes its (patched) session in `finally`, detaching `el` — re-read by id;
    # the commit is visible on the shared test connection.
    refreshed = db_session.get(ExcessList, list_id)
    assert refreshed.status == ExcessListStatus.EXPIRED


def test_register_resell_jobs_adds_expiry_job():
    """register_resell_jobs registers the expire_resell_lists cron job."""
    from app.jobs.resell_jobs import register_resell_jobs

    scheduler = MagicMock()
    register_resell_jobs(scheduler, settings=None)
    ids = {c.kwargs.get("id") for c in scheduler.add_job.call_args_list}
    assert "expire_resell_lists" in ids


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
