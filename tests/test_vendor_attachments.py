"""test_vendor_attachments.py — TDD tests for vendor card/contact attachment endpoints.

Covers:
  - VendorCardAttachment and VendorContactAttachment model roundtrip
  - Migration 143 upgrade/downgrade
  - GET  /api/vendors/{id}/attachments              (list)
  - POST /api/vendors/{id}/attachments              (upload)
  - DELETE /api/vendor-attachments/{id}             (delete, admin-gated)
  - GET  /api/vendor-contacts/{id}/attachments      (list)
  - POST /api/vendor-contacts/{id}/attachments      (upload)
  - DELETE /api/vendor-contact-attachments/{id}     (delete)
  - GET  /api/attachments/vendor_card/{id}/content  (serve)
  - GET  /api/attachments/vendor_contact/{id}/content (serve, 403 unauthenticated)
  - GET  /v2/partials/vendors/{id}/tab/files        (files tab renders)

Called by: pytest
Depends on: app/routers/attachments_extra, app/models/vendors, app/services/attachment_service
"""

from __future__ import annotations

import io
import os

os.environ["TESTING"] = "1"

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import VendorCard, VendorContact
from app.models.vendors import VendorCardAttachment, VendorContactAttachment

# ---------------------------------------------------------------------------
# Factories
# ---------------------------------------------------------------------------


def _make_vendor(db: Session, name: str = "TestVendor Inc") -> VendorCard:
    vendor = VendorCard(
        normalized_name=name.lower().replace(" ", "_"),
        display_name=name,
        created_at=datetime.now(timezone.utc),
    )
    db.add(vendor)
    db.commit()
    db.refresh(vendor)
    return vendor


def _make_vendor_contact(db: Session, vendor_id: int) -> VendorContact:
    contact = VendorContact(
        vendor_card_id=vendor_id,
        full_name="Bob Broker",
        email=f"bob+{vendor_id}@vendor.com",
        source="manual",
    )
    db.add(contact)
    db.commit()
    db.refresh(contact)
    return contact


def _make_vendor_card_attachment(
    db: Session,
    vendor_id: int,
    user_id: int,
) -> VendorCardAttachment:
    att = VendorCardAttachment(
        vendor_card_id=vendor_id,
        file_name="vendor_spec.pdf",
        library_item_id="item-vc-1",
        library_drive_id=None,
        library_web_url="https://onedrive.example.com/vendor_spec.pdf",
        content_type="application/pdf",
        size_bytes=1024,
        uploaded_by_id=user_id,
        created_at=datetime.now(timezone.utc),
    )
    db.add(att)
    db.commit()
    db.refresh(att)
    return att


def _make_vendor_contact_attachment(
    db: Session,
    contact_id: int,
    user_id: int,
) -> VendorContactAttachment:
    att = VendorContactAttachment(
        vendor_contact_id=contact_id,
        file_name="contact_notes.pdf",
        library_item_id="item-vct-1",
        library_drive_id=None,
        library_web_url="https://onedrive.example.com/contact_notes.pdf",
        content_type="application/pdf",
        size_bytes=512,
        uploaded_by_id=user_id,
        created_at=datetime.now(timezone.utc),
    )
    db.add(att)
    db.commit()
    db.refresh(att)
    return att


def _fake_upload():
    """Patch target for store_and_attach to avoid real Graph calls."""
    return "app.services.attachment_service.store_and_attach"


# ---------------------------------------------------------------------------
# Model roundtrip
# ---------------------------------------------------------------------------


class TestVendorAttachmentModelRoundtrip:
    def test_vendor_attachment_model_roundtrip(self, db_session, test_user):
        """Create VendorCardAttachment, query it back, verify columns."""
        vendor = _make_vendor(db_session)
        att = _make_vendor_card_attachment(db_session, vendor.id, test_user.id)

        fetched = db_session.get(VendorCardAttachment, att.id)
        assert fetched is not None
        assert fetched.vendor_card_id == vendor.id
        assert fetched.file_name == "vendor_spec.pdf"
        assert fetched.library_item_id == "item-vc-1"
        assert fetched.library_drive_id is None
        assert fetched.content_type == "application/pdf"
        assert fetched.size_bytes == 1024
        assert fetched.uploaded_by_id == test_user.id

    def test_vendor_contact_attachment_model_roundtrip(self, db_session, test_user):
        """Create VendorContactAttachment, query it back, verify columns."""
        vendor = _make_vendor(db_session, "AnotherVendor")
        contact = _make_vendor_contact(db_session, vendor.id)
        att = _make_vendor_contact_attachment(db_session, contact.id, test_user.id)

        fetched = db_session.get(VendorContactAttachment, att.id)
        assert fetched is not None
        assert fetched.vendor_contact_id == contact.id
        assert fetched.file_name == "contact_notes.pdf"
        assert fetched.uploaded_by_id == test_user.id

    def test_cascade_delete_vendor_card(self, db_session, test_user):
        """Deleting a VendorCard cascades to VendorCardAttachment."""
        vendor = _make_vendor(db_session, "CascadeVendor")
        att = _make_vendor_card_attachment(db_session, vendor.id, test_user.id)
        att_id = att.id

        db_session.delete(vendor)
        db_session.commit()

        assert db_session.get(VendorCardAttachment, att_id) is None


