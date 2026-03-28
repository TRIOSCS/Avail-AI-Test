"""Tests for app/routers/requisitions/requirements.py — Requirements endpoints.

Covers: list, add, update, delete requirements, search, sightings,
leads, mark unavailable, stock import, notes, tasks, history, offers,
toggle quote selection, and helper functions.

Called by: pytest
Depends on: conftest fixtures (db_session, test_user, test_requisition, client)
"""

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from sqlalchemy.orm import Session

from app.models import (
    ChangeLog,
    Contact,
    Offer,
    Requirement,
    Requisition,
    Sighting,
    User,
)
from app.models.task import RequisitionTask

# ── Helper: create requirement directly ──────────────────────────────


def _make_requirement(db: Session, req: Requisition, mpn="LM317T", qty=1000, price=0.50, **kw) -> Requirement:
    r = Requirement(
        requisition_id=req.id,
        primary_mpn=mpn,
        normalized_mpn=mpn.lower().replace("-", "").replace(" ", ""),
        target_qty=qty,
        target_price=price,
        created_at=datetime.now(timezone.utc),
        **kw,
    )
    db.add(r)
    db.commit()
    db.refresh(r)
    return r


def _make_sighting(db: Session, req_item: Requirement, vendor="Arrow", mpn="LM317T", **kw) -> Sighting:
    s = Sighting(
        requirement_id=req_item.id,
        vendor_name=vendor,
        vendor_name_normalized=vendor.lower().replace(" ", ""),
        mpn_matched=mpn,
        source_type="brokerbin",
        qty_available=500,
        unit_price=0.45,
        confidence=80,
        score=50,
        created_at=datetime.now(timezone.utc),
        **kw,
    )
    db.add(s)
    db.commit()
    db.refresh(s)
    return s


def _make_offer(db: Session, req: Requisition, req_item: Requirement, user: User, **kw) -> Offer:
    o = Offer(
        requisition_id=req.id,
        requirement_id=req_item.id,
        vendor_name="Arrow Electronics",
        vendor_name_normalized="arrowelectronics",
        mpn="LM317T",
        normalized_mpn="lm317t",
        qty_available=1000,
        unit_price=0.50,
        entered_by_id=user.id,
        status="active",
        created_at=datetime.now(timezone.utc),
        **kw,
    )
    db.add(o)
    db.commit()
    db.refresh(o)
    return o


# ── GET /api/requisitions/{req_id}/requirements ────────────────────


class TestListRequirements:
    def test_list_requirements_basic(self, client, db_session, test_user, test_requisition):
        resp = client.get(f"/api/requisitions/{test_requisition.id}/requirements")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert len(data) >= 1
        assert data[0]["primary_mpn"] == "LM317T"
        assert "sighting_count" in data[0]
        assert "step" in data[0]

    def test_list_requirements_not_found(self, client):
        resp = client.get("/api/requisitions/99999/requirements")
        assert resp.status_code == 404

    def test_list_requirements_with_sightings(self, client, db_session, test_user, test_requisition):
        req_item = test_requisition.requirements[0]
        _make_sighting(db_session, req_item)
        resp = client.get(f"/api/requisitions/{test_requisition.id}/requirements")
        assert resp.status_code == 200
        data = resp.json()
        assert data[0]["sighting_count"] >= 1
        assert data[0]["step"] == "sourced"

    def test_list_requirements_with_offers(self, client, db_session, test_user, test_requisition):
        req_item = test_requisition.requirements[0]
        _make_offer(db_session, test_requisition, req_item, test_user)
        resp = client.get(f"/api/requisitions/{test_requisition.id}/requirements")
        assert resp.status_code == 200
        data = resp.json()
        assert data[0]["offer_count"] >= 1
        assert data[0]["step"] == "offers"

    def test_list_requirements_with_selected_offers(self, client, db_session, test_user, test_requisition):
        req_item = test_requisition.requirements[0]
        _make_offer(db_session, test_requisition, req_item, test_user, selected_for_quote=True)
        resp = client.get(f"/api/requisitions/{test_requisition.id}/requirements")
        assert resp.status_code == 200
        data = resp.json()
        assert data[0]["selected_count"] >= 1
        assert data[0]["step"] == "selected"

    def test_list_requirements_with_tasks(self, client, db_session, test_user, test_requisition):
        req_item = test_requisition.requirements[0]
        task = RequisitionTask(
            requisition_id=test_requisition.id,
            title="Test task",
            task_type="general",
            status="todo",
            source="manual",
            source_ref=f"requirement:{req_item.id}",
            created_by=test_user.id,
        )
        db_session.add(task)
        db_session.commit()
        resp = client.get(f"/api/requisitions/{test_requisition.id}/requirements")
        assert resp.status_code == 200
        data = resp.json()
        assert data[0]["task_count"] >= 1


