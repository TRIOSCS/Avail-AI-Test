"""tests/test_htmx_views_nightly29.py — Direct-async coverage for bulk_archive / bulk_unarchive.

Target: Push app/routers/htmx_views.py from 84.9% → 85%+ by covering the
lines that TestClient cannot reach due to async-continuation tracking gaps:

  bulk_archive   lines 9866–9889  (requirement_ids + requisition_ids branches)
  bulk_unarchive lines 9902–9929  (requirement_ids + requisition_ids branches)

TestClient tests for these routes exist in test_htmx_views_nightly12.py, but
Python coverage.py does not reliably attribute async-continuation lines (the
statements immediately after ``await request.json()``) when tests run via WSGI.
Calling the coroutine directly (as in test_htmx_views_nightly27.py) fixes this.

Called by: pytest autodiscovery (asyncio_mode = auto)
Depends on: conftest.py fixtures (db_session, test_user), app.routers.htmx_views
"""

import os

os.environ["TESTING"] = "1"

from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session
from starlette.requests import Request

from app.constants import RequisitionStatus, SourcingStatus
from app.models import Requirement, Requisition, User

# ── Helpers ───────────────────────────────────────────────────────────────────


def _json_request(payload: dict) -> MagicMock:
    """Return a minimal Request mock whose .json() coroutine returns *payload*."""
    req = MagicMock(spec=Request)
    req.url.path = "/v2/partials/parts/bulk-archive"
    req.headers = {}
    req.query_params = MagicMock()
    req.query_params.get = lambda k, d=None: d
    req.json = AsyncMock(return_value=payload)
    return req


def _make_requisition(db: Session, user: User) -> Requisition:
    req = Requisition(name="N29 Req", status="active", created_by=user.id)
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


# ── bulk_archive ──────────────────────────────────────────────────────────────


class TestBulkArchiveDirect:
    """Direct coroutine calls for bulk_archive covering lines 9866–9889."""

    async def test_empty_payload_covers_body_lines(self, db_session: Session, test_user: User):
        """Lines 9866–9868: body parsed; empty lists skip both if-branches."""
        from app.routers.htmx_views import bulk_archive

        mock_req = _json_request({"requirement_ids": [], "requisition_ids": []})
        with patch("app.routers.htmx_views.parts_list_partial", new_callable=AsyncMock) as mock_list:
            mock_list.return_value = HTMLResponse("<div>ok</div>")
            result = await bulk_archive(request=mock_req, user=test_user, db=db_session)

        assert result.status_code == 200
        mock_list.assert_awaited_once()

    async def test_requirement_ids_branch_covered(self, db_session: Session, test_user: User):
        """Lines 9871–9874: requirement_ids is non-empty → bulk UPDATE executed."""
        from app.routers.htmx_views import bulk_archive

        req = _make_requisition(db_session, test_user)
        part = _make_requirement(db_session, req)

        mock_req = _json_request({"requirement_ids": [part.id], "requisition_ids": []})
        with patch("app.routers.htmx_views.parts_list_partial", new_callable=AsyncMock) as mock_list:
            mock_list.return_value = HTMLResponse("<div>ok</div>")
            result = await bulk_archive(request=mock_req, user=test_user, db=db_session)

        assert result.status_code == 200
        db_session.refresh(part)
        assert part.sourcing_status == SourcingStatus.ARCHIVED

    async def test_requisition_ids_branch_covered(self, db_session: Session, test_user: User):
        """Lines 9877–9884: requisition_ids non-empty → status ARCHIVED + cascade."""
        from app.routers.htmx_views import bulk_archive

        req = _make_requisition(db_session, test_user)
        part = _make_requirement(db_session, req)

        mock_req = _json_request({"requirement_ids": [], "requisition_ids": [req.id]})
        with patch("app.routers.htmx_views.parts_list_partial", new_callable=AsyncMock) as mock_list:
            mock_list.return_value = HTMLResponse("<div>ok</div>")
            result = await bulk_archive(request=mock_req, user=test_user, db=db_session)

        assert result.status_code == 200
        db_session.refresh(req)
        assert req.status == RequisitionStatus.ARCHIVED
        db_session.refresh(part)
        assert part.sourcing_status == SourcingStatus.ARCHIVED

    async def test_both_branches_covered(self, db_session: Session, test_user: User):
        """Lines 9866–9889: both requirement_ids and requisition_ids populated."""
        from app.routers.htmx_views import bulk_archive

        req = _make_requisition(db_session, test_user)
        part = _make_requirement(db_session, req)

        mock_req = _json_request({"requirement_ids": [part.id], "requisition_ids": [req.id]})
        with patch("app.routers.htmx_views.parts_list_partial", new_callable=AsyncMock) as mock_list:
            mock_list.return_value = HTMLResponse("<div>ok</div>")
            result = await bulk_archive(request=mock_req, user=test_user, db=db_session)

        assert result.status_code == 200