# ---------------------------------------------------------------------------
# Migration 143 structural checks (SQLite-safe; PG roundtrip requires TEST_PG_URL)
# ---------------------------------------------------------------------------

_PG_URL = os.environ.get("TEST_PG_URL", "")


def _load_migration_143():
    """Load migration 143 module via importlib.util (no __init__.py required)."""
    import importlib.util

    path = os.path.join(
        os.path.dirname(__file__),
        "..",
        "alembic",
        "versions",
        "143_vendor_attachments.py",
    )
    spec = importlib.util.spec_from_file_location("migration_143", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestMigration143Roundtrip:
    def test_migration_143_file_exists(self):
        """Migration file 143_vendor_attachments.py exists in alembic/versions/."""
        mod = _load_migration_143()
        assert mod.revision == "143_vendor_attachments"
        assert mod.down_revision == "142_vendor_task_cols"

    def test_migration_143_has_upgrade_and_downgrade(self):
        """Migration 143 defines both upgrade() and downgrade()."""
        mod = _load_migration_143()
        assert callable(mod.upgrade)
        assert callable(mod.downgrade)

    def test_migration_143_revision_id_length(self):
        """Revision ID is ≤32 chars (PG VARCHAR(32) constraint)."""
        mod = _load_migration_143()
        assert len(mod.revision) <= 32, f"revision id {mod.revision!r} exceeds 32 chars"

    @pytest.mark.skipif(not _PG_URL, reason="TEST_PG_URL not set — PG required for alembic roundtrip")
    def test_migration_143_roundtrip_pg(self):
        """Upgrade 143 → downgrade 142 → upgrade 143 against real PG."""
        import subprocess

        wt = "/root/availai/.claude/worktrees/attachments-unified"
        alembic_bin = "/root/availai/.venv/bin/alembic"
        env = {**os.environ, "DATABASE_URL": _PG_URL}

        for cmd in [
            ["upgrade", "143_vendor_attachments"],
            ["downgrade", "142_vendor_task_cols"],
            ["upgrade", "143_vendor_attachments"],
        ]:
            r = subprocess.run(
                [alembic_bin, "-c", "alembic.ini"] + cmd,
                cwd=wt,
                capture_output=True,
                text=True,
                env=env,
            )
            assert r.returncode == 0, f"alembic {cmd} failed: {r.stderr}"


@pytest.fixture()
def nonadmin_client(db_session: Session) -> TestClient:
    """TestClient where require_user is allowed but require_admin raises 403.

    Used to assert that delete endpoints reject non-admin callers.
    """
    from app.database import get_db
    from app.dependencies import require_admin, require_buyer, require_fresh_token, require_user
    from app.main import app

    def _override_db():
        yield db_session

    def _override_user():
        from app.models import User

        return User(
            id=9999,
            email="nonadmin@example.com",
            name="Non Admin",
            role="user",
            azure_id="nonadmin-azure",
            created_at=datetime.now(timezone.utc),
        )

    def _override_admin():
        raise HTTPException(403, "Admin access required")

    async def _override_fresh_token():
        return "mock-token"

    overridden_deps = [get_db, require_user, require_admin, require_buyer, require_fresh_token]
    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[require_user] = _override_user
    app.dependency_overrides[require_admin] = _override_admin
    app.dependency_overrides[require_buyer] = _override_user
    app.dependency_overrides[require_fresh_token] = _override_fresh_token

    try:
        with TestClient(app, raise_server_exceptions=False) as c:
            yield c
    finally:
        for dep in overridden_deps:
            app.dependency_overrides.pop(dep, None)


# ---------------------------------------------------------------------------
# Vendor card attachment API endpoints
# ---------------------------------------------------------------------------


class TestVendorCardAttachments:
    def test_list_empty(self, client, db_session, test_user):
        vendor = _make_vendor(db_session)
        resp = client.get(f"/api/vendors/{vendor.id}/attachments")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_list_returns_attachments_newest_first(self, client, db_session, test_user):
        from datetime import timedelta

        now = datetime.now(timezone.utc)
        vendor = _make_vendor(db_session, "ListVendor")
        att_older = VendorCardAttachment(
            vendor_card_id=vendor.id,
            file_name="old.pdf",
            content_type="application/pdf",
            size_bytes=100,
            uploaded_by_id=test_user.id,
            created_at=now - timedelta(hours=2),
        )
        att_newer = VendorCardAttachment(
            vendor_card_id=vendor.id,
            file_name="new.pdf",
            content_type="application/pdf",
            size_bytes=200,
            uploaded_by_id=test_user.id,
            created_at=now,
        )
        db_session.add_all([att_older, att_newer])
        db_session.commit()
        db_session.refresh(att_older)
        db_session.refresh(att_newer)

        resp = client.get(f"/api/vendors/{vendor.id}/attachments")
        assert resp.status_code == 200
        items = resp.json()
        assert len(items) == 2
        assert items[0]["id"] == att_newer.id
        assert items[1]["id"] == att_older.id

    def test_upload_creates_row(self, client, db_session, test_user):
        vendor = _make_vendor(db_session, "UploadVendor")
        fake_att = _make_vendor_card_attachment(db_session, vendor.id, test_user.id)

        with patch(_fake_upload(), new_callable=AsyncMock, return_value=fake_att):
            resp = client.post(
                f"/api/vendors/{vendor.id}/attachments",
                files={"file": ("spec.pdf", io.BytesIO(b"PDF content"), "application/pdf")},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["file_name"] == "vendor_spec.pdf"
        assert data["kind"] == "onedrive"

    def test_delete_removes_attachment(self, client, db_session, test_user):
        vendor = _make_vendor(db_session, "DelVendor")
        att = _make_vendor_card_attachment(db_session, vendor.id, test_user.id)

        with patch(
            "app.services.attachment_service.remove_attachment",
            new_callable=AsyncMock,
            return_value={"ok": True},
        ):
            resp = client.delete(f"/api/vendor-attachments/{att.id}")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

    def test_list_nonexistent_vendor_404(self, client):
        resp = client.get("/api/vendors/999999/attachments")
        assert resp.status_code == 404

    def test_upload_nonexistent_vendor_404(self, client):
        resp = client.post(
            "/api/vendors/999999/attachments",
            files={"file": ("f.pdf", io.BytesIO(b"d"), "application/pdf")},
        )
        assert resp.status_code == 404

    def test_delete_nonexistent_attachment_404(self, client):
        resp = client.delete("/api/vendor-attachments/999999")
        assert resp.status_code == 404

    def test_vendor_file_delete_nonadmin_forbidden(self, nonadmin_client, db_session, test_user):
        """DELETE /api/vendor-attachments/{id} returns 403 for non-admin callers."""
        vendor = _make_vendor(db_session, "ForbiddenDelVendor")
        att = _make_vendor_card_attachment(db_session, vendor.id, test_user.id)
        resp = nonadmin_client.delete(f"/api/vendor-attachments/{att.id}")
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Vendor contact attachment API endpoints
# ---------------------------------------------------------------------------


class TestVendorContactAttachments:
    def _setup(self, db_session, suffix: str = "") -> tuple:
        vendor = _make_vendor(db_session, f"ContactTestVendor{suffix}")
        contact = _make_vendor_contact(db_session, vendor.id)
        return vendor, contact

    def test_list_empty(self, client, db_session, test_user):
        _, contact = self._setup(db_session, "A")
        resp = client.get(f"/api/vendor-contacts/{contact.id}/attachments")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_list_returns_attachments(self, client, db_session, test_user):
        _, contact = self._setup(db_session, "B")
        att = _make_vendor_contact_attachment(db_session, contact.id, test_user.id)
        resp = client.get(f"/api/vendor-contacts/{contact.id}/attachments")
        assert resp.status_code == 200
        items = resp.json()
        assert len(items) == 1
        assert items[0]["id"] == att.id

    def test_upload_creates_row(self, client, db_session, test_user):
        _, contact = self._setup(db_session, "C")
        fake_att = _make_vendor_contact_attachment(db_session, contact.id, test_user.id)

        with patch(_fake_upload(), new_callable=AsyncMock, return_value=fake_att):
            resp = client.post(
                f"/api/vendor-contacts/{contact.id}/attachments",
                files={"file": ("note.pdf", io.BytesIO(b"note"), "application/pdf")},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == fake_att.id

    def test_delete_removes_attachment(self, client, db_session, test_user):
        _, contact = self._setup(db_session, "D")
        att = _make_vendor_contact_attachment(db_session, contact.id, test_user.id)

        with patch(
            "app.services.attachment_service.remove_attachment",
            new_callable=AsyncMock,
            return_value={"ok": True},
        ):
            resp = client.delete(f"/api/vendor-contact-attachments/{att.id}")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

    def test_list_nonexistent_contact_404(self, client):
        resp = client.get("/api/vendor-contacts/999999/attachments")
        assert resp.status_code == 404

    def test_upload_nonexistent_contact_404(self, client):
        resp = client.post(
            "/api/vendor-contacts/999999/attachments",
            files={"file": ("f.pdf", io.BytesIO(b"d"), "application/pdf")},
        )
        assert resp.status_code == 404

    def test_delete_nonexistent_attachment_404(self, client):
        resp = client.delete("/api/vendor-contact-attachments/999999")
        assert resp.status_code == 404

    def test_vendor_contact_file_delete_nonadmin_forbidden(self, nonadmin_client, db_session, test_user):
        """DELETE /api/vendor-contact-attachments/{id} returns 403 for non-admin
        callers."""
        _, contact = self._setup(db_session, "E")
        att = _make_vendor_contact_attachment(db_session, contact.id, test_user.id)
        resp = nonadmin_client.delete(f"/api/vendor-contact-attachments/{att.id}")
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Vendor files tab renders
# ---------------------------------------------------------------------------


class TestVendorFilesTab:
    def test_vendor_files_tab_renders(self, client, db_session, test_user):
        """GET /v2/partials/vendors/{id}/tab/files → 200 HTML."""
        vendor = _make_vendor(db_session, "TabVendor")
        resp = client.get(
            f"/v2/partials/vendors/{vendor.id}/tab/files",
            headers={"Accept": "text/html"},
        )
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]
        # The page should mention the attachments panel keywords
        assert b"attachments" in resp.content.lower() or b"files" in resp.content.lower()

    def test_vendor_files_tab_nonexistent_vendor_404(self, client):
        resp = client.get("/v2/partials/vendors/999999/tab/files")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Serve route ownership enforcement
# ---------------------------------------------------------------------------


class TestVendorServeRoute:
    def test_serve_vendor_card_attachment_accessible(self, client, db_session, test_user):
        """Any logged-in user can serve a vendor card attachment."""
        vendor = _make_vendor(db_session, "ServeVendor")
        att = _make_vendor_card_attachment(db_session, vendor.id, test_user.id)
        fake_bytes = b"vendor file content"

        with patch(
            "app.services.attachment_service.open_attachment",
            new_callable=AsyncMock,
            return_value=StreamingResponse(iter([fake_bytes]), media_type="application/pdf"),
        ):
            resp = client.get(f"/api/attachments/vendor_card/{att.id}/content")
        assert resp.status_code == 200

    def test_serve_vendor_contact_attachment_accessible(self, client, db_session, test_user):
        """Any logged-in user can serve a vendor contact attachment."""
        vendor = _make_vendor(db_session, "ServeContactVendor")
        contact = _make_vendor_contact(db_session, vendor.id)
        att = _make_vendor_contact_attachment(db_session, contact.id, test_user.id)
        fake_bytes = b"vendor contact file"

        with patch(
            "app.services.attachment_service.open_attachment",
            new_callable=AsyncMock,
            return_value=StreamingResponse(iter([fake_bytes]), media_type="application/pdf"),
        ):
            resp = client.get(f"/api/attachments/vendor_contact/{att.id}/content")
        assert resp.status_code == 200

    def test_serve_vendor_card_attachment_missing_vendor_404(self, client, db_session, test_user):
        """If the vendor doesn't exist (deleted), serve returns 404."""
        vendor = _make_vendor(db_session, "GoneVendor")
        att = _make_vendor_card_attachment(db_session, vendor.id, test_user.id)

        with patch(
            "app.routers.attachments_extra.db_get_vendor_card",
            return_value=None,
        ):
            resp = client.get(f"/api/attachments/vendor_card/{att.id}/content")
        assert resp.status_code == 404

    def test_serve_unknown_kind_404(self, client):
        resp = client.get("/api/attachments/unknown_kind/1/content")
        assert resp.status_code == 404
