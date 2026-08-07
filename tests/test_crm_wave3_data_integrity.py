"""tests/test_crm_wave3_data_integrity.py — CRM Wave 3 (data-integrity) regression
tests.

Covers the seven Wave 3 lanes:
1. Central 409→HX-Trigger toast seam in app/main.py's http_exception_handler
   (server owns 409 messaging; stale-guard 409s never double-toast).
2. Legacy-contact lazy first/last backfill in apply_contact_field +
   edit_site_contact (full_name-only contacts no longer lose their surname).
3. Present-vs-absent clear semantics: company.notes (+tax_id), vendor
   website/emails/phones — submitted blank clears, absent preserves.
4. Owner unassign: submitted-empty owner_id SETs NULL behind the
   can_manage_account_team gate.
5. Vendor-contact HTMX add/edit/delete sync VendorCard.emails[] via the shared
   helper (sync_card_emails_on_contact_change).
6. Model-derived length guards → 400 (not Postgres 500) + maxlength rendering.
8a. Vendors embed-context: hx_target/push_url_base threaded through the create
    form, list buttons, and delete's list re-render.

Called by: pytest
Depends on: conftest.py fixtures (client, db_session, test_user, test_company,
    test_vendor_card), app.routers.htmx.{companies,vendors}, app.main
"""

from __future__ import annotations

import asyncio
import json

from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from starlette.requests import Request as StarletteRequest

from app.models import Company, CustomerSite, SiteContact, User, VendorCard, VendorContact

HTMX = {"HX-Request": "true"}


def _make_site(db: Session, company: Company, name: str = "HQ") -> CustomerSite:
    site = CustomerSite(company_id=company.id, site_name=name, site_type="hq", is_active=True)
    db.add(site)
    db.commit()
    db.refresh(site)
    return site


def _make_contact(db: Session, site: CustomerSite, **kw) -> SiteContact:
    contact = SiteContact(customer_site_id=site.id, **kw)
    db.add(contact)
    db.commit()
    db.refresh(contact)
    return contact


def _own(db: Session, company: Company, user: User) -> None:
    company.account_owner_id = user.id
    db.commit()


# ── 1. Central 409 htmx feedback seam ────────────────────────────────────────


class Test409HtmxFeedback:
    def test_dup_company_create_409_carries_toast_trigger(self, client: TestClient, test_company: Company):
        resp = client.post(
            "/v2/partials/customers/create",
            data={"name": test_company.name},
            headers=HTMX,
        )
        assert resp.status_code == 409
        trigger = resp.headers.get("HX-Trigger")
        assert trigger, "htmx 409 must carry an HX-Trigger showToast"
        payload = json.loads(trigger)
        assert "already exists" in payload["showToast"]["message"]
        assert payload["showToast"]["type"] == "warning"
        assert resp.headers.get("HX-Reswap") == "none"
        # JSON body shape unchanged (client + API consumers still parse it)
        body = resp.json()
        assert "already exists" in body["error"]
        assert body["status_code"] == 409

    def test_dup_company_create_non_htmx_unchanged(self, client: TestClient, test_company: Company):
        resp = client.post(
            "/v2/partials/customers/create",
            data={"name": test_company.name},
        )
        assert resp.status_code == 409
        assert "HX-Trigger" not in resp.headers
        assert "HX-Reswap" not in resp.headers
        assert "already exists" in resp.json()["error"]

    def test_handler_respects_existing_hx_trigger(self):
        """A 409 that already carries an HX-Trigger (stale-guard style) is never double-
        toasted by the central seam."""
        from app.main import http_exception_handler

        own_trigger = json.dumps({"showToast": {"message": "This changed — refresh.", "type": "warning"}})
        scope = {
            "type": "http",
            "method": "POST",
            "path": "/x",
            "headers": [(b"hx-request", b"true")],
        }
        request = StarletteRequest(scope)
        exc = HTTPException(409, "conflict", headers={"HX-Trigger": own_trigger})
        resp = asyncio.run(http_exception_handler(request, exc))
        assert resp.headers.get("hx-trigger") == own_trigger
        # exactly one showToast — no second trigger appended anywhere
        assert resp.raw_headers and sum(1 for k, _ in resp.raw_headers if k.lower() == b"hx-trigger") == 1

    def test_stale_guard_conflict_response_untouched(self):
        """stale_conflict_response returns a direct HTMLResponse (never routed through
        the exception handler) with exactly one showToast trigger."""
        from app.services.stale_guard import STALE_TOAST_MESSAGE, stale_conflict_response

        resp = stale_conflict_response()
        assert resp.status_code == 409
        payload = json.loads(resp.headers["HX-Trigger"])
        assert payload["showToast"]["message"] == STALE_TOAST_MESSAGE
        assert resp.headers["HX-Reswap"] == "none"


