"""
tests/test_attachments.py — Tests for Requisition & Requirement Attachments

Covers: list, upload, delete, not-found, too-large, and OneDrive error handling.

Called by: pytest
Depends on: routers/requisitions.py, conftest fixtures
"""

from io import BytesIO
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import (
    Requirement,
    RequirementAttachment,
    Requisition,
    RequisitionAttachment,
    User,
)


@pytest.fixture()
def att_client(db_session: Session, test_user: User) -> TestClient:
    """TestClient with auth overrides and access_token set."""
    from app.database import get_db
    from app.dependencies import require_buyer, require_user
    from app.main import app

    # Set access_token on test user for OneDrive operations
    test_user.access_token = "fake-token-for-testing"
    db_session.commit()

    def _override_db():
        yield db_session

    def _override_user():
        return test_user

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[require_user] = _override_user
    app.dependency_overrides[require_buyer] = _override_user

    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


# ══════════════════════════════════════════════════════════════════════
# Requisition Attachments
# ══════════════════════════════════════════════════════════════════════


def test_list_requisition_attachments_empty(att_client, test_requisition):
    """GET /api/requisitions/{id}/attachments returns empty list when no files."""
    resp = att_client.get(f"/api/requisitions/{test_requisition.id}/attachments")
    assert resp.status_code == 200
    assert resp.json() == []


def test_list_requisition_attachments_with_data(
    att_client, db_session, test_requisition, test_user
):
    """Attachments appear in the list response."""
    att = RequisitionAttachment(
        requisition_id=test_requisition.id,
        file_name="datasheet.pdf",
        onedrive_url="https://onedrive.example.com/file",
        content_type="application/pdf",
        size_bytes=1024,
        uploaded_by_id=test_user.id,
    )
    db_session.add(att)
    db_session.commit()
    resp = att_client.get(f"/api/requisitions/{test_requisition.id}/attachments")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["file_name"] == "datasheet.pdf"
    assert data[0]["content_type"] == "application/pdf"


def test_list_requisition_attachments_not_found(att_client):
    """GET /api/requisitions/999/attachments returns 404."""
    resp = att_client.get("/api/requisitions/999/attachments")
    assert resp.status_code == 404


