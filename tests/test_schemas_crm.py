"""Tests for app/schemas/crm.py — CRM Pydantic validation models."""

import pytest
from pydantic import ValidationError

from app.schemas.crm import (
    AddContactsToVendor,
    AddContactToSite,
    CompanyCreate,
    CompanyUpdate,
    EnrichDomainRequest,
    OfferCreate,
    QuoteReopen,
    QuoteResult,
    SiteCreate,
    SiteUpdate,
    SuggestedContactItem,
    SuggestedSiteContact,
)


# ── Company schemas ──────────────────────────────────────────────────


class TestCompanyCreate:
    def test_valid_minimal(self) -> None:
        c = CompanyCreate(name="Acme Corp")
        assert c.name == "Acme Corp"
        assert c.website is None

    def test_strips_whitespace(self) -> None:
        c = CompanyCreate(name="  Acme Corp  ")
        assert c.name == "Acme Corp"

    def test_blank_name_raises(self) -> None:
        with pytest.raises(ValidationError):
            CompanyCreate(name="   ")

    def test_missing_name_raises(self) -> None:
        with pytest.raises(ValidationError):
            CompanyCreate()  # type: ignore[call-arg]


class TestCompanyUpdate:
    def test_all_optional(self) -> None:
        u = CompanyUpdate()
        assert u.name is None

    def test_exclude_unset(self) -> None:
        u = CompanyUpdate(name="New Name")
        dumped = u.model_dump(exclude_unset=True)
        assert dumped == {"name": "New Name"}
        assert "website" not in dumped


# ── Site schemas ─────────────────────────────────────────────────────


class TestSiteCreate:
    def test_valid(self) -> None:
        s = SiteCreate(site_name="HQ")
        assert s.site_name == "HQ"
        assert s.country == "US"

    def test_blank_name_raises(self) -> None:
        with pytest.raises(ValidationError):
            SiteCreate(site_name="")


class TestSiteUpdate:
    def test_all_optional(self) -> None:
        u = SiteUpdate()
        assert u.site_name is None
        assert u.is_active is None

    def test_exclude_unset(self) -> None:
        u = SiteUpdate(city="Austin", is_active=False)
        dumped = u.model_dump(exclude_unset=True)
        assert dumped == {"city": "Austin", "is_active": False}


# ── Offer schemas ────────────────────────────────────────────────────


class TestOfferCreate:
    def test_valid(self) -> None:
        o = OfferCreate(mpn="LM317T", vendor_name="Arrow")
        assert o.condition == "new"
        assert o.status == "active"

    def test_blank_mpn_raises(self) -> None:
        with pytest.raises(ValidationError):
            OfferCreate(mpn="  ", vendor_name="Arrow")

    def test_blank_vendor_raises(self) -> None:
        with pytest.raises(ValidationError):
            OfferCreate(mpn="LM317T", vendor_name="  ")


# ── Quote schemas ────────────────────────────────────────────────────


class TestQuoteResult:
    def test_won(self) -> None:
        r = QuoteResult(result="won")
        assert r.result == "won"
        assert r.reason is None

    def test_lost_with_reason(self) -> None:
        r = QuoteResult(result="lost", reason="Price too high")
        assert r.reason == "Price too high"

    def test_invalid_result_raises(self) -> None:
        with pytest.raises(ValidationError):
            QuoteResult(result="pending")  # type: ignore[arg-type]


class TestQuoteReopen:
    def test_defaults_to_no_revise(self) -> None:
        r = QuoteReopen()
        assert r.revise is False

    def test_revise_true(self) -> None:
        r = QuoteReopen(revise=True)
        assert r.revise is True


# ── Enrichment schemas ───────────────────────────────────────────────


class TestEnrichDomainRequest:
    def test_empty(self) -> None:
        e = EnrichDomainRequest()
        assert e.domain is None

    def test_with_domain(self) -> None:
        e = EnrichDomainRequest(domain="acme.com")
        assert e.domain == "acme.com"


# ── Suggested Contacts schemas ───────────────────────────────────────


class TestSuggestedContactItem:
    def test_valid(self) -> None:
        c = SuggestedContactItem(email="john@acme.com", full_name="John Doe")
        assert c.email == "john@acme.com"
        assert c.source == "enrichment"
        assert c.label == "Sales"

    def test_email_lowercased_and_stripped(self) -> None:
        c = SuggestedContactItem(email="  JOHN@ACME.COM  ")
        assert c.email == "john@acme.com"

    def test_blank_email_raises(self) -> None:
        with pytest.raises(ValidationError):
            SuggestedContactItem(email="  ")


class TestAddContactsToVendor:
    def test_valid(self) -> None:
        payload = AddContactsToVendor(
            vendor_card_id=5,
            contacts=[SuggestedContactItem(email="a@b.com")],
        )
        assert len(payload.contacts) == 1

    def test_missing_vendor_card_id_raises(self) -> None:
        with pytest.raises(ValidationError):
            AddContactsToVendor(contacts=[])  # type: ignore[call-arg]


class TestAddContactToSite:
    def test_valid(self) -> None:
        payload = AddContactToSite(
            site_id=3,
            contact=SuggestedSiteContact(full_name="Jane", email="j@co.com"),
        )
        assert payload.contact.full_name == "Jane"
