"""
schemas/crm.py — Pydantic models for CRM endpoints

Validates Companies, CustomerSites, Offers, and Quotes.

Business Rules:
- Company name is required and non-empty
- Site name is required and non-empty
- Offers require mpn + vendor_name
- Offer status must be one of: active, expired, won, lost

Called by: routers/crm.py
Depends on: pydantic
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, field_validator


# ── Companies ────────────────────────────────────────────────────────


class CompanyCreate(BaseModel):
    name: str
    website: str | None = None
    industry: str | None = None
    notes: str | None = None
    domain: str | None = None
    linkedin_url: str | None = None

    @field_validator("name")
    @classmethod
    def name_not_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Company name is required")
        return v


class CompanyUpdate(BaseModel):
    name: str | None = None
    website: str | None = None
    industry: str | None = None
    notes: str | None = None
    is_active: bool | None = None
    domain: str | None = None
    linkedin_url: str | None = None
    legal_name: str | None = None
    employee_size: str | None = None
    hq_city: str | None = None
    hq_state: str | None = None
    hq_country: str | None = None


class CompanyOut(BaseModel):
    id: int
    name: str


# ── Customer Sites ───────────────────────────────────────────────────


class SiteCreate(BaseModel):
    site_name: str
    owner_id: int | None = None
    contact_name: str | None = None
    contact_email: str | None = None
    contact_phone: str | None = None
    contact_title: str | None = None
    contact_linkedin: str | None = None
    address_line1: str | None = None
    address_line2: str | None = None
    city: str | None = None
    state: str | None = None
    zip: str | None = None
    country: str = "US"
    payment_terms: str | None = None
    shipping_terms: str | None = None
    notes: str | None = None

    @field_validator("site_name")
    @classmethod
    def site_name_not_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Site name is required")
        return v


class SiteUpdate(BaseModel):
    site_name: str | None = None
    owner_id: int | None = None
    contact_name: str | None = None
    contact_email: str | None = None
    contact_phone: str | None = None
    contact_title: str | None = None
    contact_linkedin: str | None = None
    address_line1: str | None = None
    address_line2: str | None = None
    city: str | None = None
    state: str | None = None
    zip: str | None = None
    country: str | None = None
    payment_terms: str | None = None
    shipping_terms: str | None = None
    notes: str | None = None
    is_active: bool | None = None


class SiteOut(BaseModel):
    id: int
    site_name: str


# ── Offers ───────────────────────────────────────────────────────────


class OfferCreate(BaseModel):
    mpn: str
    vendor_name: str
    requirement_id: int | None = None
    manufacturer: str | None = None
    qty_available: int | None = None
    unit_price: float | None = None
    lead_time: str | None = None
    date_code: str | None = None
    condition: str = "New"
    packaging: str | None = None
    moq: int | None = None
    source: str = "manual"
    vendor_response_id: int | None = None
    notes: str | None = None
    status: Literal["active", "expired", "won", "lost"] = "active"

    @field_validator("mpn", "vendor_name")
    @classmethod
    def not_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Field must not be blank")
        return v


class OfferUpdate(BaseModel):
    vendor_name: str | None = None
    mpn: str | None = None
    manufacturer: str | None = None
    qty_available: int | None = None
    unit_price: float | None = None
    lead_time: str | None = None
    date_code: str | None = None
    condition: str | None = None
    packaging: str | None = None
    moq: int | None = None
    notes: str | None = None
    status: Literal["active", "expired", "won", "lost"] | None = None


class OfferOut(BaseModel):
    id: int
    vendor_name: str
    mpn: str


# ── Quotes ───────────────────────────────────────────────────────────


class QuoteCreate(BaseModel):
    offer_ids: list[int] = []
    line_items: list[dict] = []


class QuoteUpdate(BaseModel):
    line_items: list[dict] | None = None
    payment_terms: str | None = None
    shipping_terms: str | None = None
    notes: str | None = None
    valid_until: str | None = None
    validity_days: int | None = None


class QuoteResult(BaseModel):
    result: Literal["won", "lost"]
    reason: str | None = None
    notes: str | None = None


class QuoteReopen(BaseModel):
    revise: bool = False


# ── Enrichment ───────────────────────────────────────────────────────


class EnrichDomainRequest(BaseModel):
    domain: str | None = None


# ── Suggested Contacts ───────────────────────────────────────────────


class SuggestedContactItem(BaseModel):
    email: str
    full_name: str | None = None
    title: str | None = None
    phone: str | None = None
    linkedin_url: str | None = None
    source: str = "enrichment"
    label: str = "Sales"

    @field_validator("email")
    @classmethod
    def email_not_blank(cls, v: str) -> str:
        v = v.strip().lower()
        if not v:
            raise ValueError("Contact email is required")
        return v


class AddContactsToVendor(BaseModel):
    vendor_card_id: int
    contacts: list[SuggestedContactItem]


class SuggestedSiteContact(BaseModel):
    full_name: str | None = None
    email: str | None = None
    phone: str | None = None
    title: str | None = None
    linkedin_url: str | None = None


class AddContactToSite(BaseModel):
    site_id: int
    contact: SuggestedSiteContact


# ── Customer Import ──────────────────────────────────────────────────


class CustomerImportRow(BaseModel):
    company_name: str
    site_name: str = "HQ"
    owner_email: str | None = None
    contact_name: str | None = None
    contact_email: str | None = None
    contact_phone: str | None = None
    contact_title: str | None = None
    payment_terms: str | None = None
    shipping_terms: str | None = None
    city: str | None = None
    state: str | None = None
    zip: str | None = None
    country: str | None = None
    notes: str | None = None
    address: str | None = None

    @field_validator("company_name")
    @classmethod
    def company_name_not_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("company_name is required")
        return v
