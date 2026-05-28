"""Pydantic schemas for the OEM spec code resolver.

Used by: app/services/spec_code_resolver.py (LLM response validation),
         app/routers/admin/spec_codes.py (admin action payloads)
Depends on: pydantic v2
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class AvlEntry(BaseModel):
    """One row of an Approved Vendor List — a single MPN with rank and notes."""

    model_config = ConfigDict(extra="forbid")

    mpn: str = Field(..., min_length=1, max_length=255)
    manufacturer: str = Field(..., min_length=1, max_length=255)
    rank: int = Field(..., ge=1)
    notes: str | None = Field(default=None, max_length=1000)


class Citation(BaseModel):
    """A web citation backing a spec-code resolution claim.

    URL scheme is constrained to http/https to prevent javascript:, data:, vbscript:,
    file:,
    and other dangerous schemes from leaking
    into the persistence layer where they could be rendered as href
    attributes (XSS surface).
    """

    model_config = ConfigDict(extra="forbid")

    url: str = Field(..., min_length=1, max_length=2000)
    snippet: str = Field(default="", max_length=2000)

    @field_validator("url")
    @classmethod
    def _validate_http_scheme(cls, v: str) -> str:
        if not (v.startswith("http://") or v.startswith("https://")):
            raise ValueError("citation url must use http:// or https:// scheme")
        return v


class ResolverLlmResponse(BaseModel):
    """Strict schema for what the LLM must return when resolving a spec code."""

    model_config = ConfigDict(extra="forbid")

    avl: list[AvlEntry]
    confidence: float = Field(..., ge=0.0, le=1.0)
    citations: list[Citation] = Field(default_factory=list)
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


ResolverStatus = Literal["approved", "pending", "unresolved"]
ResolverSource = Literal["table", "llm", "none"]