# ── 2. Legacy-contact lazy first/last backfill ───────────────────────────────


class TestLegacyNameBackfill:
    def test_inline_first_name_edit_preserves_surname(
        self, client: TestClient, db_session: Session, test_company: Company, test_user: User
    ):
        _own(db_session, test_company, test_user)
        site = _make_site(db_session, test_company)
        contact = _make_contact(db_session, site, full_name="John Smith")
        assert contact.first_name is None and contact.last_name is None

        resp = client.post(
            f"/v2/partials/customers/{test_company.id}/contacts/{contact.id}/field",
            data={"field": "first_name", "value": "Johnny"},
            headers=HTMX,
        )
        assert resp.status_code == 200
        db_session.refresh(contact)
        assert contact.first_name == "Johnny"
        assert contact.last_name == "Smith"
        assert contact.full_name == "Johnny Smith"

    def test_inline_last_name_edit_preserves_first(
        self, client: TestClient, db_session: Session, test_company: Company, test_user: User
    ):
        _own(db_session, test_company, test_user)
        site = _make_site(db_session, test_company)
        contact = _make_contact(db_session, site, full_name="John Smith")

        resp = client.post(
            f"/v2/partials/customers/{test_company.id}/contacts/{contact.id}/field",
            data={"field": "last_name", "value": "Smithson"},
            headers=HTMX,
        )
        assert resp.status_code == 200
        db_session.refresh(contact)
        assert contact.first_name == "John"
        assert contact.last_name == "Smithson"
        assert contact.full_name == "John Smithson"

    def test_structured_contact_behavior_unchanged(
        self, client: TestClient, db_session: Session, test_company: Company, test_user: User
    ):
        _own(db_session, test_company, test_user)
        site = _make_site(db_session, test_company)
        contact = _make_contact(db_session, site, full_name="Jane Doe", first_name="Jane", last_name="Doe")

        resp = client.post(
            f"/v2/partials/customers/{test_company.id}/contacts/{contact.id}/field",
            data={"field": "first_name", "value": "Janet"},
            headers=HTMX,
        )
        assert resp.status_code == 200
        db_session.refresh(contact)
        assert contact.first_name == "Janet"
        assert contact.last_name == "Doe"
        assert contact.full_name == "Janet Doe"

    def test_edit_site_contact_single_field_preserves_backfilled_last(
        self, client: TestClient, db_session: Session, test_company: Company, test_user: User
    ):
        _own(db_session, test_company, test_user)
        site = _make_site(db_session, test_company)
        contact = _make_contact(db_session, site, full_name="John Smith")

        resp = client.post(
            f"/v2/partials/customers/{test_company.id}/sites/{site.id}/contacts/{contact.id}/edit",
            data={"first_name": "Johnny"},
            headers=HTMX,
        )
        assert resp.status_code == 200
        db_session.refresh(contact)
        assert contact.first_name == "Johnny"
        assert contact.last_name == "Smith"
        assert contact.full_name == "Johnny Smith"


# ── 3. Present-vs-absent clear semantics ─────────────────────────────────────


