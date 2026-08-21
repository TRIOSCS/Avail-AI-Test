"""schemas/admin.py — Pydantic models for admin endpoints.

Validates admin-only request bodies: credential updates.

Called by: routers/admin/system.py, routers/admin/data_ops.py
Depends on: pydantic
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class SourceCredentialsUpdate(BaseModel):
    """Update credential key-value pairs for an API source.

    Body is a dict of VAR_NAME -> plaintext_value. Uses extra="allow" to accept dynamic
    credential keys.
    """

    model_config = ConfigDict(extra="allow")
