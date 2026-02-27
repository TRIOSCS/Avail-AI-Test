"""sql_helpers.py — SQL utility functions for safe query construction.

Provides escape_like() for sanitizing user input in LIKE/ILIKE patterns.

Called by: routers, services that build LIKE queries
Depends on: nothing (pure utility)
"""


def escape_like(s: str) -> str:
    """Escape %, _, and \\ for safe use in LIKE/ILIKE patterns."""
    return s.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")
