"""schemas/requisitions.py — Pydantic models for Requisition & Requirement endpoints.

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

from datetime import date, datetime

from pydantic import BaseModel, Field, field_validator

from app.utils.normalization import normalize_condition, normalize_mpn, normalize_packaging


def _validate_deadline(v: str | None) -> str | None:
    """Validate that a deadline string is a real calendar date (rejects e.g.
    2025-02-29)."""
    if v is None or v.strip() == "":
        return None
    v = v.strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%Y-%m-%dT%H:%M:%S"):
        try:
            datetime.strptime(v, fmt)
            return v
        except ValueError:
            continue
    raise ValueError(f"Invalid date: '{v}'. Use YYYY-MM-DD format and ensure the date is valid.")


# ── Batch Operations ─────────────────────────────────────────────────


class BatchArchiveByIds(BaseModel):
    """Archive specific requisitions by ID list."""

    ids: list[int] = Field(..., min_length=1, max_length=200)


class BatchAssign(BaseModel):
    """Assign an owner to specific requisitions."""

    ids: list[int] = Field(..., min_length=1, max_length=200)
    owner_id: int


# ── Requisitions ─────────────────────────────────────────────────────


class RequisitionCreate(BaseModel):
    name: str = "Untitled"
    customer_name: str | None = None
    customer_site_id: int | None = None
    deadline: str | None = None

    @field_validator("deadline")
    @classmethod
    def validate_deadline(cls, v: str | None) -> str | None:
        return _validate_deadline(v)


class RequisitionOut(BaseModel):
    id: int
    name: str


class RequisitionUpdate(BaseModel):
    name: str | None = None
    customer_site_id: int | None = None
    deadline: str | None = None
    urgency: str | None = None
    opportunity_value: float | None = None

    @field_validator("deadline")
    @classmethod
    def validate_deadline(cls, v: str | None) -> str | None:
        return _validate_deadline(v)


# ── Requirements ─────────────────────────────────────────────────────


class RequirementCreate(BaseModel):
    primary_mpn: str
    manufacturer: str
    target_qty: int = Field(default=1, ge=1)
    target_price: float | None = Field(default=None, ge=0)
    brand: str | None = None
    substitutes: list[str] = Field(default_factory=list, max_length=20)
    condition: str | None = None
    date_codes: str | None = None
    firmware: str | None = None
    hardware_codes: str | None = None
    packaging: str | None = None
    description: str | None = None
    package_type: str | None = None
    revision: str | None = None
    customer_pn: str | None = None
    need_by_date: date | None = None
    notes: str | None = None

    @field_validator("manufacturer")
    @classmethod
    def manufacturer_not_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("manufacturer must not be blank")
        return v

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


class RequirementUpdate(BaseModel):
    primary_mpn: str | None = None
    manufacturer: str | None = None
    target_qty: int | None = Field(default=None, ge=1)
    target_price: float | None = Field(default=None, ge=0)
    brand: str | None = None
    substitutes: list[str] | None = None
    firmware: str | None = None
    date_codes: str | None = None
    hardware_codes: str | None = None
    packaging: str | None = None
    condition: str | None = None
    description: str | None = None
    package_type: str | None = None
    revision: str | None = None
    customer_pn: str | None = None
    need_by_date: date | None = None
    notes: str | None = None
    sale_notes: str | None = None

    @field_validator("primary_mpn")
    @classmethod
    def normalize_primary_mpn(cls, v: str | None) -> str | None:
        if v is None:
            return v
        v = v.strip()
        if not v:
            raise ValueError("primary_mpn must not be blank")
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


class SightingUnavailableIn(BaseModel):
    unavailable: bool = True


class RequisitionOutcome(BaseModel):
    """Mark a requisition as won or lost."""

    outcome: str = Field(..., description="Must be 'won' or 'lost'")

    @field_validator("outcome")
    @classmethod
    def validate_outcome(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in ("won", "lost"):
            raise ValueError("outcome must be 'won' or 'lost'")
        return v


class RequirementNoteAdd(BaseModel):
    """Append a note to a requirement."""

    text: str = Field(..., min_length=1, max_length=2000)

    @field_validator("text")
    @classmethod
    def strip_text(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Note text is required")
        return v


class RequirementTaskCreate(BaseModel):
    """Create a task linked to a requirement."""

    title: str = Field(..., min_length=1, max_length=255)
    assigned_to_id: int | None = None
    due_at: datetime | None = None


class SearchOptions(BaseModel):
    requirement_ids: list[int] | None = None
