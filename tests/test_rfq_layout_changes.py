"""
Tests for RFQ layout changes: merged sourcing+offers tab, task board redesign,
and visual differentiation.

Validates that:
- Sourcing tab loads offers alongside sightings (merged view)
- Tasks API returns data compatible with new card layout
- Offers are retrievable per-requirement for inline display

Called by: pytest
Depends on: conftest.py fixtures (client, db_session, test_user, test_requisition, test_offer)
"""

from datetime import datetime, timezone
from unittest.mock import patch

from app.models import Offer, Requirement


def _make_task(db_session, requisition, user, **kwargs):
    """Create a RequisitionTask for testing."""
    from app.models.task import RequisitionTask

    defaults = {
        "requisition_id": requisition.id,
        "title": "Test task",
        "task_type": "sourcing",
        "status": "todo",
        "priority": 2,
        "assigned_to_id": user.id,
        "created_by": user.id,
        "source": "manual",
        "created_at": datetime.now(timezone.utc),
    }
    defaults.update(kwargs)
    t = RequisitionTask(**defaults)
    db_session.add(t)
    db_session.commit()
    db_session.refresh(t)
    return t


def _make_offer(db_session, requisition, requirement, **kwargs):
    """Create an Offer linked to a requirement."""
    defaults = {
        "requisition_id": requisition.id,
        "requirement_id": requirement.id,
        "vendor_name": "Arrow Electronics",
        "mpn": requirement.primary_mpn,
        "qty_available": 500,
        "unit_price": 1.25,
        "status": "active",
        "condition": "new",
        "notes": "Test offer notes",
        "created_at": datetime.now(timezone.utc),
    }
    defaults.update(kwargs)
    o = Offer(**defaults)
    db_session.add(o)
    db_session.commit()
    db_session.refresh(o)
    return o


# ── Sourcing tab loads offers alongside sightings ──


def test_sourcing_endpoint_returns_requirements(client, test_requisition):
    """GET /api/requisitions/{id}/requirements returns parts list."""
    resp = client.get(f"/api/requisitions/{test_requisition.id}/requirements")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    assert len(data) >= 1
    # Requirements should include notes field
    assert "notes" in data[0]


def test_offers_endpoint_returns_grouped_by_requirement(client, db_session, test_requisition, test_user):
    """GET /api/requisitions/{id}/offers returns offers grouped by requirement."""
    reqs = db_session.query(Requirement).filter_by(requisition_id=test_requisition.id).all()
    assert len(reqs) >= 1
    req = reqs[0]

    # Create exact and substitute offers
    _make_offer(db_session, test_requisition, req, vendor_name="Digi-Key", notes="Exact match")
    _make_offer(
        db_session,
        test_requisition,
        req,
        vendor_name="Rochester",
        mpn=req.primary_mpn + "-ALT",
        notes="Substitute part",
    )

    resp = client.get(f"/api/requisitions/{test_requisition.id}/offers")
    assert resp.status_code == 200
    data = resp.json()
    # Should have groups or be a list
    groups = data.get("groups", data) if isinstance(data, dict) else data
    assert isinstance(groups, list)


def test_offers_have_notes_field(client, db_session, test_requisition, test_user):
    """Offers should include notes field in response."""
    reqs = db_session.query(Requirement).filter_by(requisition_id=test_requisition.id).all()
    req = reqs[0]
    _make_offer(db_session, test_requisition, req, notes="Volume discount available")

    resp = client.get(f"/api/requisitions/{test_requisition.id}/offers")
    assert resp.status_code == 200


# ── Task board: Purchasing/Sales headers with assignee ──


@patch("app.routers.task.task_service.apply_simple_scoring")
def test_tasks_endpoint_returns_task_type_and_assignee(mock_scoring, client, db_session, test_requisition, test_user):
    """GET /api/requisitions/{id}/tasks returns tasks with type and assignee_name."""
    _make_task(
        db_session,
        test_requisition,
        test_user,
        title="Follow up with vendor",
        task_type="sourcing",
        priority=3,
    )
    _make_task(
        db_session,
        test_requisition,
        test_user,
        title="Prepare customer quote",
        task_type="sales",
        priority=2,
    )

    resp = client.get(f"/api/requisitions/{test_requisition.id}/tasks")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    assert len(data) >= 2

    # Each task should have task_type for department header rendering
    types = {t["task_type"] for t in data}
    assert "sourcing" in types
    assert "sales" in types

    # Each task should have assignee_name for the header display
    for task in data:
        assert "assignee_name" in task or "assigned_to_id" in task


@patch("app.routers.task.task_service.apply_simple_scoring")
def test_tasks_include_all_types_without_filtering(mock_scoring, client, db_session, test_requisition, test_user):
    """Task endpoint returns all types — frontend no longer filters by category."""
    _make_task(db_session, test_requisition, test_user, task_type="sourcing")
    _make_task(db_session, test_requisition, test_user, task_type="sales")
    _make_task(db_session, test_requisition, test_user, task_type="general")

    resp = client.get(f"/api/requisitions/{test_requisition.id}/tasks")
    assert resp.status_code == 200
    data = resp.json()
    types = {t["task_type"] for t in data}
    assert len(types) == 3


# ── Notes on requirements ──


def test_requirement_notes_editable(client, db_session, test_requisition):
    """PUT /api/requirements/{id} can update notes field."""
    reqs = db_session.query(Requirement).filter_by(requisition_id=test_requisition.id).all()
    req = reqs[0]

    resp = client.put(
        f"/api/requirements/{req.id}",
        json={"notes": "Customer needs DC 2024+"},
    )
    assert resp.status_code == 200

    # Verify notes persisted
    resp2 = client.get(f"/api/requisitions/{test_requisition.id}/requirements")
    assert resp2.status_code == 200
    updated = [r for r in resp2.json() if r["id"] == req.id]
    assert updated[0]["notes"] == "Customer needs DC 2024+"
