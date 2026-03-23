"""Tests for app/routers/requisitions/attachments.py — token refresh and Graph API error
handling.

Verifies that:
- Delete and OneDrive-link endpoints use get_valid_token (not user.access_token)
- Expired tokens (get_valid_token returns None) yield 401
- Graph API 401/403 responses are properly surfaced to the caller

Called by: pytest
Depends on: conftest fixtures (client, db_session, test_user, test_requisition)
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from app.models import Requirement, RequirementAttachment, RequisitionAttachment

# ── Helpers ──────────────────────────────────────────────────────────


def _make_req_attachment(db: Session, requisition_id: int, user_id: int, *, onedrive_item_id: str = "od-item-1"):
    """Create a RequisitionAttachment with a OneDrive item ID."""
    att = RequisitionAttachment(
        requisition_id=requisition_id,
        file_name="test.pdf",
        onedrive_item_id=onedrive_item_id,
        onedrive_url="https://onedrive.example.com/test.pdf",
        content_type="application/pdf",
        size_bytes=1024,
        uploaded_by_id=user_id,
    )
    db.add(att)
    db.commit()
    db.refresh(att)
    return att


def _make_requirement(db: Session, requisition_id: int):
    """Create a Requirement linked to a requisition."""
    req = Requirement(
        requisition_id=requisition_id,
        primary_mpn="TEST-PART-001",
        manufacturer="Test Mfg",
        target_qty=10,
    )
    db.add(req)
    db.commit()
    db.refresh(req)
    return req


def _make_reqmt_attachment(db: Session, requirement_id: int, user_id: int, *, onedrive_item_id: str = "od-item-2"):
    """Create a RequirementAttachment with a OneDrive item ID."""
    att = RequirementAttachment(
        requirement_id=requirement_id,
        file_name="spec.pdf",
        onedrive_item_id=onedrive_item_id,
        onedrive_url="https://onedrive.example.com/spec.pdf",
        content_type="application/pdf",
        size_bytes=2048,
        uploaded_by_id=user_id,
    )
    db.add(att)
    db.commit()
    db.refresh(att)
    return att


def _mock_http_response(status_code: int = 204, json_data: dict | None = None):
    """Build a mock httpx response."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data or {}
    resp.text = ""
    return resp


# ── Delete Requisition Attachment ────────────────────────────────────


@pytest.fixture()
def req_att(db_session, test_requisition, test_user):
    return _make_req_attachment(db_session, test_requisition.id, test_user.id)


class TestDeleteRequisitionAttachment:
    """DELETE /api/requisition-attachments/{att_id}"""

    def test_uses_refreshed_token(self, client, req_att, db_session):
        """Delete endpoint must call get_valid_token and use the refreshed token."""
        mock_resp = _mock_http_response(204)
        with (
            patch(
                "app.scheduler.get_valid_token",
                new_callable=AsyncMock,
                return_value="refreshed-token-abc",
            ) as mock_gvt,
            patch(
                "app.http_client.http.delete",
                new_callable=AsyncMock,
                return_value=mock_resp,
            ) as mock_delete,
        ):
            resp = client.delete(f"/api/requisition-attachments/{req_att.id}")
            assert resp.status_code == 200
            assert resp.json()["ok"] is True
            mock_gvt.assert_awaited_once()
            # Verify the refreshed token was used, not user.access_token
            call_headers = mock_delete.call_args.kwargs.get("headers", {})
            assert call_headers["Authorization"] == "Bearer refreshed-token-abc"

    def test_expired_token_returns_401(self, client, req_att):
        """When get_valid_token returns None, endpoint must return 401."""
        with patch(
            "app.scheduler.get_valid_token",
            new_callable=AsyncMock,
            return_value=None,
        ):
            resp = client.delete(f"/api/requisition-attachments/{req_att.id}")
            assert resp.status_code == 401
            assert "token expired" in resp.json()["error"].lower()

    def test_graph_401_returns_401(self, client, req_att):
        """When Graph API returns 401, endpoint must surface 401 to caller."""
        mock_resp = _mock_http_response(401)
        with (
            patch(
                "app.scheduler.get_valid_token",
                new_callable=AsyncMock,
                return_value="some-token",
            ),
            patch(
                "app.http_client.http.delete",
                new_callable=AsyncMock,
                return_value=mock_resp,
            ),
        ):
            resp = client.delete(f"/api/requisition-attachments/{req_att.id}")
            assert resp.status_code == 401
            assert "token expired" in resp.json()["error"].lower()

    def test_graph_403_returns_403(self, client, req_att):
        """When Graph API returns 403, endpoint must surface 403 to caller."""
        mock_resp = _mock_http_response(403)
        with (
            patch(
                "app.scheduler.get_valid_token",
                new_callable=AsyncMock,
                return_value="some-token",
            ),
            patch(
                "app.http_client.http.delete",
                new_callable=AsyncMock,
                return_value=mock_resp,
            ),
        ):
            resp = client.delete(f"/api/requisition-attachments/{req_att.id}")
            assert resp.status_code == 403
            assert "access denied" in resp.json()["error"].lower()

    def test_db_record_not_deleted_on_auth_failure(self, client, req_att, db_session):
        """On 401/403 from Graph, the DB record must NOT be deleted (prevent data
        leak)."""
        mock_resp = _mock_http_response(401)
        with (
            patch(
                "app.scheduler.get_valid_token",
                new_callable=AsyncMock,
                return_value="some-token",
            ),
            patch(
                "app.http_client.http.delete",
                new_callable=AsyncMock,
                return_value=mock_resp,
            ),
        ):
            client.delete(f"/api/requisition-attachments/{req_att.id}")
            # Record should still exist
            att = db_session.get(RequisitionAttachment, req_att.id)
            assert att is not None


