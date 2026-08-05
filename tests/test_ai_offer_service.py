"""test_ai_offer_service.py — Tests for AI offer and RFQ business logic.

Covers: prospect contact promotion, applying freeform RFQ templates, and the HTMX
form-parse save loop (save_form_parsed_offers, W3: each row through
offer_service.create_offer). The retired JSON-pair tests (save_parsed_offers /
save_freeform_offers) were deleted with the functions in W3.

Called by: pytest
Depends on: app.services.ai_offer_service, conftest fixtures
"""

from unittest.mock import patch

import pytest
from sqlalchemy.orm import Session

from app.models import (
    Offer,
    ProspectContact,
    Requisition,
    SiteContact,
    VendorCard,
    VendorContact,
)
from app.services.ai_offer_service import (
    apply_freeform_rfq,
    parse_offer_form_rows,
    promote_prospect_contact,
    save_form_parsed_offers,
)

# -- Factories ----------------------------------------------------------------


def _make_prospect_contact(
    db: Session,
    vendor_card_id=None,
    customer_site_id=None,
    email="prospect@example.com",
    full_name="Jane Prospect",
    **kw,
) -> ProspectContact:
    pc = ProspectContact(
        vendor_card_id=vendor_card_id,
        customer_site_id=customer_site_id,
        full_name=full_name,
        email=email,
        title=kw.get("title", "Sales Rep"),
        phone=kw.get("phone", "+1-555-9999"),
        linkedin_url=kw.get("linkedin_url", "https://linkedin.com/in/janeprospect"),
        source="apollo",
        confidence="high",
    )
    db.add(pc)
    db.flush()
    return pc


# -- TestPromoteProspectContact -----------------------------------------------


class TestPromoteProspectContact:
    def test_promote_vendor_contact_creates_new(self, db_session: Session, test_user, test_vendor_card):
        pc = _make_prospect_contact(db_session, vendor_card_id=test_vendor_card.id)
        result = promote_prospect_contact(db_session, pc.id, test_user.id)
        db_session.commit()

        assert result["ok"] is True
        assert result["promoted_to_type"] == "vendor_contact"
        vc = db_session.get(VendorContact, result["promoted_to_id"])
        assert vc is not None
        assert vc.email == "prospect@example.com"
        assert vc.vendor_card_id == test_vendor_card.id

    def test_promote_vendor_contact_dedupes_by_email(self, db_session: Session, test_user, test_vendor_card):
        existing = VendorContact(
            vendor_card_id=test_vendor_card.id,
            email="prospect@example.com",
            full_name=None,
            source="manual",
        )
        db_session.add(existing)
        db_session.flush()

        pc = _make_prospect_contact(db_session, vendor_card_id=test_vendor_card.id, full_name="New Name")
        result = promote_prospect_contact(db_session, pc.id, test_user.id)
        db_session.commit()

        assert result["promoted_to_id"] == existing.id
        db_session.refresh(existing)
        assert existing.full_name == "New Name"

    def test_promote_site_contact_creates_new(self, db_session: Session, test_user, test_customer_site):
        pc = _make_prospect_contact(db_session, customer_site_id=test_customer_site.id)
        result = promote_prospect_contact(db_session, pc.id, test_user.id)
        db_session.commit()

        assert result["promoted_to_type"] == "site_contact"
        sc = db_session.get(SiteContact, result["promoted_to_id"])
        assert sc is not None
        assert sc.email == "prospect@example.com"

    def test_promote_site_contact_dedupes_by_email(self, db_session: Session, test_user, test_customer_site):
        existing = SiteContact(
            customer_site_id=test_customer_site.id,
            email="prospect@example.com",
            full_name="Old Name",
        )
        db_session.add(existing)
        db_session.flush()

        pc = _make_prospect_contact(db_session, customer_site_id=test_customer_site.id, title="Director")
        result = promote_prospect_contact(db_session, pc.id, test_user.id)
        db_session.commit()

        assert result["promoted_to_id"] == existing.id
        db_session.refresh(existing)
        assert existing.full_name == "Old Name"  # not overwritten (already set)

    def test_promote_not_found_raises(self, db_session: Session, test_user):
        with pytest.raises(ValueError, match="Prospect contact not found"):
            promote_prospect_contact(db_session, 99999, test_user.id)

    def test_promote_no_linked_entity_raises(self, db_session: Session, test_user):
        pc = _make_prospect_contact(db_session)  # neither vendor nor site
        with pytest.raises(ValueError, match="no vendor_card_id or customer_site_id"):
            promote_prospect_contact(db_session, pc.id, test_user.id)

    def test_promote_sets_is_saved_and_saved_by(self, db_session: Session, test_user, test_vendor_card):
        pc = _make_prospect_contact(db_session, vendor_card_id=test_vendor_card.id)
        promote_prospect_contact(db_session, pc.id, test_user.id)
        db_session.commit()
        db_session.refresh(pc)

        assert pc.is_saved is True
        assert pc.saved_by_id == test_user.id


# -- TestApplyFreeformRfq ----------------------------------------------------


