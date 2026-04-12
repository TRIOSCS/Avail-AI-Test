"""tests/test_htmx_views_nightly9.py — Coverage for uncovered htmx_views.py routes.

Targets: vendor reviews, vendor prospects, company CRUD, site CRUD,
site contact CRUD, quote metadata edit, response status update,
material card update.

Called by: pytest autodiscovery
Depends on: conftest.py fixtures, app.routers.htmx_views
"""

import os
import uuid
from datetime import datetime, timezone

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

os.environ["TESTING"] = "1"

from app.models import (  # noqa: E402
    Company,
    CustomerSite,
    MaterialCard,
    Quote,
    Requisition,
    User,
    VendorCard,
    VendorContact,
)
from app.models.crm import SiteContact  # noqa: E402
from app.models.enrichment import ProspectContact  # noqa: E402
from app.models.offers import VendorResponse  # noqa: E402
from app.models.vendors import VendorReview  # noqa: E402

# ── Helpers ───────────────────────────────────────────────────────────────


def _vendor_review(db: Session, vendor: VendorCard, user: User, **kw) -> VendorReview:
    defaults = dict(
        vendor_card_id=vendor.id,
        user_id=user.id,
        rating=4,
        comment="Good supplier",
        created_at=datetime.now(timezone.utc),
    )
    defaults.update(kw)
    r = VendorReview(**defaults)
    db.add(r)
    db.commit()
    db.refresh(r)
    return r


def _prospect(db: Session, vendor: VendorCard, **kw) -> ProspectContact:
    defaults = dict(
        vendor_card_id=vendor.id,
        full_name="Jane Prospect",
        title="Sales Rep",
        email=f"prospect-{uuid.uuid4().hex[:6]}@vendor.com",
        source="web_search",
        confidence="medium",
        created_at=datetime.now(timezone.utc),
    )
    defaults.update(kw)
    p = ProspectContact(**defaults)
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


def _site_contact(db: Session, site: CustomerSite, **kw) -> SiteContact:
    defaults = dict(
        customer_site_id=site.id,
        full_name="Alice Contact",
        email=f"alice-{uuid.uuid4().hex[:6]}@site.com",
        title="Buyer",
    )
    defaults.update(kw)
    c = SiteContact(**defaults)
    db.add(c)
    db.commit()
    db.refresh(c)
    return c


def _vendor_response(db: Session, req: Requisition, **kw) -> VendorResponse:
    defaults = dict(
        requisition_id=req.id,
        vendor_name="Arrow",
        vendor_email="sales@arrow.com",
        subject="Re: RFQ",
        body="We have stock.",
        status="new",
        received_at=datetime.now(timezone.utc),
    )
    defaults.update(kw)
    vr = VendorResponse(**defaults)
    db.add(vr)
    db.commit()
    db.refresh(vr)
    return vr


def _req(db: Session, user: User, **kw) -> Requisition:
    defaults = dict(
        name=f"N9-REQ-{uuid.uuid4().hex[:6]}",
        customer_name="Acme",
        status="active",
        created_by=user.id,
        created_at=datetime.now(timezone.utc),
    )
    defaults.update(kw)
    r = Requisition(**defaults)
    db.add(r)
    db.commit()
    db.refresh(r)
    return r


# ── Section 1: Vendor Reviews ─────────────────────────────────────────────


