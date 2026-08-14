"""tests/test_htmx_views_nightly29.py — Direct-async coverage for bulk_outcome /
bulk_reopen.

Target: cover the async-continuation lines (the statements immediately after
``await request.json()``) that TestClient runs cannot reliably attribute in
coverage.py. Migration 210 replaced bulk_archive/bulk_unarchive (and their
requisition_ids cascade) with bulk_outcome (won/lost/hotlist via the sourcing
state machine) and bulk_reopen (requirement_ids only), so the direct calls now
target those handlers. TestClient tests for the same routes live in
tests/test_archive_system.py and tests/test_parts_bulk_outcome.py.

Called by: pytest autodiscovery (asyncio_mode = auto)
Depends on: conftest.py fixtures (db_session, test_user), app.routers.htmx.parts
"""

import os

os.environ["TESTING"] = "1"

from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session
from starlette.requests import Request

from app.constants import SourcingStatus
from app.models import Requirement, Requisition, User

# ── Helpers ───────────────────────────────────────────────────────────────────


def _json_request(payload: dict) -> MagicMock:
    """Return a minimal Request mock whose .json() coroutine returns *payload*."""
    req = MagicMock(spec=Request)
    req.url.path = "/v2/partials/parts/bulk-outcome"
    req.headers = {}
    req.query_params = MagicMock()
    req.query_params.get = lambda k, d=None: d
    req.json = AsyncMock(return_value=payload)
    return req


async def _call_bulk(handler, payload, user, db):
    """Invoke a bulk_outcome/bulk_reopen coroutine with parts_list_partial mocked.

    Returns ``(result, mock_list)`` so callers can assert on both the response and
    the patched partial.
    """
    with patch("app.routers.htmx.parts.parts_list_partial", new_callable=AsyncMock) as mock_list:
        mock_list.return_value = HTMLResponse("<div>ok</div>")
        result = await handler(request=_json_request(payload), user=user, db=db)
    return result, mock_list


def _make_requisition(db: Session, user: User) -> Requisition:
    req = Requisition(name="N29 Req", status="open", created_by=user.id)
    db.add(req)
    db.commit()
    db.refresh(req)
    return req


def _make_requirement(db: Session, requisition: Requisition) -> Requirement:
    r = Requirement(
        requisition_id=requisition.id,
        primary_mpn="LM317T-N29",
        manufacturer="TI",
    )
    db.add(r)
    db.commit()
    db.refresh(r)
    return r


# ── bulk_outcome ──────────────────────────────────────────────────────────────


class TestBulkOutcomeDirect:
    """Direct coroutine calls for bulk_outcome's post-json body lines."""

    async def test_empty_payload_covers_body_lines(self, db_session: Session, test_user: User):
        """Body parsed; empty requirement_ids skips the transition loop entirely."""
        from app.routers.htmx.parts import bulk_outcome

        result, mock_list = await _call_bulk(
            bulk_outcome, {"requirement_ids": [], "outcome": "hotlist"}, test_user, db_session
        )

        assert result.status_code == 200
        mock_list.assert_awaited_once()

    async def test_hotlist_branch_covered(self, db_session: Session, test_user: User):
        """Non-empty requirement_ids → state-machine transition to hotlist."""
        from app.routers.htmx.parts import bulk_outcome

        req = _make_requisition(db_session, test_user)
        part = _make_requirement(db_session, req)

        result, _ = await _call_bulk(
            bulk_outcome, {"requirement_ids": [part.id], "outcome": "hotlist"}, test_user, db_session
        )

        assert result.status_code == 200
        db_session.refresh(part)
        assert part.sourcing_status == SourcingStatus.HOTLIST

    async def test_terminal_branch_stamps_reason(self, db_session: Session, test_user: User):
        """Terminal outcome (lost) → transition + shared reason stamped."""
        from app.routers.htmx.parts import bulk_outcome

        req = _make_requisition(db_session, test_user)
        part = _make_requirement(db_session, req)

        result, _ = await _call_bulk(
            bulk_outcome,
            {"requirement_ids": [part.id], "outcome": "lost", "reason": "priced out"},
            test_user,
            db_session,
        )

        assert result.status_code == 200
        db_session.refresh(part)
        assert part.sourcing_status == SourcingStatus.LOST
        assert part.outcome_reason == "priced out"


# ── bulk_reopen ───────────────────────────────────────────────────────────────


class TestBulkReopenDirect:
    """Direct coroutine calls for bulk_reopen's post-json body lines."""

    async def test_empty_payload_covers_body_lines(self, db_session: Session, test_user: User):
        """Body parsed; empty requirement_ids skips the transition loop entirely."""
        from app.routers.htmx.parts import bulk_reopen

        result, mock_list = await _call_bulk(bulk_reopen, {"requirement_ids": []}, test_user, db_session)

        assert result.status_code == 200
        mock_list.assert_awaited_once()

    async def test_requirement_ids_branch_covered(self, db_session: Session, test_user: User):
        """Non-empty requirement_ids → hotlist part restored to open."""
        from app.routers.htmx.parts import bulk_reopen

        req = _make_requisition(db_session, test_user)
        part = _make_requirement(db_session, req)
        # Pre-hotlist the part so the reopen transition actually fires
        part.sourcing_status = SourcingStatus.HOTLIST
        db_session.commit()

        result, _ = await _call_bulk(bulk_reopen, {"requirement_ids": [part.id]}, test_user, db_session)

        assert result.status_code == 200
        db_session.refresh(part)
        assert part.sourcing_status == SourcingStatus.OPEN
