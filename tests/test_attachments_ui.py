"""test_attachments_ui.py — Tests for the shared attachments UI (Task 5).

Covers:
  - the attachments_panel(kind, entity_id) Jinja macro renders an upload form +
    a lazy-loading list container with the correct per-kind URLs for all 6 kinds.
  - the _attachment_list.html rows partial (items + invitational empty state).
  - the 6 list endpoints return HTML when HX-Request is present and JSON otherwise.

Called by: pytest
Depends on: app/template_env, app/routers (requisitions/attachments, crm/offers,
            attachments_extra), app/services/attachment_service
"""

import os

os.environ["TESTING"] = "1"

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models import (
    Company,
    CompanyAttachment,
    CustomerSite,
    MaterialCard,
    MaterialCardAttachment,
    Offer,
    OfferAttachment,
    Requirement,
    Requisition,
    SiteContact,
    SiteContactAttachment,
)
from app.template_env import templates

# The list-URL family the macro must emit for each kind, plus the per-row
# delete base the list partial must wire onto each delete button.
_KINDS = {
    "requisition": "/api/requisitions/42/attachments",
    "requirement": "/api/requirements/42/attachments",
    "offer": "/api/offers/42/attachments",
    "company": "/api/companies/42/attachments",
    "contact": "/api/contacts/42/attachments",
    "material": "/api/material-cards/42/attachments",
}


# ---------------------------------------------------------------------------
# Macro rendering
# ---------------------------------------------------------------------------


def _render_panel(kind: str, entity_id: int = 42) -> str:
    src = (
        '{% from "htmx/partials/shared/_attachments.html" import attachments_panel %}{{ attachments_panel(kind, eid) }}'
    )
    return templates.env.from_string(src).render(kind=kind, eid=entity_id)


class TestPanelMacro:
    def test_macro_renders_upload_form_and_list_for_each_kind(self):
        for kind, list_url in _KINDS.items():
            html = _render_panel(kind)
            # Upload form: multipart POST to the per-kind list URL, file picker.
            assert 'hx-encoding="multipart/form-data"' in html, kind
            assert f'hx-post="{list_url}"' in html, kind
            assert 'name="file"' in html, kind
            # List container: lazy-loads the list URL on load + refresh trigger.
            assert f'hx-get="{list_url}"' in html, kind
            assert "attachments:refresh" in html, kind
            # The Alpine factory is wired.
            assert "attachmentsPanel()" in html, kind

    def test_macro_sets_explicit_hx_target_on_list(self):
        # CLAUDE.md trap: a lazy-load sub-container must set an explicit hx-target
        # or it inherits hx-target="this" → #main-content and wipes the page.
        html = _render_panel("offer")
        assert 'hx-target="this"' in html

    def test_macro_unknown_kind_raises(self):
        # A typo'd kind must fail loudly (KeyError) rather than silently emit a
        # broken panel with no URLs.
        import pytest

        with pytest.raises(Exception):
            _render_panel("bogus")


# ---------------------------------------------------------------------------
# List rows partial
# ---------------------------------------------------------------------------


class TestListPartial:
    def _render(self, items):
        tmpl = templates.get_template("htmx/partials/shared/_attachment_list.html")
        return tmpl.render(
            request=None,
            kind="company",
            entity_id=42,
            items=items,
            delete_base="/api/company-attachments",
        )

    def test_rows_link_to_unified_content_route_and_delete(self):
        items = [
            {
                "id": 7,
                "file_name": "po-1234.pdf",
                "web_url": "https://x",
                "content_type": "application/pdf",
                "size_bytes": 1536000,
                "uploaded_by": "Alice",
                "created_at": "2026-06-20T10:00:00+00:00",
                "kind": "onedrive",
            }
        ]
        html = self._render(items)
        assert "/api/attachments/company/7/content" in html
        assert 'hx-delete="/api/company-attachments/7"' in html
        assert "po-1234.pdf" in html
        assert "1.5 MB" in html  # |filesizeformat
        assert "Alice" in html
        # delete dispatches the changed event so the panel re-fetches
        assert "attachments:changed" in html

    def test_empty_state_is_invitational(self):
        html = self._render([])
        assert "No files yet" in html
        assert "PO, datasheet, drawing, or photo" in html


# ---------------------------------------------------------------------------
# List endpoints: HTML when HX-Request, JSON otherwise
# ---------------------------------------------------------------------------


