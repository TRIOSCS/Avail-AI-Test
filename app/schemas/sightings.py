"""Pydantic schemas for the Sightings page.

Called by: app/routers/sightings.py
Depends on: pydantic
"""

from pydantic import BaseModel, Field


class SightingsListParams(BaseModel):
    """Query parameters for the sightings list endpoint."""

    status: str = ""
    sales_person: str = ""
    assigned: str = ""  # "mine" or "" for all
    q: str = ""
    group_by: str = ""  # "" (flat), "brand", "manufacturer"
    sort: str = "priority"
    dir: str = "desc"
    page: int = Field(default=1, ge=1)
    limit: int = Field(default=50, ge=1, le=200)
