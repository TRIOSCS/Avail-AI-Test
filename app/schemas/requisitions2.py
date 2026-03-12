"""Pydantic models for the Requisitions 2 HTMX page.

Handles query/filter parsing, pagination, and action validation
for the server-rendered requisitions list.

Called by: app/routers/requisitions2.py
Depends on: pydantic
"""

from datetime import date
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, field_validator


class ReqStatus(str, Enum):
    all = "all"
    active = "active"
    draft = "draft"
    sourcing = "sourcing"
    archived = "archived"
    won = "won"
    lost = "lost"
    closed = "closed"


class Urgency(str, Enum):
    normal = "normal"
    hot = "hot"
    critical = "critical"


class SortColumn(str, Enum):
    name = "name"
    status = "status"
    created_at = "created_at"
    deadline = "deadline"
    updated_at = "updated_at"
    customer_name = "customer_name"


class SortOrder(str, Enum):
    asc = "asc"
    desc = "desc"


class ReqListFilters(BaseModel):
    """Query parameters for the requisitions list page.

    Used by GET /requisitions2, GET /requisitions2/table,
    GET /requisitions2/table/rows.
    """

    q: str = ""
    status: ReqStatus = ReqStatus.active
    owner: Optional[int] = None
    urgency: Optional[Urgency] = None
    date_from: Optional[date] = None
    date_to: Optional[date] = None
    sort: SortColumn = SortColumn.created_at
    order: SortOrder = SortOrder.desc
    page: int = Field(default=1, ge=1)
    per_page: int = Field(default=25, ge=1, le=100)


class InlineEditField(str, Enum):
    name = "name"
    status = "status"
    urgency = "urgency"
    deadline = "deadline"
    owner = "owner"


class RowActionName(str, Enum):
    assign = "assign"
    claim = "claim"
    unclaim = "unclaim"
    archive = "archive"
    activate = "activate"
    won = "won"
    lost = "lost"


class BulkActionName(str, Enum):
    archive = "archive"
    assign = "assign"
    activate = "activate"


class BulkActionForm(BaseModel):
    """Form data for bulk actions on selected requisitions."""

    ids: str  # comma-separated int list
    owner_id: Optional[int] = None

    @field_validator("ids")
    @classmethod
    def parse_ids(cls, v: str) -> str:
        parts = [p.strip() for p in v.split(",") if p.strip()]
        for p in parts:
            if not p.isdigit():
                raise ValueError(f"Invalid ID: {p}")
        if not parts:
            raise ValueError("No IDs provided")
        if len(parts) > 200:
            raise ValueError("Maximum 200 IDs per bulk action")
        return v

    def id_list(self) -> list[int]:
        return [int(p.strip()) for p in self.ids.split(",") if p.strip()]


class PaginationContext(BaseModel):
    """Passed to templates for rendering pagination controls."""

    page: int
    per_page: int
    total: int
    total_pages: int