# ── POST /api/requisitions/{req_id}/requirements ───────────────────


class TestAddRequirements:
    @patch("app.routers.requisitions.requirements.resolve_material_card", return_value=None)
    def test_add_single_requirement(self, mock_resolve, client, db_session, test_requisition):
        resp = client.post(
            f"/api/requisitions/{test_requisition.id}/requirements",
            json={"primary_mpn": "NE555P", "manufacturer": "TI", "target_qty": 500},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["created"]) == 1
        assert data["created"][0]["primary_mpn"] == "NE555P"

    @patch("app.routers.requisitions.requirements.resolve_material_card", return_value=None)
    def test_add_batch_requirements(self, mock_resolve, client, db_session, test_requisition):
        resp = client.post(
            f"/api/requisitions/{test_requisition.id}/requirements",
            json=[
                {"primary_mpn": "NE555P", "manufacturer": "TI", "target_qty": 100},
                {"primary_mpn": "LM7805", "manufacturer": "TI", "target_qty": 200},
            ],
        )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["created"]) == 2

    @patch("app.routers.requisitions.requirements.resolve_material_card", return_value=None)
    def test_add_requirement_with_substitutes(self, mock_resolve, client, db_session, test_requisition):
        resp = client.post(
            f"/api/requisitions/{test_requisition.id}/requirements",
            json={
                "primary_mpn": "NE555P",
                "manufacturer": "TI",
                "target_qty": 100,
                "substitutes": ["NE555D", "NE556N"],
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["created"]) == 1

    def test_add_requirement_not_found(self, client):
        resp = client.post(
            "/api/requisitions/99999/requirements",
            json={"primary_mpn": "NE555P", "manufacturer": "TI"},
        )
        assert resp.status_code == 404

    @patch("app.routers.requisitions.requirements.resolve_material_card", return_value=None)
    def test_add_batch_with_invalid_skipped(self, mock_resolve, client, db_session, test_requisition):
        resp = client.post(
            f"/api/requisitions/{test_requisition.id}/requirements",
            json=[
                {"primary_mpn": "NE555P", "manufacturer": "TI"},
                {"primary_mpn": "", "manufacturer": ""},  # invalid
            ],
        )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data.get("skipped", [])) >= 1

    def test_add_single_invalid_raises_422(self, client, db_session, test_requisition):
        resp = client.post(
            f"/api/requisitions/{test_requisition.id}/requirements",
            json={"primary_mpn": "", "manufacturer": ""},
        )
        assert resp.status_code == 422


# ── DELETE /api/requirements/{item_id} ─────────────────────────────


class TestDeleteRequirement:
    def test_delete_requirement(self, client, db_session, test_user, test_requisition):
        req_item = test_requisition.requirements[0]
        resp = client.delete(f"/api/requirements/{req_item.id}")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        assert db_session.get(Requirement, req_item.id) is None

    def test_delete_requirement_not_found(self, client):
        resp = client.delete("/api/requirements/99999")
        assert resp.status_code == 404


# ── PUT /api/requirements/{item_id} ───────────────────────────────


