"""test_attachments_coverage2.py — Additional coverage for requisition/requirement
attachments.

Targets remaining uncovered branches in app/routers/requisitions/attachments.py:
- Line 39: list_requisition_attachments — get_req_for_user returns None (dead-code guard)
- Line 64: upload_requisition_attachment — get_req_for_user returns None
- Lines 123, 125-155: attach_from_onedrive — req not found, success path, error codes
- Line 178: delete_requisition_attachment — missing token
- Lines 187-190, 192: delete_requisition_attachment — 401/403, re-raise
- Line 215: list_requirement_attachments — get_req_for_user returns None
- Line 243: upload_requirement_attachment — get_req_for_user returns None
- Line 255: upload_requirement_attachment — file has no filename
- Lines 309, 319: delete_requirement_attachment — 401/403

Called by: pytest
Depends on: conftest.py (client, db_session, test_user, test_requisition)
"""

import os

os.environ["TESTING"] = "1"

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models import Requirement, RequirementAttachment, RequisitionAttachment

# ── Helpers ──────────────────────────────────────────────────────────


def _make_requirement(db, requisition_id):
    req = Requirement(
        requisition_id=requisition_id,
        primary_mpn="TL071-COV2",
        manufacturer="TI",
        target_qty=100,
    )
    db.add(req)
    db.commit()
    db.refresh(req)
    return req


def _make_req_att(db, requisition_id, user_id, *, onedrive_item_id="od-cov2-1"):
    att = RequisitionAttachment(
        requisition_id=requisition_id,
        file_name="coverage2.pdf",
        onedrive_item_id=onedrive_item_id,
        onedrive_url="https://od.example.com/cov2.pdf",
        content_type="application/pdf",
        size_bytes=512,
        uploaded_by_id=user_id,
    )
    db.add(att)
    db.commit()
    db.refresh(att)
    return att


def _make_reqmt_att(db, requirement_id, user_id, *, onedrive_item_id="od-reqmt-cov2"):
    att = RequirementAttachment(
        requirement_id=requirement_id,
        file_name="reqmt_cov2.pdf",
        onedrive_item_id=onedrive_item_id,
        onedrive_url="https://od.example.com/reqmt_cov2.pdf",
        content_type="application/pdf",
        size_bytes=1024,
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


# ── Line 39: list_requisition_attachments — get_req_for_user returns None ────


class TestListRequisitionAttachmentsLine39:
    def test_returns_404_when_get_req_for_user_returns_none(self, client, test_requisition):
        """When get_req_for_user returns None, line 39 raises 404.

        Normally get_req_for_user raises, but patching it to return None exercises the
        `if not req:` guard on line 38-39.
        """
        with patch(
            "app.routers.requisitions.attachments.get_req_for_user",
            return_value=None,
        ):
            resp = client.get(f"/api/requisitions/{test_requisition.id}/attachments")
        assert resp.status_code == 404


# ── Line 64: upload_requisition_attachment — get_req_for_user returns None ──


class TestUploadRequisitionAttachmentLine64:
    def test_returns_404_when_get_req_for_user_returns_none(self, client, test_requisition):
        """When get_req_for_user returns None, line 64 raises 404."""
        with patch(
            "app.routers.requisitions.attachments.get_req_for_user",
            return_value=None,
        ):
            resp = client.post(
                f"/api/requisitions/{test_requisition.id}/attachments",
                files={"file": ("doc.pdf", b"content", "application/pdf")},
            )
        assert resp.status_code == 404


# ── Lines 123, 125-155: attach_requisition_from_onedrive ─────────────────────


class TestAttachRequisitionFromOneDriveLine123:
    def test_returns_404_when_req_not_found(self, client, test_requisition):
        """get_req_for_user returning None → 404 at line 123."""
        with patch(
            "app.routers.requisitions.attachments.get_req_for_user",
            return_value=None,
        ):
            resp = client.post(
                f"/api/requisitions/{test_requisition.id}/attachments/onedrive",
                json={"item_id": "od-test-001"},
            )
        assert resp.status_code == 404

    def test_success_path_with_full_item_metadata(self, client, test_requisition):
        """Successful OneDrive link creates attachment — covers lines 144-160."""
        gc_mock = MagicMock()
        gc_mock.get_json = AsyncMock(
            return_value={
                "id": "od-item-abc",
                "name": "component_spec.pdf",
                "webUrl": "https://od.example.com/component_spec.pdf",
                "size": 8192,
                "file": {"mimeType": "application/pdf"},
            }
        )
        with (
            patch(
                "app.scheduler.get_valid_token",
                new_callable=AsyncMock,
                return_value="valid-token",
            ),
            patch(
                "app.utils.graph_client.GraphClient",
                return_value=gc_mock,
            ),
        ):
            resp = client.post(
                f"/api/requisitions/{test_requisition.id}/attachments/onedrive",
                json={"item_id": "od-item-abc"},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["file_name"] == "component_spec.pdf"
        assert data["onedrive_url"] == "https://od.example.com/component_spec.pdf"
        assert data["content_type"] == "application/pdf"

    def test_token_expired_error_code_returns_401(self, client, test_requisition):
        """Graph returns TokenExpired → 401."""
        gc_mock = MagicMock()
        gc_mock.get_json = AsyncMock(return_value={"error": {"code": "TokenExpired"}})
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
                json={"item_id": "od-item-001"},
            )
        assert resp.status_code == 401

    def test_access_denied_uppercase_returns_403(self, client, test_requisition):
        """Graph returns AccessDenied (uppercase) → 403."""
        gc_mock = MagicMock()
        gc_mock.get_json = AsyncMock(return_value={"error": {"code": "AccessDenied"}})
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
                json={"item_id": "od-item-001"},
            )
        assert resp.status_code == 403

    def test_non_dict_error_value_returns_404(self, client, test_requisition):
        """Graph returns error as a non-dict string → falls through to 404.

        Covers the `else ""` branch of the isinstance check on line 138.
        """
        gc_mock = MagicMock()
        gc_mock.get_json = AsyncMock(return_value={"error": "string-error-not-dict"})
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
                json={"item_id": "od-item-001"},
            )
        assert resp.status_code == 404


