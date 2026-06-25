"""test_attachments_serve.py — Tests for the unified attachment serve route.

Covers GET /api/attachments/{kind}/{att_id}/content:
- 404 for unknown kind
- 404 for valid kind but missing attachment
- Library row (library_drive_id set): streams bytes
- OneDrive row (library_drive_id=None, library_web_url set): redirects
- IDOR guard: non-owner of requisition gets 404 (Fix A)

Called by: pytest
Depends on: app/routers/attachments_extra, app/services/attachment_service
"""

import os

os.environ["TESTING"] = "1"

from unittest.mock import AsyncMock, patch

from fastapi.responses import RedirectResponse, StreamingResponse

from app.models import RequisitionAttachment

# ── Helpers ──────────────────────────────────────────────────────────


def _make_library_attachment(db, requisition_id, user_id):
    """Attachment stored in company SharePoint library."""
    att = RequisitionAttachment(
        requisition_id=requisition_id,
        file_name="library_file.pdf",
        library_item_id="lib-item-1",
        library_drive_id="drive-abc",
        library_web_url="https://sharepoint.example.com/file.pdf",
        content_type="application/pdf",
        size_bytes=1024,
        uploaded_by_id=user_id,
    )
    db.add(att)
    db.commit()
    db.refresh(att)
    return att


def _make_onedrive_attachment(db, requisition_id, user_id):
    """Attachment stored in user OneDrive (no library_drive_id)."""
    att = RequisitionAttachment(
        requisition_id=requisition_id,
        file_name="onedrive_file.pdf",
        library_item_id="od-item-2",
        library_drive_id=None,
        library_web_url="https://onedrive.example.com/file.pdf",
        content_type="application/pdf",
        size_bytes=512,
        uploaded_by_id=user_id,
    )
    db.add(att)
    db.commit()
    db.refresh(att)
    return att


# ── Tests ─────────────────────────────────────────────────────────────


class TestServeAttachment:
    def test_unknown_kind_returns_404(self, client):
        resp = client.get("/api/attachments/bogus/1/content")
        assert resp.status_code == 404

    def test_valid_kind_missing_id_returns_404(self, client):
        resp = client.get("/api/attachments/requisition/999999/content")
        assert resp.status_code == 404

    def test_library_attachment_streams_bytes(self, client, db_session, test_requisition, test_user):
        """Library attachment (library_drive_id set) → streams file bytes."""
        att = _make_library_attachment(db_session, test_requisition.id, test_user.id)
        fake_bytes = b"PDF content here"
        with patch(
            "app.services.attachment_service.open_attachment",
            new_callable=AsyncMock,
            return_value=StreamingResponse(
                iter([fake_bytes]),
                media_type="application/pdf",
            ),
        ):
            resp = client.get(f"/api/attachments/requisition/{att.id}/content")
        assert resp.status_code == 200

    def test_onedrive_attachment_redirects(self, client, db_session, test_requisition, test_user):
        """OneDrive attachment (no library_drive_id) → redirect response."""
        att = _make_onedrive_attachment(db_session, test_requisition.id, test_user.id)
        with patch(
            "app.services.attachment_service.open_attachment",
            new_callable=AsyncMock,
            return_value=RedirectResponse("https://onedrive.example.com/file.pdf"),
        ):
            resp = client.get(
                f"/api/attachments/requisition/{att.id}/content",
                follow_redirects=False,
            )
        assert resp.status_code in (302, 307)

    def test_serve_non_owner_requisition_gets_404(self, client, db_session, test_requisition, test_user):
        """Fix A: user without access to a requisition gets 404 from the serve route."""
        att = _make_library_attachment(db_session, test_requisition.id, test_user.id)
        # Mock get_req_for_user to simulate a different user with no access to this req.
        with patch(
            "app.routers.attachments_extra.get_req_for_user",
            return_value=None,
        ):
            resp = client.get(f"/api/attachments/requisition/{att.id}/content")
        assert resp.status_code == 404

    def test_serve_owner_requisition_gets_file(self, client, db_session, test_requisition, test_user):
        """Fix A: owner of the requisition successfully receives the file."""
        att = _make_library_attachment(db_session, test_requisition.id, test_user.id)
        fake_bytes = b"owner file content"
        with patch(
            "app.services.attachment_service.open_attachment",
            new_callable=AsyncMock,
            return_value=StreamingResponse(iter([fake_bytes]), media_type="application/pdf"),
        ):
            resp = client.get(f"/api/attachments/requisition/{att.id}/content")
        assert resp.status_code == 200
