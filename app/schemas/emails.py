"""
schemas/emails.py — Pydantic models for email thread endpoints

Request/response models for surfacing vendor email threads on
requirement and vendor detail views.

Business Rules:
- EmailThreadSummary represents a conversation thread with metadata
- EmailMessage represents a single message within a thread
- direction is "sent" (from TRIOSCS) or "received" (from vendor)

Called by: routers/emails.py
Depends on: pydantic
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class EmailThreadSummary(BaseModel):
    conversation_id: str
    subject: str
    participants: list[str] = []
    message_count: int = 0
    last_message_date: datetime | None = None
    snippet: str = ""
    needs_response: bool = False
    matched_via: str = ""  # conversation_id, subject_token, part_number, vendor_domain


class EmailMessage(BaseModel):
    id: str
    from_name: str = ""
    from_email: str = ""
    to: list[str] = []
    subject: str = ""
    body_preview: str = ""
    received_date: datetime | None = None
    direction: str = ""  # "sent" or "received"


class EmailThreadListResponse(BaseModel):
    threads: list[EmailThreadSummary] = []
    error: str | None = None


class EmailThreadMessagesResponse(BaseModel):
    messages: list[EmailMessage] = []
    error: str | None = None


class EmailReplyRequest(BaseModel):
    conversation_id: str
    to: str
    subject: str
    body: str
