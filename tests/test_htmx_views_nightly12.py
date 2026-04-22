"""tests/test_htmx_views_nightly12.py — Coverage for parts tabs, tasks, archive, and buy
plan routes.

Targets: parts list (with filters), parts tabs (offers/sourcing/req-details/activity/comms/notes),
save-notes, create-task, mark-task-done/reopen, archive/unarchive single + bulk,
buy plan list/detail/cancel/reset.

Called by: pytest autodiscovery
Depends on: conftest.py fixtures, app.routers.htmx_views
"""

import os

os.environ["TESTING"] = "1"

from datetime import datetime, timezone
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import (
    BuyPlan,
    Quote,
    Requirement,
    Requisition,
    User,
)
from app.models.task import RequisitionTask

# ── Helpers ───────────────────────────────────────────────────────────────


def _make_requirement(db: Session, req: Requisition, mpn: str = "BC547", **kw) -> Requirement:
    defaults = dict(
        requisition_id=req.id,
        primary_mpn=mpn,
        target_qty=100,
        created_at=datetime.now(timezone.utc),
    )
    defaults.update(kw)
    r = Requirement(**defaults)
    db.add(r)
    db.commit()
    db.refresh(r)
    return r


def _make_task(
    db: Session,
    req: Requisition,
    requirement: Requirement | None,
    user: User,
    **kw,
) -> RequisitionTask:
    defaults = dict(
        requisition_id=req.id,
        requirement_id=requirement.id if requirement else None,
        title="Test Task",
        status="todo",
        created_by=user.id,
        source="manual",
        created_at=datetime.now(timezone.utc),
    )
    defaults.update(kw)
    t = RequisitionTask(**defaults)
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


def _make_buy_plan(
    db: Session,
    quote: Quote,
    req: Requisition,
    **kw,
) -> BuyPlan:
    defaults = dict(
        quote_id=quote.id,
        requisition_id=req.id,
        status="draft",
        created_at=datetime.now(timezone.utc),
    )
    defaults.update(kw)
    bp = BuyPlan(**defaults)
    db.add(bp)
    db.commit()
    db.refresh(bp)
    return bp


# ── Parts list ────────────────────────────────────────────────────────────


class TestPartsListPartial:
    def test_parts_list_returns_200(self, client: TestClient) -> None:
        resp = client.get("/v2/partials/parts")
        assert resp.status_code == 200

    def test_parts_list_with_search(self, client: TestClient, test_requisition: Requisition) -> None:
        resp = client.get("/v2/partials/parts?q=LM317T")
        assert resp.status_code == 200

    def test_parts_list_with_status_filter(self, client: TestClient) -> None:
        resp = client.get("/v2/partials/parts?status=found")
        assert resp.status_code == 200

    def test_parts_list_archived_filter(self, client: TestClient) -> None:
        resp = client.get("/v2/partials/parts?status=archived")
        assert resp.status_code == 200

    def test_parts_list_include_archived(self, client: TestClient) -> None:
        resp = client.get("/v2/partials/parts?include_archived=true")
        assert resp.status_code == 200

    def test_parts_list_invalid_dates_ignored(self, client: TestClient) -> None:
        resp = client.get("/v2/partials/parts?date_from=notadate&date_to=alsonotadate")
        assert resp.status_code == 200


# ── Parts tabs ────────────────────────────────────────────────────────────


class TestPartTabOffers:
    def test_offers_tab_200(
        self,
        client: TestClient,
        db_session: Session,
        test_requisition: Requisition,
    ) -> None:
        req = db_session.query(Requirement).filter_by(requisition_id=test_requisition.id).first()
        resp = client.get(f"/v2/partials/parts/{req.id}/tab/offers")
        assert resp.status_code == 200

    def test_offers_tab_not_found(self, client: TestClient) -> None:
        resp = client.get("/v2/partials/parts/99999/tab/offers")
        assert resp.status_code == 404


class TestPartTabSourcing:
    def test_sourcing_tab_200(
        self,
        client: TestClient,
        db_session: Session,
        test_requisition: Requisition,
    ) -> None:
        req = db_session.query(Requirement).filter_by(requisition_id=test_requisition.id).first()
        resp = client.get(f"/v2/partials/parts/{req.id}/tab/sourcing")
        assert resp.status_code == 200

    def test_sourcing_tab_not_found(self, client: TestClient) -> None:
        resp = client.get("/v2/partials/parts/99999/tab/sourcing")
        assert resp.status_code == 404


