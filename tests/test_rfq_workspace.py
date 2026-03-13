"""
tests/test_rfq_workspace.py — Tests for the part-centric RFQ workspace endpoints.

Covers: enhanced requirement offers listing (current + historical + flags),
quote selection toggle, part-level task merging, requirement history timeline,
and enriched requirements list (chip cluster data, stepper step).

Called by: pytest
Depends on: routers/requisitions/requirements.py, conftest fixtures
"""

from datetime import datetime, timezone

from app.models import ChangeLog, Offer, RequisitionTask

# ── Enhanced Offers Endpoint ──────────────────────────────────────────


def test_requirement_offers_empty(client, test_requisition):
    """GET /api/requirements/{id}/offers returns empty list when no offers."""
    req = test_requisition
    r = req.requirements[0]
    resp = client.get(f"/api/requirements/{r.id}/offers")
    assert resp.status_code == 200
    assert resp.json() == []


def test_requirement_offers_current(client, test_requisition, db_session):
    """Current offers for a requirement are returned with correct fields."""
    req = test_requisition
    r = req.requirements[0]
    offer = Offer(
        requisition_id=req.id,
        requirement_id=r.id,
        vendor_name="Arrow",
        mpn="LM317T",
        qty_available=500,
        unit_price=0.45,
        condition="New",
        status="active",
        source="manual",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(offer)
    db_session.commit()

    resp = client.get(f"/api/requirements/{r.id}/offers")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    o = data[0]
    assert o["vendor_name"] == "Arrow"
    assert o["mpn"] == "LM317T"
    assert o["unit_price"] == 0.45
    assert o["is_historical"] is False
    assert o["is_substitute"] is False
    assert o["selected_for_quote"] is False
    assert "age_days" in o
    assert "entered_by" in o


def test_requirement_offers_includes_selected_state(client, test_requisition, db_session):
    """Offers with selected_for_quote=True are returned with that flag."""
    req = test_requisition
    r = req.requirements[0]
    offer = Offer(
        requisition_id=req.id,
        requirement_id=r.id,
        vendor_name="Digi-Key",
        mpn="LM317T",
        qty_available=1000,
        unit_price=0.40,
        status="active",
        source="manual",
        selected_for_quote=True,
        selected_at=datetime.now(timezone.utc),
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(offer)
    db_session.commit()

    resp = client.get(f"/api/requirements/{r.id}/offers")
    data = resp.json()
    assert len(data) == 1
    assert data[0]["selected_for_quote"] is True
    assert data[0]["selected_at"] is not None


# ── Quote Selection Toggle ────────────────────────────────────────────


def test_toggle_quote_selection(client, test_requisition, db_session):
    """POST /api/offers/{id}/toggle-quote-selection toggles the flag."""
    req = test_requisition
    r = req.requirements[0]
    offer = Offer(
        requisition_id=req.id,
        requirement_id=r.id,
        vendor_name="Arrow",
        mpn="LM317T",
        qty_available=500,
        unit_price=0.45,
        status="active",
        source="manual",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(offer)
    db_session.commit()
    db_session.refresh(offer)

    # Select
    resp = client.post(f"/api/offers/{offer.id}/toggle-quote-selection")
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["selected_for_quote"] is True

    # Deselect
    resp2 = client.post(f"/api/offers/{offer.id}/toggle-quote-selection")
    assert resp2.status_code == 200
    assert resp2.json()["selected_for_quote"] is False


def test_toggle_quote_selection_not_found(client):
    """Toggle on non-existent offer returns 404."""
    resp = client.post("/api/offers/99999/toggle-quote-selection")
    assert resp.status_code == 404


# ── Enhanced Tasks Endpoint ───────────────────────────────────────────


def test_requirement_tasks_merges_offer_tasks(client, test_requisition, db_session):
    """GET /api/requirements/{id}/tasks merges part and offer tasks."""
    req = test_requisition
    r = req.requirements[0]

    # Part-level task
    t1 = RequisitionTask(
        requisition_id=req.id,
        title="Follow up with vendor",
        task_type="sourcing",
        status="todo",
        source="manual",
        source_ref=f"requirement:{r.id}",
        created_by=req.created_by,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(t1)

    # Create an offer and an offer-level task
    offer = Offer(
        requisition_id=req.id,
        requirement_id=r.id,
        vendor_name="Arrow",
        mpn="LM317T",
        qty_available=500,
        unit_price=0.45,
        status="active",
        source="manual",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(offer)
    db_session.flush()

    t2 = RequisitionTask(
        requisition_id=req.id,
        title="Verify Arrow price",
        task_type="sales",
        status="in_progress",
        source="manual",
        source_ref=f"offer:{offer.id}",
        created_by=req.created_by,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(t2)
    db_session.commit()

    resp = client.get(f"/api/requirements/{r.id}/tasks")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 2
    titles = {t["title"] for t in data}
    assert "Follow up with vendor" in titles
    assert "Verify Arrow price" in titles
    # Check enriched fields
    assert "task_type" in data[0]
    assert "assigned_to" in data[0]
    assert "source_ref" in data[0]


def test_requirement_tasks_include_assigned_to(client, test_requisition, db_session):
    """Part-task list includes assigned_to field for RFQ UI."""
    req = test_requisition
    r = req.requirements[0]
    t = RequisitionTask(
        requisition_id=req.id,
        title="Alias field task",
        task_type="general",
        status="todo",
        source="manual",
        source_ref=f"requirement:{r.id}",
        assigned_to_id=req.created_by,
        created_by=req.created_by,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(t)
    db_session.commit()

    resp = client.get(f"/api/requirements/{r.id}/tasks")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) >= 1
    row = next(x for x in data if x["id"] == t.id)
    assert "assigned_to" in row
    assert row["assigned_to"] is not None


def test_create_requirement_task_persists_assignment(client, test_requisition, test_user):
    """Creating part task preserves assigned_to_id."""
    req = test_requisition
    r = req.requirements[0]

    create_resp = client.post(
        f"/api/requirements/{r.id}/tasks",
        json={
            "title": "RFQ follow-up task",
            "assigned_to_id": test_user.id,
        },
    )
    assert create_resp.status_code == 200

    list_resp = client.get(f"/api/requirements/{r.id}/tasks")
    assert list_resp.status_code == 200
    rows = list_resp.json()
    created = next(x for x in rows if x["title"] == "RFQ follow-up task")
    assert created["assigned_to"] is not None


# ── Requirement History Timeline ──────────────────────────────────────


def test_requirement_history(client, test_requisition, db_session):
    """GET /api/requirements/{id}/history returns timeline events."""
    req = test_requisition
    r = req.requirements[0]

    # Add a change log entry
    cl = ChangeLog(
        entity_type="requirement",
        entity_id=r.id,
        user_id=req.created_by,
        field_name="target_qty",
        old_value="1000",
        new_value="2000",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(cl)

    # Add an offer
    offer = Offer(
        requisition_id=req.id,
        requirement_id=r.id,
        vendor_name="Mouser",
        mpn="LM317T",
        qty_available=2000,
        unit_price=0.50,
        status="active",
        source="manual",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(offer)
    db_session.commit()

    resp = client.get(f"/api/requirements/{r.id}/history")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) >= 2  # At least the change + offer_created
    types = {e["type"] for e in data}
    assert "change" in types
    assert "offer_created" in types


def test_requirement_history_not_found(client):
    """History on non-existent requirement returns 404."""
    resp = client.get("/api/requirements/99999/history")
    assert resp.status_code == 404


# ── Enhanced Requirements List (Chip Cluster + Stepper) ───────────────


def test_list_requirements_enriched(client, test_requisition, db_session):
    """GET /api/requisitions/{id}/requirements returns chip cluster data."""
    req = test_requisition
    r = req.requirements[0]

    # Add an offer to get step='offers'
    offer = Offer(
        requisition_id=req.id,
        requirement_id=r.id,
        vendor_name="Arrow",
        mpn="LM317T",
        qty_available=500,
        unit_price=0.45,
        status="active",
        source="manual",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(offer)
    db_session.commit()

    resp = client.get(f"/api/requisitions/{req.id}/requirements")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    p = data[0]
    assert p["offer_count"] == 1
    assert "selected_count" in p
    assert "task_count" in p
    assert "step" in p
    assert p["step"] == "offers"


def test_list_requirements_step_new(client, test_requisition):
    """Requirements with no sightings or offers have step='new'."""
    req = test_requisition
    resp = client.get(f"/api/requisitions/{req.id}/requirements")
    data = resp.json()
    assert data[0]["step"] == "new"


# ── Standalone Notes Endpoint ─────────────────────────────────────────


def test_requirement_notes_empty(client, test_requisition):
    """GET /api/requirements/{id}/notes returns empty data when no notes."""
    req = test_requisition
    r = req.requirements[0]
    resp = client.get(f"/api/requirements/{r.id}/notes")
    assert resp.status_code == 200
    data = resp.json()
    assert "requirement_notes" in data or "notes" in data


def test_requirement_notes_add_and_list(client, test_requisition):
    """POST then GET /api/requirements/{id}/notes round-trip."""
    req = test_requisition
    r = req.requirements[0]
    resp = client.post(
        f"/api/requirements/{r.id}/notes",
        json={"text": "Test note for standalone tab"},
    )
    assert resp.status_code == 200

    resp2 = client.get(f"/api/requirements/{r.id}/notes")
    assert resp2.status_code == 200
    data = resp2.json()
    assert "Test note for standalone tab" in str(data)


# ── Standalone Tasks Endpoint ─────────────────────────────────────────


def test_requirement_tasks_empty(client, test_requisition):
    """GET /api/requirements/{id}/tasks returns empty list when no tasks."""
    req = test_requisition
    r = req.requirements[0]
    resp = client.get(f"/api/requirements/{r.id}/tasks")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
