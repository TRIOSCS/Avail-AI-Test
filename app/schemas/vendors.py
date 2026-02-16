"""
schemas/vendors.py — Pydantic models for Vendor & Material endpoints

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

from pydantic import BaseModel, field_validator


class VendorCardUpdate(BaseModel):
    emails: list[str] | None = None
    phones: list[str] | None = None
    website: str | None = None
    display_name: str | None = None
    is_blacklisted: bool | None = None

    @field_validator("emails", mode="before")
    @classmethod
    def clean_emails(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return None
        seen: set[str] = set()
        result: list[str] = []
        for e in v:
            if not e or "@" not in str(e):
                continue
            cleaned = str(e).strip().lower()
            if cleaned not in seen:
                seen.add(cleaned)
                result.append(cleaned)
        return result

    @field_validator("phones", mode="before")
    @classmethod
    def clean_phones(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return None
        seen: set[str] = set()
        result: list[str] = []
        for p in v:
            if not p or not str(p).strip():
                continue
            cleaned = str(p).strip()
            if cleaned not in seen:
                seen.add(cleaned)
                result.append(cleaned)
        return result


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
        v = v.strip()
        if not v:
            raise ValueError("vendor_name required")
        return v


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
        v = v.strip()
        if not v:
            raise ValueError("vendor_name required")
        return v

    @field_validator("email")
    @classmethod
    def email_valid(cls, v: str) -> str:
        v = v.strip().lower()
        if not v or "@" not in v:
            raise ValueError("valid email required")
        return v


class MaterialCardUpdate(BaseModel):
    manufacturer: str | None = None
    description: str | None = None
    display_mpn: str | None = None