class TestPartTabReqDetails:
    def test_req_details_tab_200(
        self,
        client: TestClient,
        db_session: Session,
        test_requisition: Requisition,
    ) -> None:
        req = db_session.query(Requirement).filter_by(requisition_id=test_requisition.id).first()
        resp = client.get(f"/v2/partials/parts/{req.id}/tab/req-details")
        assert resp.status_code == 200

    def test_req_details_tab_not_found(self, client: TestClient) -> None:
        resp = client.get("/v2/partials/parts/99999/tab/req-details")
        assert resp.status_code == 404


class TestPartHeader:
    def test_header_200(
        self,
        client: TestClient,
        db_session: Session,
        test_requisition: Requisition,
    ) -> None:
        req = db_session.query(Requirement).filter_by(requisition_id=test_requisition.id).first()
        resp = client.get(f"/v2/partials/parts/{req.id}/header")
        assert resp.status_code == 200

    def test_header_not_found(self, client: TestClient) -> None:
        resp = client.get("/v2/partials/parts/99999/header")
        assert resp.status_code == 404


class TestPartTabActivity:
    def test_activity_tab_200(
        self,
        client: TestClient,
        db_session: Session,
        test_requisition: Requisition,
    ) -> None:
        req = db_session.query(Requirement).filter_by(requisition_id=test_requisition.id).first()
        resp = client.get(f"/v2/partials/parts/{req.id}/tab/activity")
        assert resp.status_code == 200

    def test_activity_tab_not_found(self, client: TestClient) -> None:
        resp = client.get("/v2/partials/parts/99999/tab/activity")
        assert resp.status_code == 404


class TestPartTabComms:
    def test_comms_tab_200(
        self,
        client: TestClient,
        db_session: Session,
        test_requisition: Requisition,
    ) -> None:
        req = db_session.query(Requirement).filter_by(requisition_id=test_requisition.id).first()
        resp = client.get(f"/v2/partials/parts/{req.id}/tab/comms")
        assert resp.status_code == 200

    def test_comms_tab_not_found(self, client: TestClient) -> None:
        resp = client.get("/v2/partials/parts/99999/tab/comms")
        assert resp.status_code == 404


class TestPartTabNotes:
    def test_notes_tab_200(
        self,
        client: TestClient,
        db_session: Session,
        test_requisition: Requisition,
    ) -> None:
        req = db_session.query(Requirement).filter_by(requisition_id=test_requisition.id).first()
        resp = client.get(f"/v2/partials/parts/{req.id}/tab/notes")
        assert resp.status_code == 200

    def test_save_notes(
        self,
        client: TestClient,
        db_session: Session,
        test_requisition: Requisition,
    ) -> None:
        req = db_session.query(Requirement).filter_by(requisition_id=test_requisition.id).first()
        resp = client.patch(
            f"/v2/partials/parts/{req.id}/notes",
            data={"sale_notes": "Important: check lead times"},
        )
        assert resp.status_code == 200
        db_session.refresh(req)
        assert req.sale_notes == "Important: check lead times"

    def test_save_notes_not_found(self, client: TestClient) -> None:
        resp = client.patch("/v2/partials/parts/99999/notes", data={"sale_notes": "test"})
        assert resp.status_code == 404


# ── Part tasks ────────────────────────────────────────────────────────────


class TestCreatePartTask:
    def test_create_task_success(
        self,
        client: TestClient,
        db_session: Session,
        test_requisition: Requisition,
    ) -> None:
        req = db_session.query(Requirement).filter_by(requisition_id=test_requisition.id).first()
        resp = client.post(
            f"/v2/partials/parts/{req.id}/tasks",
            data={"title": "Call vendor for quote"},
        )
        assert resp.status_code == 200
        task = db_session.query(RequisitionTask).filter_by(requirement_id=req.id).first()
        assert task is not None
        assert task.title == "Call vendor for quote"

    def test_create_task_no_title(
        self,
        client: TestClient,
        db_session: Session,
        test_requisition: Requisition,
    ) -> None:
        req = db_session.query(Requirement).filter_by(requisition_id=test_requisition.id).first()
        resp = client.post(f"/v2/partials/parts/{req.id}/tasks", data={"title": ""})
        assert resp.status_code == 422

    def test_create_task_not_found(self, client: TestClient) -> None:
        resp = client.post("/v2/partials/parts/99999/tasks", data={"title": "test"})
        assert resp.status_code == 404