class TestUpdateRequirement:
    def test_update_target_qty(self, client, db_session, test_user, test_requisition):
        req_item = test_requisition.requirements[0]
        resp = client.put(f"/api/requirements/{req_item.id}", json={"target_qty": 2000})
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        db_session.refresh(req_item)
        assert req_item.target_qty == 2000

    def test_update_target_price(self, client, db_session, test_user, test_requisition):
        req_item = test_requisition.requirements[0]
        resp = client.put(f"/api/requirements/{req_item.id}", json={"target_price": 1.25})
        assert resp.status_code == 200
        db_session.refresh(req_item)
        assert float(req_item.target_price) == 1.25

    @patch("app.routers.requisitions.requirements.resolve_material_card", return_value=None)
    def test_update_primary_mpn(self, mock_resolve, client, db_session, test_user, test_requisition):
        req_item = test_requisition.requirements[0]
        resp = client.put(f"/api/requirements/{req_item.id}", json={"primary_mpn": "NE555P"})
        assert resp.status_code == 200
        db_session.refresh(req_item)
        assert "NE555P" in req_item.primary_mpn.upper()

    def test_update_notes(self, client, db_session, test_user, test_requisition):
        req_item = test_requisition.requirements[0]
        resp = client.put(f"/api/requirements/{req_item.id}", json={"notes": "Test note"})
        assert resp.status_code == 200
        db_session.refresh(req_item)
        assert req_item.notes == "Test note"

    def test_update_sale_notes(self, client, db_session, test_user, test_requisition):
        req_item = test_requisition.requirements[0]
        resp = client.put(f"/api/requirements/{req_item.id}", json={"sale_notes": "Sale note"})
        assert resp.status_code == 200
        db_session.refresh(req_item)
        assert req_item.sale_notes == "Sale note"

    def test_update_firmware_date_codes(self, client, db_session, test_user, test_requisition):
        req_item = test_requisition.requirements[0]
        resp = client.put(
            f"/api/requirements/{req_item.id}",
            json={"firmware": "v2.1", "date_codes": "2024+", "hardware_codes": "REV-A"},
        )
        assert resp.status_code == 200
        db_session.refresh(req_item)
        assert req_item.firmware == "v2.1"
        assert req_item.date_codes == "2024+"
        assert req_item.hardware_codes == "REV-A"

    def test_update_substitutes(self, client, db_session, test_user, test_requisition):
        req_item = test_requisition.requirements[0]
        resp = client.put(
            f"/api/requirements/{req_item.id}",
            json={"substitutes": ["NE555P", "LM7805"]},
        )
        assert resp.status_code == 200
        db_session.refresh(req_item)
        assert len(req_item.substitutes) >= 1

    def test_update_creates_changelog(self, client, db_session, test_user, test_requisition):
        req_item = test_requisition.requirements[0]
        client.put(f"/api/requirements/{req_item.id}", json={"target_qty": 5000})
        logs = (
            db_session.query(ChangeLog)
            .filter(ChangeLog.entity_type == "requirement", ChangeLog.entity_id == req_item.id)
            .all()
        )
        assert len(logs) >= 1
        assert any(log.field_name == "target_qty" for log in logs)

    def test_update_not_found(self, client):
        resp = client.put("/api/requirements/99999", json={"target_qty": 100})
        assert resp.status_code == 404

    def test_update_manufacturer(self, client, db_session, test_user, test_requisition):
        req_item = test_requisition.requirements[0]
        resp = client.put(f"/api/requirements/{req_item.id}", json={"manufacturer": "Texas Instruments"})
        assert resp.status_code == 200

    def test_update_packaging_condition(self, client, db_session, test_user, test_requisition):
        req_item = test_requisition.requirements[0]
        resp = client.put(
            f"/api/requirements/{req_item.id}",
            json={"packaging": "Tape and Reel", "condition": "New"},
        )
        assert resp.status_code == 200

    def test_update_description_fields(self, client, db_session, test_user, test_requisition):
        req_item = test_requisition.requirements[0]
        resp = client.put(
            f"/api/requirements/{req_item.id}",
            json={
                "description": "Voltage regulator",
                "package_type": "TO-220",
                "revision": "B",
                "customer_pn": "CUST-001",
                "brand": "TI",
            },
        )
        assert resp.status_code == 200
        db_session.refresh(req_item)
        assert req_item.description == "Voltage regulator"
        assert req_item.package_type == "TO-220"
        assert req_item.revision == "B"
        assert req_item.customer_pn == "CUST-001"
        assert req_item.brand == "TI"


# ── PUT /api/sightings/{sighting_id}/unavailable ──────────────────


