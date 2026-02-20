"""Tests for normalization helpers and data cleanup logic."""

import os

os.environ["TESTING"] = "1"


from app.utils.normalization import (
    normalize_condition,
    normalize_mpn,
    normalize_packaging,
)
from app.utils.normalization_helpers import (
    clean_contact_name,
    fix_encoding,
    normalize_country,
    normalize_phone_e164,
    normalize_us_state,
)
from app.vendor_utils import normalize_vendor_name

# ── Phone normalization ──────────────────────────────────────────────


class TestNormalizePhone:
    def test_us_10_digit(self):
        assert normalize_phone_e164("(555) 123-4567") == "+15551234567"

    def test_us_11_digit_with_1(self):
        assert normalize_phone_e164("1-800-555-0100") == "+18005550100"

    def test_international_with_plus(self):
        assert normalize_phone_e164("+44 20 7946 0958") == "+442079460958"

    def test_strips_extension(self):
        assert normalize_phone_e164("555-123-4567 ext. 123") == "+15551234567"

    def test_too_short(self):
        assert normalize_phone_e164("12345") is None

    def test_none(self):
        assert normalize_phone_e164(None) is None

    def test_empty(self):
        assert normalize_phone_e164("") is None

    def test_ext_only(self):
        assert normalize_phone_e164("ext 123") is None


# ── Contact name cleanup ────────────────────────────────────────────


class TestCleanContactName:
    def test_title_case(self):
        name, is_person = clean_contact_name("JOHN DOE")
        assert name == "John Doe"
        assert is_person is True

    def test_strip_extension(self):
        name, is_person = clean_contact_name("Kay Jordan - Ext: 1025")
        assert name == "Kay Jordan"
        assert is_person is True

    def test_department_detected(self):
        name, is_person = clean_contact_name("MTE Sales")
        assert is_person is False

    def test_bare_sales(self):
        name, is_person = clean_contact_name("Sales")
        assert is_person is False

    def test_real_name_with_sales_substring(self):
        # "Salesperson" should not match "^sales\b" because of word boundary
        name, is_person = clean_contact_name("Leslie Thompson")
        assert name == "Leslie Thompson"
        assert is_person is True

    def test_casing_fix(self):
        name, _ = clean_contact_name("LEslie thompson")
        assert name == "Leslie Thompson"

    def test_none(self):
        name, is_person = clean_contact_name(None)
        assert name == ""
        assert is_person is False

    def test_empty(self):
        name, is_person = clean_contact_name("")
        assert name == ""
        assert is_person is False


# ── Country normalization ────────────────────────────────────────────


class TestNormalizeCountry:
    def test_full_name(self):
        assert normalize_country("United States") == "US"

    def test_usa(self):
        assert normalize_country("USA") == "US"

    def test_already_code(self):
        assert normalize_country("DE") == "DE"

    def test_lowercase_code(self):
        assert normalize_country("gb") == "GB"

    def test_full_name_other(self):
        assert normalize_country("Deutschland") == "DE"

    def test_none(self):
        assert normalize_country(None) is None

    def test_empty(self):
        assert normalize_country("") is None

    def test_unknown_passthrough(self):
        assert normalize_country("Wakanda") == "Wakanda"

    def test_japan(self):
        assert normalize_country("Japan") == "JP"

    def test_hong_kong(self):
        assert normalize_country("Hong Kong") == "HK"


# ── US State normalization ───────────────────────────────────────────


class TestNormalizeUSState:
    def test_full_name(self):
        assert normalize_us_state("California") == "CA"

    def test_already_code(self):
        assert normalize_us_state("TX") == "TX"

    def test_lowercase(self):
        assert normalize_us_state("new york") == "NY"

    def test_none(self):
        assert normalize_us_state(None) is None

    def test_dc(self):
        assert normalize_us_state("District of Columbia") == "DC"

    def test_unknown_passthrough(self):
        assert normalize_us_state("Ontario") == "Ontario"


# ── Encoding fix ─────────────────────────────────────────────────────


class TestFixEncoding:
    def test_intl(self):
        assert fix_encoding("int?l") == "Int'l"

    def test_clean_text(self):
        assert fix_encoding("Normal Text") == "Normal Text"

    def test_none(self):
        assert fix_encoding(None) is None


