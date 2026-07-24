"""utils/column_limits.py — model-derived string-length guards (CRM Wave 3, item 6).

Purpose: Postgres raises StringDataRightTruncation (a 500) when a form value
         exceeds a bounded ``String(n)`` column; SQLite (tests) silently accepts
         it. These helpers make the MODEL the single source of truth for length
         limits: routes call ``ensure_fits_column`` to reject over-length input
         with a clean 400, and templates render matching ``maxlength``
         attributes via the ``column_maxlength`` Jinja2 global
         (app/template_env.py) so client and server can never drift.

Called by: app.routers.htmx.companies (_registries, core, sites),
    app.routers.htmx.vendors, app.template_env (column_maxlength global)
Depends on: fastapi (HTTPException only)
"""

from typing import Any

from fastapi import HTTPException


def column_max_length(model: type[Any], field: str) -> int | None:
    """The declared max length of *model*'s *field* column, or None.

    None means "no declared bound" — unbounded ``Text`` columns, non-string
    types, and unknown fields all return None (callers skip the guard).
    """
    column = model.__table__.columns.get(field)
    if column is None:
        return None
    length = getattr(column.type, "length", None)
    return length if isinstance(length, int) else None


def ensure_fits_column(model: type[Any], field: str, value: str | None, label: str | None = None) -> None:
    """Raise HTTPException(400) when *value* exceeds the column's declared length.

    No-op for empty values and for columns without a declared length. *label* is the
    user-facing field name in the error message (defaults to a titled version of the
    column name).
    """
    if not value:
        return
    max_length = column_max_length(model, field)
    if max_length is not None and len(value) > max_length:
        display = label or field.replace("_", " ").capitalize()
        raise HTTPException(400, f"{display} is too long (max {max_length} characters)")
