"""Request/response schemas for the Requisition Task Board API.

Called by: routers/task.py
Depends on: nothing
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from pydantic import BaseModel, Field, field_validator


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


class TaskStatusUpdate(BaseModel):
    """Quick status change from the task manager."""

    status: str = Field(..., description="New status: todo, in_progress, or done")

    @field_validator("status")
    @classmethod
    def validate_status(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in ("todo", "in_progress", "done"):
            raise ValueError(f"Invalid status: {v}. Must be todo, in_progress, or done")
        return v
