"""Shared utility helpers used across connectors and services."""

from typing import Any


def safe_int(v: Any) -> int | None:
    """Safely convert a value to int, returning None on failure."""
    if v is None:
        return None
    try:
        return int(v)
    except (ValueError, TypeError):
        return None


def safe_float(v: Any) -> float | None:
    """Safely convert a value to float, returning None on failure."""
    if v is None:
        return None
    try:
        return float(v)
    except (ValueError, TypeError):
        return None
