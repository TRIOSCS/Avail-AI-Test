"""Authz regression tests for requisition/requirement attachment deletion.

Covers the requisition-ownership IDOR guards added to the DELETE endpoints in
app/routers/requisitions/attachments.py:

- DELETE /api/requisition-attachments/{att_id}   (HIGH)
- DELETE /api/requirement-attachments/{att_id}   (HIGH)

A restricted SALES/TRADER user who does NOT own the parent requisition must get
a 404 BEFORE any OneDrive deletion or DB delete happens. Buyer/admin (owner or
unrestricted) keep working.
"""

from datetime import datetime, timezone

from app.constants import UserRole
from app.models import (
    Requirement,
    RequirementAttachment,
    RequisitionAttachment,
)


def _make_req_attachment(db_session, requisition_id):
    att = RequisitionAttachment(
        requisition_id=requisition_id,
        file_name="spec.pdf",
        library_item_id=None,  # no cloud call; isolates the authz guard
        library_web_url="https://example.com/spec.pdf",
        content_type="application/pdf",
        size_bytes=123,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(att)
    db_session.commit()
    db_session.refresh(att)
    return att


def _make_requirement_attachment(db_session, requirement_id):
    att = RequirementAttachment(
        requirement_id=requirement_id,
        file_name="datasheet.pdf",
        library_item_id=None,
        library_web_url="https://example.com/datasheet.pdf",
        content_type="application/pdf",
        size_bytes=456,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(att)
    db_session.commit()
    db_session.refresh(att)
    return att


# ── DELETE /api/requisition-attachments/{att_id} ──────────────────────────


def test_delete_requisition_attachment_blocks_non_owner_sales(
    client, db_session, test_requisition, test_user, admin_user
):
    """SALES non-owner gets 404 and the attachment is NOT deleted."""
    test_user.role = UserRole.SALES
    test_requisition.created_by = admin_user.id  # owned by someone else
    db_session.commit()

    att = _make_req_attachment(db_session, test_requisition.id)

    resp = client.delete(f"/api/requisition-attachments/{att.id}")
    assert resp.status_code == 404
    # Guard ran before db.delete — record still present.
    assert db_session.get(RequisitionAttachment, att.id) is not None


def test_delete_requisition_attachment_blocks_non_owner_trader(
    client, db_session, test_requisition, test_user, admin_user
):
    """TRADER is restricted the same way as SALES."""
    test_user.role = UserRole.TRADER
    test_requisition.created_by = admin_user.id
    db_session.commit()

    att = _make_req_attachment(db_session, test_requisition.id)

    resp = client.delete(f"/api/requisition-attachments/{att.id}")
    assert resp.status_code == 404
    assert db_session.get(RequisitionAttachment, att.id) is not None


def test_delete_requisition_attachment_allows_owner_sales(client, db_session, test_requisition, test_user):
    """SALES owner (created_by == user) may delete their own attachment."""
    test_user.role = UserRole.SALES
    test_requisition.created_by = test_user.id
    db_session.commit()

    att = _make_req_attachment(db_session, test_requisition.id)

    resp = client.delete(f"/api/requisition-attachments/{att.id}")
    assert resp.status_code == 200
    assert db_session.get(RequisitionAttachment, att.id) is None


def test_delete_requisition_attachment_allows_buyer(client, db_session, test_requisition, test_user, admin_user):
    """Unrestricted buyer can delete regardless of ownership (happy path)."""
    test_user.role = UserRole.BUYER
    test_requisition.created_by = admin_user.id
    db_session.commit()

    att = _make_req_attachment(db_session, test_requisition.id)

    resp = client.delete(f"/api/requisition-attachments/{att.id}")
    assert resp.status_code == 200
    assert db_session.get(RequisitionAttachment, att.id) is None


# ── DELETE /api/requirement-attachments/{att_id} ──────────────────────────


def _requirement_id(db_session, requisition_id):
    req = db_session.query(Requirement).filter(Requirement.requisition_id == requisition_id).first()
    assert req is not None
    return req.id


def test_delete_requirement_attachment_blocks_non_owner_sales(
    client, db_session, test_requisition, test_user, admin_user
):
    """SALES non-owner gets 404 via the requirement->requisition FK chain."""
    test_user.role = UserRole.SALES
    test_requisition.created_by = admin_user.id
    db_session.commit()

    req_id = _requirement_id(db_session, test_requisition.id)
    att = _make_requirement_attachment(db_session, req_id)

    resp = client.delete(f"/api/requirement-attachments/{att.id}")
    assert resp.status_code == 404
    assert db_session.get(RequirementAttachment, att.id) is not None


def test_delete_requirement_attachment_blocks_non_owner_trader(
    client, db_session, test_requisition, test_user, admin_user
):
    test_user.role = UserRole.TRADER
    test_requisition.created_by = admin_user.id
    db_session.commit()

    req_id = _requirement_id(db_session, test_requisition.id)
    att = _make_requirement_attachment(db_session, req_id)

    resp = client.delete(f"/api/requirement-attachments/{att.id}")
    assert resp.status_code == 404
    assert db_session.get(RequirementAttachment, att.id) is not None


def test_delete_requirement_attachment_allows_owner_sales(client, db_session, test_requisition, test_user):
    test_user.role = UserRole.SALES
    test_requisition.created_by = test_user.id
    db_session.commit()

    req_id = _requirement_id(db_session, test_requisition.id)
    att = _make_requirement_attachment(db_session, req_id)

    resp = client.delete(f"/api/requirement-attachments/{att.id}")
    assert resp.status_code == 200
    assert db_session.get(RequirementAttachment, att.id) is None


def test_delete_requirement_attachment_allows_buyer(client, db_session, test_requisition, test_user, admin_user):
    test_user.role = UserRole.BUYER
    test_requisition.created_by = admin_user.id
    db_session.commit()

    req_id = _requirement_id(db_session, test_requisition.id)
    att = _make_requirement_attachment(db_session, req_id)

    resp = client.delete(f"/api/requirement-attachments/{att.id}")
    assert resp.status_code == 200
    assert db_session.get(RequirementAttachment, att.id) is None
