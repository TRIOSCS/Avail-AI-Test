"""schemas/tags.py — Pydantic response models for AI tagging endpoints.

Called by: app.routers.tags, app.routers.tagging_admin
Depends on: pydantic
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class TagResponse(BaseModel, extra="allow"):
    id: int
    name: str
    tag_type: str
    parent_id: int | None = None


class MaterialTagResponse(BaseModel, extra="allow"):
    tag: TagResponse
    confidence: float
    source: str
    classified_at: datetime | None = None


class EntityTagResponse(BaseModel, extra="allow"):
    tag: TagResponse
    interaction_count: float
    total_entity_interactions: float
    is_visible: bool
    first_seen_at: datetime | None = None
    last_seen_at: datetime | None = None


class EntityTagSummary(BaseModel, extra="allow"):
    tag_name: str
    tag_type: str
    count: float
    is_visible: bool
