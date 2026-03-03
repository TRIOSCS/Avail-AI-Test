"""Shared helpers used across dashboard sub-modules.

Called by: overview.py, briefs.py, leaderboard.py
"""

from datetime import timezone


def _ensure_aware(dt):
    """Ensure a datetime is timezone-aware (SQLite strips tzinfo)."""
    if dt and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _age_label(hours: float) -> str:
    """Convert hours-old to human-readable label."""
    if hours < 1:
        return "just now"
    if hours < 24:
        return f"{int(hours)}h ago"
    return f"{int(hours / 24)}d ago"