class TestApplyFreeformRfq:
    @patch("app.search_service.resolve_material_card", return_value=None)
    def test_creates_requisition_and_requirements(
        self, mock_resolve, db_session: Session, test_user, test_customer_site
    ):
        items = [
            {"primary_mpn": "LM317T", "manufacturer": "TI", "target_qty": 500},
            {"primary_mpn": "NE555P", "manufacturer": "TI", "target_qty": 200},
        ]
        result = apply_freeform_rfq(db_session, "Test RFQ", test_customer_site.id, None, None, items, test_user.id)
        db_session.commit()

        assert result["requirements_added"] == 2
        req = db_session.get(Requisition, result["id"])
        assert req.status == "draft"
        assert req.name == "Test RFQ"

    @patch("app.search_service.resolve_material_card", return_value=None)
    def test_max_50_requirements(self, mock_resolve, db_session: Session, test_user, test_customer_site):
        items = [{"primary_mpn": f"PART{i:03d}", "manufacturer": "TI", "target_qty": 1} for i in range(60)]
        result = apply_freeform_rfq(db_session, "Big RFQ", test_customer_site.id, None, None, items, test_user.id)
        db_session.commit()

        assert result["requirements_added"] == 50

    @patch("app.search_service.resolve_material_card", return_value=None)
    def test_invalid_requirement_skipped(self, mock_resolve, db_session: Session, test_user, test_customer_site):
        items = [
            {"primary_mpn": "", "manufacturer": "TI", "target_qty": 1},  # blank MPN should fail validation
            {"primary_mpn": "GOOD-PART", "manufacturer": "TI", "target_qty": 100},
        ]
        result = apply_freeform_rfq(db_session, "Mixed RFQ", test_customer_site.id, None, None, items, test_user.id)
        db_session.commit()

        assert result["requirements_added"] == 1

    def test_customer_site_not_found_raises(self, db_session: Session, test_user):
        with pytest.raises(ValueError, match="Customer site not found"):
            apply_freeform_rfq(db_session, "RFQ", 99999, None, None, [], test_user.id)


# -- TestParseOfferFormRows ---------------------------------------------------
# P4.2: form-array parsing extracted from routers/htmx/offers.py::save_parsed_offers.


class TestParseOfferFormRows:
    def test_parses_sequential_offer_rows(self):
        """offers[0].* / offers[1].* fields collect into a row dict each, stopping at
        the first gap."""
        form = {
            "offers[0].mpn": "LM317T",
            "offers[0].qty_available": "100",
            "offers[0].unit_price": "0.42",
            "offers[1].mpn": "NE555P",
        }
        rows = parse_offer_form_rows(form, vendor_name="Acme Distribution")
        assert len(rows) == 2
        assert rows[0]["mpn"] == "LM317T"
        assert rows[0]["qty_available"] == 100
        assert rows[0]["unit_price"] == 0.42
        assert rows[1]["mpn"] == "NE555P"

    def test_no_offer_rows_returns_empty_list(self):
        """A form with no offers[i].* fields at all returns [] — the router's signal to
        render 'No offers to save' without calling the save function."""
        assert parse_offer_form_rows({}, vendor_name="Acme Distribution") == []

    def test_zero_string_qty_and_price_parse_to_zero_not_none(self):
        """Regression: qty_available/moq/unit_price now go through the shared
        app.utils.safe_int/safe_float instead of a private falsy-pre-check helper.
        A literal "0" string (a real, if unusual, form value — e.g. an explicit
        zero-stock row) must still parse to 0, not None — form values are always
        `str | None`, so the string "0" is truthy and takes the int()/float() branch
        under both the old and new implementation."""
        form = {
            "offers[0].mpn": "LM317T",
            "offers[0].qty_available": "0",
            "offers[0].unit_price": "0",
            "offers[0].moq": "0",
        }
        rows = parse_offer_form_rows(form, vendor_name="Acme Distribution")
        assert rows[0]["qty_available"] == 0
        assert rows[0]["unit_price"] == 0.0
        assert rows[0]["moq"] == 0

    def test_blank_qty_and_price_parse_to_none(self):
        """An empty-string form field (left blank by the user) still parses to None, not
        0 — unchanged from the pre-dedup private helper."""
        form = {
            "offers[0].mpn": "LM317T",
            "offers[0].qty_available": "",
            "offers[0].unit_price": "",
            "offers[0].moq": "",
        }
        rows = parse_offer_form_rows(form, vendor_name="Acme Distribution")
        assert rows[0]["qty_available"] is None
        assert rows[0]["unit_price"] is None
        assert rows[0]["moq"] is None


# -- TestSaveFormParsedOffers -------------------------------------------------
# The HTMX form-review-then-save flow — saves straight to ACTIVE since the user
# already reviewed/edited the rows in the form. W3: each row now goes through the
# canonical offer_service.create_offer (vendor resolve/create, normalized_mpn,
# qualification, activity, release hook all live in the service).


class TestSaveFormParsedOffers:
    async def test_creates_active_offer_with_exact_requirement_match(
        self, db_session: Session, test_requisition, test_user
    ):
        offers_data = parse_offer_form_rows(
            {"offers[0].mpn": "LM317T", "offers[0].vendor_name": "Acme Distribution"}, vendor_name=""
        )
        saved_count = await save_form_parsed_offers(db_session, test_requisition.id, "", offers_data, test_user)

        assert saved_count == 1
        offer = db_session.query(Offer).filter_by(requisition_id=test_requisition.id).first()
        assert offer.status == "active"
        assert offer.source == "ai_parsed"
        assert offer.requirement_id is not None  # exact match on "LM317T"
        card = db_session.get(VendorCard, offer.vendor_card_id)
        assert card.display_name == "Acme Distribution"

    async def test_rows_with_no_mpn_are_skipped(self, db_session: Session, test_requisition, test_user):
        """A row with a blank mpn is silently skipped — no Offer, no VendorCard."""
        offers_data = parse_offer_form_rows({"offers[0].vendor_name": "Freeform Vendor"}, vendor_name="")
        saved_count = await save_form_parsed_offers(db_session, test_requisition.id, "", offers_data, test_user)

        assert saved_count == 0
        assert db_session.query(Offer).filter_by(requisition_id=test_requisition.id).count() == 0
