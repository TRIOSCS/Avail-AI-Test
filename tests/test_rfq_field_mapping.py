"""
tests/test_rfq_field_mapping.py — Tests for legacy Salesforce field mapping in the RFQ workspace.

Verifies that all critical legacy fields (sourcing_status, condition, packaging,
sale_notes, substitutes, date_codes, firmware, hardware_codes) are returned by
the requirements list endpoint and available for display in the UI.

Called by: pytest
Depends on: routers/requisitions/requirements.py, conftest fixtures
"""

from datetime import datetime, timezone

from app.models import Offer, Requirement, Sighting


# ── Requirements list returns legacy fields ─────────────────────────


def test_requirements_include_sourcing_status(client, test_requisition, db_session):
    """sourcing_status field is returned for each part in the list."""
    req = test_requisition
    r = req.requirements[0]
    r.sourcing_status = "sourcing"
    db_session.commit()

    resp = client.get(f"/api/requisitions/{req.id}/requirements")
    assert resp.status_code == 200
    data = resp.json()
    assert data[0]["sourcing_status"] == "sourcing"


def test_requirements_include_condition_and_packaging(client, test_requisition, db_session):
    """condition and packaging fields are returned for indicator display."""
    req = test_requisition
    r = req.requirements[0]
    r.condition = "New"
    r.packaging = "Tape & Reel"
    db_session.commit()

    resp = client.get(f"/api/requisitions/{req.id}/requirements")
    data = resp.json()
    assert data[0]["condition"] == "New"
    assert data[0]["packaging"] == "Tape & Reel"


def test_requirements_include_sale_notes(client, test_requisition, db_session):
    """sale_notes field is returned for panel header display."""
    req = test_requisition
    r = req.requirements[0]
    r.sale_notes = "Customer needs COC"
    db_session.commit()

    resp = client.get(f"/api/requisitions/{req.id}/requirements")
    data = resp.json()
    assert data[0]["sale_notes"] == "Customer needs COC"


def test_requirements_include_date_codes_firmware_hardware(client, test_requisition, db_session):
    """date_codes, firmware, hardware_codes returned as panel detail chips."""
    req = test_requisition
    r = req.requirements[0]
    r.date_codes = "2024+"
    r.firmware = "v3.1"
    r.hardware_codes = "Rev C"
    db_session.commit()

    resp = client.get(f"/api/requisitions/{req.id}/requirements")
    data = resp.json()
    assert data[0]["date_codes"] == "2024+"
    assert data[0]["firmware"] == "v3.1"
    assert data[0]["hardware_codes"] == "Rev C"


def test_requirements_include_substitutes(client, test_requisition, db_session):
    """substitutes list is returned for panel header sub chips."""
    req = test_requisition
    r = req.requirements[0]
    r.substitutes = ["LM317T-ALT", "LM317T-SUB"]
    db_session.commit()

    resp = client.get(f"/api/requisitions/{req.id}/requirements")
    data = resp.json()
    assert data[0]["substitutes"] == ["LM317T-ALT", "LM317T-SUB"]


# ── Offers endpoint returns legacy availability fields ──────────────


def test_offer_includes_manufacturer_warranty_coo(client, test_requisition, db_session):
    """Offers include manufacturer, warranty, country_of_origin fields."""
    req = test_requisition
    r = req.requirements[0]
    offer = Offer(
        requisition_id=req.id,
        requirement_id=r.id,
        vendor_name="Arrow",
        mpn="LM317T",
        manufacturer="TI",
        warranty="1 year",
        country_of_origin="MY",
        qty_available=500,
        unit_price=0.45,
        status="active",
        source="manual",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(offer)
    db_session.commit()

    resp = client.get(f"/api/requirements/{r.id}/offers")
    data = resp.json()
    assert len(data) == 1
    assert data[0]["manufacturer"] == "TI"
    assert data[0]["warranty"] == "1 year"
    assert data[0]["country_of_origin"] == "MY"


def test_offer_includes_packaging_and_firmware(client, test_requisition, db_session):
    """Offers include packaging and firmware fields."""
    req = test_requisition
    r = req.requirements[0]
    offer = Offer(
        requisition_id=req.id,
        requirement_id=r.id,
        vendor_name="Mouser",
        mpn="LM317T",
        packaging="Tube",
        firmware="v2.0",
        qty_available=1000,
        unit_price=0.50,
        status="active",
        source="manual",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(offer)
    db_session.commit()

    resp = client.get(f"/api/requirements/{r.id}/offers")
    data = resp.json()
    assert data[0]["packaging"] == "Tube"
    assert data[0]["firmware"] == "v2.0"


# ── Sightings include expanded fields ──────────────────────────────


def test_sighting_fields_available(client, test_requisition, db_session):
    """Sightings include packaging, date_code, manufacturer, condition, lead_time."""
    req = test_requisition
    r = req.requirements[0]
    sighting = Sighting(
        requirement_id=r.id,
        vendor_name="Test Vendor",
        mpn_matched="LM317T",
        manufacturer="TI",
        qty_available=500,
        unit_price=0.40,
        condition="New",
        packaging="T&R",
        date_code="2024+",
        lead_time="2-3 weeks",
        source_type="broker",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(sighting)
    db_session.commit()

    resp = client.get(f"/api/requisitions/{req.id}/sightings")
    assert resp.status_code == 200
    data = resp.json()
    part_data = data.get(str(r.id), {})
    sightings_list = part_data.get("sightings", [])
    assert len(sightings_list) >= 1
    s = sightings_list[0]
    assert s["condition"] == "New"
    assert s["manufacturer"] == "TI"
