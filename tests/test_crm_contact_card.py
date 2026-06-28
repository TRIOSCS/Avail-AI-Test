"""test_crm_contact_card.py — CRM customer-tab contact-card rework (2026-06-26).

Covers the four reworked asks on the active `contact_row` macro + its expand drawer:
  1. Role dropdown — the canonical ContactRole vocabulary (buyer/manager/engineer/
     planner/other) is accepted by the setter; legacy/unknown values 400; the editor
     renders all five options; legacy DB values still render read-only.
  2. Phone + email — click-to-contact (tel: / Outlook-compose) links in the row + drawer.
  3. Recent notes + notes modal — GET renders the feed + add form; POST logs an
     ActivityLog NOTE (can_manage_account gated; blank → inline error).
  4. Horizontal layout — the drawer lays detail fields in a dense flex-wrap flow.

Also pins: the ContactRole enum is the single source of truth (CANONICAL_ROLES /
the `roles` Jinja global both derive from it) and the dead `contact_card` macro is gone.

Called by: pytest
Depends on: app.constants.ContactRole, app.routers.htmx_views, app.template_env,
    app.models (Company, CustomerSite, SiteContact, ActivityLog)
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.constants import ContactRole
from app.models import Company
from app.models.auth import User
from app.models.crm import CustomerSite, SiteContact
from app.models.intelligence import ActivityLog


def _company_site_contact(
    db: Session,
    *,
    owner_id: int | None = None,
    contact_role: str | None = None,
    phone: str | None = "+16175551212",
    email: str | None = "pat@buyerco.com",
    do_not_contact: bool = False,
):
    """Create a company + active site + active contact for contact-card tests."""
    company = Company(name="BuyerCo", is_active=True, account_owner_id=owner_id)
    db.add(company)
    db.flush()
    site = CustomerSite(company_id=company.id, site_name="HQ", is_active=True)
    db.add(site)
    db.flush()
    contact = SiteContact(
        customer_site_id=site.id,
        full_name="Pat Buyer",
        email=email,
        phone=phone,
        contact_role=contact_role,
        do_not_contact=do_not_contact,
    )
    db.add(contact)
    db.commit()
    db.refresh(company)
    db.refresh(contact)
    return company, site, contact


# ─────────────────────────────────────────────────────────────────────────────
# 0. ContactRole enum is the single source of truth
# ─────────────────────────────────────────────────────────────────────────────


class TestContactRoleSourceOfTruth:
    def test_enum_members_are_mikes_vocabulary(self):
        assert tuple(ContactRole) == (
            ContactRole.BUYER,
            ContactRole.MANAGER,
            ContactRole.ENGINEER,
            ContactRole.PLANNER,
            ContactRole.OTHER,
        )
        assert [r.value for r in ContactRole] == ["buyer", "manager", "engineer", "planner", "other"]

    def test_canonical_roles_and_jinja_global_derive_from_enum(self):
        from app.routers.htmx.companies import _VALID_ROLES, CANONICAL_ROLES
        from app.template_env import _CANONICAL_ROLES

        assert tuple(CANONICAL_ROLES) == tuple(ContactRole)
        assert tuple(_CANONICAL_ROLES) == tuple(ContactRole)
        assert _VALID_ROLES == frozenset(ContactRole)

    def test_dead_contact_card_macro_removed(self):
        from app.template_env import templates

        src = templates.env.loader.get_source(templates.env, "htmx/partials/customers/_contact_macros.html")[0]
        assert "macro contact_card" not in src
        assert "macro contact_row" in src


# ─────────────────────────────────────────────────────────────────────────────
# 1. Role dropdown
# ─────────────────────────────────────────────────────────────────────────────


class TestRoleDropdown:
    @pytest.mark.parametrize("role_val", ["buyer", "manager", "engineer", "planner", "other"])
    def test_setter_accepts_each_canonical_role(
        self, client: TestClient, db_session: Session, test_user: User, role_val: str
    ):
        company, _site, contact = _company_site_contact(db_session, owner_id=test_user.id)
        resp = client.post(
            f"/v2/partials/customers/{company.id}/contacts/{contact.id}/role",
            data={"contact_role": role_val},
        )
        assert resp.status_code == 200, f"{role_val} should be accepted"
        db_session.refresh(contact)
        assert contact.contact_role == role_val

    def test_setter_rejects_non_canonical_value(self, client: TestClient, db_session: Session, test_user: User):
        company, _site, contact = _company_site_contact(db_session, owner_id=test_user.id)
        resp = client.post(
            f"/v2/partials/customers/{company.id}/contacts/{contact.id}/role",
            data={"contact_role": "wizard"},
        )
        assert resp.status_code == 400

    def test_setter_rejects_legacy_value(self, client: TestClient, db_session: Session, test_user: User):
        """Legacy DB roles (buyer_po) are no longer selectable — POST 400s."""
        company, _site, contact = _company_site_contact(db_session, owner_id=test_user.id)
        resp = client.post(
            f"/v2/partials/customers/{company.id}/contacts/{contact.id}/role",
            data={"contact_role": "buyer_po"},
        )
        assert resp.status_code == 400

    def test_setter_blank_clears_role(self, client: TestClient, db_session: Session, test_user: User):
        company, _site, contact = _company_site_contact(db_session, owner_id=test_user.id, contact_role="buyer")
        resp = client.post(
            f"/v2/partials/customers/{company.id}/contacts/{contact.id}/role",
            data={"contact_role": ""},
        )
        assert resp.status_code == 200
        db_session.refresh(contact)
        assert contact.contact_role is None

    def test_editor_renders_all_five_options(self, client: TestClient, db_session: Session, test_user: User):
        company, _site, contact = _company_site_contact(db_session, owner_id=test_user.id)
        resp = client.post(
            f"/v2/partials/customers/{company.id}/contacts/{contact.id}/role",
            data={"contact_role": "buyer"},
        )
        assert resp.status_code == 200
        html = resp.text
        for role in ("buyer", "manager", "engineer", "planner", "other"):
            assert f"value='{role}'" in html or f'value="{role}"' in html, f"missing option {role}"
        # The "— clear —" affordance is present.
        assert "clear" in html.lower()

    def test_legacy_role_renders_read_only_label(self, client: TestClient, db_session: Session, test_user: User):
        """A pre-existing legacy value still renders a clean chip (display-label
        fallback)."""
        company, _site, _contact = _company_site_contact(
            db_session, owner_id=test_user.id, contact_role="decision_maker"
        )
        resp = client.get(f"/v2/partials/customers/{company.id}")
        assert resp.status_code == 200
        # legacy label "DM" (decision_maker) renders without error
        assert "DM" in resp.text or "decision_maker" in resp.text


# ─────────────────────────────────────────────────────────────────────────────
# 2. Click-to-contact phone + email
# ─────────────────────────────────────────────────────────────────────────────


class TestClickToContact:
    def test_phone_renders_tel_link(self, client: TestClient, db_session: Session, test_user: User):
        company, _site, _contact = _company_site_contact(db_session, owner_id=test_user.id, phone="+16175551212")
        resp = client.get(f"/v2/partials/customers/{company.id}")
        assert resp.status_code == 200
        assert "tel:+16175551212" in resp.text

    def test_email_renders_outlook_compose_link(self, client: TestClient, db_session: Session, test_user: User):
        company, _site, _contact = _company_site_contact(db_session, owner_id=test_user.id, email="pat@buyerco.com")
        resp = client.get(f"/v2/partials/customers/{company.id}")
        assert resp.status_code == 200
        assert "outlook.office.com/mail/deeplink/compose?to=pat%40buyerco.com" in resp.text

    def test_dnc_contact_suppresses_clickable_phone(self, client: TestClient, db_session: Session, test_user: User):
        company, _site, _contact = _company_site_contact(
            db_session, owner_id=test_user.id, phone="+16175559999", do_not_contact=True
        )
        resp = client.get(f"/v2/partials/customers/{company.id}")
        assert resp.status_code == 200
        # No clickable tel: link for a DNC contact (the value is shown struck-through instead).
        assert "tel:+16175559999" not in resp.text


# ─────────────────────────────────────────────────────────────────────────────
# 3. Recent notes + notes modal
# ─────────────────────────────────────────────────────────────────────────────


class TestNotesModal:
    def test_get_notes_modal_renders_feed_and_form(self, client: TestClient, db_session: Session, test_user: User):
        from app.services.activity_service import log_site_contact_note

        company, site, contact = _company_site_contact(db_session, owner_id=test_user.id)
        log_site_contact_note(
            user_id=test_user.id,
            site_contact_id=contact.id,
            customer_site_id=site.id,
            company_id=company.id,
            notes="First call went well",
            db=db_session,
        )
        db_session.commit()
        resp = client.get(f"/v2/partials/customers/{company.id}/contacts/{contact.id}/notes-modal")
        assert resp.status_code == 200
        assert 'id="modal-title"' in resp.text
        assert "First call went well" in resp.text
        # Add-note form posts back to the notes endpoint.
        assert f"/v2/partials/customers/{company.id}/contacts/{contact.id}/notes" in resp.text

    def test_get_notes_modal_404_for_contact_not_under_company(
        self, client: TestClient, db_session: Session, test_user: User
    ):
        company_a, _sa, _ca = _company_site_contact(db_session, owner_id=test_user.id)
        company_b, _sb, contact_b = _company_site_contact(db_session, owner_id=test_user.id, email="other@x.com")
        resp = client.get(f"/v2/partials/customers/{company_a.id}/contacts/{contact_b.id}/notes-modal")
        assert resp.status_code == 404

    def test_post_note_adds_activity_log(self, client: TestClient, db_session: Session, test_user: User):
        company, _site, contact = _company_site_contact(db_session, owner_id=test_user.id)
        resp = client.post(
            f"/v2/partials/customers/{company.id}/contacts/{contact.id}/notes",
            data={"notes": "Following up next week"},
        )
        assert resp.status_code == 200
        assert "Following up next week" in resp.text
        notes = (
            db_session.query(ActivityLog)
            .filter(ActivityLog.site_contact_id == contact.id, ActivityLog.activity_type == "note")
            .all()
        )
        assert len(notes) == 1
        assert notes[0].notes == "Following up next week"
        assert notes[0].user_id == test_user.id

    def test_post_blank_note_is_inline_error_no_write(self, client: TestClient, db_session: Session, test_user: User):
        company, _site, contact = _company_site_contact(db_session, owner_id=test_user.id)
        resp = client.post(
            f"/v2/partials/customers/{company.id}/contacts/{contact.id}/notes",
            data={"notes": "   "},
        )
        assert resp.status_code == 200
        assert "empty" in resp.text.lower()
        count = (
            db_session.query(ActivityLog)
            .filter(ActivityLog.site_contact_id == contact.id, ActivityLog.activity_type == "note")
            .count()
        )
        assert count == 0

    def test_post_note_403_for_non_owner(self, client: TestClient, db_session: Session, test_user: User):
        """A buyer who is neither owner nor manager cannot add notes
        (can_manage_account)."""
        # owner_id=None + site owner_id=None → test_user (role=buyer) cannot manage.
        company, _site, contact = _company_site_contact(db_session, owner_id=None)
        resp = client.post(
            f"/v2/partials/customers/{company.id}/contacts/{contact.id}/notes",
            data={"notes": "I should not be allowed"},
        )
        assert resp.status_code == 403
        count = (
            db_session.query(ActivityLog)
            .filter(ActivityLog.site_contact_id == contact.id, ActivityLog.activity_type == "note")
            .count()
        )
        assert count == 0

    def test_drawer_links_to_notes_modal(self, client: TestClient, db_session: Session, test_user: User):
        company, _site, contact = _company_site_contact(db_session, owner_id=test_user.id)
        resp = client.get(f"/v2/partials/customers/{company.id}")
        assert resp.status_code == 200
        assert f"contacts/{contact.id}/notes-modal" in resp.text
        # The open-modal dispatch must keep its inner quotes single (Alpine double-quote gotcha).
        assert "$dispatch('open-modal'" in resp.text


# ─────────────────────────────────────────────────────────────────────────────
# 4. Horizontal layout
# ─────────────────────────────────────────────────────────────────────────────


class TestHorizontalDrawerLayout:
    def test_drawer_uses_dense_horizontal_flow(self, client: TestClient, db_session: Session, test_user: User):
        company, _site, _contact = _company_site_contact(db_session, owner_id=test_user.id)
        resp = client.get(f"/v2/partials/customers/{company.id}")
        assert resp.status_code == 200
        # Dense horizontal flow utilities (flex-wrap + horizontal gap) per the brief.
        assert "flex flex-wrap items-center gap-x-4" in resp.text
