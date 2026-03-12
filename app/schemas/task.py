"""Request/response schemas for the Requisition Task Board API.

Called by: routers/task.py
Depends on: nothing
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from pydantic import BaseModel, ConfigDict, Field, field_validator


class TaskCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=255)
    description: str | None = None
    assigned_to_id: int = Field(..., description="User to assign the task to")
    due_at: datetime = Field(..., description="Due date (min 24 hours from now)")

    @field_validator("due_at")
    @classmethod
    def due_at_min_24h(cls, v: datetime) -> datetime:
        now = datetime.now(timezone.utc)
        # Make naive datetimes UTC for comparison
        check = v.replace(tzinfo=timezone.utc) if v.tzinfo is None else v
        if check < now + timedelta(hours=24):
            raise ValueError("Due date must be at least 24 hours from now")
        return v


class TaskUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None
    assigned_to_id: int | None = None
    due_at: datetime | None = None

    @field_validator("due_at")
    @classmethod
    def due_at_min_24h(cls, v: datetime | None) -> datetime | None:
        if v is None:
            return v
        now = datetime.now(timezone.utc)
        check = v.replace(tzinfo=timezone.utc) if v.tzinfo is None else v
        if check < now + timedelta(hours=24):
            raise ValueError("Due date must be at least 24 hours from now")
        return v


class TaskComplete(BaseModel):
    completion_note: str = Field(..., min_length=1, description="How was this task resolved?")


class TaskResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    requisition_id: int
    title: str
    description: str | None = None
    task_type: str
    status: str
    priority: int
    ai_priority_score: float | None = None
    ai_risk_flag: str | None = None
    assigned_to_id: int | None = None
    assignee_name: str | None = None
    created_by: int | None = None
    creator_name: str | None = None
    source: str
    source_ref: str | None = None
    completion_note: str | None = None
    requisition_name: str | None = None
    due_at: datetime | None = None
    completed_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class TaskSummary(BaseModel):
    assigned_to_me: int = 0
    waiting_on: int = 0
    overdue: int = 0
