"""test_htmx_views_nightly2.py — Second nightly coverage boost for htmx_views.py.

Targets: proactive routes, parts list/tabs/inline-edit, archive system,
         trouble tickets, knowledge base, admin routes.

Called by: pytest
Depends on: conftest.py (client, db_session, test_user, admin_user)
"""

import os

os.environ["TESTING"] = "1"

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.constants import RequisitionStatus, SourcingStatus, TaskStatus, TicketSource
from app.models import (
    Company,
    Requirement,
    Requisition,
    User,
    VendorCard,
)
from app.models.intelligence import MaterialCard
from app.models.knowledge import KnowledgeEntry
from app.models.task import RequisitionTask
from app.models.trouble_ticket import TroubleTicket

# ── Fixtures ────────────────────────────────────────────────────────────


@pytest.fixture()
def admin_client(db_session: Session, admin_user: User) -> TestClient:
    """TestClient authenticated as an admin user."""
    from app.database import get_db
    from app.dependencies import require_admin, require_buyer, require_fresh_token, require_user
    from app.main import app

    def _db():
        yield db_session

    def _user():
        return admin_user

    async def _token():
        return "mock-token"

    overridden = [get_db, require_user, require_admin, require_buyer, require_fresh_token]
    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[require_user] = _user
    app.dependency_overrides[require_admin] = _user
    app.dependency_overrides[require_buyer] = _user
    app.dependency_overrides[require_fresh_token] = _token

    try:
        with TestClient(app) as c:
            yield c
    finally:
        for dep in overridden:
            app.dependency_overrides.pop(dep, None)


# ── Helpers ─────────────────────────────────────────────────────────────


def _req(db: Session, user: User, **kw) -> Requisition:
    defaults = dict(
        name="NIGHTLY2-REQ",
        customer_name="Nightly2 Corp",
        status=RequisitionStatus.ACTIVE,
        created_by=user.id,
        created_at=datetime.now(timezone.utc),
    )
    defaults.update(kw)
    obj = Requisition(**defaults)
    db.add(obj)
    db.flush()
    return obj


def _requirement(db: Session, req: Requisition, mpn="LM317T", **kw) -> Requirement:
    defaults = dict(
        requisition_id=req.id,
        primary_mpn=mpn,
        target_qty=100,
        sourcing_status=SourcingStatus.OPEN,
        created_at=datetime.now(timezone.utc),
    )
    defaults.update(kw)
    obj = Requirement(**defaults)
    db.add(obj)
    db.flush()
    return obj


def _material_card(db: Session, mpn="MPN-N2-001", **kw) -> MaterialCard:
    defaults = dict(
        normalized_mpn=mpn,
        display_mpn=mpn,
        manufacturer="TestCo",
        created_at=datetime.now(timezone.utc),
    )
    defaults.update(kw)
    obj = MaterialCard(**defaults)
    db.add(obj)
    db.flush()
    return obj


def _knowledge_entry(db: Session, user: User, **kw) -> KnowledgeEntry:
    defaults = dict(
        entry_type="note",
        content="Test knowledge entry",
        source="manual",
        created_by=user.id,
    )
    defaults.update(kw)
    obj = KnowledgeEntry(**defaults)
    db.add(obj)
    db.flush()
    return obj


def _ticket(db: Session, user: User, **kw) -> TroubleTicket:
    import uuid

    defaults = dict(
        ticket_number=f"TT-{uuid.uuid4().hex[:8].upper()}",
        submitted_by=user.id,
        status="submitted",
        title="Test Ticket",
        description="Test description",
        source=TicketSource.REPORT_BUTTON,
        created_at=datetime.now(timezone.utc),
    )
    defaults.update(kw)
    obj = TroubleTicket(**defaults)
    db.add(obj)
    db.flush()
    return obj


# ── Parts List Partial ────────────────────────────────────────────────


