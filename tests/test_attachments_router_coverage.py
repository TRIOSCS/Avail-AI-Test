"""test_attachments_router_coverage.py — Extra coverage for requisition/requirement
attachments.

Targets uncovered branches in app/routers/requisitions/attachments.py:
- list_requisition_attachments: 404 path, returns list
- upload_requisition_attachment: file too large, token failure, 401/403/502 from OneDrive
- attach_requisition_from_onedrive: missing item_id, token failure, Graph error codes
- delete_requisition_attachment: no onedrive_item_id path, ConnectionError fallback
- list/upload/delete for requirements

Called by: pytest
Depends on: conftest (client, db_session, test_user, test_requisition)
"""

import os

os.environ["TESTING"] = "1"

from unittest.mock import AsyncMock, MagicMock, patch

from app.models import Requirement, RequirementAttachment, RequisitionAttachment

# ── Helpers ──────────────────────────────────────────────────────────


def _make_req_attachment(db, requisition_id, user_id, *, onedrive_item_id="od-req-1"):
    att = RequisitionAttachment(
        requisition_id=requisition_id,
        file_name="test.pdf",
        onedrive_item_id=onedrive_item_id,
        onedrive_url="https://od.example.com/test.pdf",
        content_type="application/pdf",
        size_bytes=1024,
        uploaded_by_id=user_id,
    )
    db.add(att)
    db.commit()
    db.refresh(att)
    return att


def _make_att_no_onedrive(db, requisition_id, user_id):
    """Attachment without an OneDrive item ID."""
    att = RequisitionAttachment(
        requisition_id=requisition_id,
        file_name="local.pdf",
        onedrive_item_id=None,
        onedrive_url=None,
        content_type="application/pdf",
        size_bytes=512,
        uploaded_by_id=user_id,
    )
    db.add(att)
    db.commit()
    db.refresh(att)
    return att


def _make_requirement(db, requisition_id):
    req = Requirement(
        requisition_id=requisition_id,
        primary_mpn="NE555-EXTRA",
        manufacturer="TI",
        target_qty=50,
    )
    db.add(req)
    db.commit()
    db.refresh(req)
    return req


def _make_reqmt_attachment(db, requirement_id, user_id, *, onedrive_item_id="od-reqmt-1"):
    att = RequirementAttachment(
        requirement_id=requirement_id,
        file_name="spec.pdf",
        onedrive_item_id=onedrive_item_id,
        onedrive_url="https://od.example.com/spec.pdf",
        content_type="application/pdf",
        size_bytes=2048,
        uploaded_by_id=user_id,
    )
    db.add(att)
    db.commit()
    db.refresh(att)
    return att


def _make_reqmt_att_no_onedrive(db, requirement_id, user_id):
    att = RequirementAttachment(
        requirement_id=requirement_id,
        file_name="local_spec.pdf",
        onedrive_item_id=None,
        onedrive_url=None,
        content_type="application/pdf",
        size_bytes=256,
        uploaded_by_id=user_id,
    )
    db.add(att)
    db.commit()
    db.refresh(att)
    return att


def _mock_resp(status_code, json_data=None, text=""):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data or {}
    resp.text = text
    return resp


# ── List Requisition Attachments ─────────────────────────────────────


class TestListRequisitionAttachments:
    def test_returns_200_with_empty_list(self, client, test_requisition):
        resp = client.get(f"/api/requisitions/{test_requisition.id}/attachments")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_returns_404_for_missing_requisition(self, client):
        resp = client.get("/api/requisitions/999999/attachments")
        assert resp.status_code == 404

    def test_returns_attachments_list(self, client, db_session, test_requisition, test_user):
        _make_req_attachment(db_session, test_requisition.id, test_user.id)
        resp = client.get(f"/api/requisitions/{test_requisition.id}/attachments")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["file_name"] == "test.pdf"
        assert data[0]["content_type"] == "application/pdf"


# ── Upload Requisition Attachment ────────────────────────────────────


