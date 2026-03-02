"""Pydantic schemas for trouble ticket request/response validation.

Called by: routers/trouble_tickets.py
Depends on: nothing (pure validation)
"""

from datetime import datetime

from pydantic import BaseModel, Field, field_validator


class TroubleTicketCreate(BaseModel):
    title: str = Field(..., max_length=200)
    description: str
    current_page: str | None = None
    frontend_errors: list[dict] | None = None

    @field_validator("title")
    @classmethod
    def title_not_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Title is required")
        return v

    @field_validator("description")
    @classmethod
    def description_not_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Description is required")
        return v


class TroubleTicketUpdate(BaseModel):
    status: str | None = None
    resolution_notes: str | None = None
    risk_tier: str | None = None
    category: str | None = None


class TroubleTicketResponse(BaseModel, extra="allow"):
    id: int
    ticket_number: str
    submitted_by: int | None = None
    submitted_by_name: str | None = None
    status: str
    risk_tier: str | None = None
    category: str | None = None
    title: str
    description: str
    current_page: str | None = None
    auto_captured_context: dict | None = None
    sanitized_context: dict | None = None
    diagnosis: dict | None = None
    generated_prompt: str | None = None
    file_mapping: list | None = None
    fix_branch: str | None = None
    fix_pr_url: str | None = None
    iterations_used: int | None = None
    cost_tokens: int | None = None
    cost_usd: float | None = None
    resolution_notes: str | None = None
    parent_ticket_id: int | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    diagnosed_at: datetime | None = None
    resolved_at: datetime | None = None
