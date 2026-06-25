"""Regression tests for the 8 endpoints the first apply pass missed (caught by re-
audit).

Covers: follow-up send (email), sourcing-lead status/feedback, sightings batch
assign/status/notes, requisitions2 bulk action, and core batch-archive. A restricted
non-owner must be unable to act on another user's requisition-scoped resource.
"""

import json
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.constants import UserRole
from app.models.offers import Contact
from app.models.sourcing import Requirement
from app.models.sourcing_lead import SourcingLead


def _requirement(db: Session, requisition) -> Requirement:
    return db.query(Requirement).filter(Requirement.requisition_id == requisition.id).first()


def _as_sales_non_owner(db, test_user, test_requisition, admin_user):
    test_user.role = UserRole.SALES
    test_requisition.created_by = admin_user.id  # owned by someone else
    db.commit()


def _make_contact(db, requisition_id, user_id) -> Contact:
    c = Contact(
        requisition_id=requisition_id,
        user_id=user_id,
        contact_type="rfq",
        vendor_name="Acme",
        vendor_contact="sales@acme.example",
        subject="RFQ",
        status="sent",
        created_at=datetime.now(timezone.utc),
    )
    db.add(c)
    db.commit()
    db.refresh(c)
    return c


def _make_lead(db, requirement_id, requisition_id) -> SourcingLead:
    lead = SourcingLead(
        lead_id=f"L-{requirement_id}-{requisition_id}",
        requirement_id=requirement_id,
        requisition_id=requisition_id,
        part_number_requested="P1",
        part_number_matched="P1",
        vendor_name="VendorCo",
        vendor_name_normalized="vendorco",
        primary_source_type="broker",
        primary_source_name="src",
    )
    db.add(lead)
    db.commit()
    db.refresh(lead)
    return lead


# ── follow-up send (sends email) ─────────────────────────────────────────────
def test_send_follow_up_blocks_non_owner_sales(client, db_session, test_requisition, test_user, admin_user):
    _as_sales_non_owner(db_session, test_user, test_requisition, admin_user)
    c = _make_contact(db_session, test_requisition.id, admin_user.id)
    resp = client.post(f"/v2/partials/follow-ups/{c.id}/send", data={"body": "should not send"})
    assert resp.status_code == 404


# ── sourcing leads ───────────────────────────────────────────────────────────
def test_lead_status_blocks_non_owner_sales(client, db_session, test_requisition, test_user, admin_user):
    req = _requirement(db_session, test_requisition)
    _as_sales_non_owner(db_session, test_user, test_requisition, admin_user)
    lead = _make_lead(db_session, req.id, test_requisition.id)
    resp = client.post(f"/v2/partials/sourcing/leads/{lead.id}/status", data={"status": "contacted"})
    assert resp.status_code == 404


def test_lead_feedback_blocks_non_owner_sales(client, db_session, test_requisition, test_user, admin_user):
    req = _requirement(db_session, test_requisition)
    _as_sales_non_owner(db_session, test_user, test_requisition, admin_user)
    lead = _make_lead(db_session, req.id, test_requisition.id)
    resp = client.post(f"/v2/partials/sourcing/leads/{lead.id}/feedback", data={"note": "x"})
    assert resp.status_code == 404


# ── sightings batch (form-body multi-id) ─────────────────────────────────────
def test_batch_assign_blocks_non_owner_sales(client, db_session, test_requisition, test_user, admin_user):
    req = _requirement(db_session, test_requisition)
    _as_sales_non_owner(db_session, test_user, test_requisition, admin_user)
    resp = client.post(
        "/v2/partials/sightings/batch-assign",
        data={"requirement_ids": json.dumps([req.id]), "buyer_id": str(admin_user.id)},
    )
    assert resp.status_code == 404
    db_session.refresh(req)
    assert req.assigned_buyer_id != admin_user.id


def test_batch_status_blocks_non_owner_sales(client, db_session, test_requisition, test_user, admin_user):
    req = _requirement(db_session, test_requisition)
    _as_sales_non_owner(db_session, test_user, test_requisition, admin_user)
    resp = client.post(
        "/v2/partials/sightings/batch-status",
        data={"requirement_ids": json.dumps([req.id]), "status": "sourcing"},
    )
    assert resp.status_code == 404


def test_batch_notes_blocks_non_owner_sales(client, db_session, test_requisition, test_user, admin_user):
    req = _requirement(db_session, test_requisition)
    _as_sales_non_owner(db_session, test_user, test_requisition, admin_user)
    resp = client.post(
        "/v2/partials/sightings/batch-notes",
        data={"requirement_ids": json.dumps([req.id]), "notes": "hi"},
    )
    assert resp.status_code == 404


# ── requisitions2 bulk + core batch-archive now restrict TRADER too ──────────
def test_bulk_action_silently_excludes_non_owner_trader(client, db_session, test_requisition, test_user, admin_user):
    test_user.role = UserRole.TRADER
    test_requisition.created_by = admin_user.id
    db_session.commit()
    client.post("/requisitions2/bulk/archive", data={"ids": str(test_requisition.id)})
    db_session.refresh(test_requisition)
    assert test_requisition.status != "archived"  # trader non-owner cannot archive it


def test_batch_archive_excludes_non_owner_trader(client, db_session, test_requisition, test_user, admin_user):
    test_user.role = UserRole.TRADER
    test_requisition.created_by = admin_user.id
    db_session.commit()
    resp = client.put("/api/requisitions/batch-archive", json={"ids": [test_requisition.id]})
    assert resp.status_code == 200
    db_session.refresh(test_requisition)
    assert test_requisition.status != "archived"


# ── parts bulk archive/unarchive + sightings batch-refresh (round-2 misses) ──
def test_parts_bulk_archive_blocks_non_owner_sales(client, db_session, test_requisition, test_user, admin_user):
    _as_sales_non_owner(db_session, test_user, test_requisition, admin_user)
    resp = client.post(
        "/v2/partials/parts/bulk-archive",
        json={"requisition_ids": [test_requisition.id], "requirement_ids": []},
    )
    assert resp.status_code == 404
    db_session.refresh(test_requisition)
    assert test_requisition.status != "archived"


def test_parts_bulk_unarchive_blocks_non_owner_sales(client, db_session, test_requisition, test_user, admin_user):
    _as_sales_non_owner(db_session, test_user, test_requisition, admin_user)
    resp = client.post(
        "/v2/partials/parts/bulk-unarchive",
        json={"requisition_ids": [test_requisition.id], "requirement_ids": []},
    )
    assert resp.status_code == 404


def test_batch_refresh_blocks_non_owner_sales(client, db_session, test_requisition, test_user, admin_user):
    req = _requirement(db_session, test_requisition)
    _as_sales_non_owner(db_session, test_user, test_requisition, admin_user)
    resp = client.post(
        "/v2/partials/sightings/batch-refresh",
        data={"requirement_ids": json.dumps([req.id])},
    )
    assert resp.status_code == 404


def test_preview_inquiry_blocks_non_owner_sales(client, db_session, test_requisition, test_user, admin_user):
    req = _requirement(db_session, test_requisition)
    _as_sales_non_owner(db_session, test_user, test_requisition, admin_user)
    resp = client.post(
        "/v2/partials/sightings/preview-inquiry",
        data={"requirement_ids": str(req.id), "vendor_names": "Acme"},
    )
    assert resp.status_code == 404