class TestUploadRequisitionAttachment:
    def test_file_too_large_returns_400(self, client, test_requisition):
        big_content = b"x" * (10 * 1024 * 1024 + 1)
        resp = client.post(
            f"/api/requisitions/{test_requisition.id}/attachments",
            files={"file": ("big.pdf", big_content, "application/pdf")},
        )
        assert resp.status_code == 400
        assert "large" in resp.json()["error"].lower()

    def test_missing_token_returns_401(self, client, test_requisition):
        with patch(
            "app.scheduler.get_valid_token",
            new_callable=AsyncMock,
            return_value=None,
        ):
            resp = client.post(
                f"/api/requisitions/{test_requisition.id}/attachments",
                files={"file": ("doc.pdf", b"small content", "application/pdf")},
            )
        assert resp.status_code == 401

    def test_onedrive_returns_401(self, client, test_requisition):
        with (
            patch(
                "app.scheduler.get_valid_token",
                new_callable=AsyncMock,
                return_value="tok",
            ),
            patch(
                "app.http_client.http.put",
                new_callable=AsyncMock,
                return_value=_mock_resp(401),
            ),
        ):
            resp = client.post(
                f"/api/requisitions/{test_requisition.id}/attachments",
                files={"file": ("doc.pdf", b"content", "application/pdf")},
            )
        assert resp.status_code == 401

    def test_onedrive_returns_403(self, client, test_requisition):
        with (
            patch(
                "app.scheduler.get_valid_token",
                new_callable=AsyncMock,
                return_value="tok",
            ),
            patch(
                "app.http_client.http.put",
                new_callable=AsyncMock,
                return_value=_mock_resp(403),
            ),
        ):
            resp = client.post(
                f"/api/requisitions/{test_requisition.id}/attachments",
                files={"file": ("doc.pdf", b"content", "application/pdf")},
            )
        assert resp.status_code == 403

    def test_onedrive_returns_502_on_server_error(self, client, test_requisition):
        with (
            patch(
                "app.scheduler.get_valid_token",
                new_callable=AsyncMock,
                return_value="tok",
            ),
            patch(
                "app.http_client.http.put",
                new_callable=AsyncMock,
                return_value=_mock_resp(500, text="server error"),
            ),
        ):
            resp = client.post(
                f"/api/requisitions/{test_requisition.id}/attachments",
                files={"file": ("doc.pdf", b"content", "application/pdf")},
            )
        assert resp.status_code == 502

    def test_successful_upload_returns_201(self, client, test_requisition):
        onedrive_resp = {"id": "od-123", "webUrl": "https://od.example.com/doc.pdf"}
        with (
            patch(
                "app.scheduler.get_valid_token",
                new_callable=AsyncMock,
                return_value="tok",
            ),
            patch(
                "app.http_client.http.put",
                new_callable=AsyncMock,
                return_value=_mock_resp(201, onedrive_resp),
            ),
        ):
            resp = client.post(
                f"/api/requisitions/{test_requisition.id}/attachments",
                files={"file": ("doc.pdf", b"file content here", "application/pdf")},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["file_name"] == "doc.pdf"
        assert data["onedrive_url"] == "https://od.example.com/doc.pdf"

    def test_not_found_requisition_returns_404(self, client):
        resp = client.post(
            "/api/requisitions/999999/attachments",
            files={"file": ("doc.pdf", b"content", "application/pdf")},
        )
        assert resp.status_code == 404


# ── Attach Existing OneDrive File to Requisition ─────────────────────


class TestAttachRequisitionFromOneDrive:
    def test_missing_item_id_returns_400(self, client, test_requisition):
        resp = client.post(
            f"/api/requisitions/{test_requisition.id}/attachments/onedrive",
            json={},
        )
        assert resp.status_code == 400
        assert "item_id" in resp.json()["error"].lower()

    def test_missing_token_returns_401(self, client, test_requisition):
        with patch(
            "app.scheduler.get_valid_token",
            new_callable=AsyncMock,
            return_value=None,
        ):
            resp = client.post(
                f"/api/requisitions/{test_requisition.id}/attachments/onedrive",
                json={"item_id": "od-file-001"},
            )
        assert resp.status_code == 401

    def test_graph_token_expired_returns_401(self, client, test_requisition):
        gc_mock = MagicMock()
        gc_mock.get_json = AsyncMock(return_value={"error": {"code": "InvalidAuthenticationToken"}})
        with (
            patch(
                "app.scheduler.get_valid_token",
                new_callable=AsyncMock,
                return_value="tok",
            ),
            patch(
                "app.utils.graph_client.GraphClient",
                return_value=gc_mock,
            ),
        ):
            resp = client.post(
                f"/api/requisitions/{test_requisition.id}/attachments/onedrive",
                json={"item_id": "od-file-001"},
            )
        assert resp.status_code == 401

    def test_graph_access_denied_returns_403(self, client, test_requisition):
        gc_mock = MagicMock()
        gc_mock.get_json = AsyncMock(return_value={"error": {"code": "accessDenied"}})
        with (
            patch(
                "app.scheduler.get_valid_token",
                new_callable=AsyncMock,
                return_value="tok",
            ),
            patch(
                "app.utils.graph_client.GraphClient",
                return_value=gc_mock,
            ),
        ):
            resp = client.post(
                f"/api/requisitions/{test_requisition.id}/attachments/onedrive",
                json={"item_id": "od-file-001"},
            )
        assert resp.status_code == 403

    def test_graph_item_not_found_returns_404(self, client, test_requisition):
        gc_mock = MagicMock()
        gc_mock.get_json = AsyncMock(return_value={"error": {"code": "itemNotFound"}})
        with (
            patch(
                "app.scheduler.get_valid_token",
                new_callable=AsyncMock,
                return_value="tok",
            ),
            patch(
                "app.utils.graph_client.GraphClient",
                return_value=gc_mock,
            ),
        ):
            resp = client.post(
                f"/api/requisitions/{test_requisition.id}/attachments/onedrive",
                json={"item_id": "od-file-001"},
            )
        assert resp.status_code == 404

    def test_successful_link_returns_attachment(self, client, test_requisition):
        gc_mock = MagicMock()
        gc_mock.get_json = AsyncMock(
            return_value={
                "id": "od-file-001",
                "name": "datasheet.pdf",
                "webUrl": "https://od.example.com/datasheet.pdf",
                "size": 4096,
                "file": {"mimeType": "application/pdf"},
            }
        )
        with (
            patch(
                "app.scheduler.get_valid_token",
                new_callable=AsyncMock,
                return_value="tok",
            ),
            patch(
                "app.utils.graph_client.GraphClient",
                return_value=gc_mock,
            ),
        ):
            resp = client.post(
                f"/api/requisitions/{test_requisition.id}/attachments/onedrive",
                json={"item_id": "od-file-001"},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["file_name"] == "datasheet.pdf"


# ── Delete Requisition Attachment (no OneDrive item) ─────────────────


class TestDeleteRequisitionAttachmentNoOneDrive:
    def test_delete_without_onedrive_id_succeeds(self, client, db_session, test_requisition, test_user):
        """Attachment with no onedrive_item_id deletes directly from DB."""
        att = _make_att_no_onedrive(db_session, test_requisition.id, test_user.id)
        resp = client.delete(f"/api/requisition-attachments/{att.id}")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

    def test_delete_not_found_returns_404(self, client):
        resp = client.delete("/api/requisition-attachments/999999")
        assert resp.status_code == 404

    def test_delete_network_error_deletes_db_record_with_warning(self, client, db_session, test_requisition, test_user):
        """Connection error during OneDrive delete → DB record deleted + warning."""
        att = _make_req_attachment(db_session, test_requisition.id, test_user.id)
        with (
            patch(
                "app.scheduler.get_valid_token",
                new_callable=AsyncMock,
                return_value="tok",
            ),
            patch(
                "app.http_client.http.delete",
                new_callable=AsyncMock,
                side_effect=ConnectionError("network failure"),
            ),
        ):
            resp = client.delete(f"/api/requisition-attachments/{att.id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert "warning" in data


# ── List Requirement Attachments ──────────────────────────────────────


class TestListRequirementAttachments:
    def test_returns_200_with_empty_list(self, client, db_session, test_requisition):
        requirement = _make_requirement(db_session, test_requisition.id)
        resp = client.get(f"/api/requirements/{requirement.id}/attachments")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_returns_404_for_missing_requirement(self, client):
        resp = client.get("/api/requirements/999999/attachments")
        assert resp.status_code == 404

    def test_returns_attachments_list(self, client, db_session, test_requisition, test_user):
        requirement = _make_requirement(db_session, test_requisition.id)
        _make_reqmt_attachment(db_session, requirement.id, test_user.id)
        resp = client.get(f"/api/requirements/{requirement.id}/attachments")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["file_name"] == "spec.pdf"


# ── Upload Requirement Attachment ────────────────────────────────────


class TestUploadRequirementAttachment:
    def test_file_too_large_returns_400(self, client, db_session, test_requisition):
        requirement = _make_requirement(db_session, test_requisition.id)
        big_content = b"x" * (10 * 1024 * 1024 + 1)
        resp = client.post(
            f"/api/requirements/{requirement.id}/attachments",
            files={"file": ("big.pdf", big_content, "application/pdf")},
        )
        assert resp.status_code == 400

    def test_missing_token_returns_401(self, client, db_session, test_requisition):
        requirement = _make_requirement(db_session, test_requisition.id)
        with patch(
            "app.scheduler.get_valid_token",
            new_callable=AsyncMock,
            return_value=None,
        ):
            resp = client.post(
                f"/api/requirements/{requirement.id}/attachments",
                files={"file": ("spec.pdf", b"data", "application/pdf")},
            )
        assert resp.status_code == 401

    def test_onedrive_401_returns_401(self, client, db_session, test_requisition):
        requirement = _make_requirement(db_session, test_requisition.id)
        with (
            patch(
                "app.scheduler.get_valid_token",
                new_callable=AsyncMock,
                return_value="tok",
            ),
            patch(
                "app.http_client.http.put",
                new_callable=AsyncMock,
                return_value=_mock_resp(401),
            ),
        ):
            resp = client.post(
                f"/api/requirements/{requirement.id}/attachments",
                files={"file": ("spec.pdf", b"data", "application/pdf")},
            )
        assert resp.status_code == 401

    def test_onedrive_403_returns_403(self, client, db_session, test_requisition):
        requirement = _make_requirement(db_session, test_requisition.id)
        with (
            patch(
                "app.scheduler.get_valid_token",
                new_callable=AsyncMock,
                return_value="tok",
            ),
            patch(
                "app.http_client.http.put",
                new_callable=AsyncMock,
                return_value=_mock_resp(403),
            ),
        ):
            resp = client.post(
                f"/api/requirements/{requirement.id}/attachments",
                files={"file": ("spec.pdf", b"data", "application/pdf")},
            )
        assert resp.status_code == 403

    def test_onedrive_502_on_server_error(self, client, db_session, test_requisition):
        requirement = _make_requirement(db_session, test_requisition.id)
        with (
            patch(
                "app.scheduler.get_valid_token",
                new_callable=AsyncMock,
                return_value="tok",
            ),
            patch(
                "app.http_client.http.put",
                new_callable=AsyncMock,
                return_value=_mock_resp(500, text="fail"),
            ),
        ):
            resp = client.post(
                f"/api/requirements/{requirement.id}/attachments",
                files={"file": ("spec.pdf", b"data", "application/pdf")},
            )
        assert resp.status_code == 502

    def test_successful_upload(self, client, db_session, test_requisition):
        requirement = _make_requirement(db_session, test_requisition.id)
        onedrive_resp = {"id": "od-spec-1", "webUrl": "https://od.example.com/spec.pdf"}
        with (
            patch(
                "app.scheduler.get_valid_token",
                new_callable=AsyncMock,
                return_value="tok",
            ),
            patch(
                "app.http_client.http.put",
                new_callable=AsyncMock,
                return_value=_mock_resp(201, onedrive_resp),
            ),
        ):
            resp = client.post(
                f"/api/requirements/{requirement.id}/attachments",
                files={"file": ("spec.pdf", b"spec content", "application/pdf")},
            )
        assert resp.status_code == 200
        assert resp.json()["file_name"] == "spec.pdf"

    def test_not_found_requirement_returns_404(self, client):
        resp = client.post(
            "/api/requirements/999999/attachments",
            files={"file": ("spec.pdf", b"data", "application/pdf")},
        )
        assert resp.status_code == 404


# ── Delete Requirement Attachment ────────────────────────────────────


class TestDeleteRequirementAttachmentExtra:
    def test_delete_without_onedrive_id_succeeds(self, client, db_session, test_requisition, test_user):
        requirement = _make_requirement(db_session, test_requisition.id)
        att = _make_reqmt_att_no_onedrive(db_session, requirement.id, test_user.id)
        resp = client.delete(f"/api/requirement-attachments/{att.id}")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

    def test_delete_not_found_returns_404(self, client):
        resp = client.delete("/api/requirement-attachments/999999")
        assert resp.status_code == 404

    def test_delete_network_error_returns_warning(self, client, db_session, test_requisition, test_user):
        requirement = _make_requirement(db_session, test_requisition.id)
        att = _make_reqmt_attachment(db_session, requirement.id, test_user.id)
        with (
            patch(
                "app.scheduler.get_valid_token",
                new_callable=AsyncMock,
                return_value="tok",
            ),
            patch(
                "app.http_client.http.delete",
                new_callable=AsyncMock,
                side_effect=ConnectionError("timeout"),
            ),
        ):
            resp = client.delete(f"/api/requirement-attachments/{att.id}")
        assert resp.status_code == 200
        assert "warning" in resp.json()

    def test_delete_graph_403_returns_403(self, client, db_session, test_requisition, test_user):
        requirement = _make_requirement(db_session, test_requisition.id)
        att = _make_reqmt_attachment(db_session, requirement.id, test_user.id)
        with (
            patch(
                "app.scheduler.get_valid_token",
                new_callable=AsyncMock,
                return_value="tok",
            ),
            patch(
                "app.http_client.http.delete",
                new_callable=AsyncMock,
                return_value=_mock_resp(403),
            ),
        ):
            resp = client.delete(f"/api/requirement-attachments/{att.id}")
        assert resp.status_code == 403
