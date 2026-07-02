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
    manufacturer: str = Field(default="", max_length=255)
    group_by: str = ""  # "" (flat), "brand", "manufacturer"
    sort: str = "priority"
    dir: str = "desc"
    page: int = Field(default=1, ge=1)
    limit: int = Field(default=50, ge=1, le=200)
    # Dashboard-strip quick filters. These mirror the counter predicates computed in
    # sightings_list so clicking "N Urgent" / "N Stale" shows exactly those N rows.
    urgent: bool = False  # priority_score >= 70 OR need_by_date within 48h
    stale: bool = False  # no ActivityLog within sighting_stale_days