class TestClearOnBlankSemantics:
    def test_blank_notes_clears(self, client: TestClient, db_session: Session, test_company: Company, test_user: User):
        _own(db_session, test_company, test_user)
        test_company.notes = "old note"
        db_session.commit()

        resp = client.post(
            f"/v2/partials/customers/{test_company.id}/edit",
            data={"name": test_company.name, "notes": ""},
            headers=HTMX,
        )
        assert resp.status_code == 200
        db_session.refresh(test_company)
        assert test_company.notes is None

    def test_absent_notes_preserved(
        self, client: TestClient, db_session: Session, test_company: Company, test_user: User
    ):
        _own(db_session, test_company, test_user)
        test_company.notes = "old note"
        db_session.commit()

        resp = client.post(
            f"/v2/partials/customers/{test_company.id}/edit",
            data={"name": test_company.name},
            headers=HTMX,
        )
        assert resp.status_code == 200
        db_session.refresh(test_company)
        assert test_company.notes == "old note"

    def test_absent_tax_id_preserved_blank_clears(
        self, client: TestClient, db_session: Session, test_company: Company, test_user: User
    ):
        _own(db_session, test_company, test_user)
        test_company.tax_id = "12-3456789"
        db_session.commit()

        resp = client.post(
            f"/v2/partials/customers/{test_company.id}/edit",
            data={"name": test_company.name},
            headers=HTMX,
        )
        assert resp.status_code == 200
        db_session.refresh(test_company)
        assert test_company.tax_id == "12-3456789"

        resp = client.post(
            f"/v2/partials/customers/{test_company.id}/edit",
            data={"name": test_company.name, "tax_id": ""},
            headers=HTMX,
        )
        assert resp.status_code == 200
        db_session.refresh(test_company)
        assert test_company.tax_id is None

    def test_source_sentinel_preserved(
        self, client: TestClient, db_session: Session, test_company: Company, test_user: User
    ):
        """Source keeps blank-sentinel semantics — its select has a real '— Keep current
        —' option."""
        _own(db_session, test_company, test_user)
        test_company.source = "sfdc"
        db_session.commit()

        resp = client.post(
            f"/v2/partials/customers/{test_company.id}/edit",
            data={"name": test_company.name, "source": ""},
            headers=HTMX,
        )
        assert resp.status_code == 200
        db_session.refresh(test_company)
        assert test_company.source == "sfdc"

    def test_vendor_blank_website_clears(self, client: TestClient, db_session: Session, test_vendor_card: VendorCard):
        resp = client.post(
            f"/v2/partials/vendors/{test_vendor_card.id}/edit",
            data={"display_name": test_vendor_card.display_name, "website": ""},
            headers=HTMX,
        )
        assert resp.status_code == 200
        db_session.refresh(test_vendor_card)
        assert test_vendor_card.website is None

    def test_vendor_blank_emails_and_phones_clear(
        self, client: TestClient, db_session: Session, test_vendor_card: VendorCard
    ):
        resp = client.post(
            f"/v2/partials/vendors/{test_vendor_card.id}/edit",
            data={"display_name": test_vendor_card.display_name, "emails": "", "phones": ""},
            headers=HTMX,
        )
        assert resp.status_code == 200
        db_session.refresh(test_vendor_card)
        assert test_vendor_card.emails == []
        assert test_vendor_card.phones == []

    def test_vendor_absent_fields_preserved(
        self, client: TestClient, db_session: Session, test_vendor_card: VendorCard
    ):
        resp = client.post(
            f"/v2/partials/vendors/{test_vendor_card.id}/edit",
            data={"display_name": "Arrow Renamed"},
            headers=HTMX,
        )
        assert resp.status_code == 200
        db_session.refresh(test_vendor_card)
        assert test_vendor_card.website == "https://arrow.com"
        assert test_vendor_card.emails == ["sales@arrow.com"]
        assert test_vendor_card.phones == ["+1-555-0100"]


# ── 4. Owner unassign ────────────────────────────────────────────────────────


class TestOwnerUnassign:
    def test_owner_can_unassign(self, client: TestClient, db_session: Session, test_company: Company, test_user: User):
        _own(db_session, test_company, test_user)

        resp = client.post(
            f"/v2/partials/customers/{test_company.id}/edit",
            data={"name": test_company.name, "owner_id": ""},
            headers=HTMX,
        )
        assert resp.status_code == 200
        db_session.refresh(test_company)
        assert test_company.account_owner_id is None

    def test_site_owner_cannot_unassign(
        self, client: TestClient, db_session: Session, test_company: Company, test_user: User
    ):
        """A site-owner passes can_manage_account but NOT can_manage_account_team — the
        unassign must 403 and leave the owner untouched."""
        other = User(email="realowner@trioscs.com", name="Real Owner", role="buyer", azure_id="azure-realowner-1")
        db_session.add(other)
        db_session.commit()
        test_company.account_owner_id = other.id
        db_session.commit()
        site = _make_site(db_session, test_company)
        site.owner_id = test_user.id
        db_session.commit()

        resp = client.post(
            f"/v2/partials/customers/{test_company.id}/edit",
            data={"name": test_company.name, "owner_id": ""},
            headers=HTMX,
        )
        assert resp.status_code == 403
        db_session.refresh(test_company)
        assert test_company.account_owner_id == other.id

    def test_absent_owner_field_is_noop(
        self, client: TestClient, db_session: Session, test_company: Company, test_user: User
    ):
        _own(db_session, test_company, test_user)

        resp = client.post(
            f"/v2/partials/customers/{test_company.id}/edit",
            data={"name": test_company.name},
            headers=HTMX,
        )
        assert resp.status_code == 200
        db_session.refresh(test_company)
        assert test_company.account_owner_id == test_user.id

    def test_blank_owner_on_unowned_company_is_noop(
        self, client: TestClient, db_session: Session, test_company: Company, test_user: User, manager_client
    ):
        """No owner set + blank submit → nothing to unassign, no gate error."""
        assert test_company.account_owner_id is None
        resp = manager_client.post(
            f"/v2/partials/customers/{test_company.id}/edit",
            data={"name": test_company.name, "owner_id": ""},
            headers=HTMX,
        )
        assert resp.status_code == 200
        db_session.refresh(test_company)
        assert test_company.account_owner_id is None


