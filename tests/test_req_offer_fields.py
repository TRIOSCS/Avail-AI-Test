"""Tests for Requirement & Offer Fields + Column Picker feature.

Covers: new model columns (customer_pn, need_by_date, spq), schema validation,
router form handling, column preference persistence, and default rendering.

Called by: pytest
Depends on: conftest.py fixtures (test_user, test_requisition, test_offer, client, db_session)
"""

import os

os.environ["TESTING"] = "1"
os.environ["RATE_LIMIT_ENABLED"] = "false"

from datetime import date

import pytest

from app.models import Offer, Requirement
from app.schemas.crm import OfferCreate, OfferOut, OfferUpdate
from app.schemas.requisitions import RequirementCreate, RequirementOut, RequirementUpdate

# ── Requirement Schema Tests ──────────────────────────────────────────


class TestRequirementSchemas:
    """Test new fields on RequirementCreate/Update/Out."""

    def test_create_with_new_fields(self):
        """RequirementCreate accepts customer_pn, need_by_date, brand."""
        data = RequirementCreate(
            primary_mpn="LM317T",
            target_qty=100,
            brand="Texas Instruments",
            customer_pn="CUST-001",
            need_by_date=date(2026, 4, 15),
            condition="new",
            packaging="Tape & Reel",
        )
        assert data.brand == "Texas Instruments"
        assert data.customer_pn == "CUST-001"
        assert data.need_by_date == date(2026, 4, 15)
        assert data.condition == "new"

    def test_create_defaults_none(self):
        """New fields default to None when not provided."""
        data = RequirementCreate(primary_mpn="LM317T")
        assert data.brand is None
        assert data.customer_pn is None
        assert data.need_by_date is None

    def test_update_with_new_fields(self):
        """RequirementUpdate accepts customer_pn and need_by_date."""
        data = RequirementUpdate(
            customer_pn="CUST-002",
            need_by_date=date(2026, 5, 1),
            brand="Analog Devices",
        )
        assert data.customer_pn == "CUST-002"
        assert data.need_by_date == date(2026, 5, 1)
        assert data.brand == "Analog Devices"

    def test_out_includes_new_fields(self):
        """RequirementOut includes all new fields."""
        data = RequirementOut(
            id=1,
            primary_mpn="LM317T",
            target_qty=100,
            brand="TI",
            customer_pn="CUST-001",
            need_by_date=date(2026, 4, 15),
            condition="new",
            date_codes="2024+",
            firmware="v1.2",
            hardware_codes="REV-A",
            packaging="T&R",
            notes="Test note",
        )
        assert data.brand == "TI"
        assert data.customer_pn == "CUST-001"
        assert data.need_by_date == date(2026, 4, 15)
        assert data.condition == "new"
        assert data.notes == "Test note"

    def test_condition_normalization(self):
        """RequirementCreate normalizes condition values."""
        data = RequirementCreate(primary_mpn="LM317T", condition="Refurbished")
        assert data.condition == "refurb"


# ── Offer Schema Tests ────────────────────────────────────────────────


class TestOfferSchemas:
    """Test spq field on OfferCreate/Update/Out."""

    def test_create_with_spq(self):
        """OfferCreate accepts spq field."""
        data = OfferCreate(
            mpn="LM317T",
            vendor_name="Arrow",
            spq=50,
        )
        assert data.spq == 50

    def test_create_spq_ge_1(self):
        """OfferCreate rejects spq < 1."""
        with pytest.raises(Exception):
            OfferCreate(mpn="LM317T", vendor_name="Arrow", spq=0)

    def test_update_with_spq(self):
        """OfferUpdate accepts spq field."""
        data = OfferUpdate(spq=25)
        assert data.spq == 25

    def test_out_includes_spq(self):
        """OfferOut includes spq and other new fields."""
        data = OfferOut(
            id=1,
            vendor_name="Arrow",
            mpn="LM317T",
            spq=100,
            manufacturer="TI",
            warranty="1 year",
        )
        assert data.spq == 100
        assert data.manufacturer == "TI"
        assert data.warranty == "1 year"


# ── Model Tests ───────────────────────────────────────────────────────


