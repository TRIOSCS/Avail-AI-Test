"""
schemas/requisitions.py — Pydantic models for Requisition & Requirement endpoints

Validates request bodies and documents response shapes for OpenAPI.

Business Rules:
- Requisition name defaults to "Untitled" if not provided
- Requirements need a non-empty primary_mpn
- Substitutes list capped at 20 items
- target_qty defaults to 1

Called by: routers/requisitions.py
Depends on: pydantic
"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

from app.utils.normalization import normalize_condition, normalize_mpn, normalize_packaging


# ── Requisitions ─────────────────────────────────────────────────────


class RequisitionCreate(BaseModel):
    name: str = "Untitled"
    customer_name: str | None = None
    customer_site_id: int | None = None
    deadline: str | None = None


class RequisitionOut(BaseModel):
    id: int
    name: str


class RequisitionUpdate(BaseModel):
    name: str | None = None
    customer_site_id: int | None = None
    deadline: str | None = None


class RequisitionArchiveOut(BaseModel):
    ok: bool = True
    status: str


# ── Requirements ─────────────────────────────────────────────────────


class RequirementCreate(BaseModel):
    primary_mpn: str
    target_qty: int = Field(default=1, ge=1)
    target_price: float | None = None
    substitutes: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("primary_mpn")
    @classmethod
    def mpn_not_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("primary_mpn must not be blank")
        return normalize_mpn(v) or v

    @field_validator("substitutes", mode="before")
    @classmethod
    def parse_substitutes(cls, v):
        if isinstance(v, str):
            v = [s.strip() for s in v.replace("\n", ",").split(",") if s.strip()]
        if isinstance(v, list):
            return [normalize_mpn(s) or s for s in v if s]
        return v


class RequirementUpdate(BaseModel):
    primary_mpn: str | None = None
    target_qty: int | None = None
    target_price: float | None = None
    substitutes: list[str] | None = None
    firmware: str | None = None
    date_codes: str | None = None
    hardware_codes: str | None = None
    packaging: str | None = None
    condition: str | None = None
    notes: str | None = None

    @field_validator("primary_mpn")
    @classmethod
    def normalize_primary_mpn(cls, v: str | None) -> str | None:
        if v is None:
            return v
        return normalize_mpn(v) or v

    @field_validator("substitutes", mode="before")
    @classmethod
    def normalize_substitutes(cls, v):
        if isinstance(v, list):
            return [normalize_mpn(s) or s for s in v if s]
        return v

    @field_validator("condition")
    @classmethod
    def normalize_condition_field(cls, v: str | None) -> str | None:
        if v is None:
            return v
        return normalize_condition(v) or v

    @field_validator("packaging")
    @classmethod
    def normalize_packaging_field(cls, v: str | None) -> str | None:
        if v is None:
            return v
        return normalize_packaging(v) or v


class RequirementOut(BaseModel):
    id: int
    primary_mpn: str
    target_qty: int = 1
    target_price: float | None = None
    substitutes: list[str] = Field(default_factory=list)
    sighting_count: int = 0


class SightingUnavailableIn(BaseModel):
    unavailable: bool = True