class TestVendorReviews:
    """Covers add_vendor_review and delete_vendor_review."""

    def test_add_review_success(
        self,
        client: TestClient,
        db_session: Session,
        test_vendor_card: VendorCard,
    ):
        resp = client.post(
            f"/v2/partials/vendors/{test_vendor_card.id}/reviews",
            data={"rating": "5", "comment": "Excellent"},
        )
        assert resp.status_code == 200
        review = db_session.query(VendorReview).filter_by(vendor_card_id=test_vendor_card.id).first()
        assert review is not None
        assert review.rating == 5

    def test_add_review_invalid_rating_defaults_to_3(
        self,
        client: TestClient,
        db_session: Session,
        test_vendor_card: VendorCard,
    ):
        resp = client.post(
            f"/v2/partials/vendors/{test_vendor_card.id}/reviews",
            data={"rating": "not-a-number", "comment": ""},
        )
        assert resp.status_code == 200
        review = db_session.query(VendorReview).filter_by(vendor_card_id=test_vendor_card.id).first()
        assert review is not None
        assert review.rating == 3

    def test_add_review_clamps_rating_above_5(
        self,
        client: TestClient,
        db_session: Session,
        test_vendor_card: VendorCard,
    ):
        resp = client.post(
            f"/v2/partials/vendors/{test_vendor_card.id}/reviews",
            data={"rating": "99"},
        )
        assert resp.status_code == 200
        review = db_session.query(VendorReview).filter_by(vendor_card_id=test_vendor_card.id).first()
        assert review is not None
        assert review.rating == 5

    def test_add_review_vendor_not_found(self, client: TestClient):
        resp = client.post("/v2/partials/vendors/99999/reviews", data={"rating": "3"})
        assert resp.status_code == 404

    def test_delete_review_success(
        self,
        client: TestClient,
        db_session: Session,
        test_vendor_card: VendorCard,
        test_user: User,
    ):
        review = _vendor_review(db_session, test_vendor_card, test_user)
        resp = client.delete(f"/v2/partials/vendors/{test_vendor_card.id}/reviews/{review.id}")
        assert resp.status_code == 200
        gone = db_session.get(VendorReview, review.id)
        assert gone is None

    def test_delete_review_not_found(
        self,
        client: TestClient,
        test_vendor_card: VendorCard,
    ):
        resp = client.delete(f"/v2/partials/vendors/{test_vendor_card.id}/reviews/99999")
        assert resp.status_code == 404

    def test_delete_review_other_user_forbidden(
        self,
        client: TestClient,
        db_session: Session,
        test_vendor_card: VendorCard,
    ):
        # Create a different user and attach the review to them
        other = User(
            email="other@trioscs.com",
            name="Other User",
            role="buyer",
            azure_id="other-azure-99",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(other)
        db_session.commit()
        db_session.refresh(other)
        review = _vendor_review(db_session, test_vendor_card, other)

        resp = client.delete(f"/v2/partials/vendors/{test_vendor_card.id}/reviews/{review.id}")
        assert resp.status_code == 403


# ── Section 2: Vendor Prospects ───────────────────────────────────────────


class TestVendorProspects:
    """Covers vendor_prospect_save, vendor_prospect_promote, vendor_prospect_delete."""

    def test_save_prospect_success(
        self,
        client: TestClient,
        db_session: Session,
        test_vendor_card: VendorCard,
        test_user: User,
    ):
        p = _prospect(db_session, test_vendor_card)
        resp = client.post(f"/v2/partials/vendors/{test_vendor_card.id}/ai/prospect/{p.id}/save")
        assert resp.status_code == 200
        db_session.refresh(p)
        assert p.is_saved is True
        assert p.saved_by_id == test_user.id

    def test_save_prospect_not_found(
        self,
        client: TestClient,
        test_vendor_card: VendorCard,
    ):
        resp = client.post(f"/v2/partials/vendors/{test_vendor_card.id}/ai/prospect/99999/save")
        assert resp.status_code == 404

    def test_promote_prospect_creates_vendor_contact(
        self,
        client: TestClient,
        db_session: Session,
        test_vendor_card: VendorCard,
    ):
        p = _prospect(db_session, test_vendor_card)
        resp = client.post(f"/v2/partials/vendors/{test_vendor_card.id}/ai/prospect/{p.id}/promote")
        assert resp.status_code == 200
        db_session.refresh(p)
        assert p.promoted_to_type == "vendor_contact"
        vc = db_session.get(VendorContact, p.promoted_to_id)
        assert vc is not None
        assert vc.vendor_card_id == test_vendor_card.id

    def test_promote_prospect_updates_existing_contact(
        self,
        client: TestClient,
        db_session: Session,
        test_vendor_card: VendorCard,
    ):
        # Pre-existing contact with same email
        email = f"shared-{uuid.uuid4().hex[:6]}@vendor.com"
        existing_vc = VendorContact(
            vendor_card_id=test_vendor_card.id,
            full_name="",
            email=email,
            source="manual",
        )
        db_session.add(existing_vc)
        db_session.commit()
        db_session.refresh(existing_vc)

        p = _prospect(db_session, test_vendor_card, email=email, full_name="New Name")
        resp = client.post(f"/v2/partials/vendors/{test_vendor_card.id}/ai/prospect/{p.id}/promote")
        assert resp.status_code == 200
        db_session.refresh(existing_vc)
        # full_name should be populated from prospect
        assert existing_vc.full_name == "New Name"

    def test_promote_prospect_not_found(
        self,
        client: TestClient,
        test_vendor_card: VendorCard,
    ):
        resp = client.post(f"/v2/partials/vendors/{test_vendor_card.id}/ai/prospect/99999/promote")
        assert resp.status_code == 404

    def test_delete_prospect_success(
        self,
        client: TestClient,
        db_session: Session,
        test_vendor_card: VendorCard,
    ):
        p = _prospect(db_session, test_vendor_card)
        resp = client.delete(f"/v2/partials/vendors/{test_vendor_card.id}/ai/prospect/{p.id}")
        assert resp.status_code == 200
        assert resp.text == ""
        gone = db_session.get(ProspectContact, p.id)
        assert gone is None

    def test_delete_prospect_not_found(
        self,
        client: TestClient,
        test_vendor_card: VendorCard,
    ):
        resp = client.delete(f"/v2/partials/vendors/{test_vendor_card.id}/ai/prospect/99999")
        assert resp.status_code == 404


# ── Section 3: Company CRUD ───────────────────────────────────────────────


class TestCompanyCRUD:
    """Covers create_company and edit_company."""

    def test_create_company_success(
        self,
        client: TestClient,
        db_session: Session,
    ):
        name = f"New Corp {uuid.uuid4().hex[:6]}"
        resp = client.post(
            "/v2/partials/customers/create",
            data={"name": name, "website": "https://newcorp.com", "industry": "Tech"},
        )
        assert resp.status_code == 200
        company = db_session.query(Company).filter(Company.name == name).first()
        assert company is not None
        # Auto-created HQ site
        site = db_session.query(CustomerSite).filter_by(company_id=company.id, site_name="HQ").first()
        assert site is not None

    def test_create_company_no_name_raises_400(self, client: TestClient):
        resp = client.post("/v2/partials/customers/create", data={"name": ""})
        assert resp.status_code == 400

    def test_create_company_duplicate_raises_409(
        self,
        client: TestClient,
        test_company: Company,
    ):
        resp = client.post(
            "/v2/partials/customers/create",
            data={"name": test_company.name},
        )
        assert resp.status_code == 409

    def test_edit_company_success(
        self,
        client: TestClient,
        db_session: Session,
        test_company: Company,
    ):
        resp = client.post(
            f"/v2/partials/customers/{test_company.id}/edit",
            data={
                "name": "Updated Corp Name",
                "website": "https://updated.com",
                "industry": "Aerospace",
            },
        )
        assert resp.status_code == 200
        db_session.refresh(test_company)
        assert test_company.name == "Updated Corp Name"
        assert test_company.website == "https://updated.com"

    def test_edit_company_not_found(self, client: TestClient):
        resp = client.post(
            "/v2/partials/customers/99999/edit",
            data={"name": "Nobody"},
        )
        assert resp.status_code == 404


# ── Section 4: Site CRUD ──────────────────────────────────────────────────


class TestSiteCRUD:
    """Covers create_site, delete_site, edit_site."""

    def test_create_site_success(
        self,
        client: TestClient,
        db_session: Session,
        test_company: Company,
    ):
        resp = client.post(
            f"/v2/partials/customers/{test_company.id}/sites",
            data={
                "site_name": "East Coast Office",
                "site_type": "branch",
                "city": "Boston",
                "country": "US",
            },
        )
        assert resp.status_code == 200
        site = (
            db_session.query(CustomerSite).filter_by(company_id=test_company.id, site_name="East Coast Office").first()
        )
        assert site is not None
        assert site.city == "Boston"

    def test_create_site_no_name_returns_error_html(
        self,
        client: TestClient,
        test_company: Company,
    ):
        resp = client.post(
            f"/v2/partials/customers/{test_company.id}/sites",
            data={"site_name": ""},
        )
        assert resp.status_code == 200
        assert "required" in resp.text.lower()

    def test_create_site_company_not_found(self, client: TestClient):
        resp = client.post(
            "/v2/partials/customers/99999/sites",
            data={"site_name": "Branch"},
        )
        assert resp.status_code == 404

    def test_create_site_no_owner_id_succeeds(
        self,
        client: TestClient,
        db_session: Session,
        test_company: Company,
    ):
        # Create a site with no owner_id — skips the owner-conflict branch
        resp = client.post(
            f"/v2/partials/customers/{test_company.id}/sites",
            data={"site_name": "Branch Office"},
        )
        assert resp.status_code == 200
        site = db_session.query(CustomerSite).filter_by(company_id=test_company.id, site_name="Branch Office").first()
        assert site is not None

    def test_delete_site_success(
        self,
        client: TestClient,
        db_session: Session,
        test_company: Company,
        test_customer_site: CustomerSite,
    ):
        resp = client.delete(f"/v2/partials/customers/{test_company.id}/sites/{test_customer_site.id}")
        assert resp.status_code == 200
        assert resp.text == ""
        db_session.refresh(test_customer_site)
        assert test_customer_site.is_active is False

    def test_delete_site_not_found(
        self,
        client: TestClient,
        test_company: Company,
    ):
        resp = client.delete(f"/v2/partials/customers/{test_company.id}/sites/99999")
        assert resp.status_code == 404

    def test_edit_site_success(
        self,
        client: TestClient,
        db_session: Session,
        test_company: Company,
        test_customer_site: CustomerSite,
    ):
        resp = client.post(
            f"/v2/partials/customers/{test_company.id}/sites/{test_customer_site.id}/edit",
            data={"site_name": "Renamed HQ", "city": "New York", "country": "US"},
        )
        assert resp.status_code == 200
        db_session.refresh(test_customer_site)
        assert test_customer_site.site_name == "Renamed HQ"
        assert test_customer_site.city == "New York"

    def test_edit_site_not_found(
        self,
        client: TestClient,
        test_company: Company,
    ):
        resp = client.post(
            f"/v2/partials/customers/{test_company.id}/sites/99999/edit",
            data={"site_name": "X"},
        )
        assert resp.status_code == 404


# ── Section 5: Site Contact CRUD ─────────────────────────────────────────


class TestSiteContactCRUD:
    """Covers create_site_contact, delete_site_contact, set_primary_contact,
    add_site_contact_note, get_site_contact_notes."""

    def test_create_site_contact_success(
        self,
        client: TestClient,
        db_session: Session,
        test_company: Company,
        test_customer_site: CustomerSite,
    ):
        resp = client.post(
            f"/v2/partials/customers/{test_company.id}/sites/{test_customer_site.id}/contacts",
            data={
                "full_name": "Bob Builder",
                "email": "bob@builder.com",
                "title": "Engineer",
                "phone": "+1-555-1234",
            },
        )
        assert resp.status_code == 200
        contact = (
            db_session.query(SiteContact)
            .filter_by(customer_site_id=test_customer_site.id, full_name="Bob Builder")
            .first()
        )
        assert contact is not None

    def test_create_site_contact_no_name_returns_error(
        self,
        client: TestClient,
        test_company: Company,
        test_customer_site: CustomerSite,
    ):
        resp = client.post(
            f"/v2/partials/customers/{test_company.id}/sites/{test_customer_site.id}/contacts",
            data={"full_name": "", "email": "x@x.com"},
        )
        assert resp.status_code == 200
        assert "required" in resp.text.lower()

    def test_create_site_contact_site_not_found(
        self,
        client: TestClient,
        test_company: Company,
    ):
        resp = client.post(
            f"/v2/partials/customers/{test_company.id}/sites/99999/contacts",
            data={"full_name": "X", "email": "x@x.com"},
        )
        assert resp.status_code == 404

    def test_create_site_contact_duplicate_email_skips(
        self,
        client: TestClient,
        db_session: Session,
        test_company: Company,
        test_customer_site: CustomerSite,
    ):
        # Create contact first
        email = f"dup-{uuid.uuid4().hex[:6]}@site.com"
        _site_contact(db_session, test_customer_site, email=email)

        resp = client.post(
            f"/v2/partials/customers/{test_company.id}/sites/{test_customer_site.id}/contacts",
            data={"full_name": "Duplicate", "email": email},
        )
        # Should succeed (returns list without creating duplicate)
        assert resp.status_code == 200

    def test_delete_site_contact_success(
        self,
        client: TestClient,
        db_session: Session,
        test_company: Company,
        test_customer_site: CustomerSite,
    ):
        contact = _site_contact(db_session, test_customer_site)
        resp = client.delete(
            f"/v2/partials/customers/{test_company.id}/sites/{test_customer_site.id}/contacts/{contact.id}"
        )
        assert resp.status_code == 200
        assert resp.text == ""
        gone = db_session.get(SiteContact, contact.id)
        assert gone is None

    def test_delete_site_contact_not_found(
        self,
        client: TestClient,
        test_company: Company,
        test_customer_site: CustomerSite,
    ):
        resp = client.delete(f"/v2/partials/customers/{test_company.id}/sites/{test_customer_site.id}/contacts/99999")
        assert resp.status_code == 404

    def test_set_primary_contact_success(
        self,
        client: TestClient,
        db_session: Session,
        test_company: Company,
        test_customer_site: CustomerSite,
    ):
        contact = _site_contact(db_session, test_customer_site)
        resp = client.post(
            f"/v2/partials/customers/{test_company.id}/sites/{test_customer_site.id}/contacts/{contact.id}/primary"
        )
        assert resp.status_code == 200
        db_session.refresh(contact)
        assert contact.is_primary is True

    def test_set_primary_contact_unsets_others(
        self,
        client: TestClient,
        db_session: Session,
        test_company: Company,
        test_customer_site: CustomerSite,
    ):
        c1 = _site_contact(db_session, test_customer_site, email="c1@s.com", full_name="C1")
        c2 = _site_contact(db_session, test_customer_site, email="c2@s.com", full_name="C2")
        c1.is_primary = True
        db_session.commit()

        resp = client.post(
            f"/v2/partials/customers/{test_company.id}/sites/{test_customer_site.id}/contacts/{c2.id}/primary"
        )
        assert resp.status_code == 200
        db_session.refresh(c1)
        db_session.refresh(c2)
        assert c2.is_primary is True
        assert c1.is_primary is False

    def test_set_primary_contact_not_found(
        self,
        client: TestClient,
        test_company: Company,
        test_customer_site: CustomerSite,
    ):
        resp = client.post(
            f"/v2/partials/customers/{test_company.id}/sites/{test_customer_site.id}/contacts/99999/primary"
        )
        assert resp.status_code == 404

    def test_add_site_contact_note_success(
        self,
        client: TestClient,
        db_session: Session,
        test_company: Company,
        test_customer_site: CustomerSite,
    ):
        contact = _site_contact(db_session, test_customer_site)
        resp = client.post(
            f"/v2/partials/customers/{test_company.id}/sites/{test_customer_site.id}/contacts/{contact.id}/notes",
            data={"notes": "This is a test note."},
        )
        assert resp.status_code == 200

    def test_add_site_contact_note_empty_raises_400(
        self,
        client: TestClient,
        db_session: Session,
        test_company: Company,
        test_customer_site: CustomerSite,
    ):
        contact = _site_contact(db_session, test_customer_site)
        resp = client.post(
            f"/v2/partials/customers/{test_company.id}/sites/{test_customer_site.id}/contacts/{contact.id}/notes",
            data={"notes": ""},
        )
        assert resp.status_code == 400

    def test_add_site_contact_note_contact_not_found(
        self,
        client: TestClient,
        test_company: Company,
        test_customer_site: CustomerSite,
    ):
        resp = client.post(
            f"/v2/partials/customers/{test_company.id}/sites/{test_customer_site.id}/contacts/99999/notes",
            data={"notes": "hello"},
        )
        assert resp.status_code == 404

    def test_get_site_contact_notes_success(
        self,
        client: TestClient,
        db_session: Session,
        test_company: Company,
        test_customer_site: CustomerSite,
    ):
        contact = _site_contact(db_session, test_customer_site)
        resp = client.get(
            f"/v2/partials/customers/{test_company.id}/sites/{test_customer_site.id}/contacts/{contact.id}/notes"
        )
        assert resp.status_code == 200

    def test_get_site_contact_notes_not_found(
        self,
        client: TestClient,
        test_company: Company,
        test_customer_site: CustomerSite,
    ):
        resp = client.get(
            f"/v2/partials/customers/{test_company.id}/sites/{test_customer_site.id}/contacts/99999/notes"
        )
        assert resp.status_code == 404


# ── Section 6: Quote Metadata Edit ───────────────────────────────────────


class TestEditQuoteMetadata:
    """Covers edit_quote_metadata (lines 5301-5328)."""

    def test_edit_quote_metadata_success(
        self,
        client: TestClient,
        db_session: Session,
        test_quote: Quote,
    ):
        resp = client.post(
            f"/v2/partials/quotes/{test_quote.id}/edit",
            data={
                "payment_terms": "Net 30",
                "shipping_terms": "FOB Origin",
                "notes": "Expedite if possible",
                "valid_until": "2026-12-31",
            },
        )
        assert resp.status_code == 200
        db_session.refresh(test_quote)
        assert test_quote.payment_terms == "Net 30"
        assert test_quote.shipping_terms == "FOB Origin"
        assert test_quote.notes == "Expedite if possible"

    def test_edit_quote_metadata_not_found(self, client: TestClient):
        resp = client.post(
            "/v2/partials/quotes/99999/edit",
            data={"payment_terms": "Net 60"},
        )
        assert resp.status_code == 404


# ── Section 7: Response Status Update ────────────────────────────────────


class TestUpdateResponseStatus:
    """Covers update_response_status (lines 5515-5550)."""

    def test_update_status_to_reviewed(
        self,
        client: TestClient,
        db_session: Session,
        test_user: User,
    ):
        req = _req(db_session, test_user)
        vr = _vendor_response(db_session, req)
        resp = client.patch(
            f"/v2/partials/requisitions/{req.id}/responses/{vr.id}/status",
            data={"status": "reviewed"},
        )
        assert resp.status_code == 200
        db_session.refresh(vr)
        assert vr.status == "reviewed"

    def test_update_status_to_rejected(
        self,
        client: TestClient,
        db_session: Session,
        test_user: User,
    ):
        req = _req(db_session, test_user)
        vr = _vendor_response(db_session, req)
        resp = client.patch(
            f"/v2/partials/requisitions/{req.id}/responses/{vr.id}/status",
            data={"status": "rejected"},
        )
        assert resp.status_code == 200
        db_session.refresh(vr)
        assert vr.status == "rejected"

    def test_update_status_to_flagged(
        self,
        client: TestClient,
        db_session: Session,
        test_user: User,
    ):
        req = _req(db_session, test_user)
        vr = _vendor_response(db_session, req)
        resp = client.patch(
            f"/v2/partials/requisitions/{req.id}/responses/{vr.id}/status",
            data={"status": "flagged"},
        )
        assert resp.status_code == 200

    def test_update_status_invalid_raises_400(
        self,
        client: TestClient,
        db_session: Session,
        test_user: User,
    ):
        req = _req(db_session, test_user)
        vr = _vendor_response(db_session, req)
        resp = client.patch(
            f"/v2/partials/requisitions/{req.id}/responses/{vr.id}/status",
            data={"status": "banana"},
        )
        assert resp.status_code == 400

    def test_update_status_not_found(
        self,
        client: TestClient,
        db_session: Session,
        test_user: User,
    ):
        req = _req(db_session, test_user)
        resp = client.patch(
            f"/v2/partials/requisitions/{req.id}/responses/99999/status",
            data={"status": "reviewed"},
        )
        assert resp.status_code == 404


# ── Section 8: Material Card Update ──────────────────────────────────────


class TestUpdateMaterialCard:
    """Covers update_material_card (lines 7334-7380)."""

    def test_update_material_card_success(
        self,
        client: TestClient,
        db_session: Session,
        test_material_card: MaterialCard,
    ):
        resp = client.put(
            f"/v2/partials/materials/{test_material_card.id}",
            data={
                "manufacturer": "ON Semiconductor",
                "description": "Updated description",
                "category": "Regulators",
                "package_type": "TO-220",
                "lifecycle_status": "active",
                "rohs_status": "compliant",
                "pin_count": "3",
            },
        )
        assert resp.status_code == 200
        db_session.refresh(test_material_card)
        assert test_material_card.manufacturer == "ON Semiconductor"
        assert test_material_card.category == "Regulators"
        assert test_material_card.pin_count == 3

    def test_update_material_card_invalid_pin_count_ignored(
        self,
        client: TestClient,
        db_session: Session,
        test_material_card: MaterialCard,
    ):
        resp = client.put(
            f"/v2/partials/materials/{test_material_card.id}",
            data={"pin_count": "not-a-number"},
        )
        assert resp.status_code == 200
        db_session.refresh(test_material_card)
        # Invalid int → stored as None
        assert test_material_card.pin_count is None

    def test_update_material_card_not_found(self, client: TestClient):
        resp = client.put(
            "/v2/partials/materials/99999",
            data={"manufacturer": "X"},
        )
        assert resp.status_code == 404