# ── Delete Requirement Attachment ────────────────────────────────────


@pytest.fixture()
def reqmt_att(db_session, test_requisition, test_user):
    requirement = _make_requirement(db_session, test_requisition.id)
    return _make_reqmt_attachment(db_session, requirement.id, test_user.id)


class TestDeleteRequirementAttachment:
    """DELETE /api/requirement-attachments/{att_id}"""

    def test_uses_refreshed_token(self, client, reqmt_att, db_session):
        """Delete endpoint must call get_valid_token and use the refreshed token."""
        mock_resp = _mock_http_response(204)
        with (
            patch(
                "app.scheduler.get_valid_token",
                new_callable=AsyncMock,
                return_value="refreshed-token-xyz",
            ) as mock_gvt,
            patch(
                "app.http_client.http.delete",
                new_callable=AsyncMock,
                return_value=mock_resp,
            ) as mock_delete,
        ):
            resp = client.delete(f"/api/requirement-attachments/{reqmt_att.id}")
            assert resp.status_code == 200
            assert resp.json()["ok"] is True
            mock_gvt.assert_awaited_once()
            call_headers = mock_delete.call_args.kwargs.get("headers", {})
            assert call_headers["Authorization"] == "Bearer refreshed-token-xyz"

    def test_expired_token_returns_401(self, client, reqmt_att):
        """When get_valid_token returns None, endpoint must return 401."""
        with patch(
            "app.scheduler.get_valid_token",
            new_callable=AsyncMock,
            return_value=None,
        ):
            resp = client.delete(f"/api/requirement-attachments/{reqmt_att.id}")
            assert resp.status_code == 401
            assert "token expired" in resp.json()["error"].lower()

    def test_graph_401_returns_401(self, client, reqmt_att):
        """When Graph API returns 401, endpoint must surface 401 to caller."""
        mock_resp = _mock_http_response(401)
        with (
            patch(
                "app.scheduler.get_valid_token",
                new_callable=AsyncMock,
                return_value="some-token",
            ),
            patch(
                "app.http_client.http.delete",
                new_callable=AsyncMock,
                return_value=mock_resp,
            ),
        ):
            resp = client.delete(f"/api/requirement-attachments/{reqmt_att.id}")
            assert resp.status_code == 401


# ── OneDrive Link Endpoint ───────────────────────────────────────────


class TestAttachFromOneDrive:
    """POST /api/requisitions/{req_id}/attachments/onedrive."""

    def test_uses_refreshed_token(self, client, test_requisition, db_session):
        """OneDrive link endpoint must use get_valid_token, not user.access_token."""
        mock_item = {
            "id": "od-123",
            "name": "linked.pdf",
            "webUrl": "https://od.example.com/linked.pdf",
            "file": {"mimeType": "application/pdf"},
            "size": 4096,
        }
        with (
            patch(
                "app.scheduler.get_valid_token",
                new_callable=AsyncMock,
                return_value="refreshed-link-token",
            ) as mock_gvt,
            patch(
                "app.utils.graph_client.GraphClient",
            ) as MockGC,
        ):
            gc_instance = MockGC.return_value
            gc_instance.get_json = AsyncMock(return_value=mock_item)
            resp = client.post(
                f"/api/requisitions/{test_requisition.id}/attachments/onedrive",
                json={"item_id": "od-123"},
            )
            assert resp.status_code == 200
            assert resp.json()["file_name"] == "linked.pdf"
            mock_gvt.assert_awaited_once()
            # GraphClient should have been constructed with the refreshed token
            MockGC.assert_called_once_with("refreshed-link-token")

    def test_expired_token_returns_401(self, client, test_requisition):
        """When get_valid_token returns None, endpoint must return 401."""
        with patch(
            "app.scheduler.get_valid_token",
            new_callable=AsyncMock,
            return_value=None,
        ):
            resp = client.post(
                f"/api/requisitions/{test_requisition.id}/attachments/onedrive",
                json={"item_id": "od-123"},
            )
            assert resp.status_code == 401
            assert "token expired" in resp.json()["error"].lower()

    def test_graph_auth_error_returns_401(self, client, test_requisition):
        """When Graph returns InvalidAuthenticationToken error, endpoint must return
        401."""
        error_response = {"error": {"code": "InvalidAuthenticationToken", "message": "Token expired"}}
        with (
            patch(
                "app.scheduler.get_valid_token",
                new_callable=AsyncMock,
                return_value="some-token",
            ),
            patch(
                "app.utils.graph_client.GraphClient",
            ) as MockGC,
        ):
            gc_instance = MockGC.return_value
            gc_instance.get_json = AsyncMock(return_value=error_response)
            resp = client.post(
                f"/api/requisitions/{test_requisition.id}/attachments/onedrive",
                json={"item_id": "od-123"},
            )
            assert resp.status_code == 401

    def test_graph_access_denied_returns_403(self, client, test_requisition):
        """When Graph returns accessDenied error, endpoint must return 403."""
        error_response = {"error": {"code": "accessDenied", "message": "Forbidden"}}
        with (
            patch(
                "app.scheduler.get_valid_token",
                new_callable=AsyncMock,
                return_value="some-token",
            ),
            patch(
                "app.utils.graph_client.GraphClient",
            ) as MockGC,
        ):
            gc_instance = MockGC.return_value
            gc_instance.get_json = AsyncMock(return_value=error_response)
            resp = client.post(
                f"/api/requisitions/{test_requisition.id}/attachments/onedrive",
                json={"item_id": "od-123"},
            )
            assert resp.status_code == 403
