"""
test_schemas_vendors.py — Tests for app/schemas/vendors.py

Validates VendorCardUpdate, VendorBlacklistToggle, VendorReviewCreate,
VendorContactLookup, VendorContactCreate, VendorEmailAdd, MaterialCardUpdate.

Called by: pytest
Depends on: app/schemas/vendors.py
"""

import pytest
from pydantic import ValidationError

from app.schemas.vendors import (
    MaterialCardUpdate,
    VendorBlacklistToggle,
    VendorCardUpdate,
    VendorContactCreate,
    VendorContactLookup,
    VendorEmailAdd,
    VendorReviewCreate,
)


class TestVendorCardUpdate:
    def test_all_none_defaults(self):
        u = VendorCardUpdate()
        assert u.emails is None and u.phones is None

    def test_cleans_and_dedupes_emails(self):
        u = VendorCardUpdate(emails=["A@Test.com", " a@test.com ", "b@x.com"])
        assert u.emails == ["a@test.com", "b@x.com"]

    def test_filters_invalid_emails(self):
        u = VendorCardUpdate(emails=["good@x.com", "no-at-sign", ""])
        assert u.emails == ["good@x.com"]

    def test_dedupes_phones(self):
        u = VendorCardUpdate(phones=["555-1234", " 555-1234 ", "555-5678"])
        assert u.phones == ["555-1234", "555-5678"]


class TestVendorBlacklistToggle:
    def test_none_means_flip(self):
        t = VendorBlacklistToggle()
        assert t.blacklisted is None

    def test_explicit_true(self):
        t = VendorBlacklistToggle(blacklisted=True)
        assert t.blacklisted is True


class TestVendorReviewCreate:
    def test_defaults(self):
        r = VendorReviewCreate()
        assert r.rating == 3 and r.comment == ""

    def test_clamps_high_rating(self):
        r = VendorReviewCreate(rating=99)
        assert r.rating == 5

    def test_clamps_low_rating(self):
        r = VendorReviewCreate(rating=-5)
        assert r.rating == 1

    def test_truncates_long_comment(self):
        r = VendorReviewCreate(comment="x" * 600)
        assert len(r.comment) == 500


class TestVendorContactLookup:
    def test_valid(self):
        v = VendorContactLookup(vendor_name="Acme Corp")
        assert v.vendor_name == "Acme Corp"

    def test_blank_raises(self):
        with pytest.raises(ValidationError, match="vendor_name required"):
            VendorContactLookup(vendor_name="  ")

    def test_strips_whitespace(self):
        v = VendorContactLookup(vendor_name="  Acme  ")
        assert v.vendor_name == "Acme"


class TestVendorContactCreate:
    def test_valid_minimal(self):
        c = VendorContactCreate(email="test@acme.com")
        assert c.email == "test@acme.com" and c.label == "Sales"

    def test_lowercases_email(self):
        c = VendorContactCreate(email="TEST@Acme.COM")
        assert c.email == "test@acme.com"

    def test_blank_email_raises(self):
        with pytest.raises(ValidationError, match="Email is required"):
            VendorContactCreate(email="  ")


class TestVendorEmailAdd:
    def test_valid(self):
        e = VendorEmailAdd(vendor_name="Acme", email="a@acme.com")
        assert e.email == "a@acme.com"

    def test_invalid_email_raises(self):
        with pytest.raises(ValidationError, match="valid email required"):
            VendorEmailAdd(vendor_name="Acme", email="no-at-sign")

    def test_blank_vendor_raises(self):
        with pytest.raises(ValidationError, match="vendor_name required"):
            VendorEmailAdd(vendor_name="", email="a@b.com")


class TestMaterialCardUpdate:
    def test_all_none_defaults(self):
        m = MaterialCardUpdate()
        assert m.manufacturer is None and m.description is None

    def test_with_values(self):
        m = MaterialCardUpdate(manufacturer="TI", description="Op-amp")
        assert m.manufacturer == "TI"
