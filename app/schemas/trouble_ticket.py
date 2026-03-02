"""Pydantic schemas for trouble ticket request/response validation.

Supports both sources:
- ticket_form: structured submission from sidebar Tickets view
- report_button: quick bug report (formerly ErrorReport)

Called by: routers/trouble_tickets.py, routers/error_reports.py
Depends on: nothing (pure validation)
"""

from datetime import datetime

from pydantic import BaseModel, Field, field_validator, model_validator


class TroubleTicketCreate(BaseModel):
    title: str | None = Field(None, max_length=200)
    description: str | None = None
    message: str | None = Field(None, max_length=5000)  # alias used by report button
    current_page: str | None = None
    frontend_errors: list[dict] | None = None
    source: str | None = None  # 'report_button' | 'ticket_form'

    # Error-report fields (from report button)
    screenshot_b64: str | None = None
    browser_info: str | None = None
    screen_size: str | None = None
    console_errors: str | None = None
    page_state: str | None = None
    current_view: str | None = None
    current_url: str | None = None  # alias for current_page used by report button

    @model_validator(mode="after")
    def require_title_or_message(self):
        """Either title or message must be provided."""
        title = (self.title or "").strip()
        message = (self.message or "").strip()
        desc = (self.description or "").strip()
        if not title and not message and not desc:
            raise ValueError("Either title or message is required")
        # Normalize: if message provided but not title, derive title
        if not title and message:
            self.title = message[:200]
        # Normalize: if message provided but not description, use message
        if not desc and message:
            self.description = message
        # If title but no description, use title as description
        if not (self.description or "").strip() and title:
            self.description = title
        # If current_url provided but not current_page, use it
        if self.current_url and not self.current_page:
            self.current_page = self.current_url
        return self


class TroubleTicketUpdate(BaseModel):
    status: str | None = None
    resolution_notes: str | None = None
    risk_tier: str | None = None
    category: str | None = None
    admin_notes: str | None = None


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
    # Unified fields
    source: str | None = None
    has_screenshot: bool = False
    has_ai_prompt: bool = False
    screenshot_b64: str | None = None
    ai_prompt: str | None = None
    admin_notes: str | None = None
    browser_info: str | None = None
    screen_size: str | None = None
    console_errors: str | None = None
    current_view: str | None = None
