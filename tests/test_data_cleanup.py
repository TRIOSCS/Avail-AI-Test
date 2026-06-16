"""Tests for normalization helpers and data cleanup logic."""

import os

os.environ["TESTING"] = "1"

import pytest

from app.utils.normalization import (
    normalize_condition,
    normalize_mpn,
    normalize_packaging,
)
from app.utils.normalization_helpers import (
    fix_encoding,
    normalize_country,
    normalize_phone_e164,
    normalize_us_state,
)
from app.vendor_utils import normalize_vendor_name

# ── Phone normalization ──────────────────────────────────────────────


class TestNormalizePhone:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            pytest.param("(555) 123-4567", "+15551234567", id="us_10_digit"),
            pytest.param("1-800-555-0100", "+18005550100", id="us_11_digit_with_1"),
            pytest.param("+44 20 7946 0958", "+442079460958", id="international_with_plus"),
            pytest.param("555-123-4567 ext. 123", "+15551234567", id="strips_extension"),
            pytest.param("12345", None, id="too_short"),
            pytest.param(None, None, id="none"),
            pytest.param("", None, id="empty"),
            pytest.param("ext 123", None, id="ext_only"),
        ],
    )
    def test_normalize(self, raw, expected):
        assert normalize_phone_e164(raw) == expected


# ── Country normalization ────────────────────────────────────────────


class TestNormalizeCountry:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            pytest.param("United States", "US", id="full_name"),
            pytest.param("USA", "US", id="usa"),
            pytest.param("DE", "DE", id="already_code"),
            pytest.param("gb", "GB", id="lowercase_code"),
            pytest.param("Deutschland", "DE", id="full_name_other"),
            pytest.param(None, None, id="none"),
            pytest.param("", None, id="empty"),
            pytest.param("Wakanda", "Wakanda", id="unknown_passthrough"),
            pytest.param("Japan", "JP", id="japan"),
            pytest.param("Hong Kong", "HK", id="hong_kong"),
        ],
    )
    def test_normalize(self, raw, expected):
        assert normalize_country(raw) == expected


# ── US State normalization ───────────────────────────────────────────


class TestNormalizeUSState:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            pytest.param("California", "CA", id="full_name"),
            pytest.param("TX", "TX", id="already_code"),
            pytest.param("new york", "NY", id="lowercase"),
            pytest.param(None, None, id="none"),
            pytest.param("District of Columbia", "DC", id="dc"),
            pytest.param("Ontario", "Ontario", id="unknown_passthrough"),
        ],
    )
    def test_normalize(self, raw, expected):
        assert normalize_us_state(raw) == expected


# ── Encoding fix ─────────────────────────────────────────────────────


class TestFixEncoding:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            pytest.param("int?l", "Int'l", id="intl"),
            pytest.param("Normal Text", "Normal Text", id="clean_text"),
            pytest.param(None, None, id="none"),
        ],
    )
    def test_fix(self, raw, expected):
        assert fix_encoding(raw) == expected


# ── Vendor name normalization (improved suffixes) ────────────────────


class TestVendorNameNormalization:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            pytest.param("Mouser Electronics, Inc.", "mouser electronics", id="basic"),
            pytest.param("CompanyName S.A.S.", "companyname", id="european_sas"),
            pytest.param("CompanyName S.r.l.", "companyname", id="european_srl"),
            pytest.param("CompanyName S.p.A.", "companyname", id="european_spa"),
            pytest.param("CompanyName K.K.", "companyname", id="european_kk"),
            pytest.param("CompanyName A.S.", "companyname", id="european_as"),
            pytest.param("4HFIX Sp.z o.o.", "4hfix", id="polish_sp"),
            pytest.param("The Phoenix Company LLC", "phoenix", id="leading_the"),
            pytest.param("", "", id="empty"),
        ],
    )
    def test_normalize(self, raw, expected):
        assert normalize_vendor_name(raw) == expected

    def test_no_fragment_strip(self):
        # "co" should NOT be stripped from "technologyco" (word boundary issue)
        result = normalize_vendor_name("TechnologyCo")
        assert "technologyco" in result