# ── Lines 125-155 via direct async handler call ──────────────────────────────


class TestAttachOneDriveDirectHandler:
    """Call attach_requisition_from_onedrive directly to guarantee coverage.

    The HTTP-layer tests verify the endpoint works end-to-end. These async tests call
    the handler function directly with mocked dependencies to guarantee coverage.py
    traces lines 125-155 regardless of xdist behaviour.
    """

    async def test_direct_success_path_creates_attachment(self, db_session, test_requisition, test_user):
        """Direct handler call — success path covers lines 128-155."""
        from unittest.mock import MagicMock

        from fastapi import Request

        from app.routers.requisitions.attachments import attach_requisition_from_onedrive

        gc_mock = MagicMock()
        gc_mock.get_json = AsyncMock(
            return_value={
                "id": "od-direct-001",
                "name": "direct_test.pdf",
                "webUrl": "https://od.example.com/direct_test.pdf",
                "size": 1024,
                "file": {"mimeType": "application/pdf"},
            }
        )

        mock_request = MagicMock(spec=Request)
        mock_request.json = AsyncMock(return_value={"item_id": "od-direct-001"})

        with (
            patch(
                "app.scheduler.get_valid_token",
                new_callable=AsyncMock,
                return_value="direct-tok",
            ),
            patch(
                "app.utils.graph_client.GraphClient",
                return_value=gc_mock,
            ),
        ):
            result = await attach_requisition_from_onedrive(
                req_id=test_requisition.id,
                request=mock_request,
                user=test_user,
                db=db_session,
            )

        assert result["file_name"] == "direct_test.pdf"
        assert result["onedrive_url"] == "https://od.example.com/direct_test.pdf"

    async def test_direct_no_token_raises_401(self, db_session, test_requisition, test_user):
        """Direct handler: no token raises HTTPException 401 — line 132."""
        from fastapi import HTTPException, Request

        from app.routers.requisitions.attachments import attach_requisition_from_onedrive

        mock_request = MagicMock(spec=Request)
        mock_request.json = AsyncMock(return_value={"item_id": "od-001"})

        with (
            patch(
                "app.scheduler.get_valid_token",
                new_callable=AsyncMock,
                return_value=None,
            ),
            pytest.raises(HTTPException) as exc_info,
        ):
            await attach_requisition_from_onedrive(
                req_id=test_requisition.id,
                request=mock_request,
                user=test_user,
                db=db_session,
            )

        assert exc_info.value.status_code == 401

    async def test_direct_graph_auth_error_raises_401(self, db_session, test_requisition, test_user):
        """Direct handler: Graph auth error → 401 — lines 137-140."""
        from fastapi import HTTPException, Request

        from app.routers.requisitions.attachments import attach_requisition_from_onedrive

        gc_mock = MagicMock()
        gc_mock.get_json = AsyncMock(return_value={"error": {"code": "InvalidAuthenticationToken"}})

        mock_request = MagicMock(spec=Request)
        mock_request.json = AsyncMock(return_value={"item_id": "od-001"})

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
            pytest.raises(HTTPException) as exc_info,
        ):
            await attach_requisition_from_onedrive(
                req_id=test_requisition.id,
                request=mock_request,
                user=test_user,
                db=db_session,
            )

        assert exc_info.value.status_code == 401

    async def test_direct_graph_access_denied_raises_403(self, db_session, test_requisition, test_user):
        """Direct handler: Graph accessDenied → 403 — line 141-143."""
        from fastapi import HTTPException, Request

        from app.routers.requisitions.attachments import attach_requisition_from_onedrive

        gc_mock = MagicMock()
        gc_mock.get_json = AsyncMock(return_value={"error": {"code": "accessDenied"}})

        mock_request = MagicMock(spec=Request)
        mock_request.json = AsyncMock(return_value={"item_id": "od-001"})

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
            pytest.raises(HTTPException) as exc_info,
        ):
            await attach_requisition_from_onedrive(
                req_id=test_requisition.id,
                request=mock_request,
                user=test_user,
                db=db_session,
            )

        assert exc_info.value.status_code == 403

    async def test_direct_item_not_found_raises_404(self, db_session, test_requisition, test_user):
        """Direct handler: unknown error code → 404 — line 143."""
        from fastapi import HTTPException, Request

        from app.routers.requisitions.attachments import attach_requisition_from_onedrive

        gc_mock = MagicMock()
        gc_mock.get_json = AsyncMock(return_value={"error": {"code": "someOtherError"}})

        mock_request = MagicMock(spec=Request)
        mock_request.json = AsyncMock(return_value={"item_id": "od-001"})

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
            pytest.raises(HTTPException) as exc_info,
        ):
            await attach_requisition_from_onedrive(
                req_id=test_requisition.id,
                request=mock_request,
                user=test_user,
                db=db_session,
            )

        assert exc_info.value.status_code == 404

    async def test_direct_missing_item_id_raises_400(self, db_session, test_requisition, test_user):
        """Direct handler: item_id missing → 400 — lines 125-127."""
        from fastapi import HTTPException, Request

        from app.routers.requisitions.attachments import attach_requisition_from_onedrive

        mock_request = MagicMock(spec=Request)
        mock_request.json = AsyncMock(return_value={})

        with pytest.raises(HTTPException) as exc_info:
            await attach_requisition_from_onedrive(
                req_id=test_requisition.id,
                request=mock_request,
                user=test_user,
                db=db_session,
            )

        assert exc_info.value.status_code == 400


