"""test_ai_offer_service.py — Tests for AI offer and RFQ business logic.

Covers: prospect contact promotion, saving AI-parsed offers, applying freeform
RFQ templates, and saving freeform offers.

Called by: pytest
Depends on: app.services.ai_offer_service, conftest fixtures
"""

from types import SimpleNamespace
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
    save_freeform_offers,
    save_parsed_offers,
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


def _make_offer_input(**kw) -> SimpleNamespace:
    defaults = dict(
        mpn="LM317T",
        vendor_name="Arrow Electronics",
        manufacturer="Texas Instruments",
        qty_available=1000,
        unit_price=0.50,
        currency="USD",
        lead_time="2 weeks",
        date_code="2025+",
        condition="new",
        packaging="tape_reel",
        moq=100,
        notes="Test offer",
    )
    defaults.update(kw)
    return SimpleNamespace(**defaults)


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


# -- TestSaveParsedOffers -----------------------------------------------------


class TestSaveParsedOffers:
    @patch("app.search_service.resolve_material_card")
    def test_creates_offers_with_requirement_match(
        self, mock_resolve, db_session: Session, test_requisition, test_user, test_material_card
    ):
        mock_resolve.return_value = test_material_card
        offer_in = _make_offer_input(mpn="LM317T")

        result = save_parsed_offers(db_session, test_requisition.id, None, [offer_in], test_user.id)
        db_session.commit()

        assert result["created"] == 1
        offer = db_session.get(Offer, result["offer_ids"][0])
        assert offer.material_card_id == test_material_card.id
        assert offer.source == "ai_parsed"
        assert offer.status == "pending_review"

    @patch("app.search_service.resolve_material_card", return_value=None)
    def test_creates_offers_with_no_matching_requirement(
        self, mock_resolve, db_session: Session, test_requisition, test_user
    ):
        """Offer with unrelated MPN gets no requirement match."""
        offer_in = _make_offer_input(mpn="ZZZZUNKNOWN")
        result = save_parsed_offers(db_session, test_requisition.id, None, [offer_in], test_user.id)
        db_session.commit()

        assert result["created"] == 1
        offer = db_session.get(Offer, result["offer_ids"][0])
        assert offer.requirement_id is None

    @patch("app.search_service.resolve_material_card")
    def test_returns_count_and_ids(self, mock_resolve, db_session: Session, test_requisition, test_user):
        mock_resolve.return_value = None
        offers = [_make_offer_input(mpn=f"PART{i}") for i in range(3)]
        result = save_parsed_offers(db_session, test_requisition.id, None, offers, test_user.id)
        db_session.commit()

        assert result["created"] == 3
        assert len(result["offer_ids"]) == 3

    @patch("app.search_service.resolve_material_card")
    def test_empty_offers_list(self, mock_resolve, db_session: Session, test_requisition, test_user):
        result = save_parsed_offers(db_session, test_requisition.id, None, [], test_user.id)
        assert result["created"] == 0
        assert result["offer_ids"] == []


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


# -- TestSaveFreeformOffers --------------------------------------------------