# ── MPN normalization ────────────────────────────────────────────────


class TestNormalizeMPN:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            pytest.param("lm317t", "LM317T", id="uppercase"),
            pytest.param("  LM 317T  ", "LM317T", id="strip_whitespace"),
            pytest.param("'LM317T'", "LM317T", id="strip_quotes"),
            pytest.param("AB", None, id="too_short"),
            pytest.param(None, None, id="none"),
        ],
    )
    def test_normalize(self, raw, expected):
        assert normalize_mpn(raw) == expected


# ── Condition normalization ──────────────────────────────────────────


class TestNormalizeCondition:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            pytest.param("Factory New", "new", id="factory_new"),
            pytest.param("Refurbished", "refurb", id="refurbished"),
            pytest.param("Surplus", "used", id="surplus"),
            pytest.param(None, None, id="none"),
        ],
    )
    def test_normalize(self, raw, expected):
        assert normalize_condition(raw) == expected


# ── Packaging normalization ──────────────────────────────────────────


class TestNormalizePackaging:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            pytest.param("Tape and Reel", "reel", id="tape_and_reel"),
            pytest.param("Tray", "tray", id="tray"),
            pytest.param("Cut Tape", "cut_tape", id="cut_tape"),
            pytest.param(None, None, id="none"),
        ],
    )
    def test_normalize(self, raw, expected):
        assert normalize_packaging(raw) == expected


# ── Schema validators (write-path hooks) ─────────────────────────────


class TestSchemaValidators:
    def test_requirement_create_normalizes_mpn(self):
        from app.schemas.requisitions import RequirementCreate

        r = RequirementCreate(primary_mpn="lm317t", manufacturer="TI", target_qty=100)
        assert r.primary_mpn == "LM317T"

    def test_requirement_create_normalizes_substitutes(self):
        from app.schemas.requisitions import RequirementCreate

        r = RequirementCreate(primary_mpn="LM317T", manufacturer="TI", substitutes=["ne555p", "lm7805"])
        assert r.substitutes == ["NE555P", "LM7805"]

    def test_requirement_update_normalizes_mpn(self):
        from app.schemas.requisitions import RequirementUpdate

        r = RequirementUpdate(primary_mpn="lm317t")
        assert r.primary_mpn == "LM317T"

    def test_requirement_update_normalizes_condition(self):
        from app.schemas.requisitions import RequirementUpdate

        r = RequirementUpdate(condition="Factory New")
        assert r.condition == "new"

    def test_requirement_update_normalizes_packaging(self):
        from app.schemas.requisitions import RequirementUpdate

        r = RequirementUpdate(packaging="Tape and Reel")
        assert r.packaging == "reel"

    def test_company_update_normalizes_country(self):
        from app.schemas.crm import CompanyUpdate

        c = CompanyUpdate(hq_country="United States")
        assert c.hq_country == "US"

    def test_company_update_normalizes_state(self):
        from app.schemas.crm import CompanyUpdate

        c = CompanyUpdate(hq_state="California")
        assert c.hq_state == "CA"

    def test_company_create_normalizes_phone(self):
        from app.schemas.crm import CompanyCreate

        c = CompanyCreate(name="Acme", phone="(555) 123-4567")
        assert c.phone == "+15551234567"

    def test_offer_create_normalizes_mpn(self):
        from app.schemas.crm import OfferCreate

        o = OfferCreate(mpn="lm317t", vendor_name="Arrow")
        assert o.mpn == "LM317T"

    def test_none_fields_pass_through(self):
        from app.schemas.crm import CompanyUpdate

        c = CompanyUpdate(hq_country=None, hq_state=None, phone=None)
        assert c.hq_country is None
        assert c.hq_state is None
        assert c.phone is None
