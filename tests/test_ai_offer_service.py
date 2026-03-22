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
    promote_prospect_contact,
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
        assert offer.condition == "new"
        assert offer.currency == "USD"