# ── bulk_unarchive ────────────────────────────────────────────────────────────


class TestBulkUnarchiveDirect:
    """Direct coroutine calls for bulk_unarchive covering lines 9902–9929."""

    async def test_empty_payload_covers_body_lines(self, db_session: Session, test_user: User):
        """Lines 9902–9904: body parsed; empty lists skip both if-branches."""
        from app.routers.htmx_views import bulk_unarchive

        mock_req = _json_request({"requirement_ids": [], "requisition_ids": []})
        with patch("app.routers.htmx_views.parts_list_partial", new_callable=AsyncMock) as mock_list:
            mock_list.return_value = HTMLResponse("<div>ok</div>")
            result = await bulk_unarchive(request=mock_req, user=test_user, db=db_session)

        assert result.status_code == 200
        mock_list.assert_awaited_once()

    async def test_requirement_ids_branch_covered(self, db_session: Session, test_user: User):
        """Lines 9907–9911: requirement_ids non-empty → archived parts restored to open."""
        from app.routers.htmx_views import bulk_unarchive

        req = _make_requisition(db_session, test_user)
        part = _make_requirement(db_session, req)
        # Pre-archive the part so the unarchive UPDATE actually matches
        part.sourcing_status = SourcingStatus.ARCHIVED
        db_session.commit()

        mock_req = _json_request({"requirement_ids": [part.id], "requisition_ids": []})
        with patch("app.routers.htmx_views.parts_list_partial", new_callable=AsyncMock) as mock_list:
            mock_list.return_value = HTMLResponse("<div>ok</div>")
            result = await bulk_unarchive(request=mock_req, user=test_user, db=db_session)

        assert result.status_code == 200
        db_session.refresh(part)
        assert part.sourcing_status == SourcingStatus.OPEN

    async def test_requisition_ids_branch_covered(self, db_session: Session, test_user: User):
        """Lines 9914–9922: requisition_ids non-empty → status ACTIVE + cascade."""
        from app.routers.htmx_views import bulk_unarchive

        req = _make_requisition(db_session, test_user)
        part = _make_requirement(db_session, req)
        req.status = RequisitionStatus.ARCHIVED
        part.sourcing_status = SourcingStatus.ARCHIVED
        db_session.commit()

        mock_req = _json_request({"requirement_ids": [], "requisition_ids": [req.id]})
        with patch("app.routers.htmx_views.parts_list_partial", new_callable=AsyncMock) as mock_list:
            mock_list.return_value = HTMLResponse("<div>ok</div>")
            result = await bulk_unarchive(request=mock_req, user=test_user, db=db_session)

        assert result.status_code == 200
        db_session.refresh(req)
        assert req.status == RequisitionStatus.ACTIVE
        db_session.refresh(part)
        assert part.sourcing_status == SourcingStatus.OPEN

    async def test_both_branches_covered(self, db_session: Session, test_user: User):
        """Lines 9902–9929: both requirement_ids and requisition_ids populated."""
        from app.routers.htmx_views import bulk_unarchive

        req = _make_requisition(db_session, test_user)
        part = _make_requirement(db_session, req)
        req.status = RequisitionStatus.ARCHIVED
        part.sourcing_status = SourcingStatus.ARCHIVED
        db_session.commit()

        mock_req = _json_request({"requirement_ids": [part.id], "requisition_ids": [req.id]})
        with patch("app.routers.htmx_views.parts_list_partial", new_callable=AsyncMock) as mock_list:
            mock_list.return_value = HTMLResponse("<div>ok</div>")
            result = await bulk_unarchive(request=mock_req, user=test_user, db=db_session)

        assert result.status_code == 200
