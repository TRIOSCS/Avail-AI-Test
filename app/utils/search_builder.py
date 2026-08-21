"""search_builder.py — Unified ILIKE search query builder.

Consolidates the escape_like + ILIKE pattern used across 13 files into a
single reusable utility.

Called by: routers and services that build search queries
Depends on: app.utils.sql_helpers.escape_like, sqlalchemy
"""

from sqlalchemy import or_
from sqlalchemy import true as sa_true

from app.utils.sql_helpers import escape_like


class SearchBuilder:
    """Build ILIKE filters from user search input.

    Usage:
        sb = SearchBuilder("resistor 100k")
        query = query.filter(sb.ilike_filter(Material.description, Material.mpn))
    """

    def __init__(self, q: str):
        self.q = q.strip()
        self.safe = escape_like(self.q)

    def ilike_filter(self, *columns, prefix=False):
        """Return an or_() filter across columns using ILIKE.

        Args:
            *columns: SQLAlchemy column objects to search
            prefix: If True, use 'term%' instead of '%term%'

        Returns:
            SQLAlchemy BooleanClauseList (or_() of ILIKE filters)
        """
        if not self.q:
            return sa_true()
        pattern = f"{self.safe}%" if prefix else f"%{self.safe}%"
        # escape="\\" so escape_like()'s \%, \_, \\ are matched literally
        # (PostgreSQL defaults LIKE's escape to \, but SQLite/tests do not).
        return or_(*[col.ilike(pattern, escape="\\") for col in columns])