class TestPartsListPartial:
    def test_parts_list_empty(self, client, db_session: Session):
        resp = client.get("/v2/partials/parts")
        assert resp.status_code == 200

    def test_parts_list_with_data(self, client, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        _requirement(db_session, req)
        db_session.commit()

        resp = client.get("/v2/partials/parts")
        assert resp.status_code == 200

    def test_parts_list_filter_q(self, client, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        _requirement(db_session, req, mpn="LM317T-SEARCH")
        db_session.commit()

        resp = client.get("/v2/partials/parts?q=LM317T")
        assert resp.status_code == 200

    def test_parts_list_filter_status(self, client, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        _requirement(db_session, req, sourcing_status=SourcingStatus.OPEN)
        db_session.commit()

        resp = client.get("/v2/partials/parts?status=open")
        assert resp.status_code == 200

    def test_parts_list_filter_archived(self, client, db_session: Session, test_user: User):
        req = _req(db_session, test_user, status=RequisitionStatus.ARCHIVED)
        _requirement(db_session, req, sourcing_status=SourcingStatus.ARCHIVED)
        db_session.commit()

        resp = client.get("/v2/partials/parts?status=archived")
        assert resp.status_code == 200

    def test_parts_list_include_archived(self, client, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        _requirement(db_session, req)
        db_session.commit()

        resp = client.get("/v2/partials/parts?include_archived=true")
        assert resp.status_code == 200

    def test_parts_list_filter_customer(self, client, db_session: Session, test_user: User):
        req = _req(db_session, test_user, customer_name="Acme Corp")
        _requirement(db_session, req)
        db_session.commit()

        resp = client.get("/v2/partials/parts?customer=Acme")
        assert resp.status_code == 200

    def test_parts_list_sort_mpn_asc(self, client, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        _requirement(db_session, req)
        db_session.commit()

        resp = client.get("/v2/partials/parts?sort=mpn&dir=asc")
        assert resp.status_code == 200

    def test_parts_list_sort_qty_desc(self, client, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        _requirement(db_session, req, target_qty=500)
        db_session.commit()

        resp = client.get("/v2/partials/parts?sort=qty&dir=desc")
        assert resp.status_code == 200

    def test_parts_list_date_filters(self, client, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        _requirement(db_session, req)
        db_session.commit()

        resp = client.get("/v2/partials/parts?date_from=2025-01-01&date_to=2026-12-31")
        assert resp.status_code == 200

    def test_parts_list_invalid_date_ignored(self, client, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        _requirement(db_session, req)
        db_session.commit()

        resp = client.get("/v2/partials/parts?date_from=not-a-date")
        assert resp.status_code == 200


# ── Parts Tabs ────────────────────────────────────────────────────────


class TestPartsTabs:
    def test_part_tab_offers(self, client, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        item = _requirement(db_session, req)
        db_session.commit()

        resp = client.get(f"/v2/partials/parts/{item.id}/tab/offers")
        assert resp.status_code == 200

    def test_part_tab_offers_not_found(self, client, db_session: Session):
        resp = client.get("/v2/partials/parts/999999/tab/offers")
        assert resp.status_code == 404

    def test_part_tab_sourcing(self, client, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        item = _requirement(db_session, req)
        db_session.commit()

        resp = client.get(f"/v2/partials/parts/{item.id}/tab/sourcing")
        assert resp.status_code == 200

    def test_part_tab_req_details(self, client, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        item = _requirement(db_session, req)
        db_session.commit()

        resp = client.get(f"/v2/partials/parts/{item.id}/tab/req-details")
        assert resp.status_code == 200

    def test_part_tab_activity(self, client, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        item = _requirement(db_session, req)
        db_session.commit()

        resp = client.get(f"/v2/partials/parts/{item.id}/tab/activity")
        assert resp.status_code == 200

    def test_part_tab_comms(self, client, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        item = _requirement(db_session, req)
        db_session.commit()

        resp = client.get(f"/v2/partials/parts/{item.id}/tab/comms")
        assert resp.status_code == 200

    def test_part_tab_notes(self, client, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        item = _requirement(db_session, req)
        db_session.commit()

        resp = client.get(f"/v2/partials/parts/{item.id}/tab/notes")
        assert resp.status_code == 200

    def test_part_tab_notes_not_found(self, client, db_session: Session):
        resp = client.get("/v2/partials/parts/999999/tab/notes")
        assert resp.status_code == 404

    def test_save_part_notes(self, client, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        item = _requirement(db_session, req)
        db_session.commit()

        resp = client.patch(
            f"/v2/partials/parts/{item.id}/notes",
            data={"sale_notes": "Important note here"},
        )
        assert resp.status_code == 200


# ── Part Header Inline Edit ───────────────────────────────────────────


class TestPartHeaderEdit:
    def test_part_header_edit_name(self, client, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        item = _requirement(db_session, req)
        db_session.commit()

        resp = client.get(f"/v2/partials/parts/{item.id}/header/edit/brand")
        assert resp.status_code == 200

    def test_part_header_edit_sourcing_status(self, client, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        item = _requirement(db_session, req)
        db_session.commit()

        resp = client.get(f"/v2/partials/parts/{item.id}/header/edit/sourcing_status")
        assert resp.status_code == 200

    def test_part_header_edit_condition(self, client, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        item = _requirement(db_session, req)
        db_session.commit()

        resp = client.get(f"/v2/partials/parts/{item.id}/header/edit/condition")
        assert resp.status_code == 200

    def test_part_header_edit_description(self, client, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        item = _requirement(db_session, req)
        db_session.commit()

        resp = client.get(f"/v2/partials/parts/{item.id}/header/edit/description")
        assert resp.status_code == 200

    def test_part_header_edit_substitutes(self, client, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        item = _requirement(db_session, req)
        db_session.commit()

        resp = client.get(f"/v2/partials/parts/{item.id}/header/edit/substitutes")
        assert resp.status_code == 200

    def test_part_header_edit_target_qty(self, client, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        item = _requirement(db_session, req)
        db_session.commit()

        resp = client.get(f"/v2/partials/parts/{item.id}/header/edit/target_qty")
        assert resp.status_code == 200

    def test_part_header_edit_invalid_field(self, client, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        item = _requirement(db_session, req)
        db_session.commit()

        resp = client.get(f"/v2/partials/parts/{item.id}/header/edit/not_a_field")
        assert resp.status_code == 400

    def test_part_header_save_brand(self, client, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        item = _requirement(db_session, req)
        db_session.commit()

        resp = client.patch(
            f"/v2/partials/parts/{item.id}/header",
            data={"field": "brand", "value": "Texas Instruments"},
        )
        assert resp.status_code == 200

    def test_part_header_save_target_qty(self, client, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        item = _requirement(db_session, req)
        db_session.commit()

        resp = client.patch(
            f"/v2/partials/parts/{item.id}/header",
            data={"field": "target_qty", "value": "500"},
        )
        assert resp.status_code == 200

    def test_part_header_save_target_price(self, client, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        item = _requirement(db_session, req)
        db_session.commit()

        resp = client.patch(
            f"/v2/partials/parts/{item.id}/header",
            data={"field": "target_price", "value": "0.50"},
        )
        assert resp.status_code == 200

    def test_part_header_save_manufacturer(self, client, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        item = _requirement(db_session, req)
        db_session.commit()

        resp = client.patch(
            f"/v2/partials/parts/{item.id}/header",
            data={"field": "manufacturer", "value": "TI"},
        )
        assert resp.status_code == 200

    def test_part_header_save_invalid_field(self, client, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        item = _requirement(db_session, req)
        db_session.commit()

        resp = client.patch(
            f"/v2/partials/parts/{item.id}/header",
            data={"field": "not_valid", "value": "x"},
        )
        assert resp.status_code == 400


# ── Part Cell Inline Edit ─────────────────────────────────────────────


class TestPartCellEdit:
    def test_cell_edit_sourcing_status(self, client, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        item = _requirement(db_session, req)
        db_session.commit()

        resp = client.get(f"/v2/partials/parts/{item.id}/cell/edit/sourcing_status")
        assert resp.status_code == 200

    def test_cell_edit_invalid_field(self, client, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        item = _requirement(db_session, req)
        db_session.commit()

        resp = client.get(f"/v2/partials/parts/{item.id}/cell/edit/invalid")
        assert resp.status_code == 400

    def test_cell_display(self, client, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        item = _requirement(db_session, req)
        db_session.commit()

        resp = client.get(f"/v2/partials/parts/{item.id}/cell/display/target_qty")
        assert resp.status_code == 200

    def test_cell_display_invalid(self, client, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        item = _requirement(db_session, req)
        db_session.commit()

        resp = client.get(f"/v2/partials/parts/{item.id}/cell/display/invalid")
        assert resp.status_code == 400

    def test_cell_save_target_qty(self, client, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        item = _requirement(db_session, req)
        db_session.commit()

        resp = client.patch(
            f"/v2/partials/parts/{item.id}/cell",
            data={"field": "target_qty", "value": "250"},
        )
        assert resp.status_code == 200

    def test_cell_save_target_price(self, client, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        item = _requirement(db_session, req)
        db_session.commit()

        resp = client.patch(
            f"/v2/partials/parts/{item.id}/cell",
            data={"field": "target_price", "value": "0.75"},
        )
        assert resp.status_code == 200

    def test_cell_save_invalid_field(self, client, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        item = _requirement(db_session, req)
        db_session.commit()

        resp = client.patch(
            f"/v2/partials/parts/{item.id}/cell",
            data={"field": "invalid_field", "value": "x"},
        )
        assert resp.status_code == 400


# ── Part Spec Edit ────────────────────────────────────────────────────


class TestPartSpecEdit:
    def test_spec_edit_customer_pn(self, client, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        item = _requirement(db_session, req)
        db_session.commit()

        resp = client.get(f"/v2/partials/parts/{item.id}/edit-spec/customer_pn")
        assert resp.status_code == 200

    def test_spec_edit_condition(self, client, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        item = _requirement(db_session, req)
        db_session.commit()

        resp = client.get(f"/v2/partials/parts/{item.id}/edit-spec/condition")
        assert resp.status_code == 200

    def test_spec_edit_invalid_field(self, client, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        item = _requirement(db_session, req)
        db_session.commit()

        resp = client.get(f"/v2/partials/parts/{item.id}/edit-spec/invalid")
        assert resp.status_code == 400

    def test_spec_save(self, client, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        item = _requirement(db_session, req)
        db_session.commit()

        resp = client.patch(
            f"/v2/partials/parts/{item.id}/save-spec",
            data={"field": "customer_pn", "value": "CUST-123"},
        )
        assert resp.status_code == 200

    def test_spec_save_invalid_field(self, client, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        item = _requirement(db_session, req)
        db_session.commit()

        resp = client.patch(
            f"/v2/partials/parts/{item.id}/save-spec",
            data={"field": "not_valid", "value": "x"},
        )
        assert resp.status_code == 400


# ── Part Tasks ────────────────────────────────────────────────────────


class TestPartTasks:
    def test_create_part_task(self, client, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        item = _requirement(db_session, req)
        db_session.commit()

        resp = client.post(
            f"/v2/partials/parts/{item.id}/tasks",
            data={"title": "Follow up with Arrow", "notes": "Check pricing"},
        )
        assert resp.status_code == 200

    def test_create_part_task_no_title(self, client, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        item = _requirement(db_session, req)
        db_session.commit()

        resp = client.post(f"/v2/partials/parts/{item.id}/tasks", data={"notes": "No title"})
        assert resp.status_code == 422

    def test_mark_task_done(self, client, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        item = _requirement(db_session, req)
        db_session.flush()
        task = RequisitionTask(
            requisition_id=req.id,
            requirement_id=item.id,
            title="Test task",
            created_by=test_user.id,
            status=TaskStatus.TODO,
            source="manual",
        )
        db_session.add(task)
        db_session.commit()

        resp = client.post(f"/v2/partials/parts/tasks/{task.id}/done")
        assert resp.status_code == 200

    def test_reopen_task(self, client, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        item = _requirement(db_session, req)
        db_session.flush()
        task = RequisitionTask(
            requisition_id=req.id,
            requirement_id=item.id,
            title="Done task",
            created_by=test_user.id,
            status=TaskStatus.DONE,
            source="manual",
        )
        db_session.add(task)
        db_session.commit()

        resp = client.post(f"/v2/partials/parts/tasks/{task.id}/reopen")
        assert resp.status_code == 200

    def test_mark_task_done_not_found(self, client, db_session: Session):
        resp = client.post("/v2/partials/parts/tasks/999999/done")
        assert resp.status_code == 404


# ── Archive System ────────────────────────────────────────────────────


class TestArchiveSystem:
    def test_archive_single_part(self, client, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        item = _requirement(db_session, req)
        db_session.commit()

        resp = client.patch(f"/v2/partials/parts/{item.id}/archive")
        assert resp.status_code == 200

    def test_archive_single_part_not_found(self, client, db_session: Session):
        resp = client.patch("/v2/partials/parts/999999/archive")
        assert resp.status_code == 404

    def test_unarchive_single_part(self, client, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        item = _requirement(db_session, req, sourcing_status=SourcingStatus.ARCHIVED)
        db_session.commit()

        resp = client.patch(f"/v2/partials/parts/{item.id}/unarchive")
        assert resp.status_code == 200

    def test_archive_requisition(self, client, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        _requirement(db_session, req)
        db_session.commit()

        resp = client.patch(f"/v2/partials/requisitions/{req.id}/archive")
        assert resp.status_code == 200

    def test_archive_requisition_not_found(self, client, db_session: Session):
        resp = client.patch("/v2/partials/requisitions/999999/archive")
        assert resp.status_code == 404

    def test_unarchive_requisition(self, client, db_session: Session, test_user: User):
        req = _req(db_session, test_user, status=RequisitionStatus.ARCHIVED)
        _requirement(db_session, req, sourcing_status=SourcingStatus.ARCHIVED)
        db_session.commit()

        resp = client.patch(f"/v2/partials/requisitions/{req.id}/unarchive")
        assert resp.status_code == 200

    def test_bulk_archive(self, client, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        item = _requirement(db_session, req)
        db_session.commit()

        resp = client.post(
            "/v2/partials/parts/bulk-archive",
            json={"requirement_ids": [item.id], "requisition_ids": []},
        )
        assert resp.status_code == 200

    def test_bulk_unarchive(self, client, db_session: Session, test_user: User):
        req = _req(db_session, test_user, status=RequisitionStatus.ARCHIVED)
        item = _requirement(db_session, req, sourcing_status=SourcingStatus.ARCHIVED)
        db_session.commit()

        resp = client.post(
            "/v2/partials/parts/bulk-unarchive",
            json={"requirement_ids": [item.id], "requisition_ids": [req.id]},
        )
        assert resp.status_code == 200

    def test_bulk_archive_requisitions(self, client, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        _requirement(db_session, req)
        db_session.commit()

        resp = client.post(
            "/v2/partials/parts/bulk-archive",
            json={"requirement_ids": [], "requisition_ids": [req.id]},
        )
        assert resp.status_code == 200


# ── Trouble Tickets ───────────────────────────────────────────────────


class TestTroubleTickets:
    def test_trouble_tickets_workspace(self, client, db_session: Session):
        resp = client.get("/v2/partials/trouble-tickets/workspace")
        assert resp.status_code == 200

    def test_trouble_tickets_list_empty(self, client, db_session: Session):
        resp = client.get("/v2/partials/trouble-tickets/list")
        assert resp.status_code == 200

    def test_trouble_tickets_list_with_data(self, client, db_session: Session, test_user: User):
        _ticket(db_session, test_user)
        db_session.commit()

        resp = client.get("/v2/partials/trouble-tickets/list")
        assert resp.status_code == 200

    def test_trouble_tickets_list_filter_status(self, client, db_session: Session, test_user: User):
        _ticket(db_session, test_user, status="submitted")
        db_session.commit()

        resp = client.get("/v2/partials/trouble-tickets/list?status=submitted")
        assert resp.status_code == 200

    def test_trouble_ticket_detail_not_found(self, client, db_session: Session):
        resp = client.get("/v2/partials/trouble-tickets/999999")
        assert resp.status_code == 404


# ── Knowledge Base ────────────────────────────────────────────────────


class TestKnowledgeBase:
    def test_knowledge_list_empty(self, client, db_session: Session):
        resp = client.get("/v2/partials/knowledge")
        assert resp.status_code == 200

    def test_knowledge_list_with_data(self, client, db_session: Session, test_user: User):
        _knowledge_entry(db_session, test_user)
        db_session.commit()

        resp = client.get("/v2/partials/knowledge")
        assert resp.status_code == 200

    def test_knowledge_list_search(self, client, db_session: Session, test_user: User):
        _knowledge_entry(db_session, test_user, content="Arrow Electronics contact info")
        db_session.commit()

        resp = client.get("/v2/partials/knowledge?q=Arrow")
        assert resp.status_code == 200

    def test_create_knowledge_entry(self, client, db_session: Session):
        resp = client.post(
            "/v2/partials/knowledge",
            data={"content": "New knowledge entry", "entry_type": "note"},
        )
        assert resp.status_code == 200

    def test_create_knowledge_entry_empty_content(self, client, db_session: Session):
        resp = client.post(
            "/v2/partials/knowledge",
            data={"content": "", "entry_type": "note"},
        )
        assert resp.status_code == 400


# ── Proactive Routes ──────────────────────────────────────────────────


class TestProactiveRoutes:
    def test_proactive_list(self, client, db_session: Session):
        with patch(
            "app.services.proactive_service.get_matches_for_user",
            return_value={"groups": [], "stats": {"total": 0}},
        ):
            with patch(
                "app.services.proactive_service.get_sent_offers",
                return_value=[],
            ):
                resp = client.get("/v2/partials/proactive")
        assert resp.status_code == 200

    def test_proactive_badge_empty(self, client, db_session: Session):
        resp = client.get("/v2/partials/proactive/badge")
        assert resp.status_code == 200

    def test_proactive_scorecard(self, client, db_session: Session):
        with patch(
            "app.services.proactive_service.get_scorecard",
            return_value={"total_sent": 5, "total_converted": 2, "conversion_rate": 40, "total_revenue": 1000},
        ):
            resp = client.get("/v2/partials/proactive/scorecard")
        assert resp.status_code == 200

    def test_proactive_do_not_offer(self, client, db_session: Session, test_user: User):
        company = Company(
            name="Test Co",
            website="https://testco.com",
            is_active=True,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(company)
        db_session.commit()

        resp = client.post(
            "/v2/partials/proactive/do-not-offer",
            data={"mpn": "LM317T", "company_id": str(company.id)},
        )
        assert resp.status_code == 200

    def test_proactive_do_not_offer_missing_params(self, client, db_session: Session):
        resp = client.post(
            "/v2/partials/proactive/do-not-offer",
            data={"mpn": "LM317T"},
        )
        assert resp.status_code == 400

    def test_material_insights(self, client, db_session: Session):
        card = _material_card(db_session)
        db_session.commit()

        resp = client.get(f"/v2/partials/materials/{card.id}/insights")
        assert resp.status_code == 200

    def test_material_insights_not_found(self, client, db_session: Session):
        resp = client.get("/v2/partials/materials/999999/insights")
        assert resp.status_code == 404

    def test_enrich_material(self, client, db_session: Session):
        card = _material_card(db_session)
        db_session.commit()

        with patch(
            "app.services.material_enrichment_service.enrich_material_cards",
            AsyncMock(return_value=None),
        ):
            resp = client.post(f"/v2/partials/materials/{card.id}/enrich")
        assert resp.status_code == 200

    def test_enrich_material_not_found(self, client, db_session: Session):
        with patch(
            "app.services.material_enrichment_service.enrich_material_cards",
            AsyncMock(return_value=None),
        ):
            resp = client.post("/v2/partials/materials/999999/enrich")
        assert resp.status_code == 404


# ── Admin Routes ──────────────────────────────────────────────────────


class TestAdminRoutes:
    def test_admin_data_ops_as_admin(self, admin_client, db_session: Session):
        resp = admin_client.get("/v2/partials/admin/data-ops")
        assert resp.status_code == 200

    def test_admin_api_health(self, admin_client, db_session: Session):
        # Route catches ImportError and returns fallback — no patch needed
        resp = admin_client.get("/v2/partials/admin/api-health")
        assert resp.status_code == 200

    def test_vendor_merge(self, admin_client, db_session: Session):
        v1 = VendorCard(
            normalized_name="v1_merge",
            display_name="V1",
            emails=[],
            phones=[],
            created_at=datetime.now(timezone.utc),
        )
        v2 = VendorCard(
            normalized_name="v2_merge",
            display_name="V2",
            emails=[],
            phones=[],
            created_at=datetime.now(timezone.utc),
        )
        db_session.add_all([v1, v2])
        db_session.flush()
        db_session.commit()

        with patch(
            "app.services.vendor_merge_service.merge_vendor_cards",
            return_value={"kept_name": "V1", "reassigned": 3},
        ):
            resp = admin_client.post(
                "/v2/partials/admin/vendor-merge",
                data={"keep_id": str(v1.id), "remove_id": str(v2.id)},
            )
        assert resp.status_code == 200

    def test_company_merge(self, admin_client, db_session: Session):
        c1 = Company(
            name="Co1",
            is_active=True,
            created_at=datetime.now(timezone.utc),
        )
        c2 = Company(
            name="Co2",
            is_active=True,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add_all([c1, c2])
        db_session.flush()
        db_session.commit()

        with patch(
            "app.services.company_merge_service.merge_companies",
            return_value={"kept_name": "Co1"},
        ):
            resp = admin_client.post(
                "/v2/partials/admin/company-merge",
                data={"keep_id": str(c1.id), "remove_id": str(c2.id)},
            )
        assert resp.status_code == 200