# ── 5. Vendor-contact HTMX ↔ VendorCard.emails[] sync ────────────────────────


class TestVendorContactEmailSync:
    def test_add_appends_email(self, client: TestClient, db_session: Session, test_vendor_card: VendorCard):
        resp = client.post(
            f"/v2/partials/vendors/{test_vendor_card.id}/contacts",
            data={"email": "new@arrow.com", "full_name": "New Person"},
            headers=HTMX,
        )
        assert resp.status_code == 200
        db_session.refresh(test_vendor_card)
        assert "new@arrow.com" in (test_vendor_card.emails or [])
        assert "sales@arrow.com" in test_vendor_card.emails

    def test_add_existing_array_email_no_dup(
        self, client: TestClient, db_session: Session, test_vendor_card: VendorCard
    ):
        resp = client.post(
            f"/v2/partials/vendors/{test_vendor_card.id}/contacts",
            data={"email": "sales@arrow.com"},
            headers=HTMX,
        )
        assert resp.status_code == 200
        db_session.refresh(test_vendor_card)
        assert test_vendor_card.emails.count("sales@arrow.com") == 1

    def test_edit_swaps_email(self, client: TestClient, db_session: Session, test_vendor_card: VendorCard):
        vc = VendorContact(
            vendor_card_id=test_vendor_card.id, email="old@arrow.com", source="manual", contact_type="company"
        )
        db_session.add(vc)
        test_vendor_card.emails = ["sales@arrow.com", "old@arrow.com"]
        db_session.commit()
        db_session.refresh(vc)

        resp = client.put(
            f"/v2/partials/vendors/{test_vendor_card.id}/contacts/{vc.id}",
            data={"email": "changed@arrow.com"},
            headers=HTMX,
        )
        assert resp.status_code == 200
        db_session.refresh(test_vendor_card)
        assert "old@arrow.com" not in test_vendor_card.emails
        assert "changed@arrow.com" in test_vendor_card.emails
        assert "sales@arrow.com" in test_vendor_card.emails

    def test_delete_removes_email(self, client: TestClient, db_session: Session, test_vendor_card: VendorCard):
        vc = VendorContact(
            vendor_card_id=test_vendor_card.id, email="gone@arrow.com", source="manual", contact_type="company"
        )
        db_session.add(vc)
        test_vendor_card.emails = ["sales@arrow.com", "gone@arrow.com"]
        db_session.commit()
        db_session.refresh(vc)

        resp = client.delete(
            f"/v2/partials/vendors/{test_vendor_card.id}/contacts/{vc.id}",
            headers=HTMX,
        )
        assert resp.status_code == 200
        db_session.refresh(test_vendor_card)
        assert "gone@arrow.com" not in test_vendor_card.emails
        assert "sales@arrow.com" in test_vendor_card.emails

    def test_shared_helper_used_by_both_routers(self):
        """The sync logic must be ONE shared helper, not copy-pasted blocks."""
        import inspect

        # getsource on the vendors PACKAGE would return only __init__.py; point at the
        # defining submodule (the three call sites live in contact add/edit/delete).
        import app.routers.htmx.vendors.contacts as htmx_vendors
        import app.routers.vendor_contacts as legacy
        from app.utils.vendor_helpers import sync_card_emails_on_contact_change  # noqa: F401

        assert "sync_card_emails_on_contact_change" in inspect.getsource(htmx_vendors)
        assert "sync_card_emails_on_contact_change" in inspect.getsource(legacy)


