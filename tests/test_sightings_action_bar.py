"""test_sightings_action_bar.py — Sightings multi-select action-bar UX fixes.

Covers two UX-audit fixes on the sightings board action bar:
  Fix 1 — the bulk "Assign to buyer" dropdown (wired to the previously-orphaned
          POST /v2/partials/sightings/batch-assign endpoint) with a buyer picker.
  Fix 2 — batch-assign / batch-status / batch-notes re-render #sightings-table (they
          used to return an empty body + hx-swap="none", leaving stale rows).

Called by: pytest
Depends on: app/routers/sightings.py, app/templates/htmx/partials/sightings/table.html,
            tests/conftest.py
"""

import json
import os

os.environ["TESTING"] = "1"

from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.constants import UserRole

# ── Fixtures / helpers ───────────────────────────────────────────


def _make_req_and_requirement(db: Session, user_id: int, mpn: str = "LM317T"):
    from app.models import Requirement, Requisition

    req = Requisition(name="Test Req", status="open", created_by=user_id)
    db.add(req)
    db.flush()

    requirement = Requirement(
        requisition_id=req.id,
        primary_mpn=mpn,
        normalized_mpn=mpn.upper(),
        target_qty=10,
        sourcing_status="open",
    )
    db.add(requirement)
    db.flush()
    return req, requirement


# ── Fix 1: Assign dropdown renders with a buyer picker ────────────


def test_action_bar_renders_assign_dropdown_with_buyer_picker(client, db_session, test_user):
    """The multi-select action bar exposes an Assign dropdown wired to batch-assign,
    with a buyer picker listing assignable (active buyer/trader) users."""
    _make_req_and_requirement(db_session, test_user.id, mpn="ASSIGN-MPN")
    db_session.commit()

    resp = client.get("/v2/partials/sightings")
    assert resp.status_code == 200
    body = resp.text

    # Wired to the (formerly orphaned) batch-assign endpoint via a buyer picker.
    assert "/v2/partials/sightings/batch-assign" in body
    assert 'name="buyer_id"' in body
    assert "Assign to buyer" in body
    # test_user is an active buyer → appears as a selectable option.
    assert test_user.name in body


def test_assign_dropdown_lists_only_active_buyers_and_traders(client, db_session, test_user):
    """The buyer picker lists active buyers/traders and excludes sales/inactive
    users."""
    from app.models import User

    trader = User(
        email="trader@test.com",
        name="Trader Tina",
        role=UserRole.TRADER,
        azure_id="trader-azure",
        is_active=True,
        created_at=datetime.now(UTC),
    )
    sales = User(
        email="sales@test.com",
        name="Sales Sam",
        role=UserRole.SALES,
        azure_id="sales-azure",
        is_active=True,
        created_at=datetime.now(UTC),
    )
    inactive_buyer = User(
        email="inactive@test.com",
        name="Inactive Ivan",
        role=UserRole.BUYER,
        azure_id="inactive-azure",
        is_active=False,
        created_at=datetime.now(UTC),
    )
    db_session.add_all([trader, sales, inactive_buyer])
    _make_req_and_requirement(db_session, test_user.id)
    db_session.commit()

    body = client.get("/v2/partials/sightings").text
    assert "Trader Tina" in body  # active trader — assignable
    assert "Sales Sam" not in body  # sales — not an assignment target
    assert "Inactive Ivan" not in body  # inactive — excluded


# ── Fix 1 + 2: batch-assign assigns and re-renders the table ──────


def test_batch_assign_assigns_and_rerenders_table(client, db_session, test_user):
    """Batch-assign sets the buyer AND returns the re-rendered #sightings-table (not an
    empty body), so the assigned rows refresh in place."""
    _, requirement = _make_req_and_requirement(db_session, test_user.id, mpn="REASSIGN-MPN")
    db_session.commit()

    resp = client.post(
        "/v2/partials/sightings/batch-assign",
        data={
            "requirement_ids": json.dumps([requirement.id]),
            "buyer_id": str(test_user.id),
        },
        headers={"HX-Target": "sightings-table"},
    )

    assert resp.status_code == 200
    # Buyer applied.
    db_session.refresh(requirement)
    assert requirement.assigned_buyer_id == test_user.id
    # Re-rendered table (not the old empty body) — contains the row + table chrome.
    assert resp.text.strip() != ""
    assert "REASSIGN-MPN" in resp.text
    assert "sightingSelection" in resp.text
    # Toast still fires via the HX-Trigger bridge.
    assert "Assigned" in resp.headers.get("HX-Trigger", "")