class TestMarkUnavailable:
    def test_mark_unavailable(self, client, db_session, test_user, test_requisition):
        req_item = test_requisition.requirements[0]
        s = _make_sighting(db_session, req_item)
        resp = client.put(f"/api/sightings/{s.id}/unavailable", json={"unavailable": True})
        assert resp.status_code == 200
        assert resp.json()["is_unavailable"] is True
        db_session.refresh(s)
        assert s.is_unavailable is True

    def test_mark_available_again(self, client, db_session, test_user, test_requisition):
        req_item = test_requisition.requirements[0]
        s = _make_sighting(db_session, req_item, is_unavailable=True)
        resp = client.put(f"/api/sightings/{s.id}/unavailable", json={"unavailable": False})
        assert resp.status_code == 200
        assert resp.json()["is_unavailable"] is False

    def test_mark_unavailable_not_found(self, client):
        resp = client.put("/api/sightings/99999/unavailable", json={"unavailable": True})
        assert resp.status_code == 404


# ── GET /api/requisitions/{req_id}/sightings ──────────────────────


class TestGetSavedSightings:
    @patch("app.routers.requisitions._enrich_with_vendor_cards")
    def test_get_saved_sightings(self, mock_enrich, client, db_session, test_user, test_requisition):
        req_item = test_requisition.requirements[0]
        _make_sighting(db_session, req_item)
        resp = client.get(f"/api/requisitions/{test_requisition.id}/sightings")
        assert resp.status_code == 200

    def test_get_saved_sightings_not_found(self, client):
        resp = client.get("/api/requisitions/99999/sightings")
        assert resp.status_code == 404


# ── POST /api/offers/{offer_id}/toggle-quote-selection ─────────────


class TestToggleQuoteSelection:
    def test_toggle_on(self, client, db_session, test_user, test_requisition):
        req_item = test_requisition.requirements[0]
        offer = _make_offer(db_session, test_requisition, req_item, test_user, selected_for_quote=False)
        resp = client.post(f"/api/offers/{offer.id}/toggle-quote-selection")
        assert resp.status_code == 200
        assert resp.json()["selected_for_quote"] is True

    def test_toggle_off(self, client, db_session, test_user, test_requisition):
        req_item = test_requisition.requirements[0]
        offer = _make_offer(db_session, test_requisition, req_item, test_user, selected_for_quote=True)
        resp = client.post(f"/api/offers/{offer.id}/toggle-quote-selection")
        assert resp.status_code == 200
        assert resp.json()["selected_for_quote"] is False

    def test_toggle_not_found(self, client):
        resp = client.post("/api/offers/99999/toggle-quote-selection")
        assert resp.status_code == 404


# ── GET /api/requirements/{requirement_id}/notes ──────────────────


class TestRequirementNotes:
    def test_list_notes_empty(self, client, db_session, test_user, test_requisition):
        req_item = test_requisition.requirements[0]
        resp = client.get(f"/api/requirements/{req_item.id}/notes")
        assert resp.status_code == 200
        data = resp.json()
        assert "requirement_notes" in data
        assert "notes" in data

    def test_list_notes_with_offer_notes(self, client, db_session, test_user, test_requisition):
        req_item = test_requisition.requirements[0]
        _make_offer(db_session, test_requisition, req_item, test_user, notes="Good vendor")
        resp = client.get(f"/api/requirements/{req_item.id}/notes")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["notes"]) >= 1
        assert data["notes"][0]["note"] == "Good vendor"

    def test_list_notes_not_found(self, client):
        resp = client.get("/api/requirements/99999/notes")
        assert resp.status_code == 404

    def test_add_note(self, client, db_session, test_user, test_requisition):
        req_item = test_requisition.requirements[0]
        resp = client.post(
            f"/api/requirements/{req_item.id}/notes",
            json={"text": "Need urgently"},
        )
        assert resp.status_code == 200
        assert "Need urgently" in resp.json()["notes"]

    def test_add_note_appends(self, client, db_session, test_user, test_requisition):
        req_item = test_requisition.requirements[0]
        req_item.notes = "Existing note"
        db_session.commit()
        resp = client.post(
            f"/api/requirements/{req_item.id}/notes",
            json={"text": "Additional info"},
        )
        assert resp.status_code == 200
        assert "Existing note" in resp.json()["notes"]
        assert "Additional info" in resp.json()["notes"]

    def test_add_note_not_found(self, client):
        resp = client.post("/api/requirements/99999/notes", json={"text": "Hello"})
        assert resp.status_code == 404