# ── 6. Model-derived length guards ───────────────────────────────────────────


class TestLengthGuards:
    def test_column_max_length_resolution(self):
        from app.utils.column_limits import column_max_length

        assert column_max_length(Company, "name") == 255
        assert column_max_length(SiteContact, "first_name") == 120
        assert column_max_length(CustomerSite, "zip") == 20
        assert column_max_length(VendorCard, "website") == 500
        assert column_max_length(Company, "notes") is None  # Text — unbounded

    def test_company_create_overlong_name_400(self, client: TestClient):
        resp = client.post(
            "/v2/partials/customers/create",
            data={"name": "x" * 600},
            headers=HTMX,
        )
        assert resp.status_code == 400
        assert "too long" in resp.json()["error"]

    def test_company_inline_field_overlong_400(
        self, client: TestClient, db_session: Session, test_company: Company, test_user: User
    ):
        _own(db_session, test_company, test_user)
        resp = client.post(
            f"/v2/partials/customers/{test_company.id}/field",
            data={"field": "hq_city", "value": "x" * 600},
            headers=HTMX,
        )
        assert resp.status_code == 400
        assert "too long" in resp.json()["error"]

    def test_contact_inline_field_overlong_400(
        self, client: TestClient, db_session: Session, test_company: Company, test_user: User
    ):
        _own(db_session, test_company, test_user)
        site = _make_site(db_session, test_company)
        contact = _make_contact(db_session, site, full_name="Jane Doe", first_name="Jane", last_name="Doe")
        resp = client.post(
            f"/v2/partials/customers/{test_company.id}/contacts/{contact.id}/field",
            data={"field": "title", "value": "x" * 600},
            headers=HTMX,
        )
        assert resp.status_code == 400
        assert "too long" in resp.json()["error"]

    def test_site_edit_overlong_zip_400(
        self, client: TestClient, db_session: Session, test_company: Company, test_user: User
    ):
        _own(db_session, test_company, test_user)
        site = _make_site(db_session, test_company)
        resp = client.post(
            f"/v2/partials/customers/{test_company.id}/sites/{site.id}/edit",
            data={"site_name": "HQ", "zip": "9" * 50},
            headers=HTMX,
        )
        assert resp.status_code == 400
        assert "too long" in resp.json()["error"]

    def test_vendor_edit_overlong_display_name_400(self, client: TestClient, test_vendor_card: VendorCard):
        resp = client.post(
            f"/v2/partials/vendors/{test_vendor_card.id}/edit",
            data={"display_name": "x" * 600},
            headers=HTMX,
        )
        assert resp.status_code == 400
        assert "too long" in resp.json()["error"]

    def test_vendor_create_overlong_website_400(self, client: TestClient):
        resp = client.post(
            "/v2/partials/vendors/create",
            data={"display_name": "Shorty", "website": "https://" + "x" * 600 + ".com"},
            headers=HTMX,
        )
        assert resp.status_code == 400
        assert "too long" in resp.json()["error"]

    def test_vendor_create_overlong_industry_400(self, client: TestClient):
        # industry/hq_city/hq_country/employee_size are bounded columns too — the guard
        # must cover every bounded write in the create path, not just name/website.
        resp = client.post(
            "/v2/partials/vendors/create",
            data={"display_name": "Shorty Two", "industry": "x" * 300},
            headers=HTMX,
        )
        assert resp.status_code == 400
        assert "too long" in resp.json()["error"]

    def test_company_edit_overlong_source_400(
        self, client: TestClient, db_session: Session, test_company: Company, test_user: User
    ):
        # source keeps blank-sentinel semantics but is still a bounded String(50) —
        # an over-length submitted value must 400, not 500 on Postgres.
        _own(db_session, test_company, test_user)
        resp = client.post(
            f"/v2/partials/customers/{test_company.id}/edit",
            data={"source": "x" * 60},
            headers=HTMX,
        )
        assert resp.status_code == 400
        assert "too long" in resp.json()["error"]

    def test_company_edit_form_emits_maxlength(
        self, client: TestClient, db_session: Session, test_company: Company, test_user: User
    ):
        _own(db_session, test_company, test_user)
        resp = client.get(
            f"/v2/partials/customers/{test_company.id}/edit-form",
            headers=HTMX,
        )
        assert resp.status_code == 200
        # Per-field: each bound must match the model column, not just appear somewhere.
        assert 'name="name" maxlength="255"' in resp.text
        assert 'name="website" maxlength="500"' in resp.text
        assert 'name="phone" maxlength="100"' in resp.text

    def test_company_edit_overlong_name_400(
        self, client: TestClient, db_session: Session, test_company: Company, test_user: User
    ):
        _own(db_session, test_company, test_user)
        resp = client.post(
            f"/v2/partials/customers/{test_company.id}/edit",
            data={"name": "x" * 600},
            headers=HTMX,
        )
        assert resp.status_code == 400
        assert "too long" in resp.json()["error"]

    def test_site_create_overlong_site_name_400(
        self, client: TestClient, db_session: Session, test_company: Company, test_user: User
    ):
        _own(db_session, test_company, test_user)
        resp = client.post(
            f"/v2/partials/customers/{test_company.id}/sites",
            data={"site_name": "x" * 300},
            headers=HTMX,
        )
        assert resp.status_code == 400
        assert "too long" in resp.json()["error"]

    def test_vendor_create_overlong_display_name_400(self, client: TestClient):
        resp = client.post(
            "/v2/partials/vendors/create",
            data={"display_name": "x" * 600},
            headers=HTMX,
        )
        assert resp.status_code == 400
        assert "too long" in resp.json()["error"]

    def test_vendor_create_overlong_hq_city_400(self, client: TestClient):
        resp = client.post(
            "/v2/partials/vendors/create",
            data={"display_name": "Shorty Three", "hq_city": "x" * 300},
            headers=HTMX,
        )
        assert resp.status_code == 400
        assert "too long" in resp.json()["error"]

    def test_vendor_edit_overlong_website_400(self, client: TestClient, test_vendor_card: VendorCard):
        resp = client.post(
            f"/v2/partials/vendors/{test_vendor_card.id}/edit",
            data={"website": "https://" + "x" * 600 + ".com"},
            headers=HTMX,
        )
        assert resp.status_code == 400
        assert "too long" in resp.json()["error"]

    # ── Contact create/edit paths (Contacts-tab modal) — the highest-traffic
    #    contact surface; guards must cover the non-registry writes too. ──────

    def test_contact_create_overlong_first_name_400(
        self, client: TestClient, db_session: Session, test_company: Company, test_user: User
    ):
        _own(db_session, test_company, test_user)
        _make_site(db_session, test_company)
        resp = client.post(
            f"/v2/partials/customers/{test_company.id}/contacts",
            data={"first_name": "x" * 200},
            headers=HTMX,
        )
        assert resp.status_code == 400
        assert "too long" in resp.json()["error"]

    def test_contact_create_overlong_title_400(
        self, client: TestClient, db_session: Session, test_company: Company, test_user: User
    ):
        _own(db_session, test_company, test_user)
        _make_site(db_session, test_company)
        resp = client.post(
            f"/v2/partials/customers/{test_company.id}/contacts",
            data={"first_name": "Ann", "title": "x" * 300},
            headers=HTMX,
        )
        assert resp.status_code == 400
        assert "too long" in resp.json()["error"]

    def test_contact_create_overlong_legacy_full_name_400(
        self, client: TestClient, db_session: Session, test_company: Company, test_user: User
    ):
        # Legacy fallback path: a spaceless 300-char full_name lands whole in
        # first_name (120) — must 400, not 500 on Postgres.
        _own(db_session, test_company, test_user)
        _make_site(db_session, test_company)
        resp = client.post(
            f"/v2/partials/customers/{test_company.id}/contacts",
            data={"full_name": "y" * 300},
            headers=HTMX,
        )
        assert resp.status_code == 400
        assert "too long" in resp.json()["error"]

    def test_contact_create_overlong_new_site_name_400(
        self, client: TestClient, db_session: Session, test_company: Company, test_user: User
    ):
        # The '__new__' site branch writes CustomerSite.site_name (255) directly,
        # bypassing create_site — needs its own guard before any flush.
        _own(db_session, test_company, test_user)
        resp = client.post(
            f"/v2/partials/customers/{test_company.id}/contacts",
            data={"first_name": "Ann", "site_id": "__new__", "new_site_name": "z" * 300},
            headers=HTMX,
        )
        assert resp.status_code == 400
        assert "too long" in resp.json()["error"]
        assert db_session.query(CustomerSite).filter_by(company_id=test_company.id).count() == 0

    def test_contact_edit_overlong_first_name_400(
        self, client: TestClient, db_session: Session, test_company: Company, test_user: User
    ):
        # edit_site_contact assigns first/last directly (atomic block), bypassing
        # the guarded apply_contact_field loop — needs its own guard.
        _own(db_session, test_company, test_user)
        site = _make_site(db_session, test_company)
        contact = _make_contact(db_session, site, full_name="Jane Doe", first_name="Jane", last_name="Doe")
        resp = client.post(
            f"/v2/partials/customers/{test_company.id}/sites/{site.id}/contacts/{contact.id}/edit",
            data={"first_name": "x" * 200},
            headers=HTMX,
        )
        assert resp.status_code == 400
        assert "too long" in resp.json()["error"]
        db_session.expire_all()
        assert contact.first_name == "Jane"

    def test_legacy_site_contact_create_overlong_title_400(
        self, client: TestClient, db_session: Session, test_company: Company, test_user: User
    ):
        # Deprecated per-site create route still writes bounded columns — same guard.
        _own(db_session, test_company, test_user)
        site = _make_site(db_session, test_company)
        resp = client.post(
            f"/v2/partials/customers/{test_company.id}/sites/{site.id}/contacts",
            data={"full_name": "Ann Legacy", "title": "x" * 300},
            headers=HTMX,
        )
        assert resp.status_code == 400
        assert "too long" in resp.json()["error"]

    def test_contact_add_form_emits_maxlength(
        self, client: TestClient, db_session: Session, test_company: Company, test_user: User
    ):
        _own(db_session, test_company, test_user)
        _make_site(db_session, test_company)
        resp = client.get(
            f"/v2/partials/customers/{test_company.id}/contacts/add-form",
            headers=HTMX,
        )
        assert resp.status_code == 200
        assert "name='first_name' maxlength='120'" in resp.text
        assert "name='last_name' maxlength='120'" in resp.text
        assert "name='title' maxlength='255'" in resp.text
        assert "name='email' maxlength='255'" in resp.text
        assert "name='phone' maxlength='100'" in resp.text

    def test_inline_field_edit_widget_emits_maxlength(
        self, client: TestClient, db_session: Session, test_company: Company, test_user: User
    ):
        _own(db_session, test_company, test_user)
        resp = client.get(
            f"/v2/partials/customers/{test_company.id}/field/edit/hq_city",
            headers=HTMX,
        )
        assert resp.status_code == 200
        assert "maxlength='255'" in resp.text or 'maxlength="255"' in resp.text


