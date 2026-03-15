"""test_rfq_compose_service.py — Tests for RFQ compose/send business logic.

Covers: building vendor lists from sightings, creating RFQ contact records,
dedup of already-asked vendors, and edge cases.

Called by: pytest
Depends on: app.services.rfq_compose_service, conftest fixtures
"""

from datetime import datetime, timezone

import pytest
from sqlalchemy.orm import Session

from app.models import Requirement, Sighting, VendorCard, VendorContact
from app.models.offers import Contact as RfqContact
from app.services.rfq_compose_service import build_rfq_vendor_list, create_rfq_contacts


# -- Factories ----------------------------------------------------------------


def _make_sighting(db: Session, requirement_id: int, vendor_norm: str, **kw) -> Sighting:
    s = Sighting(
        requirement_id=requirement_id,
        vendor_name=kw.get("vendor_name", vendor_norm.title()),
        vendor_name_normalized=vendor_norm,
        mpn_matched=kw.get("mpn_matched", "LM317T"),
        source_type=kw.get("source_type", "api"),
        qty_available=kw.get("qty_available", 100),
        unit_price=kw.get("unit_price", 0.50),
        created_at=datetime.now(timezone.utc),
    )
    db.add(s)
    db.flush()
    return s


def _make_vendor_contact(db: Session, vendor_card_id: int, email: str, **kw) -> VendorContact:
    vc = VendorContact(
        vendor_card_id=vendor_card_id,
        email=email,
        full_name=kw.get("full_name", "Sales Rep"),
        source="manual",
    )
    db.add(vc)
    db.flush()
    return vc


# -- TestBuildRfqVendorList ---------------------------------------------------


