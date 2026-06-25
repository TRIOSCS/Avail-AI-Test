"""test_attachments_extra.py — Tests for company/contact/material-card attachment
endpoints.

Covers Task 4 endpoints in app/routers/attachments_extra.py:
  GET/POST /api/companies/{id}/attachments
  DELETE   /api/company-attachments/{id}
  GET/POST /api/contacts/{id}/attachments
  DELETE   /api/contact-attachments/{id}
  GET/POST /api/material-cards/{id}/attachments
  DELETE   /api/material-card-attachments/{id}
  GET      /api/attachments/company/{id}/content (ownership enforcement)

Called by: pytest
Depends on: app/routers/attachments_extra, app/services/attachment_service
"""

import io
import os

os.environ["TESTING"] = "1"

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.models import (
    Company,
    CompanyAttachment,
    CustomerSite,
    MaterialCard,
    MaterialCardAttachment,
    SiteContact,
    SiteContactAttachment,
)

# ---------------------------------------------------------------------------
# Factories
# ---------------------------------------------------------------------------


def _make_company(db: Session, name: str = "TestCo", owner_id: int | None = None) -> Company:
    co = Company(
        name=name,
        is_active=True,
        account_owner_id=owner_id,
        created_at=datetime.now(timezone.utc),
    )
    db.add(co)
    db.commit()
    db.refresh(co)
    return co


def _make_other_company(db: Session) -> Company:
    """A second company NOT owned by the test user.

    Post phase1-authz: company/contact attachment access is gated by can_manage_account,
    so a non-owner (non-manager) user can no longer reach this company's attachments.
    """
    return _make_company(db, "OtherCo")


def _make_site(db: Session, company_id: int) -> CustomerSite:
    site = CustomerSite(
        company_id=company_id,
        site_name="HQ",
        is_active=True,
        created_at=datetime.now(timezone.utc),
    )
    db.add(site)
    db.commit()
    db.refresh(site)
    return site


def _make_contact(db: Session, site_id: int) -> SiteContact:
    contact = SiteContact(
        customer_site_id=site_id,
        full_name="Jane Buyer",
        email="jane@example.com",
        is_active=True,
        created_at=datetime.now(timezone.utc),
    )
    db.add(contact)
    db.commit()
    db.refresh(contact)
    return contact


def _make_material_card(db: Session) -> MaterialCard:
    card = MaterialCard(
        normalized_mpn="lm317t",
        display_mpn="LM317T",
        manufacturer="Texas Instruments",
        created_at=datetime.now(timezone.utc),
    )
    db.add(card)
    db.commit()
    db.refresh(card)
    return card


def _make_company_attachment(
    db: Session,
    company_id: int,
    user_id: int,
    created_at: datetime | None = None,
) -> CompanyAttachment:
    att = CompanyAttachment(
        company_id=company_id,
        file_name="spec.pdf",
        library_item_id="item-co-1",
        library_drive_id=None,
        library_web_url="https://onedrive.example.com/spec.pdf",
        content_type="application/pdf",
        size_bytes=2048,
        uploaded_by_id=user_id,
        created_at=created_at or datetime.now(timezone.utc),
    )
    db.add(att)
    db.commit()
    db.refresh(att)
    return att


def _make_contact_attachment(db: Session, contact_id: int, user_id: int) -> SiteContactAttachment:
    att = SiteContactAttachment(
        site_contact_id=contact_id,
        file_name="contact_file.docx",
        library_item_id="item-ct-1",
        library_drive_id=None,
        library_web_url="https://onedrive.example.com/contact_file.docx",
        content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        size_bytes=512,
        uploaded_by_id=user_id,
        created_at=datetime.now(timezone.utc),
    )
    db.add(att)
    db.commit()
    db.refresh(att)
    return att


