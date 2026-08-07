"""test_archive_system.py — Tests for archived-part visibility in the parts list.

Tests the archived pill filter and default archived-exclusion in
GET /v2/partials/parts (Requirement.sourcing_status). The archive/unarchive
WRITE endpoints died with the split-panel workspace (spec §5.1) — archiving now
happens from the requisition detail editor. There is NO requisition-level
archive/hide capability — a requisition ends in Won or Lost (see
test_requisition_state) — so the only archive flag here is the part-level
sourcing_status.

Called by: pytest
Depends on: conftest fixtures (client, db_session, test_user, test_requisition)
"""

from datetime import UTC, datetime

from app.models import Requirement, Requisition
from tests.conftest import engine  # noqa: F401


def _make_requisition(db, user_id, name="REQ-ARCH-001", status="open"):
    """Helper to create a requisition with requirements."""
    req = Requisition(
        name=name,
        customer_name="Test Co",
        status=status,
        created_by=user_id,
        created_at=datetime.now(UTC),
    )
    db.add(req)
    db.flush()
    return req


def _make_requirement(db, requisition_id, mpn="LM317T", sourcing_status="open"):
    """Helper to create a requirement."""
    item = Requirement(
        requisition_id=requisition_id,
        primary_mpn=mpn,
        target_qty=1000,
        sourcing_status=sourcing_status,
        created_at=datetime.now(UTC),
    )
    db.add(item)
    db.flush()
    return item


def test_archived_pill_filter(client, db_session, test_user):
    """GET /v2/partials/parts?status=archived shows only archived parts."""
    req = _make_requisition(db_session, test_user.id, name="REQ-FILT")
    _make_requirement(db_session, req.id, mpn="ACTIVE-PART", sourcing_status="open")
    _make_requirement(db_session, req.id, mpn="ARCHIVED-PART", sourcing_status="archived")
    db_session.commit()

    resp = client.get("/v2/partials/parts?status=archived")
    assert resp.status_code == 200
    text = resp.text
    assert "ARCHIVED-PART" in text
    assert "ACTIVE-PART" not in text


def test_non_archived_filter_excludes_archived(client, db_session, test_user):
    """GET /v2/partials/parts (no status filter) excludes archived parts by default."""
    req = _make_requisition(db_session, test_user.id, name="REQ-EXCL")
    _make_requirement(db_session, req.id, mpn="VISIBLE-PART", sourcing_status="open")
    _make_requirement(db_session, req.id, mpn="HIDDEN-ARCH", sourcing_status="archived")
    db_session.commit()

    resp = client.get("/v2/partials/parts")
    assert resp.status_code == 200
    text = resp.text
    assert "VISIBLE-PART" in text
    assert "HIDDEN-ARCH" not in text