class TestBuildRfqVendorList:
    def test_returns_vendors_from_sightings(
        self, db_session: Session, test_requisition, test_vendor_card
    ):
        """Vendors with sightings for the req's parts appear in the list."""
        req_id = test_requisition.id
        part = db_session.query(Requirement).filter(
            Requirement.requisition_id == req_id
        ).first()
        _make_sighting(db_session, part.id, "arrow electronics")
        _make_vendor_contact(db_session, test_vendor_card.id, "sales@arrow.com")
        db_session.commit()

        result = build_rfq_vendor_list(db_session, req_id)

        assert len(result) == 1
        assert result[0]["display_name"] == "Arrow Electronics"
        assert result[0]["normalized_name"] == "arrow electronics"
        assert "sales@arrow.com" in result[0]["emails"]

    def test_empty_when_no_parts(self, db_session: Session):
        """Requisition with no requirements returns empty list."""
        from app.models import Requisition

        req = Requisition(
            name="EMPTY-REQ", customer_name="Test Co", status="open",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(req)
        db_session.commit()

        result = build_rfq_vendor_list(db_session, req.id)
        assert result == []

    def test_empty_when_no_sightings(self, db_session: Session, test_requisition):
        """Requisition with parts but no sightings returns empty list."""
        result = build_rfq_vendor_list(db_session, test_requisition.id)
        assert result == []

    def test_already_asked_flag(
        self, db_session: Session, test_requisition, test_vendor_card, test_user
    ):
        """Vendors that already have RFQ contacts are flagged."""
        req_id = test_requisition.id
        part = db_session.query(Requirement).filter(
            Requirement.requisition_id == req_id
        ).first()
        _make_sighting(db_session, part.id, "arrow electronics")

        # Create an existing RFQ contact for this vendor
        existing = RfqContact(
            requisition_id=req_id,
            user_id=test_user.id,
            contact_type="email",
            vendor_name="Arrow Electronics",
            vendor_name_normalized="arrow electronics",
            vendor_contact="sales@arrow.com",
            status="sent",
        )
        db_session.add(existing)
        db_session.commit()

        result = build_rfq_vendor_list(db_session, req_id)

        assert len(result) == 1
        assert result[0]["already_asked"] is True

    def test_multiple_vendors(
        self, db_session: Session, test_requisition
    ):
        """Multiple distinct vendors from sightings all appear."""
        req_id = test_requisition.id
        part = db_session.query(Requirement).filter(
            Requirement.requisition_id == req_id
        ).first()

        # Create two vendor cards
        v1 = VendorCard(
            normalized_name="digikey", display_name="Digikey",
            created_at=datetime.now(timezone.utc),
        )
        v2 = VendorCard(
            normalized_name="mouser electronics", display_name="Mouser Electronics",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add_all([v1, v2])
        db_session.flush()

        _make_sighting(db_session, part.id, "digikey")
        _make_sighting(db_session, part.id, "mouser electronics")
        db_session.commit()

        result = build_rfq_vendor_list(db_session, req_id)

        names = {v["normalized_name"] for v in result}
        assert "digikey" in names
        assert "mouser electronics" in names

    def test_contacts_limited_to_five(
        self, db_session: Session, test_requisition, test_vendor_card
    ):
        """Each vendor's contact list is capped at 5."""
        req_id = test_requisition.id
        part = db_session.query(Requirement).filter(
            Requirement.requisition_id == req_id
        ).first()
        _make_sighting(db_session, part.id, "arrow electronics")

        for i in range(8):
            _make_vendor_contact(db_session, test_vendor_card.id, f"rep{i}@arrow.com")
        db_session.commit()

        result = build_rfq_vendor_list(db_session, req_id)

        assert len(result) == 1
        assert len(result[0]["contacts"]) <= 5


# -- TestCreateRfqContacts ----------------------------------------------------


class TestCreateRfqContacts:
    def test_creates_contacts(self, db_session: Session, test_requisition, test_user):
        """Creates RFQ contact records for each vendor/email pair."""
        result = create_rfq_contacts(
            db_session, test_requisition.id, test_user.id,
            vendor_names=["Arrow Electronics", "Digikey"],
            vendor_emails=["sales@arrow.com", "sales@digikey.com"],
            subject="RFQ for LM317T",
            parts_text="LM317T x 1000",
        )
        db_session.commit()

        assert len(result) == 2
        assert result[0]["vendor"] == "Arrow Electronics"
        assert result[0]["email"] == "sales@arrow.com"
        assert result[0]["status"] == "sent"

        # Verify DB records
        contacts = db_session.query(RfqContact).filter(
            RfqContact.requisition_id == test_requisition.id
        ).all()
        assert len(contacts) == 2

    def test_skips_empty_emails(self, db_session: Session, test_requisition, test_user):
        """Vendors with empty email are skipped."""
        result = create_rfq_contacts(
            db_session, test_requisition.id, test_user.id,
            vendor_names=["Arrow", "Digikey"],
            vendor_emails=["", "sales@digikey.com"],
            subject="RFQ",
            parts_text="LM317T",
        )
        db_session.commit()

        assert len(result) == 1
        assert result[0]["vendor"] == "Digikey"

    def test_normalizes_vendor_name(self, db_session: Session, test_requisition, test_user):
        """Vendor name is normalized to lowercase/stripped."""
        create_rfq_contacts(
            db_session, test_requisition.id, test_user.id,
            vendor_names=["  Arrow Electronics  "],
            vendor_emails=["sales@arrow.com"],
            subject="RFQ",
            parts_text="LM317T",
        )
        db_session.commit()

        contact = db_session.query(RfqContact).filter(
            RfqContact.requisition_id == test_requisition.id
        ).first()
        assert contact.vendor_name_normalized == "arrow electronics"

    def test_empty_lists_returns_empty(self, db_session: Session, test_requisition, test_user):
        """No vendors = no contacts created."""
        result = create_rfq_contacts(
            db_session, test_requisition.id, test_user.id,
            vendor_names=[], vendor_emails=[],
            subject="RFQ", parts_text="",
        )
        assert result == []

    def test_sets_correct_fields(self, db_session: Session, test_requisition, test_user):
        """Verify all fields are set correctly on created contact."""
        create_rfq_contacts(
            db_session, test_requisition.id, test_user.id,
            vendor_names=["TestVendor"],
            vendor_emails=["test@vendor.com"],
            subject="RFQ for parts",
            parts_text="LM317T x 500",
        )
        db_session.commit()

        contact = db_session.query(RfqContact).filter(
            RfqContact.requisition_id == test_requisition.id
        ).first()
        assert contact.user_id == test_user.id
        assert contact.contact_type == "email"
        assert contact.vendor_name == "TestVendor"
        assert contact.vendor_contact == "test@vendor.com"
        assert contact.subject == "RFQ for parts"
        assert contact.parts_included == "LM317T x 500"
        assert contact.status == "sent"
        assert contact.status_updated_at is not None
