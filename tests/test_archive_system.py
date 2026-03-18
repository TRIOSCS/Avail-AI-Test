"""test_archive_system.py — Tests for the archive system.

Tests single-part archive/unarchive, whole-requisition archive (cascading to
children), bulk archive/unarchive, and the archived pill filter in parts list.

Called by: pytest
Depends on: conftest fixtures (client, db_session, test_user, test_requisition)
"""

from datetime import datetime, timezone

from app.models import Requirement, Requisition
from tests.conftest import engine  # noqa: F401


def _make_requisition(db, user_id, name="REQ-ARCH-001", status="active"):
    """Helper to create a requisition with requirements."""
    req = Requisition(
        name=name,
        customer_name="Test Co",
        status=status,
        created_by=user_id,
        created_at=datetime.now(timezone.utc),
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
        created_at=datetime.now(timezone.utc),
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


def test_archive_whole_requisition(client, db_session, test_user):
    """PATCH /v2/partials/requisitions/{id}/archive cascades to all children."""
    req = _make_requisition(db_session, test_user.id, name="REQ-CASCADE")
    p1 = _make_requirement(db_session, req.id, mpn="PART-A", sourcing_status="open")
    p2 = _make_requirement(db_session, req.id, mpn="PART-B", sourcing_status="sourcing")
    p3 = _make_requirement(db_session, req.id, mpn="PART-C", sourcing_status="offered")
    db_session.commit()

    resp = client.patch(f"/v2/partials/requisitions/{req.id}/archive")
    assert resp.status_code == 200

    db_session.refresh(req)
    assert req.status == "archived"

    for p in [p1, p2, p3]:
        db_session.refresh(p)
        assert p.sourcing_status == "archived"


def test_unarchive_whole_requisition(client, db_session, test_user):
    """PATCH /v2/partials/requisitions/{id}/unarchive restores requisition and parts."""
    req = _make_requisition(db_session, test_user.id, name="REQ-UNARCH", status="archived")
    p1 = _make_requirement(db_session, req.id, mpn="PART-A", sourcing_status="archived")
    p2 = _make_requirement(db_session, req.id, mpn="PART-B", sourcing_status="archived")
    db_session.commit()

    resp = client.patch(f"/v2/partials/requisitions/{req.id}/unarchive")
    assert resp.status_code == 200

    db_session.refresh(req)
    assert req.status == "active"

    for p in [p1, p2]:
        db_session.refresh(p)
        assert p.sourcing_status == "open"


def test_bulk_archive(client, db_session, test_user):
    """POST /v2/partials/parts/bulk-archive archives mixed part and requisition IDs."""
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

    db_session.refresh(req2)
    assert req2.status == "archived"

    for p in [p2, p3]:
        db_session.refresh(p)
        assert p.sourcing_status == "archived"


def test_bulk_unarchive(client, db_session, test_user):
    """POST /v2/partials/parts/bulk-unarchive restores parts and requisitions."""
    req = _make_requisition(db_session, test_user.id, name="REQ-BULKUN", status="archived")
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

    db_session.refresh(req)
    assert req.status == "active"

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