class TestMarkTaskDone:
    def test_mark_done(
        self,
        client: TestClient,
        db_session: Session,
        test_requisition: Requisition,
        test_user: User,
    ) -> None:
        req = db_session.query(Requirement).filter_by(requisition_id=test_requisition.id).first()
        task = _make_task(db_session, test_requisition, req, test_user)
        resp = client.post(f"/v2/partials/parts/tasks/{task.id}/done")
        assert resp.status_code == 200
        db_session.refresh(task)
        assert task.status == "done"

    def test_mark_done_not_found(self, client: TestClient) -> None:
        resp = client.post("/v2/partials/parts/tasks/99999/done")
        assert resp.status_code == 404


class TestReopenTask:
    def test_reopen(
        self,
        client: TestClient,
        db_session: Session,
        test_requisition: Requisition,
        test_user: User,
    ) -> None:
        req = db_session.query(Requirement).filter_by(requisition_id=test_requisition.id).first()
        task = _make_task(db_session, test_requisition, req, test_user, status="done")
        resp = client.post(f"/v2/partials/parts/tasks/{task.id}/reopen")
        assert resp.status_code == 200
        db_session.refresh(task)
        assert task.status == "todo"

    def test_reopen_not_found(self, client: TestClient) -> None:
        resp = client.post("/v2/partials/parts/tasks/99999/reopen")
        assert resp.status_code == 404


# ── Archive / unarchive ───────────────────────────────────────────────────


class TestArchiveSinglePart:
    def test_archive_part(
        self,
        client: TestClient,
        db_session: Session,
        test_requisition: Requisition,
    ) -> None:
        req = db_session.query(Requirement).filter_by(requisition_id=test_requisition.id).first()
        resp = client.patch(f"/v2/partials/parts/{req.id}/archive")
        assert resp.status_code == 200
        db_session.refresh(req)
        assert req.sourcing_status == "archived"

    def test_archive_part_not_found(self, client: TestClient) -> None:
        resp = client.patch("/v2/partials/parts/99999/archive")
        assert resp.status_code == 404

    def test_unarchive_part(
        self,
        client: TestClient,
        db_session: Session,
        test_requisition: Requisition,
    ) -> None:
        req = db_session.query(Requirement).filter_by(requisition_id=test_requisition.id).first()
        req.sourcing_status = "archived"
        db_session.commit()
        resp = client.patch(f"/v2/partials/parts/{req.id}/unarchive")
        assert resp.status_code == 200
        db_session.refresh(req)
        assert req.sourcing_status == "open"

    def test_unarchive_part_not_found(self, client: TestClient) -> None:
        resp = client.patch("/v2/partials/parts/99999/unarchive")
        assert resp.status_code == 404


class TestArchiveRequisition:
    def test_archive_requisition(
        self,
        client: TestClient,
        db_session: Session,
        test_requisition: Requisition,
    ) -> None:
        resp = client.patch(f"/v2/partials/requisitions/{test_requisition.id}/archive")
        assert resp.status_code == 200
        db_session.refresh(test_requisition)
        assert test_requisition.status == "archived"

    def test_archive_req_not_found(self, client: TestClient) -> None:
        resp = client.patch("/v2/partials/requisitions/99999/archive")
        assert resp.status_code == 404

    def test_unarchive_requisition(
        self,
        client: TestClient,
        db_session: Session,
        test_requisition: Requisition,
    ) -> None:
        test_requisition.status = "archived"
        db_session.commit()
        resp = client.patch(f"/v2/partials/requisitions/{test_requisition.id}/unarchive")
        assert resp.status_code == 200
        db_session.refresh(test_requisition)
        assert test_requisition.status == "active"

    def test_unarchive_req_not_found(self, client: TestClient) -> None:
        resp = client.patch("/v2/partials/requisitions/99999/unarchive")
        assert resp.status_code == 404