# ── Line 178: delete_requisition_attachment — missing token ──────────────────


class TestDeleteRequisitionAttachmentLine178:
    def test_missing_token_returns_401(self, client, db_session, test_requisition, test_user):
        """Attachment with onedrive_item_id but no token → 401 at line 178."""
        att = _make_req_att(db_session, test_requisition.id, test_user.id)
        with patch(
            "app.scheduler.get_valid_token",
            new_callable=AsyncMock,
            return_value=None,
        ):
            resp = client.delete(f"/api/requisition-attachments/{att.id}")
        assert resp.status_code == 401

    def test_onedrive_delete_returns_401(self, client, db_session, test_requisition, test_user):
        """OneDrive returns 401 during delete → 401 re-raised — line 187-188."""
        att = _make_req_att(db_session, test_requisition.id, test_user.id)
        with (
            patch(
                "app.scheduler.get_valid_token",
                new_callable=AsyncMock,
                return_value="tok",
            ),
            patch(
                "app.http_client.http.delete",
                new_callable=AsyncMock,
                return_value=_mock_resp(401),
            ),
        ):
            resp = client.delete(f"/api/requisition-attachments/{att.id}")
        assert resp.status_code == 401

    def test_onedrive_delete_returns_403(self, client, db_session, test_requisition, test_user):
        """OneDrive returns 403 during delete → 403 re-raised — lines 189-190."""
        att = _make_req_att(db_session, test_requisition.id, test_user.id)
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
            resp = client.delete(f"/api/requisition-attachments/{att.id}")
        assert resp.status_code == 403