# ── Vendor name normalization (improved suffixes) ────────────────────


class TestVendorNameNormalization:
    def test_basic(self):
        assert normalize_vendor_name("Mouser Electronics, Inc.") == "mouser electronics"

    def test_european_sas(self):
        result = normalize_vendor_name("CompanyName S.A.S.")
        assert result == "companyname"

    def test_european_srl(self):
        result = normalize_vendor_name("CompanyName S.r.l.")
        assert result == "companyname"

    def test_european_spa(self):
        result = normalize_vendor_name("CompanyName S.p.A.")
        assert result == "companyname"

    def test_european_kk(self):
        result = normalize_vendor_name("CompanyName K.K.")
        assert result == "companyname"

    def test_european_as(self):
        result = normalize_vendor_name("CompanyName A.S.")
        assert result == "companyname"

    def test_polish_sp(self):
        result = normalize_vendor_name("4HFIX Sp.z o.o.")
        assert result == "4hfix"

    def test_no_fragment_strip(self):
        # "co" should NOT be stripped from "technologyco" (word boundary issue)
        result = normalize_vendor_name("TechnologyCo")
        assert "technologyco" in result

    def test_leading_the(self):
        result = normalize_vendor_name("The Phoenix Company LLC")
        assert result == "phoenix"

    def test_empty(self):
        assert normalize_vendor_name("") == ""


# ── MPN normalization ────────────────────────────────────────────────


class TestNormalizeMPN:
    def test_uppercase(self):
        assert normalize_mpn("lm317t") == "LM317T"

    def test_strip_whitespace(self):
        assert normalize_mpn("  LM 317T  ") == "LM317T"

    def test_strip_quotes(self):
        assert normalize_mpn("'LM317T'") == "LM317T"

    def test_too_short(self):
        assert normalize_mpn("AB") is None

    def test_none(self):
        assert normalize_mpn(None) is None


# ── Condition normalization ──────────────────────────────────────────


class TestNormalizeCondition:
    def test_factory_new(self):
        assert normalize_condition("Factory New") == "new"

    def test_refurbished(self):
        assert normalize_condition("Refurbished") == "refurb"

    def test_surplus(self):
        assert normalize_condition("Surplus") == "used"

    def test_none(self):
        assert normalize_condition(None) is None


# ── Packaging normalization ──────────────────────────────────────────


class TestNormalizePackaging:
    def test_tape_and_reel(self):
        assert normalize_packaging("Tape and Reel") == "reel"

    def test_tray(self):
        assert normalize_packaging("Tray") == "tray"

    def test_cut_tape(self):
        assert normalize_packaging("Cut Tape") == "cut_tape"

    def test_none(self):
        assert normalize_packaging(None) is None


# ── Schema validators (write-path hooks) ─────────────────────────────


class TestSchemaValidators:
    def test_requirement_create_normalizes_mpn(self):
        from app.schemas.requisitions import RequirementCreate
        r = RequirementCreate(primary_mpn="lm317t", target_qty=100)
        assert r.primary_mpn == "LM317T"

    def test_requirement_create_normalizes_substitutes(self):
        from app.schemas.requisitions import RequirementCreate
        r = RequirementCreate(primary_mpn="LM317T", substitutes=["ne555p", "lm7805"])
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

    def test_site_create_normalizes_country(self):
        from app.schemas.crm import SiteCreate
        s = SiteCreate(site_name="HQ", country="United States")
        assert s.country == "US"

    def test_site_create_normalizes_state(self):
        from app.schemas.crm import SiteCreate
        s = SiteCreate(site_name="HQ", state="California")
        assert s.state == "CA"

    def test_offer_create_normalizes_mpn(self):
        from app.schemas.crm import OfferCreate
        o = OfferCreate(mpn="lm317t", vendor_name="Arrow")
        assert o.mpn == "LM317T"

    def test_site_contact_normalizes_phone(self):
        from app.schemas.crm import SiteContactCreate
        c = SiteContactCreate(full_name="John Doe", phone="555-123-4567")
        assert c.phone == "+15551234567"

    def test_none_fields_pass_through(self):
        from app.schemas.crm import CompanyUpdate
        c = CompanyUpdate(hq_country=None, hq_state=None, phone=None)
        assert c.hq_country is None
        assert c.hq_state is None
        assert c.phone is None
