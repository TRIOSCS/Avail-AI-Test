"""Tests for requirement & offer field expansions.

Covers: RequirementCreate/Update/Out schema fields, OfferCreate/Update/Out schema fields.
Depends on: app/schemas/requisitions.py, app/schemas/crm.py
"""

import datetime

import pytest

from app.schemas.crm import OfferCreate, OfferOut, OfferUpdate
from app.schemas.requisitions import RequirementCreate, RequirementOut, RequirementUpdate


class TestRequirementCreateSchema:
    def test_brand_accepted(self):
        r = RequirementCreate(primary_mpn="LM358DR", brand="Texas Instruments")
        assert r.brand == "Texas Instruments"

    def test_customer_pn_accepted(self):
        r = RequirementCreate(primary_mpn="LM358DR", customer_pn="CUST-001")
        assert r.customer_pn == "CUST-001"

    def test_need_by_date_accepted(self):
        d = datetime.date(2026, 4, 15)
        r = RequirementCreate(primary_mpn="LM358DR", need_by_date=d)
        assert r.need_by_date == d

    def test_all_new_fields_default_none(self):
        r = RequirementCreate(primary_mpn="LM358DR")
        assert r.brand is None
        assert r.customer_pn is None
        assert r.need_by_date is None


class TestRequirementUpdateSchema:
    def test_brand_update(self):
        r = RequirementUpdate(brand="Analog Devices")
        assert r.brand == "Analog Devices"

    def test_customer_pn_update(self):
        r = RequirementUpdate(customer_pn="CUST-002")
        assert r.customer_pn == "CUST-002"

    def test_need_by_date_update(self):
        d = datetime.date(2026, 5, 1)
        r = RequirementUpdate(need_by_date=d)
        assert r.need_by_date == d


class TestRequirementOutSchema:
    def test_includes_all_fields(self):
        data = {
            "id": 1,
            "primary_mpn": "LM358DR",
            "target_qty": 100,
            "target_price": 0.55,
            "substitutes": [],
            "sighting_count": 3,
            "brand": "TI",
            "customer_pn": "CUST-001",
            "need_by_date": datetime.date(2026, 4, 15),
            "condition": "new",
            "date_codes": "2025+",
            "firmware": None,
            "hardware_codes": None,
            "packaging": "Tape & Reel",
            "notes": "Urgent",
        }
        r = RequirementOut(**data)
        assert r.brand == "TI"
        assert r.customer_pn == "CUST-001"
        assert r.condition == "new"
        assert r.notes == "Urgent"


class TestOfferCreateSchema:
    def test_spq_accepted(self):
        o = OfferCreate(mpn="LM358DR", vendor_name="Acme", spq=100)
        assert o.spq == 100

    def test_spq_defaults_none(self):
        o = OfferCreate(mpn="LM358DR", vendor_name="Acme")
        assert o.spq is None

    def test_spq_rejects_zero(self):
        with pytest.raises(Exception):
            OfferCreate(mpn="LM358DR", vendor_name="Acme", spq=0)


class TestOfferUpdateSchema:
    def test_spq_update(self):
        o = OfferUpdate(spq=50)
        assert o.spq == 50

    def test_valid_until_update(self):
        o = OfferUpdate(valid_until=datetime.date(2026, 6, 1))
        assert o.valid_until == datetime.date(2026, 6, 1)


class TestOfferOutSchema:
    def test_includes_all_fields(self):
        data = {
            "id": 1,
            "vendor_name": "Acme",
            "mpn": "LM358DR",
            "manufacturer": "TI",
            "qty_available": 500,
            "unit_price": 0.45,
            "lead_time": "2-3 weeks",
            "date_code": "2025+",
            "condition": "new",
            "packaging": "Tape & Reel",
            "moq": 100,
            "spq": 50,
            "firmware": "v2.1",
            "hardware_code": "REV-B",
            "warranty": "1 year",
            "country_of_origin": "US",
            "notes": "In stock",
            "status": "active",
        }
        o = OfferOut(**data)
        assert o.spq == 50
        assert o.manufacturer == "TI"
        assert o.status == "active"