class TestBulkArchive:
    def test_bulk_archive_requirements(
        self,
        client: TestClient,
        db_session: Session,
        test_requisition: Requisition,
    ) -> None:
        req = db_session.query(Requirement).filter_by(requisition_id=test_requisition.id).first()
        resp = client.post(
            "/v2/partials/parts/bulk-archive",
            json={"requirement_ids": [req.id], "requisition_ids": []},
        )
        assert resp.status_code == 200
        db_session.refresh(req)
        assert req.sourcing_status == "archived"

    def test_bulk_archive_requisitions(
        self,
        client: TestClient,
        db_session: Session,
        test_requisition: Requisition,
    ) -> None:
        resp = client.post(
            "/v2/partials/parts/bulk-archive",
            json={"requirement_ids": [], "requisition_ids": [test_requisition.id]},
        )
        assert resp.status_code == 200
        db_session.refresh(test_requisition)
        assert test_requisition.status == "archived"

    def test_bulk_unarchive_requirements(
        self,
        client: TestClient,
        db_session: Session,
        test_requisition: Requisition,
    ) -> None:
        req = db_session.query(Requirement).filter_by(requisition_id=test_requisition.id).first()
        req.sourcing_status = "archived"
        db_session.commit()
        resp = client.post(
            "/v2/partials/parts/bulk-unarchive",
            json={"requirement_ids": [req.id], "requisition_ids": []},
        )
        assert resp.status_code == 200
        db_session.refresh(req)
        assert req.sourcing_status == "open"

    def test_bulk_unarchive_empty_body(self, client: TestClient) -> None:
        resp = client.post(
            "/v2/partials/parts/bulk-unarchive",
            json={"requirement_ids": [], "requisition_ids": []},
        )
        assert resp.status_code == 200


# ── Buy plan routes ───────────────────────────────────────────────────────


class TestBuyPlanListPartial:
    def test_buy_plan_list_200(self, client: TestClient) -> None:
        resp = client.get("/v2/partials/buy-plans")
        assert resp.status_code == 200

    def test_buy_plan_list_with_status(self, client: TestClient) -> None:
        resp = client.get("/v2/partials/buy-plans?status=draft")
        assert resp.status_code == 200

    def test_buy_plan_list_with_search(self, client: TestClient) -> None:
        resp = client.get("/v2/partials/buy-plans?q=SO-1234")
        assert resp.status_code == 200


class TestBuyPlanDetailPartial:
    def test_detail_not_found(self, client: TestClient) -> None:
        resp = client.get("/v2/partials/buy-plans/99999")
        assert resp.status_code == 404

    def test_detail_200(
        self,
        client: TestClient,
        db_session: Session,
        test_quote: Quote,
        test_requisition: Requisition,
    ) -> None:
        bp = _make_buy_plan(db_session, test_quote, test_requisition)
        resp = client.get(f"/v2/partials/buy-plans/{bp.id}")
        assert resp.status_code == 200


class TestBuyPlanCancel:
    def test_cancel_draft_plan(
        self,
        client: TestClient,
        db_session: Session,
        test_quote: Quote,
        test_requisition: Requisition,
    ) -> None:
        bp = _make_buy_plan(db_session, test_quote, test_requisition, status="draft")
        resp = client.post(
            f"/v2/partials/buy-plans/{bp.id}/cancel",
            data={"reason": "Customer cancelled order"},
        )
        assert resp.status_code == 200
        db_session.refresh(bp)
        assert bp.status == "cancelled"

    def test_cancel_already_cancelled(
        self,
        client: TestClient,
        db_session: Session,
        test_quote: Quote,
        test_requisition: Requisition,
    ) -> None:
        bp = _make_buy_plan(db_session, test_quote, test_requisition, status="cancelled")
        resp = client.post(f"/v2/partials/buy-plans/{bp.id}/cancel", data={})
        assert resp.status_code == 400

    def test_cancel_not_found(self, client: TestClient) -> None:
        resp = client.post("/v2/partials/buy-plans/99999/cancel", data={})
        assert resp.status_code == 404


class TestBuyPlanReset:
    def test_reset_cancelled_plan(
        self,
        client: TestClient,
        db_session: Session,
        test_quote: Quote,
        test_requisition: Requisition,
    ) -> None:
        bp = _make_buy_plan(db_session, test_quote, test_requisition, status="cancelled")
        with patch("app.services.buyplan_workflow.reset_buy_plan_to_draft") as mock_reset:
            mock_reset.return_value = bp
            resp = client.post(f"/v2/partials/buy-plans/{bp.id}/reset")
        assert resp.status_code == 200

    def test_reset_invalid_plan(
        self,
        client: TestClient,
        db_session: Session,
        test_quote: Quote,
        test_requisition: Requisition,
    ) -> None:
        bp = _make_buy_plan(db_session, test_quote, test_requisition, status="draft")
        with patch("app.services.buyplan_workflow.reset_buy_plan_to_draft") as mock_reset:
            mock_reset.side_effect = ValueError("Cannot reset plan in draft status")
            resp = client.post(f"/v2/partials/buy-plans/{bp.id}/reset")
        assert resp.status_code == 400