def _make_material_attachment(db: Session, card_id: int, user_id: int) -> MaterialCardAttachment:
    att = MaterialCardAttachment(
        material_card_id=card_id,
        file_name="drawing.pdf",
        library_item_id="item-mc-1",
        library_drive_id=None,
        library_web_url="https://onedrive.example.com/drawing.pdf",
        content_type="application/pdf",
        size_bytes=4096,
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
# Company attachment tests
# ---------------------------------------------------------------------------


class TestCompanyAttachments:
    def test_list_empty(self, client, db_session, test_user):
        co = _make_company(db_session, owner_id=test_user.id)
        resp = client.get(f"/api/companies/{co.id}/attachments")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_list_returns_attachments_newest_first(self, client, db_session, test_user):
        now = datetime.now(timezone.utc)
        co = _make_company(db_session, owner_id=test_user.id)
        att_older = _make_company_attachment(db_session, co.id, test_user.id, created_at=now - timedelta(hours=1))
        att_newer = _make_company_attachment(db_session, co.id, test_user.id, created_at=now)
        resp = client.get(f"/api/companies/{co.id}/attachments")
        assert resp.status_code == 200
        items = resp.json()
        assert len(items) == 2
        # Newest first: att_newer must precede att_older.
        assert items[0]["id"] == att_newer.id
        assert items[1]["id"] == att_older.id

    def test_upload_creates_row(self, client, db_session, test_user):
        co = _make_company(db_session, owner_id=test_user.id)
        fake_att = _make_company_attachment(db_session, co.id, test_user.id)

        with patch(_fake_upload(), new_callable=AsyncMock, return_value=fake_att):
            resp = client.post(
                f"/api/companies/{co.id}/attachments",
                files={"file": ("test.pdf", io.BytesIO(b"PDF content"), "application/pdf")},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["file_name"] == "spec.pdf"
        assert data["kind"] == "onedrive"

    def test_delete_removes_attachment(self, client, db_session, test_user):
        co = _make_company(db_session, owner_id=test_user.id)
        att = _make_company_attachment(db_session, co.id, test_user.id)

        with patch(
            "app.services.attachment_service.remove_attachment",
            new_callable=AsyncMock,
            return_value={"ok": True},
        ):
            resp = client.delete(f"/api/company-attachments/{att.id}")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

    def test_list_nonexistent_company_404(self, client):
        resp = client.get("/api/companies/999999/attachments")
        assert resp.status_code == 404

    def test_upload_nonexistent_company_404(self, client):
        resp = client.post(
            "/api/companies/999999/attachments",
            files={"file": ("test.pdf", io.BytesIO(b"data"), "application/pdf")},
        )
        assert resp.status_code == 404

    def test_delete_nonexistent_attachment_404(self, client):
        resp = client.delete("/api/company-attachments/999999")
        assert resp.status_code == 404

    def test_list_other_company_blocked_for_non_owner(self, client, db_session):
        """A non-owner (non-manager) user is gated out of another account's attachments.

        Phase1-authz closed the IDOR where user_can_access_company was a no-op: the buyer
        test_user does not own OtherCo, so the list endpoint now 404s (no existence leak).
        """
        co = _make_other_company(db_session)
        resp = client.get(f"/api/companies/{co.id}/attachments")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Contact attachment tests
# ---------------------------------------------------------------------------


class TestContactAttachments:
    def _setup(self, db_session, test_user):
        co = _make_company(db_session, owner_id=test_user.id)
        site = _make_site(db_session, co.id)
        contact = _make_contact(db_session, site.id)
        return co, site, contact

    def test_list_empty(self, client, db_session, test_user):
        _, _, contact = self._setup(db_session, test_user)
        resp = client.get(f"/api/contacts/{contact.id}/attachments")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_list_returns_attachments(self, client, db_session, test_user):
        _, _, contact = self._setup(db_session, test_user)
        att = _make_contact_attachment(db_session, contact.id, test_user.id)
        resp = client.get(f"/api/contacts/{contact.id}/attachments")
        assert resp.status_code == 200
        items = resp.json()
        assert len(items) == 1
        assert items[0]["id"] == att.id

    def test_upload_creates_row(self, client, db_session, test_user):
        _, _, contact = self._setup(db_session, test_user)
        fake_att = _make_contact_attachment(db_session, contact.id, test_user.id)

        with patch(_fake_upload(), new_callable=AsyncMock, return_value=fake_att):
            resp = client.post(
                f"/api/contacts/{contact.id}/attachments",
                files={
                    "file": (
                        "note.docx",
                        io.BytesIO(b"docx data"),
                        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    )
                },
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == fake_att.id

    def test_delete_removes_attachment(self, client, db_session, test_user):
        _, _, contact = self._setup(db_session, test_user)
        att = _make_contact_attachment(db_session, contact.id, test_user.id)

        with patch(
            "app.services.attachment_service.remove_attachment",
            new_callable=AsyncMock,
            return_value={"ok": True},
        ):
            resp = client.delete(f"/api/contact-attachments/{att.id}")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

    def test_list_nonexistent_contact_404(self, client):
        resp = client.get("/api/contacts/999999/attachments")
        assert resp.status_code == 404

    def test_upload_nonexistent_contact_404(self, client):
        resp = client.post(
            "/api/contacts/999999/attachments",
            files={"file": ("f.pdf", io.BytesIO(b"d"), "application/pdf")},
        )
        assert resp.status_code == 404

    def test_delete_nonexistent_attachment_404(self, client):
        resp = client.delete("/api/contact-attachments/999999")
        assert resp.status_code == 404

    def test_contact_under_inaccessible_company_gives_404_on_list(self, client, db_session, test_user):
        """Contact whose parent company doesn't exist → 404 (existence leak
        prevention)."""
        # Create a contact then orphan the company by deleting the site link
        _, _, contact = self._setup(db_session, test_user)
        # Patch user_can_access_company to return None (simulates missing company)
        with patch(
            "app.routers.attachments_extra.user_can_access_company",
            return_value=None,
        ):
            resp = client.get(f"/api/contacts/{contact.id}/attachments")
        assert resp.status_code == 404

    def test_contact_under_inaccessible_company_gives_404_on_upload(self, client, db_session, test_user):
        _, _, contact = self._setup(db_session, test_user)
        with patch(
            "app.routers.attachments_extra.user_can_access_company",
            return_value=None,
        ):
            resp = client.post(
                f"/api/contacts/{contact.id}/attachments",
                files={"file": ("f.pdf", io.BytesIO(b"d"), "application/pdf")},
            )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Material card attachment tests
# ---------------------------------------------------------------------------


class TestMaterialCardAttachments:
    def test_list_empty(self, client, db_session):
        card = _make_material_card(db_session)
        resp = client.get(f"/api/material-cards/{card.id}/attachments")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_list_returns_attachments(self, client, db_session, test_user):
        card = _make_material_card(db_session)
        att = _make_material_attachment(db_session, card.id, test_user.id)
        resp = client.get(f"/api/material-cards/{card.id}/attachments")
        assert resp.status_code == 200
        items = resp.json()
        assert len(items) == 1
        assert items[0]["id"] == att.id

    def test_upload_creates_row(self, client, db_session, test_user):
        card = _make_material_card(db_session)
        fake_att = _make_material_attachment(db_session, card.id, test_user.id)

        with patch(_fake_upload(), new_callable=AsyncMock, return_value=fake_att):
            resp = client.post(
                f"/api/material-cards/{card.id}/attachments",
                files={"file": ("drawing.pdf", io.BytesIO(b"pdf data"), "application/pdf")},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == fake_att.id

    def test_delete_removes_attachment(self, client, db_session, test_user):
        card = _make_material_card(db_session)
        att = _make_material_attachment(db_session, card.id, test_user.id)

        with patch(
            "app.services.attachment_service.remove_attachment",
            new_callable=AsyncMock,
            return_value={"ok": True},
        ):
            resp = client.delete(f"/api/material-card-attachments/{att.id}")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

    def test_list_nonexistent_card_404(self, client):
        resp = client.get("/api/material-cards/999999/attachments")
        assert resp.status_code == 404

    def test_upload_nonexistent_card_404(self, client):
        resp = client.post(
            "/api/material-cards/999999/attachments",
            files={"file": ("f.pdf", io.BytesIO(b"d"), "application/pdf")},
        )
        assert resp.status_code == 404

    def test_delete_nonexistent_attachment_404(self, client):
        resp = client.delete("/api/material-card-attachments/999999")
        assert resp.status_code == 404

    def test_any_logged_in_user_can_list_material_attachments(self, client, db_session):
        """Material cards are shared catalog — any authenticated user can access."""
        card = _make_material_card(db_session)
        resp = client.get(f"/api/material-cards/{card.id}/attachments")
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Serve route: company ownership enforcement
# ---------------------------------------------------------------------------


class TestServeRouteCompanyOwnership:
    def test_serve_company_attachment_accessible(self, client, db_session, test_user):
        """Any logged-in user can serve a company attachment (mirrors
        company_detail_partial)."""
        co = _make_company(db_session, owner_id=test_user.id)
        att = _make_company_attachment(db_session, co.id, test_user.id)
        fake_bytes = b"company file content"

        with patch(
            "app.services.attachment_service.open_attachment",
            new_callable=AsyncMock,
            return_value=StreamingResponse(iter([fake_bytes]), media_type="application/pdf"),
        ):
            resp = client.get(f"/api/attachments/company/{att.id}/content")
        assert resp.status_code == 200

    def test_serve_company_attachment_missing_company_404(self, client, db_session, test_user):
        """If company_can_access returns None (missing company), serve returns 404."""
        co = _make_company(db_session, owner_id=test_user.id)
        att = _make_company_attachment(db_session, co.id, test_user.id)

        with patch(
            "app.routers.attachments_extra.user_can_access_company",
            return_value=None,
        ):
            resp = client.get(f"/api/attachments/company/{att.id}/content")
        assert resp.status_code == 404

    def test_serve_contact_attachment_missing_company_404(self, client, db_session, test_user):
        """Contact attachment whose company is inaccessible → 404."""
        co = _make_company(db_session, owner_id=test_user.id)
        site = _make_site(db_session, co.id)
        contact = _make_contact(db_session, site.id)
        att = _make_contact_attachment(db_session, contact.id, test_user.id)

        with patch(
            "app.routers.attachments_extra.user_can_access_company",
            return_value=None,
        ):
            resp = client.get(f"/api/attachments/contact/{att.id}/content")
        assert resp.status_code == 404

    def test_serve_material_attachment_any_user(self, client, db_session, test_user):
        """Material card attachments are accessible to any logged-in user."""
        card = _make_material_card(db_session)
        att = _make_material_attachment(db_session, card.id, test_user.id)
        fake_bytes = b"material file"

        with patch(
            "app.services.attachment_service.open_attachment",
            new_callable=AsyncMock,
            return_value=StreamingResponse(iter([fake_bytes]), media_type="application/pdf"),
        ):
            resp = client.get(f"/api/attachments/material/{att.id}/content")
        assert resp.status_code == 200