class TestRequirementModel:
    """Test new columns on Requirement model."""

    def test_customer_pn_and_need_by_date(self, db_session, test_requisition):
        """Requirement stores customer_pn and need_by_date."""
        req = test_requisition
        item = Requirement(
            requisition_id=req.id,
            primary_mpn="TEST-123",
            customer_pn="CUST-XYZ",
            need_by_date=date(2026, 6, 1),
        )
        db_session.add(item)
        db_session.commit()
        db_session.refresh(item)

        assert item.customer_pn == "CUST-XYZ"
        assert item.need_by_date == date(2026, 6, 1)

    def test_customer_pn_nullable(self, db_session, test_requisition):
        """customer_pn and need_by_date are nullable."""
        item = Requirement(
            requisition_id=test_requisition.id,
            primary_mpn="TEST-456",
        )
        db_session.add(item)
        db_session.commit()
        db_session.refresh(item)

        assert item.customer_pn is None
        assert item.need_by_date is None


class TestOfferModel:
    """Test spq column on Offer model."""

    def test_spq_stored(self, db_session, test_requisition):
        """Offer stores spq value."""
        offer = Offer(
            requisition_id=test_requisition.id,
            vendor_name="Test Vendor",
            mpn="TEST-SPQ",
            spq=25,
            status="active",
        )
        db_session.add(offer)
        db_session.commit()
        db_session.refresh(offer)

        assert offer.spq == 25

    def test_spq_nullable(self, db_session, test_requisition):
        """Spq is nullable."""
        offer = Offer(
            requisition_id=test_requisition.id,
            vendor_name="Test Vendor",
            mpn="TEST-SPQ2",
            status="active",
        )
        db_session.add(offer)
        db_session.commit()
        db_session.refresh(offer)

        assert offer.spq is None


class TestUserColumnPrefs:
    """Test column preference columns on User model."""

    def test_requirements_column_prefs(self, db_session, test_user):
        """User.requirements_column_prefs stores JSON array."""
        test_user.requirements_column_prefs = ["mpn", "brand", "qty"]
        db_session.commit()
        db_session.refresh(test_user)

        assert test_user.requirements_column_prefs == ["mpn", "brand", "qty"]

    def test_offers_column_prefs(self, db_session, test_user):
        """User.offers_column_prefs stores JSON array."""
        test_user.offers_column_prefs = ["vendor", "mpn", "price"]
        db_session.commit()
        db_session.refresh(test_user)

        assert test_user.offers_column_prefs == ["vendor", "mpn", "price"]

    def test_prefs_default_none(self, db_session, test_user):
        """Column prefs default to None."""
        assert test_user.requirements_column_prefs is None
        assert test_user.offers_column_prefs is None


# ── HTMX Route Tests ─────────────────────────────────────────────────


class TestAddRequirementWithNewFields:
    """Test add_requirement route with new form fields."""

    def test_add_with_all_fields(self, client, test_requisition, db_session):
        """POST add requirement with customer_pn, need_by_date, condition, etc."""
        resp = client.post(
            f"/v2/partials/requisitions/{test_requisition.id}/requirements",
            data={
                "primary_mpn": "NEW-PART-001",
                "target_qty": "500",
                "brand": "Texas Instruments",
                "target_price": "1.2500",
                "customer_pn": "CUST-ABC",
                "need_by_date": "2026-05-01",
                "condition": "new",
                "packaging": "Tape & Reel",
                "date_codes": "2025+",
                "firmware": "v2.0",
                "hardware_codes": "REV-B",
                "substitutes": "ALT-001, ALT-002",
                "notes": "Urgent order",
            },
        )
        assert resp.status_code == 200

        item = db_session.query(Requirement).filter(Requirement.primary_mpn == "NEW-PART-001").first()
        assert item is not None
        assert item.customer_pn == "CUST-ABC"
        assert item.need_by_date == date(2026, 5, 1)
        assert item.condition == "new"
        assert item.packaging == "Tape & Reel"
        assert item.firmware == "v2.0"
        assert item.notes == "Urgent order"

    def test_add_with_minimal_fields(self, client, test_requisition, db_session):
        """POST add requirement with only required fields still works."""
        resp = client.post(
            f"/v2/partials/requisitions/{test_requisition.id}/requirements",
            data={"primary_mpn": "MINIMAL-001"},
        )
        assert resp.status_code == 200

        item = db_session.query(Requirement).filter(Requirement.primary_mpn == "MINIMAL-001").first()
        assert item is not None
        assert item.customer_pn is None
        assert item.need_by_date is None


