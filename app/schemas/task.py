"""Request/response schemas for the Requisition Task Board API.

Called by: routers/task.py
Depends on: nothing
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class TaskCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=255)
    description: str | None = None
    task_type: str = Field(default="general", pattern="^(sourcing|sales|general)$")
    priority: int = Field(default=2, ge=1, le=3)
    assigned_to_id: int | None = None
    due_at: datetime | None = None


class TaskUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None
    task_type: str | None = Field(default=None, pattern="^(sourcing|sales|general)$")
    status: str | None = Field(default=None, pattern="^(todo|in_progress|done)$")
    priority: int | None = Field(default=None, ge=1, le=3)
    assigned_to_id: int | None = None
    due_at: datetime | None = None


class TaskStatusUpdate(BaseModel):
    status: str = Field(..., pattern="^(todo|in_progress|done)$")


class TaskResponse(BaseModel):
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
    due_at: datetime | None = None
    completed_at: datetime | None = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class TaskSummary(BaseModel):
    todo: int = 0
    in_progress: int = 0
    done: int = 0
    overdue: int = 0
    total: int = 0