# ── GET /api/requirements/{requirement_id}/tasks ──────────────────


class TestRequirementTasks:
    def test_list_tasks_empty(self, client, db_session, test_user, test_requisition):
        req_item = test_requisition.requirements[0]
        resp = client.get(f"/api/requirements/{req_item.id}/tasks")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_list_tasks_with_task(self, client, db_session, test_user, test_requisition):
        req_item = test_requisition.requirements[0]
        task = RequisitionTask(
            requisition_id=test_requisition.id,
            title="Call vendor",
            task_type="sourcing",
            status="todo",
            source="manual",
            source_ref=f"requirement:{req_item.id}",
            created_by=test_user.id,
            assigned_to_id=test_user.id,
        )
        db_session.add(task)
        db_session.commit()
        resp = client.get(f"/api/requirements/{req_item.id}/tasks")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) >= 1
        assert data[0]["title"] == "Call vendor"
        assert data[0]["assigned_to"] != ""

    def test_list_tasks_not_found(self, client):
        resp = client.get("/api/requirements/99999/tasks")
        assert resp.status_code == 404

    def test_create_task(self, client, db_session, test_user, test_requisition):
        req_item = test_requisition.requirements[0]
        resp = client.post(
            f"/api/requirements/{req_item.id}/tasks",
            json={"title": "Follow up with Arrow"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["title"] == "Follow up with Arrow"
        assert data["status"] == "todo"

    def test_create_task_not_found(self, client):
        resp = client.post("/api/requirements/99999/tasks", json={"title": "Test"})
        assert resp.status_code == 404


# ── GET /api/requirements/{requirement_id}/history ────────────────


class TestRequirementHistory:
    def test_history_empty(self, client, db_session, test_user, test_requisition):
        req_item = test_requisition.requirements[0]
        resp = client.get(f"/api/requirements/{req_item.id}/history")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    def test_history_with_changes(self, client, db_session, test_user, test_requisition):
        req_item = test_requisition.requirements[0]
        # Create a change log
        cl = ChangeLog(
            entity_type="requirement",
            entity_id=req_item.id,
            user_id=test_user.id,
            field_name="target_qty",
            old_value="1000",
            new_value="2000",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(cl)
        db_session.commit()
        resp = client.get(f"/api/requirements/{req_item.id}/history")
        assert resp.status_code == 200
        data = resp.json()
        assert any(e["type"] == "change" for e in data)

    def test_history_with_offers(self, client, db_session, test_user, test_requisition):
        req_item = test_requisition.requirements[0]
        _make_offer(db_session, test_requisition, req_item, test_user)
        resp = client.get(f"/api/requirements/{req_item.id}/history")
        assert resp.status_code == 200
        data = resp.json()
        assert any(e["type"] == "offer_created" for e in data)

    def test_history_with_contacts(self, client, db_session, test_user, test_requisition):
        req_item = test_requisition.requirements[0]
        ct = Contact(
            requisition_id=test_requisition.id,
            user_id=test_user.id,
            contact_type="rfq",
            vendor_name="Arrow",
            parts_included=["LM317T"],
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(ct)
        db_session.commit()
        resp = client.get(f"/api/requirements/{req_item.id}/history")
        assert resp.status_code == 200
        data = resp.json()
        assert any(e["type"] == "rfq_sent" for e in data)

    def test_history_with_done_tasks(self, client, db_session, test_user, test_requisition):
        req_item = test_requisition.requirements[0]
        task = RequisitionTask(
            requisition_id=test_requisition.id,
            title="Done task",
            task_type="general",
            status="done",
            source="manual",
            source_ref=f"requirement:{req_item.id}",
            created_by=test_user.id,
            completed_at=datetime.now(timezone.utc),
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(task)
        db_session.commit()
        resp = client.get(f"/api/requirements/{req_item.id}/history")
        assert resp.status_code == 200
        data = resp.json()
        assert any(e["type"] == "task_done" for e in data)

    def test_history_not_found(self, client):
        resp = client.get("/api/requirements/99999/history")
        assert resp.status_code == 404


# ── GET /api/requirements/{requirement_id}/offers ─────────────────


class TestRequirementOffers:
    def test_list_offers_empty(self, client, db_session, test_user, test_requisition):
        req_item = test_requisition.requirements[0]
        resp = client.get(f"/api/requirements/{req_item.id}/offers")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_list_offers_with_current(self, client, db_session, test_user, test_requisition):
        req_item = test_requisition.requirements[0]
        _make_offer(db_session, test_requisition, req_item, test_user)
        resp = client.get(f"/api/requirements/{req_item.id}/offers")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) >= 1
        assert data[0]["is_historical"] is False

    def test_list_offers_not_found(self, client):
        resp = client.get("/api/requirements/99999/offers")
        assert resp.status_code == 404


# ── GET /api/requisitions/{req_id}/leads ──────────────────────────


class TestLeads:
    def test_list_leads_empty(self, client, db_session, test_user, test_requisition):
        resp = client.get(f"/api/requisitions/{test_requisition.id}/leads")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_list_leads_not_found(self, client):
        resp = client.get("/api/requisitions/99999/leads")
        assert resp.status_code == 404


# ── GET /api/leads/{lead_id} ─────────────────────────────────────


class TestLeadDetail:
    def test_lead_not_found(self, client):
        resp = client.get("/api/leads/99999")
        assert resp.status_code == 404


# ── PATCH /api/leads/{lead_id}/status ────────────────────────────


class TestPatchLeadStatus:
    def test_lead_not_found(self, client):
        resp = client.patch("/api/leads/99999/status", json={"status": "contacted"})
        assert resp.status_code == 404


# ── POST /api/leads/{lead_id}/feedback ───────────────────────────


class TestLeadFeedback:
    def test_lead_not_found(self, client):
        resp = client.post("/api/leads/99999/feedback", json={"note": "Good lead"})
        assert resp.status_code == 404


# ── GET /api/leads/queue ─────────────────────────────────────────


class TestLeadsQueue:
    def test_queue_empty(self, client, db_session, test_user):
        resp = client.get("/api/leads/queue")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_queue_with_status_filter(self, client, db_session, test_user):
        resp = client.get("/api/leads/queue?status=new")
        assert resp.status_code == 200


# ── Helper function tests ────────────────────────────────────────


class TestAnnotateBuyerOutcomes:
    def test_empty_results(self, db_session, test_requisition):
        from app.routers.requisitions.requirements import _annotate_buyer_outcomes

        _annotate_buyer_outcomes(test_requisition, {}, db_session)
        # No error means success

    def test_empty_req_ids(self, db_session):
        from app.routers.requisitions.requirements import _annotate_buyer_outcomes

        req = MagicMock()
        req.requirements = []
        _annotate_buyer_outcomes(req, {"1": {"sightings": []}}, db_session)

    def test_with_sightings_open(self, db_session, test_user, test_requisition):
        from app.routers.requisitions.requirements import _annotate_buyer_outcomes

        req_item = test_requisition.requirements[0]
        results = {
            str(req_item.id): {
                "sightings": [
                    {"vendor_name": "Arrow", "mpn_matched": "LM317T", "is_unavailable": False},
                ],
            }
        }
        _annotate_buyer_outcomes(test_requisition, results, db_session)
        assert results[str(req_item.id)]["sightings"][0]["buyer_outcome"] == "open"
        assert results[str(req_item.id)]["buyer_outcomes"]["open"] == 1

    def test_with_sightings_unavailable(self, db_session, test_user, test_requisition):
        from app.routers.requisitions.requirements import _annotate_buyer_outcomes

        req_item = test_requisition.requirements[0]
        results = {
            str(req_item.id): {
                "sightings": [
                    {"vendor_name": "Arrow", "mpn_matched": "LM317T", "is_unavailable": True},
                ],
            }
        }
        _annotate_buyer_outcomes(test_requisition, results, db_session)
        assert results[str(req_item.id)]["sightings"][0]["buyer_outcome"] == "unavailable_confirmed"

    def test_with_sightings_offer_logged(self, db_session, test_user, test_requisition):
        from app.routers.requisitions.requirements import _annotate_buyer_outcomes

        req_item = test_requisition.requirements[0]
        _make_offer(db_session, test_requisition, req_item, test_user)
        results = {
            str(req_item.id): {
                "sightings": [
                    {"vendor_name": "Arrow Electronics", "mpn_matched": "LM317T", "is_unavailable": False},
                ],
            }
        }
        _annotate_buyer_outcomes(test_requisition, results, db_session)
        assert results[str(req_item.id)]["sightings"][0]["buyer_outcome"] == "offer_logged"

    def test_non_dict_sighting_skipped(self, db_session, test_user, test_requisition):
        from app.routers.requisitions.requirements import _annotate_buyer_outcomes

        req_item = test_requisition.requirements[0]
        results = {
            str(req_item.id): {
                "sightings": ["not_a_dict"],
            }
        }
        _annotate_buyer_outcomes(test_requisition, results, db_session)
        # Should not raise, non-dict entries are skipped

    def test_non_dict_group_skipped(self, db_session, test_user, test_requisition):
        from app.routers.requisitions.requirements import _annotate_buyer_outcomes

        req_item = test_requisition.requirements[0]
        results = {str(req_item.id): "not_a_dict"}
        _annotate_buyer_outcomes(test_requisition, results, db_session)


class TestAttachLeadData:
    def test_empty_requirements(self, db_session):
        from app.routers.requisitions.requirements import _attach_lead_data

        _attach_lead_data([], {}, db_session)

    def test_with_no_sightings(self, db_session, test_requisition):
        from app.routers.requisitions.requirements import _attach_lead_data

        req_item = test_requisition.requirements[0]
        results = {str(req_item.id): {"sightings": []}}
        _attach_lead_data([req_item], results, db_session)
        assert results[str(req_item.id)].get("lead_cards") is not None
        assert results[str(req_item.id)]["lead_summary"]["total_leads"] == 0


class TestEnqueueIcsNcBatch:
    @patch("app.routers.requisitions.requirements.SessionLocal")
    @patch("app.routers.requisitions.requirements.enqueue_for_nc_search")
    @patch("app.routers.requisitions.requirements.enqueue_for_ics_search")
    def test_enqueue_basic(self, mock_ics, mock_nc, mock_session_cls):
        from app.routers.requisitions.requirements import _enqueue_ics_nc_batch

        mock_db = MagicMock()
        mock_session_cls.return_value = mock_db
        _enqueue_ics_nc_batch([1, 2])
        assert mock_nc.call_count == 2
        assert mock_ics.call_count == 2
        mock_db.close.assert_called_once()

    @patch("app.routers.requisitions.requirements.SessionLocal")
    @patch("app.routers.requisitions.requirements.enqueue_for_nc_search", side_effect=Exception("NC fail"))
    @patch("app.routers.requisitions.requirements.enqueue_for_ics_search", side_effect=Exception("ICS fail"))
    def test_enqueue_handles_errors(self, mock_ics, mock_nc, mock_session_cls):
        from app.routers.requisitions.requirements import _enqueue_ics_nc_batch

        mock_db = MagicMock()
        mock_session_cls.return_value = mock_db
        # Should not raise even when enqueue functions fail
        _enqueue_ics_nc_batch([1])
        mock_db.close.assert_called_once()


# ── GET /api/requirements/{requirement_id}/sightings ──────────────


class TestListRequirementSightings:
    @patch("app.routers.requisitions._enrich_with_vendor_cards")
    def test_list_sightings_basic(self, mock_enrich, client, db_session, test_user, test_requisition):
        req_item = test_requisition.requirements[0]
        _make_sighting(db_session, req_item)
        resp = client.get(f"/api/requirements/{req_item.id}/sightings")
        assert resp.status_code == 200

    def test_list_sightings_not_found(self, client):
        resp = client.get("/api/requirements/99999/sightings")
        assert resp.status_code == 404
