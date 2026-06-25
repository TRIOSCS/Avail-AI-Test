"""schemas/vendors.py — Pydantic models for Vendor & Material endpoints.

Validates VendorCard updates, reviews, contacts, and MaterialCard edits.

Business Rules:
- Emails must contain @ and be lowercased
- Review ratings clamped 1-5
- Vendor name is required for lookups
- Comments max 500 chars

Called by: routers/vendors.py
Depends on: pydantic
"""

from __future__ import annotations

from collections.abc import Callable

from pydantic import BaseModel, ConfigDict, field_validator


def _require_vendor_name(v: str) -> str:
    """Strip and reject a blank vendor_name."""
    v = v.strip()
    if not v:
        raise ValueError("vendor_name required")
    return v


def _dedupe_cleaned(v: list[str] | None, cleaner: Callable[[str], str | None]) -> list[str] | None:
    """Apply ``cleaner`` to each item, dropping ``None`` results, preserving order, and
    de-duplicating.

    Passes ``None`` through unchanged.
    """
    if v is None:
        return None
    seen: set[str] = set()
    result: list[str] = []
    for item in v:
        cleaned = cleaner(item)
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            result.append(cleaned)
    return result


class VendorCardUpdate(BaseModel):
    emails: list[str] | None = None
    phones: list[str] | None = None
    website: str | None = None
    display_name: str | None = None
    is_blacklisted: bool | None = None

    @field_validator("emails", mode="before")
    @classmethod
    def clean_emails(cls, v: list[str] | None) -> list[str] | None:
        def clean(e: str) -> str | None:
            if not e or "@" not in str(e):
                return None
            return str(e).strip().lower()

        return _dedupe_cleaned(v, clean)

    @field_validator("phones", mode="before")
    @classmethod
    def clean_phones(cls, v: list[str] | None) -> list[str] | None:
        return _dedupe_cleaned(v, lambda p: str(p).strip() if p and str(p).strip() else None)


class VendorBlacklistToggle(BaseModel):
    blacklisted: bool | None = None  # None = flip current


class VendorReviewCreate(BaseModel):
    rating: int = 3
    comment: str = ""

    @field_validator("rating")
    @classmethod
    def clamp_rating(cls, v: int) -> int:
        return max(1, min(5, v))

    @field_validator("comment")
    @classmethod
    def truncate_comment(cls, v: str) -> str:
        return (v or "")[:500]


class VendorContactLookup(BaseModel):
    vendor_name: str

    @field_validator("vendor_name")
    @classmethod
    def name_required(cls, v: str) -> str:
        return _require_vendor_name(v)


class VendorContactCreate(BaseModel):
    email: str
    full_name: str | None = None
    title: str | None = None
    label: str = "Sales"
    phone: str | None = None

    @field_validator("email")
    @classmethod
    def clean_email(cls, v: str) -> str:
        v = v.strip().lower()
        if not v:
            raise ValueError("Email is required")
        if "@" not in v:
            raise ValueError("Invalid email address")
        return v


class VendorContactUpdate(BaseModel):
    full_name: str | None = None
    title: str | None = None
    email: str | None = None
    label: str | None = None
    phone: str | None = None

    @field_validator("email", mode="before")
    @classmethod
    def clean_email(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip().lower()
        return v or None


class VendorEmailAdd(BaseModel):
    vendor_name: str
    email: str

    @field_validator("vendor_name")
    @classmethod
    def name_required(cls, v: str) -> str:
        return _require_vendor_name(v)

    @field_validator("email")
    @classmethod
    def email_valid(cls, v: str) -> str:
        v = v.strip().lower()
        if not v or "@" not in v:
            raise ValueError("valid email required")
        return v


class VendorCardCreate(BaseModel):
    """Schema for creating a new VendorCard via POST /api/vendors."""

    model_config = ConfigDict(str_strip_whitespace=True)

    display_name: str
    website: str | None = None
    emails: list[str] | None = None
    phones: list[str] | None = None
    industry: str | None = None
    hq_city: str | None = None
    hq_country: str | None = None
    employee_size: str | None = None

    @field_validator("display_name")
    @classmethod
    def name_required(cls, v: str) -> str:
        return _require_vendor_name(v)

    @field_validator("emails", mode="before")
    @classmethod
    def clean_emails(cls, v: list[str] | None) -> list[str] | None:
        def clean(e: str) -> str | None:
            if not e or "@" not in str(e):
                return None
            return str(e).strip().lower()

        return _dedupe_cleaned(v, clean)

    @field_validator("phones", mode="before")
    @classmethod
    def clean_phones(cls, v: list[str] | None) -> list[str] | None:
        return _dedupe_cleaned(v, lambda p: str(p).strip() if p and str(p).strip() else None)


class MaterialCardUpdate(BaseModel):
    manufacturer: str | None = None
    description: str | None = None
    display_mpn: str | None = None
    lifecycle_status: str | None = None
    package_type: str | None = None
    category: str | None = None
    rohs_status: str | None = None
    pin_count: int | None = None
    datasheet_url: str | None = None
    cross_references: list[dict] | None = None
    specs_summary: str | None = None