class TestSaveFreeformOffers:
    @patch("app.search_service.resolve_material_card", return_value=None)
    def test_creates_offers_and_vendor_cards(self, mock_resolve, db_session: Session, test_requisition, test_user):
        offer_in = _make_offer_input(vendor_name="New Vendor Co")
        result = save_freeform_offers(db_session, test_requisition.id, [offer_in], test_user.id)
        db_session.commit()

        assert result["created"] == 1
        offer = db_session.get(Offer, result["offer_ids"][0])
        assert offer.source == "freeform_parsed"
        assert offer.vendor_card_id is not None

        card = db_session.get(VendorCard, offer.vendor_card_id)
        assert card.display_name == "New Vendor Co"

    @patch("app.search_service.resolve_material_card", return_value=None)
    def test_reuses_existing_vendor_card(
        self, mock_resolve, db_session: Session, test_requisition, test_user, test_vendor_card
    ):
        offer_in = _make_offer_input(vendor_name="Arrow Electronics")
        result = save_freeform_offers(db_session, test_requisition.id, [offer_in], test_user.id)
        db_session.commit()

        offer = db_session.get(Offer, result["offer_ids"][0])
        assert offer.vendor_card_id == test_vendor_card.id

    @patch("app.search_service.resolve_material_card", return_value=None)
    def test_defaults_condition_and_currency(self, mock_resolve, db_session: Session, test_requisition, test_user):
        offer_in = _make_offer_input(condition=None, currency=None)
        result = save_freeform_offers(db_session, test_requisition.id, [offer_in], test_user.id)
        db_session.commit()

        offer = db_session.get(Offer, result["offer_ids"][0])
        # P2.5: default now comes from OfferCondition.NEW (value-identical to the
        # old raw "new" literal).
        from app.constants import OfferCondition

        assert offer.condition == OfferCondition.NEW
        assert offer.condition == "new"
        assert offer.currency == "USD"

    @patch("app.search_service.resolve_material_card", return_value=None)
    @patch("app.services.ai_offer_service.maybe_release_on_offer")
    def test_forwards_offer_condition_to_release_hook(
        self, mock_release, mock_resolve, db_session: Session, test_requisition, test_user
    ):
        """save_freeform_offers must forward offer.condition as offer_condition to
        maybe_release_on_offer.

        RED before Task-7 adds offer_condition=offer.condition to the call site.

        P2.5 follow-up: ``Offer._validate_condition`` now normalizes the legacy
        "refurbished" spelling to the canonical ``OfferCondition.REFURB`` ("refurb")
        the moment it's assigned to the ORM attribute, so what's actually forwarded
        here is the normalized value -- `release_on_offer`'s own broad
        `normalize_condition()` maps both "refurbished" and "refurb" to the same
        "refurb" bucket, so this is a same-vocabulary rename, not a behavior change.
        """
        offer_in = _make_offer_input(condition="refurbished")
        save_freeform_offers(db_session, test_requisition.id, [offer_in], test_user.id)

        assert mock_release.called, "maybe_release_on_offer was not called at all"
        _args, kwargs = mock_release.call_args
        forwarded_condition = kwargs.get("offer_condition", _args[4] if len(_args) > 4 else "NOT_PASSED")
        assert forwarded_condition == "refurb", (
            f"Expected offer_condition='refurb' (OfferCondition-normalized from 'refurbished') "
            f"forwarded to maybe_release_on_offer but got: {forwarded_condition!r}. "
            "Task-7: add offer_condition=offer.condition to the call site."
        )


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
# P4.2: MPN matching, VendorCard resolution, Offer construction extracted from
# routers/htmx/offers.py::save_parsed_offers (the HTMX form-review-then-save flow,
# distinct from save_parsed_offers' AI PENDING_REVIEW path above — this one saves
# straight to ACTIVE since the user already reviewed/edited the rows in the form).


class TestSaveFormParsedOffers:
    def test_creates_active_offer_with_exact_requirement_match(self, db_session: Session, test_requisition, test_user):
        offers_data = parse_offer_form_rows(
            {"offers[0].mpn": "LM317T", "offers[0].vendor_name": "Acme Distribution"}, vendor_name=""
        )
        saved_count = save_form_parsed_offers(db_session, test_requisition.id, "", offers_data, test_user)
        db_session.commit()

        assert saved_count == 1
        offer = db_session.query(Offer).filter_by(requisition_id=test_requisition.id).first()
        assert offer.status == "active"
        assert offer.source == "ai_parsed"
        assert offer.requirement_id is not None  # exact match on "LM317T"
        card = db_session.get(VendorCard, offer.vendor_card_id)
        assert card.display_name == "Acme Distribution"

    def test_rows_with_no_mpn_are_skipped(self, db_session: Session, test_requisition, test_user):
        """A row with a blank mpn is silently skipped — no Offer, no VendorCard."""
        offers_data = parse_offer_form_rows({"offers[0].vendor_name": "Freeform Vendor"}, vendor_name="")
        saved_count = save_form_parsed_offers(db_session, test_requisition.id, "", offers_data, test_user)
        db_session.commit()

        assert saved_count == 0
        assert db_session.query(Offer).filter_by(requisition_id=test_requisition.id).count() == 0