# ── Line 215: list_requirement_attachments — get_req_for_user returns None ───


class TestListRequirementAttachmentsLine215:
    def test_returns_403_when_parent_req_not_found(self, client, db_session, test_requisition):
        """When get_req_for_user returns None for parent, line 215 raises 403."""
        requirement = _make_requirement(db_session, test_requisition.id)
        with patch(
            "app.routers.requisitions.attachments.get_req_for_user",
            return_value=None,
        ):
            resp = client.get(f"/api/requirements/{requirement.id}/attachments")
        assert resp.status_code == 403


# ── Line 243: upload_requirement_attachment — get_req_for_user returns None ──


class TestUploadRequirementAttachmentLine243:
    def test_returns_403_when_parent_req_not_found(self, client, db_session, test_requisition):
        """When get_req_for_user returns None for parent, line 243 raises 403."""
        requirement = _make_requirement(db_session, test_requisition.id)
        with patch(
            "app.routers.requisitions.attachments.get_req_for_user",
            return_value=None,
        ):
            resp = client.post(
                f"/api/requirements/{requirement.id}/attachments",
                files={"file": ("spec.pdf", b"content", "application/pdf")},
            )
        assert resp.status_code == 403


# ── Line 255: upload_requirement_attachment — file has no filename ────────────


class TestUploadRequirementAttachmentNoFilename:
    async def test_file_with_no_filename_raises_400(self, db_session, test_requisition, test_user):
        """UploadFile with falsy filename → HTTPException 400 at line 255.

        Calls the handler function directly with a mocked UploadFile that has
        filename=None, bypassing FastAPI's multipart validation.
        """
        from fastapi import HTTPException
        from fastapi import UploadFile as FUF

        from app.routers.requisitions.attachments import upload_requirement_attachment

        requirement = _make_requirement(db_session, test_requisition.id)

        # Build a mock UploadFile with no filename
        mock_file = MagicMock(spec=FUF)
        mock_file.filename = None
        mock_file.content_type = "application/pdf"
        mock_file.read = AsyncMock(return_value=b"small content")

        with patch(
            "app.scheduler.get_valid_token",
            new_callable=AsyncMock,
            return_value="valid-tok",
        ):
            with pytest.raises(HTTPException) as exc_info:
                await upload_requirement_attachment(
                    req_id=requirement.id,
                    file=mock_file,
                    user=test_user,
                    db=db_session,
                )
        assert exc_info.value.status_code == 400
        assert "filename" in str(exc_info.value.detail).lower()


# ── Lines 309, 319: delete_requirement_attachment — 401/403 ──────────────────


class TestDeleteRequirementAttachmentLines309And319:
    def test_missing_token_returns_401(self, client, db_session, test_requisition, test_user):
        """Requirement attachment with onedrive_item_id but no token → 401."""
        requirement = _make_requirement(db_session, test_requisition.id)
        att = _make_reqmt_att(db_session, requirement.id, test_user.id)
        with patch(
            "app.scheduler.get_valid_token",
            new_callable=AsyncMock,
            return_value=None,
        ):
            resp = client.delete(f"/api/requirement-attachments/{att.id}")
        assert resp.status_code == 401

    def test_onedrive_delete_returns_401(self, client, db_session, test_requisition, test_user):
        """OneDrive returns 401 during requirement attachment delete → 401 — line
        309."""
        requirement = _make_requirement(db_session, test_requisition.id)
        att = _make_reqmt_att(db_session, requirement.id, test_user.id)
        with (
            patch(
                "app.scheduler.get_valid_token",
                new_callable=AsyncMock,
                return_value="tok",
            ),
            patch(
                "app.http_client.http.delete",
                new_callable=AsyncMock,
                return_value=_mock_resp(401),
            ),
        ):
            resp = client.delete(f"/api/requirement-attachments/{att.id}")
        assert resp.status_code == 401

    def test_onedrive_delete_returns_403(self, client, db_session, test_requisition, test_user):
        """OneDrive returns 403 during requirement attachment delete → 403 — line
        319."""
        requirement = _make_requirement(db_session, test_requisition.id)
        att = _make_reqmt_att(db_session, requirement.id, test_user.id)
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
