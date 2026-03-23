"""schemas/admin.py — Pydantic models for admin endpoints.

Validates admin-only request bodies: credential updates, channel routing config.

Called by: routers/admin/system.py, routers/admin/data_ops.py
Depends on: pydantic
"""

from __future__ import annotations

from pydantic import BaseModel


class SourceCredentialsUpdate(BaseModel):
    """Update credential key-value pairs for an API source.

    Body is a dict of VAR_NAME -> plaintext_value. Uses extra="allow" to accept dynamic
    credential keys.
    """

    model_config = {"extra": "allow"}


class TeamsChannelRouting(BaseModel):
    """Per-event-type Teams channel routing configuration.

    Valid keys: teams_channel_hot, teams_channel_quotes, teams_channel_inventory,
    teams_channel_ownership, teams_channel_buyplan, teams_channel_ops.
    Uses extra="allow" to accept dynamic channel keys.
    """

    model_config = {"extra": "allow"}
