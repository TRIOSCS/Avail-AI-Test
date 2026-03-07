"""Request/response schemas for the Knowledge Ledger API.

Called by: routers/knowledge.py
Depends on: nothing
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class KnowledgeEntryCreate(BaseModel):
    entry_type: str = Field(..., pattern="^(question|answer|fact|note|ai_insight)$")
    content: str = Field(..., min_length=1, max_length=10000)
    source: str = Field(default="manual", pattern="^(manual|ai_generated|system|email_parsed|teams_bot)$")
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    expires_at: datetime | None = None
    mpn: str | None = None
    vendor_card_id: int | None = None
    company_id: int | None = None
    requisition_id: int | None = None
    requirement_id: int | None = None


class QuestionCreate(BaseModel):
    content: str = Field(..., min_length=1, max_length=10000)
    assigned_to_ids: list[int] = Field(..., min_length=1)
    mpn: str | None = None
    vendor_card_id: int | None = None
    company_id: int | None = None
    requisition_id: int | None = None
    requirement_id: int | None = None


class AnswerCreate(BaseModel):
    content: str = Field(..., min_length=1, max_length=10000)


class KnowledgeEntryUpdate(BaseModel):
    content: str | None = Field(default=None, min_length=1, max_length=10000)
    is_resolved: bool | None = None
    expires_at: datetime | None = None


class KnowledgeEntryResponse(BaseModel, extra="allow"):
    id: int
    entry_type: str
    content: str
    source: str
    confidence: float | None = None
    expires_at: datetime | None = None
    is_expired: bool = False
    is_resolved: bool = False
    parent_id: int | None = None
    assigned_to_ids: list[int] = []
    created_by: int | None = None
    creator_name: str | None = None
    mpn: str | None = None
    vendor_card_id: int | None = None
    company_id: int | None = None
    requisition_id: int | None = None
    requirement_id: int | None = None
    created_at: datetime
    updated_at: datetime
    answers: list[KnowledgeEntryResponse] = []


class InsightsResponse(BaseModel, extra="allow"):
    requisition_id: int
    insights: list[KnowledgeEntryResponse] = []
    generated_at: datetime | None = None
    has_expired: bool = False