# ── 8a. Vendors embed-context threading ──────────────────────────────────────


class TestVendorEmbedContext:
    def test_create_form_threads_hx_target(self, client: TestClient):
        resp = client.get(
            "/v2/partials/vendors/create-form?hx_target=%23crm-tab-content&push_url_base=%2Fv2%2Fcrm",
            headers=HTMX,
        )
        assert resp.status_code == 200
        assert resp.text.count('hx-target="#crm-tab-content"') >= 3  # form + both Cancels
        assert '"#main-content"' not in resp.text

    def test_create_form_invalid_target_falls_back(self, client: TestClient):
        resp = client.get(
            "/v2/partials/vendors/create-form?hx_target=%23evil",
            headers=HTMX,
        )
        assert resp.status_code == 200
        assert 'hx-target="#main-content"' in resp.text
        assert "#evil" not in resp.text

    def test_create_form_default_targets_main_content(self, client: TestClient):
        resp = client.get("/v2/partials/vendors/create-form", headers=HTMX)
        assert resp.status_code == 200
        assert 'hx-target="#main-content"' in resp.text

    def test_list_add_vendor_and_contacts_carry_params(self, client: TestClient):
        resp = client.get(
            "/v2/partials/vendors?hx_target=%23crm-tab-content&push_url_base=%2Fv2%2Fcrm",
            headers=HTMX,
        )
        assert resp.status_code == 200
        assert "create-form?hx_target=%23crm-tab-content" in resp.text
        # Contacts link + Add Vendor button both retarget the embed container
        assert resp.text.count('hx-target="#crm-tab-content"') >= 4

    def test_delete_rerenders_list_with_params(self, client: TestClient, db_session: Session):
        card = VendorCard(normalized_name="deleteme corp", display_name="DeleteMe Corp")
        db_session.add(card)
        db_session.commit()
        db_session.refresh(card)

        resp = client.delete(
            f"/v2/partials/vendors/{card.id}?hx_target=%23crm-tab-content&push_url_base=%2Fv2%2Fcrm",
            headers=HTMX,
        )
        assert resp.status_code == 200
        assert 'hx-target="#crm-tab-content"' in resp.text

    def test_create_form_carries_hidden_embed_inputs(self, client: TestClient):
        # The create POST can only thread embed context if the form submits it.
        resp = client.get(
            "/v2/partials/vendors/create-form?hx_target=%23crm-tab-content&push_url_base=%2Fv2%2Fcrm",
            headers=HTMX,
        )
        assert resp.status_code == 200
        assert '<input type="hidden" name="hx_target" value="#crm-tab-content">' in resp.text
        assert '<input type="hidden" name="push_url_base" value="/v2/crm">' in resp.text

    def test_create_post_threads_embed_params_into_detail(self, client: TestClient):
        resp = client.post(
            "/v2/partials/vendors/create",
            data={
                "display_name": "Embedded Create Corp",
                "hx_target": "#crm-tab-content",
                "push_url_base": "/v2/crm",
            },
            headers=HTMX,
        )
        assert resp.status_code == 200
        # The rendered detail's Delete button URL must carry the embed params so the
        # post-delete list re-render stays inside the embed container. (Jinja's
        # urlencode leaves '/' unescaped — slashes are legal in query values.)
        assert "hx_target=%23crm-tab-content" in resp.text
        assert "push_url_base=/v2/crm" in resp.text

    def test_create_post_invalid_embed_target_falls_back(self, client: TestClient):
        resp = client.post(
            "/v2/partials/vendors/create",
            data={"display_name": "Embedded Evil Corp", "hx_target": "#evil"},
            headers=HTMX,
        )
        assert resp.status_code == 200
        assert "#evil" not in resp.text
        assert "hx_target=%23main-content" in resp.text