# ── Fix 2: batch-status re-renders the table ──────────────────────


def test_batch_status_rerenders_table(client, db_session, test_user):
    """Batch-status updates status AND returns the re-rendered table (not an empty
    body), so rows no longer show a stale status."""
    _, requirement = _make_req_and_requirement(db_session, test_user.id, mpn="STATUS-MPN")
    requirement.sourcing_status = "open"
    db_session.commit()

    resp = client.post(
        "/v2/partials/sightings/batch-status",
        data={
            "requirement_ids": json.dumps([requirement.id]),
            "status": "sourcing",
        },
        headers={"HX-Target": "sightings-table"},
    )

    assert resp.status_code == 200
    db_session.refresh(requirement)
    assert requirement.sourcing_status == "sourcing"
    # Re-rendered table body (not empty) — contains the row + table chrome.
    assert resp.text.strip() != ""
    assert "STATUS-MPN" in resp.text
    assert "sightingSelection" in resp.text
    assert "Updated" in resp.headers.get("HX-Trigger", "")


# ── Fix 2: batch-notes re-renders the table ───────────────────────


def test_batch_notes_rerenders_table(client, db_session, test_user):
    """Batch-notes logs the note AND returns the re-rendered table (not an empty
    body)."""
    from app.models import ActivityLog

    _, requirement = _make_req_and_requirement(db_session, test_user.id, mpn="NOTES-MPN")
    db_session.commit()

    resp = client.post(
        "/v2/partials/sightings/batch-notes",
        data={
            "requirement_ids": json.dumps([requirement.id]),
            "notes": "Called vendor, awaiting quote",
        },
        headers={"HX-Target": "sightings-table"},
    )

    assert resp.status_code == 200
    log = db_session.query(ActivityLog).filter_by(requirement_id=requirement.id).first()
    assert log is not None
    # Re-rendered table body (not empty) — contains the row + table chrome.
    assert resp.text.strip() != ""
    assert "NOTES-MPN" in resp.text
    assert "sightingSelection" in resp.text
    assert "Added note" in resp.headers.get("HX-Trigger", "")


# ── Auth parity with the board (requisition-ownership scoping) ────


def test_batch_assign_blocks_non_owner_sales(client, db_session, test_user, admin_user):
    """A SALES user may only act on requisitions they created — batch-assign returns 404
    for a non-owned requisition, matching the board's ownership scoping (existence not
    leaked)."""
    _, requirement = _make_req_and_requirement(db_session, admin_user.id, mpn="OWNED-BY-ADMIN")
    test_user.role = UserRole.SALES
    db_session.commit()

    resp = client.post(
        "/v2/partials/sightings/batch-assign",
        data={
            "requirement_ids": json.dumps([requirement.id]),
            "buyer_id": str(admin_user.id),
        },
    )
    assert resp.status_code == 404
    # Not reassigned.
    db_session.refresh(requirement)
    assert requirement.assigned_buyer_id is None


def test_batch_status_blocks_non_owner_sales(client, db_session, test_user, admin_user):
    """Batch-status enforces the same requisition-ownership scoping as the board."""
    _, requirement = _make_req_and_requirement(db_session, admin_user.id)
    requirement.sourcing_status = "open"
    test_user.role = UserRole.SALES
    db_session.commit()

    resp = client.post(
        "/v2/partials/sightings/batch-status",
        data={
            "requirement_ids": json.dumps([requirement.id]),
            "status": "sourcing",
        },
    )
    assert resp.status_code == 404
    db_session.refresh(requirement)
    assert requirement.sourcing_status == "open"
