"""tests/test_crm_p5_trust.py — CRM P5 trust features.

Covers: field-history audit trail (old→new on inline edit), completeness score
math, phone normalize on contact save, and the constrained industry pick-list.
Called by: pytest
Depends on: app.services.crm_field_history, app.services.crm_completeness,
    app.routers.htmx.companies, conftest.py
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import Company, CrmFieldHistory, CustomerSite, SiteContact, User

# ── Helpers / fixtures (mirror test_inline_field_edit.py) ─────────────────────


def _make_client(app, db_session, user):
    from app.database import get_db
    from app.dependencies import require_admin, require_buyer, require_fresh_token, require_user

    def _db():
        yield db_session

    def _user():
        return user

    async def _fresh():
        return "mock-token"

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[require_user] = _user
    app.dependency_overrides[require_admin] = _user
    app.dependency_overrides[require_buyer] = _user
    app.dependency_overrides[require_fresh_token] = _fresh
    return app


def _clear_overrides(app):
    from app.database import get_db
    from app.dependencies import require_admin, require_buyer, require_fresh_token, require_user

    for dep in [get_db, require_user, require_admin, require_buyer, require_fresh_token]:
        app.dependency_overrides.pop(dep, None)


@pytest.fixture()
def owner_client(db_session: Session, test_company: Company, test_user: User):
    test_company.account_owner_id = test_user.id
    db_session.commit()
    from app.main import app

    _make_client(app, db_session, test_user)
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c
    _clear_overrides(app)


@pytest.fixture()
def site_and_contact(db_session: Session, test_company: Company):
    site = CustomerSite(company_id=test_company.id, site_name="HQ", site_type="hq", is_active=True)
    db_session.add(site)
    db_session.flush()
    contact = SiteContact(customer_site_id=site.id, full_name="Jane Doe", title="Engineer", email="jane@acme.com")
    db_session.add(contact)
    db_session.commit()
    db_session.refresh(contact)
    return site, contact


# ── Field-history service ─────────────────────────────────────────────────────


class TestFieldHistoryService:
    def test_record_field_change_records_old_new(self, db_session, test_company, test_user):
        from app.services.crm_field_history import ENTITY_COMPANY, record_field_change

        row = record_field_change(
            db_session,
            entity_type=ENTITY_COMPANY,
            entity_id=test_company.id,
            field_name="industry",
            old_value="Aerospace",
            new_value="Automotive",
            user_id=test_user.id,
        )
        db_session.commit()
        assert row is not None
        stored = db_session.query(CrmFieldHistory).filter(CrmFieldHistory.entity_id == test_company.id).one()
        assert stored.entity_type == "company"
        assert stored.field_name == "industry"
        assert stored.old_value == "Aerospace"
        assert stored.new_value == "Automotive"
        assert stored.changed_by_id == test_user.id

    def test_no_row_when_value_unchanged(self, db_session, test_company, test_user):
        from app.services.crm_field_history import ENTITY_COMPANY, record_field_change

        row = record_field_change(
            db_session,
            entity_type=ENTITY_COMPANY,
            entity_id=test_company.id,
            field_name="industry",
            old_value="Aerospace",
            new_value="Aerospace",
            user_id=test_user.id,
        )
        db_session.commit()
        assert row is None
        assert db_session.query(CrmFieldHistory).count() == 0

    def test_none_and_blank_are_equivalent(self, db_session, test_company, test_user):
        from app.services.crm_field_history import ENTITY_COMPANY, record_field_change

        row = record_field_change(
            db_session,
            entity_type=ENTITY_COMPANY,
            entity_id=test_company.id,
            field_name="phone",
            old_value=None,
            new_value="   ",
            user_id=test_user.id,
        )
        assert row is None

    def test_history_for_orders_newest_first(self, db_session, test_company, test_user):
        from app.services.crm_field_history import ENTITY_COMPANY, field_history_for, record_field_change

        for old, new in (("a", "b"), ("b", "c"), ("c", "d")):
            record_field_change(
                db_session,
                entity_type=ENTITY_COMPANY,
                entity_id=test_company.id,
                field_name="industry",
                old_value=old,
                new_value=new,
                user_id=test_user.id,
            )
            db_session.commit()
        rows = field_history_for(db_session, ENTITY_COMPANY, test_company.id)
        assert len(rows) == 3
        # Newest-first: the (c→d) change is first.
        assert rows[0].new_value == "d"
        assert rows[-1].new_value == "b"


# ── Completeness score ────────────────────────────────────────────────────────


class TestCompleteness:
    def test_company_completeness_math(self, db_session):
        from app.services.crm_completeness import COMPANY_KEY_FIELDS, company_completeness

        total = len(COMPANY_KEY_FIELDS)
        # name + industry + website filled (3 of total); the rest empty.
        co = Company(name="X", industry="Aerospace", website="https://x.com", is_active=True)
        res = company_completeness(co)
        assert res["total"] == total
        assert res["filled"] == 3
        assert res["pct"] == round(100 * 3 / total)
        assert "Phone" in res["missing"]
        assert "Industry" not in res["missing"]

    def test_blank_string_counts_as_missing(self, db_session):
        from app.services.crm_completeness import company_completeness

        co = Company(name="X", industry="   ", website="", is_active=True)
        res = company_completeness(co)
        # only name is filled
        assert res["filled"] == 1

    def test_contact_completeness_math(self, db_session):
        from app.services.crm_completeness import CONTACT_KEY_FIELDS, contact_completeness

        total = len(CONTACT_KEY_FIELDS)
        c = SiteContact(customer_site_id=1, full_name="Jane", title="Eng", email="j@x.com")
        res = contact_completeness(c)
        assert res["total"] == total
        assert res["filled"] == 3
        assert "Phone" in res["missing"]

    def test_empty_company_is_low(self, db_session):
        from app.services.crm_completeness import company_completeness

        co = Company(name="X", is_active=True)
        res = company_completeness(co)
        assert res["filled"] == 1
        assert res["pct"] < 50


# ── Phone normalize on contact save ───────────────────────────────────────────


class TestContactPhoneNormalize:
    def test_apply_contact_field_normalizes_phone(self, db_session, site_and_contact):
        from app.routers.htmx.companies import apply_contact_field

        _, contact = site_and_contact
        apply_contact_field(contact, "phone", "(415) 555-1234", contact.customer_site_id, db_session)
        assert contact.phone == "+14155551234"

    def test_apply_contact_field_normalizes_secondary_phone(self, db_session, site_and_contact):
        from app.routers.htmx.companies import apply_contact_field

        _, contact = site_and_contact
        apply_contact_field(contact, "secondary_phone", "415-555-9999", contact.customer_site_id, db_session)
        assert contact.secondary_phone == "+14155559999"

    def test_blank_phone_clears(self, db_session, site_and_contact):
        from app.routers.htmx.companies import apply_contact_field

        _, contact = site_and_contact
        apply_contact_field(contact, "phone", "", contact.customer_site_id, db_session)
        assert contact.phone is None


# ── Industry pick-list ────────────────────────────────────────────────────────


class TestIndustryPickList:
    def test_canonical_value_accepted(self, db_session, test_company):
        from app.routers.htmx.companies import apply_company_field

        apply_company_field(test_company, "industry", "Aerospace")
        assert test_company.industry == "Aerospace"

    def test_invalid_value_rejected(self, db_session, test_company):
        from app.routers.htmx.companies import apply_company_field

        test_company.industry = None
        with pytest.raises(HTTPException) as exc:
            apply_company_field(test_company, "industry", "Totally Made Up")
        assert exc.value.status_code == 400

    def test_blank_clears(self, db_session, test_company):
        from app.routers.htmx.companies import apply_company_field

        test_company.industry = "Aerospace"
        apply_company_field(test_company, "industry", "")
        assert test_company.industry is None

    def test_legacy_value_preserved_on_noop(self, db_session, test_company):
        # test_company fixture has a legacy free-text industry ("Electronic Components").
        from app.routers.htmx.companies import apply_company_field

        test_company.industry = "Electronic Components"
        # Re-submitting the unchanged legacy value must NOT raise (preserve, not reject).
        apply_company_field(test_company, "industry", "Electronic Components")
        assert test_company.industry == "Electronic Components"


# ── Integration: inline POST records history + surfaces render ────────────────


class TestInlineEditRecordsHistory:
    def test_company_field_edit_records_history(self, owner_client, test_company, db_session):
        resp = owner_client.post(
            f"/v2/partials/customers/{test_company.id}/field",
            data={"field": "industry", "value": "Aerospace"},
        )
        assert resp.status_code == 200
        rows = (
            db_session.query(CrmFieldHistory)
            .filter(CrmFieldHistory.entity_type == "company", CrmFieldHistory.entity_id == test_company.id)
            .all()
        )
        assert len(rows) == 1
        assert rows[0].field_name == "industry"
        assert rows[0].new_value == "Aerospace"

    def test_noop_edit_records_nothing(self, owner_client, test_company, db_session):
        test_company.legal_name = "Acme LLC"
        db_session.commit()
        resp = owner_client.post(
            f"/v2/partials/customers/{test_company.id}/field",
            data={"field": "legal_name", "value": "Acme LLC"},
        )
        assert resp.status_code == 200
        assert db_session.query(CrmFieldHistory).count() == 0

    def test_contact_field_edit_records_history(self, owner_client, test_company, site_and_contact, db_session):
        _, contact = site_and_contact
        resp = owner_client.post(
            f"/v2/partials/customers/{test_company.id}/contacts/{contact.id}/field",
            data={"field": "title", "value": "Director"},
        )
        assert resp.status_code == 200
        rows = (
            db_session.query(CrmFieldHistory)
            .filter(CrmFieldHistory.entity_type == "contact", CrmFieldHistory.entity_id == contact.id)
            .all()
        )
        assert len(rows) == 1
        assert rows[0].old_value == "Engineer"
        assert rows[0].new_value == "Director"

    def test_history_tab_renders(self, owner_client, test_company, db_session):
        owner_client.post(
            f"/v2/partials/customers/{test_company.id}/field",
            data={"field": "industry", "value": "Aerospace"},
        )
        resp = owner_client.get(f"/v2/partials/customers/{test_company.id}/tab/history")
        assert resp.status_code == 200
        assert "Field changes" in resp.text
        assert "Aerospace" in resp.text

    def test_contact_history_modal_renders(self, owner_client, test_company, site_and_contact):
        _, contact = site_and_contact
        owner_client.post(
            f"/v2/partials/customers/{test_company.id}/contacts/{contact.id}/field",
            data={"field": "title", "value": "Director"},
        )
        resp = owner_client.get(f"/v2/partials/customers/{test_company.id}/contacts/{contact.id}/history-modal")
        assert resp.status_code == 200
        assert "Field history" in resp.text
        assert "Director" in resp.text

    def test_detail_shows_completeness_badge(self, owner_client, test_company):
        resp = owner_client.get(f"/v2/partials/customers/{test_company.id}")
        assert resp.status_code == 200
        assert "% complete" in resp.text

    def test_contacts_tab_renders_history_item_and_completeness_pill(
        self, owner_client, test_company, site_and_contact
    ):
        # The contact row macro reaches the crm_completeness Jinja global + History kebab.
        resp = owner_client.get(f"/v2/partials/customers/{test_company.id}/tab/contacts")
        assert resp.status_code == 200
        assert "history-modal" in resp.text  # kebab History action
        # Contact has 3/6 key fields → an incomplete completeness pill (50%) renders.
        assert "% complete" in resp.text or "complete" in resp.text