def _seed_requisition(db: Session, user_id: int) -> Requisition:
    req = Requisition(
        name="REQ-UI",
        status="open",
        created_by=user_id,
        created_at=datetime.now(timezone.utc),
    )
    db.add(req)
    db.commit()
    db.refresh(req)
    return req


def _att_kwargs(fk_field: str, fk_id: int, user_id: int) -> dict:
    return {
        fk_field: fk_id,
        "file_name": "doc.pdf",
        "library_item_id": "item-ui-1",
        "library_drive_id": None,
        "library_web_url": "https://onedrive.example.com/doc.pdf",
        "content_type": "application/pdf",
        "size_bytes": 2048,
        "uploaded_by_id": user_id,
        "created_at": datetime.now(timezone.utc),
    }


class TestListEndpointContentNegotiation:
    def _assert_html_vs_json(self, client, url: str):
        # JSON for a normal request (back-compat — existing tests assert arrays).
        json_resp = client.get(url)
        assert json_resp.status_code == 200
        assert json_resp.headers["content-type"].startswith("application/json")
        assert isinstance(json_resp.json(), list)

        # HTML when HX-Request is truthy.
        hx_resp = client.get(url, headers={"HX-Request": "true"})
        assert hx_resp.status_code == 200
        assert hx_resp.headers["content-type"].startswith("text/html")
        body = hx_resp.text
        # Either a file row links to the unified content route, or the empty state.
        assert "/api/attachments/" in body or "No files yet" in body

    def test_requisition(self, client, db_session, test_user):
        req = _seed_requisition(db_session, test_user.id)
        self._assert_html_vs_json(client, f"/api/requisitions/{req.id}/attachments")

    def test_requirement(self, client, db_session, test_user):
        req = _seed_requisition(db_session, test_user.id)
        r = Requirement(
            requisition_id=req.id,
            primary_mpn="LM317T",
            manufacturer="TI",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(r)
        db_session.commit()
        db_session.refresh(r)
        self._assert_html_vs_json(client, f"/api/requirements/{r.id}/attachments")

    def test_offer(self, client, db_session, test_user):
        req = _seed_requisition(db_session, test_user.id)
        offer = Offer(
            requisition_id=req.id,
            vendor_name="Acme",
            mpn="LM317T",
            status="active",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(offer)
        db_session.commit()
        db_session.refresh(offer)
        db_session.add(OfferAttachment(**_att_kwargs("offer_id", offer.id, test_user.id)))
        db_session.commit()
        self._assert_html_vs_json(client, f"/api/offers/{offer.id}/attachments")

    def test_company(self, client, db_session, test_user):
        co = Company(name="UICo", is_active=True, account_owner_id=test_user.id, created_at=datetime.now(timezone.utc))
        db_session.add(co)
        db_session.commit()
        db_session.refresh(co)
        db_session.add(CompanyAttachment(**_att_kwargs("company_id", co.id, test_user.id)))
        db_session.commit()
        self._assert_html_vs_json(client, f"/api/companies/{co.id}/attachments")

    def test_contact(self, client, db_session, test_user):
        co = Company(name="UICo2", is_active=True, account_owner_id=test_user.id, created_at=datetime.now(timezone.utc))
        db_session.add(co)
        db_session.commit()
        db_session.refresh(co)
        site = CustomerSite(company_id=co.id, site_name="HQ", is_active=True, created_at=datetime.now(timezone.utc))
        db_session.add(site)
        db_session.commit()
        db_session.refresh(site)
        contact = SiteContact(
            customer_site_id=site.id,
            full_name="Jane",
            is_active=True,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(contact)
        db_session.commit()
        db_session.refresh(contact)
        db_session.add(SiteContactAttachment(**_att_kwargs("site_contact_id", contact.id, test_user.id)))
        db_session.commit()
        self._assert_html_vs_json(client, f"/api/contacts/{contact.id}/attachments")

    def test_material(self, client, db_session, test_user):
        card = MaterialCard(
            normalized_mpn="lm317t-ui",
            display_mpn="LM317T",
            manufacturer="TI",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(card)
        db_session.commit()
        db_session.refresh(card)
        db_session.add(MaterialCardAttachment(**_att_kwargs("material_card_id", card.id, test_user.id)))
        db_session.commit()
        self._assert_html_vs_json(client, f"/api/material-cards/{card.id}/attachments")