class TestUpdateRequirementWithNewFields:
    """Test update_requirement route with new form fields."""

    def test_update_with_new_fields(self, client, test_requisition, db_session):
        """PUT updates customer_pn, need_by_date, condition, etc."""
        req = test_requisition
        item = db_session.query(Requirement).filter(Requirement.requisition_id == req.id).first()

        resp = client.put(
            f"/v2/partials/requisitions/{req.id}/requirements/{item.id}",
            data={
                "primary_mpn": item.primary_mpn,
                "target_qty": "1000",
                "brand": "TI",
                "customer_pn": "UPD-CUST-001",
                "need_by_date": "2026-07-15",
                "condition": "refurbished",
                "notes": "Updated note",
            },
        )
        assert resp.status_code == 200

        db_session.refresh(item)
        assert item.customer_pn == "UPD-CUST-001"
        assert item.need_by_date == date(2026, 7, 15)
        assert item.condition == "refurbished"  # route does not normalize
        assert item.notes == "Updated note"


class TestAddOfferWithNewFields:
    """Test add_offer route with new form fields."""

    def test_add_offer_with_spq_and_extras(self, client, test_requisition, db_session):
        """POST add offer with spq, manufacturer, packaging, etc."""
        resp = client.post(
            f"/v2/partials/requisitions/{test_requisition.id}/add-offer",
            data={
                "vendor_name": "Mouser",
                "mpn": "LM317T",
                "qty_available": "1000",
                "unit_price": "0.5000",
                "manufacturer": "Texas Instruments",
                "spq": "50",
                "packaging": "Tube",
                "firmware": "v1.0",
                "hardware_code": "REV-A",
                "warranty": "1 year",
                "country_of_origin": "US",
                "valid_until": "2026-12-31",
                "condition": "new",
            },
        )
        assert resp.status_code == 200

        offer = db_session.query(Offer).filter(Offer.vendor_name == "Mouser", Offer.mpn == "LM317T").first()
        assert offer is not None
        assert offer.spq == 50
        assert offer.manufacturer == "Texas Instruments"
        assert offer.packaging == "Tube"
        assert offer.warranty == "1 year"
        assert offer.country_of_origin == "US"
        assert offer.valid_until == date(2026, 12, 31)


class TestColumnPrefsEndpoints:
    """Test column preference save endpoints."""

    def test_save_req_column_prefs(self, client, test_user, test_requisition, db_session):
        """POST column-prefs saves to user.requirements_column_prefs."""
        resp = client.post(
            f"/v2/partials/requisitions/{test_requisition.id}/req-column-prefs",
            data={"columns": ["mpn", "brand", "qty"]},
        )
        assert resp.status_code == 200

        db_session.refresh(test_user)
        assert test_user.requirements_column_prefs == ["mpn", "brand", "qty"]

    def test_save_offer_column_prefs(self, client, test_user, test_requisition, db_session):
        """POST offers-column-prefs saves to user.offers_column_prefs."""
        resp = client.post(
            f"/v2/partials/requisitions/{test_requisition.id}/offer-column-prefs",
            data={"columns": ["vendor", "mpn", "price", "spq"]},
        )
        assert resp.status_code == 200

        db_session.refresh(test_user)
        assert test_user.offers_column_prefs == ["vendor", "mpn", "price", "spq"]

    def test_invalid_columns_reset_to_default(self, client, test_user, test_requisition, db_session):
        """Invalid column keys are filtered; empty list resets to default."""
        resp = client.post(
            f"/v2/partials/requisitions/{test_requisition.id}/req-column-prefs",
            data={"columns": ["invalid_col", "not_real"]},
        )
        assert resp.status_code == 200

        db_session.refresh(test_user)
        # Should have reset to defaults since no valid columns
        assert test_user.requirements_column_prefs == [
            "mpn",
            "brand",
            "qty",
            "target_price",
            "customer_pn",
            "need_by_date",
            "status",
            "sightings",
        ]


class TestPartsTabRendering:
    """Test that parts tab renders with column picker context."""

    def test_parts_tab_includes_column_data(self, client, test_requisition):
        """GET parts tab returns HTML with column picker."""
        resp = client.get(
            f"/v2/partials/requisitions/{test_requisition.id}/tab/parts",
        )
        assert resp.status_code == 200
        html = resp.text
        assert 'data-col-key="mpn"' in html
        assert 'data-col-key="customer_pn"' in html
        assert 'data-col-key="need_by_date"' in html

    def test_offers_tab_includes_column_data(self, client, test_requisition, test_offer):
        """GET offers tab returns HTML with column picker."""
        resp = client.get(
            f"/v2/partials/requisitions/{test_requisition.id}/tab/offers",
        )
        assert resp.status_code == 200
        html = resp.text
        assert 'data-col-key="vendor"' in html
        assert 'data-col-key="spq"' in html
        assert 'data-col-key="manufacturer"' in html
