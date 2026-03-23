"""tests/test_attachments.py — Tests for Requisition & Requirement Attachments.

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

    try:
        with TestClient(app) as c:
            yield c
    finally:
        for dep in [get_db, require_user, require_buyer]:
            app.dependency_overrides.pop(dep, None)


# ══════════════════════════════════════════════════════════════════════
# Requisition Attachments
# ══════════════════════════════════════════════════════════════════════


def test_list_requisition_attachments_empty(att_client, test_requisition):
    """GET /api/requisitions/{id}/attachments returns empty list when no files."""
    resp = att_client.get(f"/api/requisitions/{test_requisition.id}/attachments")
    assert resp.status_code == 200
    assert resp.json() == []


def test_list_requisition_attachments_with_data(att_client, db_session, test_requisition, test_user):
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


@patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="fake-token-for-testing")
@patch("app.http_client.http")
def test_upload_requisition_attachment(mock_http, _mock_token, att_client, test_requisition):
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


@patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="fake-token-for-testing")
@patch("app.http_client.http")
def test_upload_requisition_onedrive_error(mock_http, _mock_token, att_client, test_requisition):
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


def test_list_requirement_attachments_with_data(att_client, db_session, test_requisition, test_user):
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


@patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="fake-token-for-testing")
@patch("app.http_client.http")
def test_upload_requirement_attachment(mock_http, _mock_token, att_client, test_requisition, db_session):
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


# ══════════════════════════════════════════════════════════════════════
# No-token client — user without access_token for 401 branches
# ══════════════════════════════════════════════════════════════════════


@pytest.fixture()
def notoken_client(db_session: Session, test_user: User) -> TestClient:
    """TestClient with auth overrides but NO access_token."""
    from app.database import get_db
    from app.dependencies import require_buyer, require_user
    from app.main import app

    test_user.access_token = None
    db_session.commit()

    def _override_db():
        yield db_session

    def _override_user():
        return test_user

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[require_user] = _override_user
    app.dependency_overrides[require_buyer] = _override_user

    try:
        with TestClient(app) as c:
            yield c
    finally:
        for dep in [get_db, require_user, require_buyer]:
            app.dependency_overrides.pop(dep, None)


# ── Requisition: no access_token → 401 (line 1251) ──────────────────


def test_upload_requisition_no_token(notoken_client, test_requisition):
    """requisitions.py line 1251: no access_token → 401."""
    resp = notoken_client.post(
        f"/api/requisitions/{test_requisition.id}/attachments",
        files={"file": ("test.pdf", BytesIO(b"data"), "application/pdf")},
    )
    assert resp.status_code == 401


# ── Requisition: attach from OneDrive (lines 1296-1322) ─────────────


@patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="fake-token")
@patch("app.utils.graph_client.GraphClient")
def test_attach_from_onedrive_success(mock_gc_cls, mock_token, att_client, test_requisition):
    """requisitions.py lines 1296-1322: attach existing OneDrive item."""
    mock_gc = MagicMock()
    mock_gc.get_json = AsyncMock(
        return_value={
            "name": "spec.pdf",
            "webUrl": "https://onedrive.example.com/spec",
            "file": {"mimeType": "application/pdf"},
            "size": 4096,
        }
    )
    mock_gc_cls.return_value = mock_gc

    resp = att_client.post(
        f"/api/requisitions/{test_requisition.id}/attachments/onedrive",
        json={"item_id": "od-item-789"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["file_name"] == "spec.pdf"
    assert data["onedrive_url"] == "https://onedrive.example.com/spec"


@patch("app.utils.graph_client.GraphClient")
def test_attach_from_onedrive_not_found_req(mock_gc_cls, att_client):
    """requisitions.py line 1297-1298: requisition not found → 404."""
    resp = att_client.post(
        "/api/requisitions/999/attachments/onedrive",
        json={"item_id": "od-item-789"},
    )
    assert resp.status_code == 404


@patch("app.utils.graph_client.GraphClient")
def test_attach_from_onedrive_no_item_id(mock_gc_cls, att_client, test_requisition):
    """requisitions.py line 1301-1302: missing item_id → 400."""
    resp = att_client.post(
        f"/api/requisitions/{test_requisition.id}/attachments/onedrive",
        json={},
    )
    assert resp.status_code == 400


def test_attach_from_onedrive_no_token(notoken_client, test_requisition):
    """requisitions.py line 1303-1304: no access_token → 401."""
    resp = notoken_client.post(
        f"/api/requisitions/{test_requisition.id}/attachments/onedrive",
        json={"item_id": "od-item-789"},
    )
    assert resp.status_code == 401


@patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="fake-token")
@patch("app.utils.graph_client.GraphClient")
def test_attach_from_onedrive_item_error(mock_gc_cls, mock_token, att_client, test_requisition):
    """requisitions.py line 1309-1310: OneDrive item not found → 404."""
    mock_gc = MagicMock()
    mock_gc.get_json = AsyncMock(return_value={"error": {"code": "itemNotFound"}})
    mock_gc_cls.return_value = mock_gc

    resp = att_client.post(
        f"/api/requisitions/{test_requisition.id}/attachments/onedrive",
        json={"item_id": "od-item-missing"},
    )
    assert resp.status_code == 404


# ── Requisition: delete with OneDrive cleanup (lines 1341-1350) ─────


@patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="fake-token")
@patch("app.http_client.http")
def test_delete_requisition_with_onedrive(mock_http, mock_token, att_client, db_session, test_requisition, test_user):
    """requisitions.py lines 1341-1348: delete attachment + OneDrive item."""
    mock_http.delete = AsyncMock(return_value=MagicMock(status_code=204))

    att = RequisitionAttachment(
        requisition_id=test_requisition.id,
        file_name="onedrive-file.pdf",
        onedrive_item_id="od-item-to-delete",
        uploaded_by_id=test_user.id,
    )
    db_session.add(att)
    db_session.commit()
    db_session.refresh(att)

    resp = att_client.delete(f"/api/requisition-attachments/{att.id}")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    mock_http.delete.assert_called_once()


@patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="fake-token")
@patch("app.http_client.http")
def test_delete_requisition_onedrive_failure(
    mock_http, mock_token, att_client, db_session, test_requisition, test_user
):
    """requisitions.py lines 1349-1350: OneDrive delete fails → still succeeds."""
    mock_http.delete = AsyncMock(side_effect=ConnectionError("network down"))

    att = RequisitionAttachment(
        requisition_id=test_requisition.id,
        file_name="fail-delete.pdf",
        onedrive_item_id="od-fail-item",
        uploaded_by_id=test_user.id,
    )
    db_session.add(att)
    db_session.commit()
    db_session.refresh(att)

    resp = att_client.delete(f"/api/requisition-attachments/{att.id}")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


# ── Requirement: too large + no token (lines 1393, 1395) ────────────


@patch("app.http_client.http")
def test_upload_requirement_too_large(mock_http, att_client, test_requisition, db_session):
    """requisitions.py line 1393: requirement upload > 10 MB → 400."""
    req = db_session.query(Requirement).filter_by(requisition_id=test_requisition.id).first()
    big_content = b"x" * (10 * 1024 * 1024 + 1)
    resp = att_client.post(
        f"/api/requirements/{req.id}/attachments",
        files={"file": ("big.bin", BytesIO(big_content), "application/octet-stream")},
    )
    assert resp.status_code == 400


def test_upload_requirement_no_token(notoken_client, test_requisition, db_session):
    """requisitions.py line 1395: no access_token → 401."""
    req = db_session.query(Requirement).filter_by(requisition_id=test_requisition.id).first()
    resp = notoken_client.post(
        f"/api/requirements/{req.id}/attachments",
        files={"file": ("test.pdf", BytesIO(b"data"), "application/pdf")},
    )
    assert resp.status_code == 401


# ── Requirement: OneDrive upload failure (lines 1410-1411) ──────────


@patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="fake-token-for-testing")
@patch("app.http_client.http")
def test_upload_requirement_onedrive_error(mock_http, _mock_token, att_client, test_requisition, db_session):
    """requisitions.py lines 1410-1411: OneDrive upload failure → 502."""
    req = db_session.query(Requirement).filter_by(requisition_id=test_requisition.id).first()
    mock_resp = MagicMock()
    mock_resp.status_code = 500
    mock_resp.text = "Internal Server Error"
    mock_http.put = AsyncMock(return_value=mock_resp)

    resp = att_client.post(
        f"/api/requirements/{req.id}/attachments",
        files={"file": ("test.pdf", BytesIO(b"data"), "application/pdf")},
    )
    assert resp.status_code == 502


# ── Requirement: delete with OneDrive cleanup (lines 1443-1452) ─────


@patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="fake-token")
@patch("app.http_client.http")
def test_delete_requirement_with_onedrive(mock_http, mock_token, att_client, db_session, test_requisition, test_user):
    """requisitions.py lines 1443-1450: delete attachment + OneDrive item."""
    mock_http.delete = AsyncMock(return_value=MagicMock(status_code=204))

    req = db_session.query(Requirement).filter_by(requisition_id=test_requisition.id).first()
    att = RequirementAttachment(
        requirement_id=req.id,
        file_name="onedrive-req-file.pdf",
        onedrive_item_id="od-req-item-to-delete",
        uploaded_by_id=test_user.id,
    )
    db_session.add(att)
    db_session.commit()
    db_session.refresh(att)

    resp = att_client.delete(f"/api/requirement-attachments/{att.id}")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    mock_http.delete.assert_called_once()


@patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="fake-token")
@patch("app.http_client.http")
def test_delete_requirement_onedrive_failure(
    mock_http, mock_token, att_client, db_session, test_requisition, test_user
):
    """requisitions.py lines 1451-1452: OneDrive delete fails → still succeeds."""
    mock_http.delete = AsyncMock(side_effect=TimeoutError("timed out"))

    req = db_session.query(Requirement).filter_by(requisition_id=test_requisition.id).first()
    att = RequirementAttachment(
        requirement_id=req.id,
        file_name="fail-req-delete.pdf",
        onedrive_item_id="od-req-fail-item",
        uploaded_by_id=test_user.id,
    )
    db_session.add(att)
    db_session.commit()
    db_session.refresh(att)

    resp = att_client.delete(f"/api/requirement-attachments/{att.id}")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
