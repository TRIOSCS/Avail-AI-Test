"""Pydantic models for the Requisitions 2 HTMX page.

Handles query/filter parsing, pagination, and action validation
for the server-rendered requisitions list.

Called by: app/services/requisition_list_service.py
Depends on: pydantic
"""

from datetime import date
from enum import Enum

from pydantic import BaseModel, Field


class ReqStatus(str, Enum):
    all = "all"
    open = "open"
    rfqs_sent = "rfqs_sent"
    offers = "offers"
    quoted = "quoted"
    won = "won"
    lost = "lost"
    hotlist = "hotlist"
    archived = "archived"


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

    Used by GET /requisitions2, GET /requisitions2/table, GET /requisitions2/table/rows.
    """

    q: str = ""
    status: ReqStatus = ReqStatus.open
    owner: int | None = None
    urgency: Urgency | None = None
    date_from: date | None = None
    date_to: date | None = None
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
    clone = "clone"


class BulkActionName(str, Enum):
    archive = "archive"
    assign = "assign"
    activate = "activate"


class PaginationContext(BaseModel):
    """Passed to templates for rendering pagination controls."""

    page: int
    per_page: int
    total: int
    total_pages: int
