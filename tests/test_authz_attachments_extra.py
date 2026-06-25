"""Regression tests: cross-tenant IDOR guard on app/routers/attachments_extra.py.

user_can_access_company() was a deliberate NO-OP (returned the Company with no ownership
check), leaving all four company/contact attachment mutators ungated. It now enforces
can_manage_account, so a SALES (restricted) non-owner gets 404 (callers 404 on None) on:
  POST   /api/companies/{id}/attachments         (upload_company_attachment)
  DELETE /api/company-attachments/{id}           (delete_company_attachment)
  POST   /api/contacts/{id}/attachments          (upload_contact_attachment)
  DELETE /api/contact-attachments/{id}           (delete_contact_attachment)

Contact callers resolve the owning company via contact → CustomerSite → Company and pass
that company_id to the helper, so the same gate protects them. Owner/manager pass.

Setup style mirrors tests/test_attachments_extra.py (store/remove mocked on the service).
"""

import io
from unittest.mock import AsyncMock, patch

import pytest

from app.constants import UserRole
from app.models import (
    Company,
    CompanyAttachment,
    CustomerSite,
    SiteContact,
    SiteContactAttachment,
)

_STORE = "app.services.attachment_service.store_and_attach"
_REMOVE = "app.services.attachment_service.remove_attachment"


def _foreign_company(db, admin_user, name="Foreign Co"):
    co = Company(name=name, account_owner_id=admin_user.id, is_active=True)
    db.add(co)
    db.commit()
    db.refresh(co)
    return co


def _company_attachment(db, company_id):
    att = CompanyAttachment(company_id=company_id, file_name="x.pdf")
    db.add(att)
    db.commit()
    db.refresh(att)
    return att


def _contact_under(db, company_id):
    site = CustomerSite(company_id=company_id, site_name="HQ")
    db.add(site)
    db.commit()
    db.refresh(site)
    contact = SiteContact(customer_site_id=site.id, full_name="Jane Doe")
    db.add(contact)
    db.commit()
    db.refresh(contact)
    return contact


def _contact_attachment(db, contact_id):
    att = SiteContactAttachment(site_contact_id=contact_id, file_name="c.pdf")
    db.add(att)
    db.commit()
    db.refresh(att)
    return att


def _make_sales(test_user, db_session):
    test_user.role = UserRole.SALES
    db_session.commit()


# ── Non-owner SALES → 404 on company attach/delete ──────────────────────


def test_upload_company_attachment_blocks_non_owner_sales(client, db_session, test_user, admin_user):
    _make_sales(test_user, db_session)
    co = _foreign_company(db_session, admin_user)
    resp = client.post(
        f"/api/companies/{co.id}/attachments",
        files={"file": ("x.pdf", io.BytesIO(b"data"), "application/pdf")},
    )
    assert resp.status_code == 404


def test_delete_company_attachment_blocks_non_owner_sales(client, db_session, test_user, admin_user):
    _make_sales(test_user, db_session)
    co = _foreign_company(db_session, admin_user)
    att = _company_attachment(db_session, co.id)
    resp = client.delete(f"/api/company-attachments/{att.id}")
    assert resp.status_code == 404


# ── Non-owner SALES → 404 on contact attach/delete ──────────────────────


def test_upload_contact_attachment_blocks_non_owner_sales(client, db_session, test_user, admin_user):
    _make_sales(test_user, db_session)
    co = _foreign_company(db_session, admin_user)
    contact = _contact_under(db_session, co.id)
    resp = client.post(
        f"/api/contacts/{contact.id}/attachments",
        files={"file": ("c.pdf", io.BytesIO(b"data"), "application/pdf")},
    )
    assert resp.status_code == 404


def test_delete_contact_attachment_blocks_non_owner_sales(client, db_session, test_user, admin_user):
    _make_sales(test_user, db_session)
    co = _foreign_company(db_session, admin_user)
    contact = _contact_under(db_session, co.id)
    att = _contact_attachment(db_session, contact.id)
    resp = client.delete(f"/api/contact-attachments/{att.id}")
    assert resp.status_code == 404


# ── Owner SALES passes the gate (company) ───────────────────────────────


def test_upload_company_attachment_allows_owning_sales(client, db_session, test_user):
    test_user.role = UserRole.SALES
    co = Company(name="My Co", account_owner_id=test_user.id, is_active=True)
    db_session.add(co)
    db_session.commit()
    db_session.refresh(co)
    fake = CompanyAttachment(id=1, company_id=co.id, file_name="x.pdf")
    with patch(_STORE, new_callable=AsyncMock, return_value=fake):
        resp = client.post(
            f"/api/companies/{co.id}/attachments",
            files={"file": ("x.pdf", io.BytesIO(b"data"), "application/pdf")},
        )
    assert resp.status_code == 200


def test_delete_company_attachment_allows_owning_sales(client, db_session, test_user):
    test_user.role = UserRole.SALES
    co = Company(name="My Co", account_owner_id=test_user.id, is_active=True)
    db_session.add(co)
    db_session.commit()
    db_session.refresh(co)
    att = _company_attachment(db_session, co.id)
    with patch(_REMOVE, new_callable=AsyncMock, return_value={"ok": True}):
        resp = client.delete(f"/api/company-attachments/{att.id}")
    assert resp.status_code == 200


# ── Manager passes the gate regardless of ownership (contact) ───────────


def test_upload_contact_attachment_allows_manager(client, db_session, test_user, admin_user):
    test_user.role = UserRole.MANAGER
    db_session.commit()
    co = _foreign_company(db_session, admin_user)
    contact = _contact_under(db_session, co.id)
    fake = SiteContactAttachment(id=1, site_contact_id=contact.id, file_name="c.pdf")
    with patch(_STORE, new_callable=AsyncMock, return_value=fake):
        resp = client.post(
            f"/api/contacts/{contact.id}/attachments",
            files={"file": ("c.pdf", io.BytesIO(b"data"), "application/pdf")},
        )
    assert resp.status_code == 200


def test_delete_contact_attachment_allows_manager(client, db_session, test_user, admin_user):
    test_user.role = UserRole.MANAGER
    db_session.commit()
    co = _foreign_company(db_session, admin_user)
    contact = _contact_under(db_session, co.id)
    att = _contact_attachment(db_session, contact.id)
    with patch(_REMOVE, new_callable=AsyncMock, return_value={"ok": True}):
        resp = client.delete(f"/api/contact-attachments/{att.id}")
    assert resp.status_code == 200


@pytest.mark.parametrize("path", ["/api/companies/999999/attachments"])
def test_missing_company_still_404(client, db_session, test_user, path):
    test_user.role = UserRole.MANAGER
    db_session.commit()
    resp = client.post(path, files={"file": ("x.pdf", io.BytesIO(b"d"), "application/pdf")})
    assert resp.status_code == 404
