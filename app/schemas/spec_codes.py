"""Pydantic schemas for the OEM spec code resolver.

Used by: app/services/spec_code_resolver.py (LLM response validation),
         app/routers/admin/spec_codes.py (admin action payloads)
Depends on: pydantic v2
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class AvlEntry(BaseModel):
    """One row of an Approved Vendor List — a single MPN with rank and notes."""

    model_config = ConfigDict(extra="forbid")

    mpn: str = Field(..., min_length=1, max_length=255)
    manufacturer: str = Field(..., min_length=1, max_length=255)
    rank: int = Field(..., ge=1)
    notes: str | None = Field(default=None, max_length=1000)


class ResolverLlmResponse(BaseModel):
    """Strict schema for what the LLM must return when resolving a spec code."""

    model_config = ConfigDict(extra="forbid")

    avl: list[AvlEntry]
    confidence: float = Field(..., ge=0.0, le=1.0)
    citations: list[dict] = Field(default_factory=list)
    reasoning: str = Field(default="")


class ApproveActionBody(BaseModel):
    """Admin approve-pending action; edited_avl=None means approve as-is."""

    model_config = ConfigDict(extra="forbid")

    edited_avl: list[AvlEntry] | None = None


class RejectActionBody(BaseModel):
    """Admin reject-pending action; empty rejected_mpns means reject all proposed."""

    model_config = ConfigDict(extra="forbid")

    reason: str = Field(..., min_length=1, max_length=1000)
    rejected_mpns: list[str] = Field(default_factory=list)


class ReResolveActionBody(BaseModel):
    """Admin re-resolve action; no body fields — presence of the POST is enough."""

    model_config = ConfigDict(extra="forbid")


ResolverStatus = Literal["approved", "pending", "unresolved"]
ResolverSource = Literal["table", "llm", "none"]
