"""
schemas/sources.py — Pydantic models for Source & Mining endpoints

Validates API source toggles and mining lookback parameters.

Business Rules:
- Source status must be live, disabled, or pending
- Lookback days defaults to env setting if not provided
- Mining endpoints accept optional JSON body

Called by: routers/sources.py
Depends on: pydantic
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class SourceStatusToggle(BaseModel):
    status: Literal["live", "disabled", "pending"]


class MiningOptions(BaseModel):
    """Optional params for inbox/sent mining. All fields have defaults."""
    lookback_days: int = Field(default=30, ge=1, le=365)
