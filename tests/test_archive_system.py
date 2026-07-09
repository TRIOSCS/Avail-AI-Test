"""test_archive_system.py — Tests for the part-level archive system.

Tests single-part archive/unarchive (Requirement.sourcing_status), bulk
archive/unarchive of parts (optionally scoped by requisition), and the archived
pill filter in the parts list. There is NO requisition-level archive/hide
capability — a requisition ends in Won or Lost (see test_requisition_state) — so
the only archive flag here is the part-level sourcing_status.

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


def test_archive_single_part(client, db_session, test_user):
    """PATCH /v2/partials/parts/{id}/archive sets sourcing_status to archived."""
    req = _make_requisition(db_session, test_user.id)
    part = _make_requirement(db_session, req.id, mpn="ABC123", sourcing_status="open")
    db_session.commit()

    resp = client.patch(f"/v2/partials/parts/{part.id}/archive")
    assert resp.status_code == 200

    db_session.refresh(part)
    assert part.sourcing_status == "archived"


def test_archive_single_part_not_found(client):
    """PATCH nonexistent part returns 404."""
    resp = client.patch("/v2/partials/parts/99999/archive")
    assert resp.status_code == 404


def test_unarchive_single_part(client, db_session, test_user):
    """PATCH /v2/partials/parts/{id}/unarchive restores to open."""
    req = _make_requisition(db_session, test_user.id)
    part = _make_requirement(db_session, req.id, mpn="ABC123", sourcing_status="archived")
    db_session.commit()

    resp = client.patch(f"/v2/partials/parts/{part.id}/unarchive")
    assert resp.status_code == 200

    db_session.refresh(part)
    assert part.sourcing_status == "open"


def test_bulk_archive(client, db_session, test_user):
    """POST /v2/partials/parts/bulk-archive archives mixed part and requisition IDs.

    Requisition IDs cascade to their parts' sourcing_status — there is no requisition-
    level archive flag, so the requisition row itself is unchanged.
    """
    req1 = _make_requisition(db_session, test_user.id, name="REQ-BULK-1")
    p1 = _make_requirement(db_session, req1.id, mpn="BULK-A", sourcing_status="open")

    req2 = _make_requisition(db_session, test_user.id, name="REQ-BULK-2")
    p2 = _make_requirement(db_session, req2.id, mpn="BULK-B", sourcing_status="open")
    p3 = _make_requirement(db_session, req2.id, mpn="BULK-C", sourcing_status="sourcing")
    db_session.commit()

    resp = client.post(
        "/v2/partials/parts/bulk-archive",
        json={"requirement_ids": [p1.id], "requisition_ids": [req2.id]},
    )
    assert resp.status_code == 200

    db_session.refresh(p1)
    assert p1.sourcing_status == "archived"

    # Requisition IDs cascade to their parts only.
    for p in [p2, p3]:
        db_session.refresh(p)
        assert p.sourcing_status == "archived"


def test_bulk_unarchive(client, db_session, test_user):
    """POST /v2/partials/parts/bulk-unarchive restores parts (incl.

    requisition-scoped).
    """
    req = _make_requisition(db_session, test_user.id, name="REQ-BULKUN")
    p1 = _make_requirement(db_session, req.id, mpn="UN-A", sourcing_status="archived")
    p2 = _make_requirement(db_session, req.id, mpn="UN-B", sourcing_status="archived")
    db_session.commit()

    resp = client.post(
        "/v2/partials/parts/bulk-unarchive",
        json={"requirement_ids": [p1.id], "requisition_ids": [req.id]},
    )
    assert resp.status_code == 200

    db_session.refresh(p1)
    assert p1.sourcing_status == "open"

    db_session.refresh(p2)
    assert p2.sourcing_status == "open"


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
