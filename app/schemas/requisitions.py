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


# ── Requisitions ─────────────────────────────────────────────────────


class RequisitionCreate(BaseModel):
    name: str = "Untitled"
    customer_name: str | None = None
    customer_site_id: int | None = None


class RequisitionOut(BaseModel):
    id: int
    name: str


class RequisitionUpdate(BaseModel):
    name: str | None = None
    customer_site_id: int | None = None


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
        return v

    @field_validator("substitutes", mode="before")
    @classmethod
    def parse_substitutes(cls, v):
        if isinstance(v, str):
            return [s.strip() for s in v.replace("\n", ",").split(",") if s.strip()]
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


class RequirementOut(BaseModel):
    id: int
    primary_mpn: str
    target_qty: int = 1
    target_price: float | None = None
    substitutes: list[str] = Field(default_factory=list)
    sighting_count: int = 0


class SightingUnavailableIn(BaseModel):
    unavailable: bool = True