# ── Vendor-contact bounded columns (same PG-500 class; wave touched these
#    handlers for the emails[] sync, so they carry the same guards) ───────────


class TestVendorContactLengthGuards:
    def test_htmx_add_overlong_full_name_400(self, client: TestClient, test_vendor_card: VendorCard):
        resp = client.post(
            f"/v2/partials/vendors/{test_vendor_card.id}/contacts",
            data={"email": "long.name@vendor.com", "full_name": "x" * 300},
            headers=HTMX,
        )
        assert resp.status_code == 400
        assert "too long" in resp.json()["error"]

    def test_htmx_edit_overlong_title_400(self, client: TestClient, db_session: Session, test_vendor_card: VendorCard):
        vc = VendorContact(vendor_card_id=test_vendor_card.id, email="edit.me@vendor.com", source="manual")
        db_session.add(vc)
        db_session.commit()
        db_session.refresh(vc)
        resp = client.put(
            f"/v2/partials/vendors/{test_vendor_card.id}/contacts/{vc.id}",
            data={"title": "x" * 300},
            headers=HTMX,
        )
        assert resp.status_code == 400
        assert "too long" in resp.json()["error"]

    def test_json_add_overlong_full_name_400(self, client: TestClient, test_vendor_card: VendorCard):
        resp = client.post(
            f"/api/vendors/{test_vendor_card.id}/contacts",
            json={"email": "json.add@vendor.com", "full_name": "x" * 300},
        )
        assert resp.status_code == 400
        assert "too long" in resp.json()["error"]

    def test_json_update_overlong_title_400(
        self, client: TestClient, db_session: Session, test_vendor_card: VendorCard
    ):
        vc = VendorContact(vendor_card_id=test_vendor_card.id, email="json.edit@vendor.com", source="manual")
        db_session.add(vc)
        db_session.commit()
        db_session.refresh(vc)
        resp = client.put(
            f"/api/vendors/{test_vendor_card.id}/contacts/{vc.id}",
            json={"title": "x" * 300},
        )
        assert resp.status_code == 400
        assert "too long" in resp.json()["error"]
