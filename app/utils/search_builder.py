"""search_builder.py — Unified search query builder for ILIKE and optional FTS.

Consolidates the escape_like + ILIKE pattern used across 13 files and the
FTS-with-fallback cascade in vendors_crud.py into a single reusable utility.

Called by: routers and services that build search queries
Depends on: app.utils.sql_helpers.escape_like, sqlalchemy
"""

from sqlalchemy import or_
from sqlalchemy import text as sqltext
from sqlalchemy import true as sa_true
from sqlalchemy.exc import OperationalError, ProgrammingError

from app.utils.sql_helpers import escape_like


class SearchBuilder:
    """Build ILIKE and FTS filters from user search input.

    Usage:
        sb = SearchBuilder("resistor 100k")
        query = query.filter(sb.ilike_filter(Material.description, Material.mpn))

        # Or with FTS fallback:
        query = sb.fts_or_fallback(query, VendorCard, [VendorCard.normalized_name])
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
        return or_(*[col.ilike(pattern) for col in columns])

    def fts_or_fallback(self, query, model, fallback_columns, *, min_len=3):
        """Try PostgreSQL full-text search, fall back to ILIKE.

        Uses model.search_vector for FTS if available and query is long enough.
        Falls back to ILIKE on fallback_columns if FTS returns no results,
        isn't available (SQLite in tests), or the query is too short.

        Args:
            query: SQLAlchemy query object to filter
            model: SQLAlchemy model class (must have search_vector column for FTS)
            fallback_columns: List of columns for ILIKE fallback
            min_len: Minimum query length to attempt FTS (default 3)

        Returns:
            Filtered query object
        """
        if not self.q or len(self.q) < min_len or not hasattr(model, "search_vector"):
            return query.filter(self.ilike_filter(*fallback_columns))

        try:
            fts_query = (
                query.filter(
                    model.search_vector.isnot(None),
                    sqltext("search_vector @@ plainto_tsquery('english', :q)"),
                )
                .params(q=self.q)
                .order_by(sqltext("ts_rank(search_vector, plainto_tsquery('english', :q)) DESC"))
                .params(q=self.q)
            )
            if fts_query.count() > 0:
                return fts_query
            return query.filter(self.ilike_filter(*fallback_columns))
        except (ProgrammingError, OperationalError):
            return query.filter(self.ilike_filter(*fallback_columns))