@patch("app.http_client.http")
def test_upload_requisition_attachment(mock_http, att_client, test_requisition):
    """POST /api/requisitions/{id}/attachments uploads to OneDrive."""
    mock_resp = MagicMock()
    mock_resp.status_code = 201
    mock_resp.json.return_value = {"id": "od-item-123", "webUrl": "https://onedrive.example.com/file"}
    mock_http.put = AsyncMock(return_value=mock_resp)

    file_content = b"fake file content"
    resp = att_client.post(
        f"/api/requisitions/{test_requisition.id}/attachments",
        files={"file": ("test.pdf", BytesIO(file_content), "application/pdf")},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["file_name"] == "test.pdf"
    assert data["onedrive_url"] == "https://onedrive.example.com/file"


def test_upload_requisition_attachment_not_found(att_client):
    """Upload to non-existent requisition returns 404."""
    resp = att_client.post(
        "/api/requisitions/999/attachments",
        files={"file": ("test.pdf", BytesIO(b"data"), "application/pdf")},
    )
    assert resp.status_code == 404


@patch("app.http_client.http")
def test_upload_requisition_attachment_too_large(mock_http, att_client, test_requisition):
    """Upload > 10 MB returns 400."""
    big_content = b"x" * (10 * 1024 * 1024 + 1)
    resp = att_client.post(
        f"/api/requisitions/{test_requisition.id}/attachments",
        files={"file": ("big.bin", BytesIO(big_content), "application/octet-stream")},
    )
    assert resp.status_code == 400


@patch("app.http_client.http")
def test_upload_requisition_onedrive_error(mock_http, att_client, test_requisition):
    """OneDrive upload failure returns 502."""
    mock_resp = MagicMock()
    mock_resp.status_code = 500
    mock_resp.text = "Internal Server Error"
    mock_http.put = AsyncMock(return_value=mock_resp)

    resp = att_client.post(
        f"/api/requisitions/{test_requisition.id}/attachments",
        files={"file": ("test.pdf", BytesIO(b"data"), "application/pdf")},
    )
    assert resp.status_code == 502


def test_delete_requisition_attachment(att_client, db_session, test_requisition, test_user):
    """DELETE /api/requisition-attachments/{id} removes the record."""
    att = RequisitionAttachment(
        requisition_id=test_requisition.id,
        file_name="delete-me.pdf",
        uploaded_by_id=test_user.id,
    )
    db_session.add(att)
    db_session.commit()
    db_session.refresh(att)

    resp = att_client.delete(f"/api/requisition-attachments/{att.id}")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True

    # Verify it's actually gone
    assert db_session.get(RequisitionAttachment, att.id) is None


def test_delete_requisition_attachment_not_found(att_client):
    """DELETE /api/requisition-attachments/999 returns 404."""
    resp = att_client.delete("/api/requisition-attachments/999")
    assert resp.status_code == 404


# ══════════════════════════════════════════════════════════════════════
# Requirement Attachments
# ══════════════════════════════════════════════════════════════════════


def test_list_requirement_attachments_empty(att_client, test_requisition, db_session):
    """GET /api/requirements/{id}/attachments returns empty list when no files."""
    req = db_session.query(Requirement).filter_by(requisition_id=test_requisition.id).first()
    resp = att_client.get(f"/api/requirements/{req.id}/attachments")
    assert resp.status_code == 200
    assert resp.json() == []


def test_list_requirement_attachments_with_data(
    att_client, db_session, test_requisition, test_user
):
    """Attachments appear in the requirement list response."""
    req = db_session.query(Requirement).filter_by(requisition_id=test_requisition.id).first()
    att = RequirementAttachment(
        requirement_id=req.id,
        file_name="spec.pdf",
        onedrive_url="https://onedrive.example.com/spec",
        content_type="application/pdf",
        size_bytes=2048,
        uploaded_by_id=test_user.id,
    )
    db_session.add(att)
    db_session.commit()
    resp = att_client.get(f"/api/requirements/{req.id}/attachments")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["file_name"] == "spec.pdf"


def test_list_requirement_attachments_not_found(att_client):
    """GET /api/requirements/999/attachments returns 404."""
    resp = att_client.get("/api/requirements/999/attachments")
    assert resp.status_code == 404


@patch("app.http_client.http")
def test_upload_requirement_attachment(mock_http, att_client, test_requisition, db_session):
    """POST /api/requirements/{id}/attachments uploads to OneDrive."""
    req = db_session.query(Requirement).filter_by(requisition_id=test_requisition.id).first()
    mock_resp = MagicMock()
    mock_resp.status_code = 201
    mock_resp.json.return_value = {"id": "od-req-456", "webUrl": "https://onedrive.example.com/spec"}
    mock_http.put = AsyncMock(return_value=mock_resp)

    resp = att_client.post(
        f"/api/requirements/{req.id}/attachments",
        files={"file": ("spec.pdf", BytesIO(b"spec data"), "application/pdf")},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["file_name"] == "spec.pdf"


def test_upload_requirement_attachment_not_found(att_client):
    """Upload to non-existent requirement returns 404."""
    resp = att_client.post(
        "/api/requirements/999/attachments",
        files={"file": ("test.pdf", BytesIO(b"data"), "application/pdf")},
    )
    assert resp.status_code == 404


def test_delete_requirement_attachment(att_client, db_session, test_requisition, test_user):
    """DELETE /api/requirement-attachments/{id} removes the record."""
    req = db_session.query(Requirement).filter_by(requisition_id=test_requisition.id).first()
    att = RequirementAttachment(
        requirement_id=req.id,
        file_name="remove-me.pdf",
        uploaded_by_id=test_user.id,
    )
    db_session.add(att)
    db_session.commit()
    db_session.refresh(att)

    resp = att_client.delete(f"/api/requirement-attachments/{att.id}")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert db_session.get(RequirementAttachment, att.id) is None


def test_delete_requirement_attachment_not_found(att_client):
    """DELETE /api/requirement-attachments/999 returns 404."""
    resp = att_client.delete("/api/requirement-attachments/999")
    assert resp.status_code == 404
