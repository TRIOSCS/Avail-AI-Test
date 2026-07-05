"""csv_export.py — shared streaming-CSV helper for list/table exports.

What it does: turns a header + row iterable into a downloadable, formula-injection-safe
CSV StreamingResponse. Centralises the pattern previously duplicated in
``app/routers/crm/export.py`` and ``app/routers/sightings.py`` so every list export
(parts, materials, requisitions, vendors, approvals, resell, …) shares one implementation.

Called by: the ``/…/export`` endpoints across the router layer.
Depends on: stdlib ``csv`` + FastAPI ``StreamingResponse``.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Iterable, Sequence

from fastapi.responses import StreamingResponse

# Leading characters a spreadsheet may interpret as a formula, INCLUDING the tab (\t) and
# carriage-return (\r) whitespace that Excel/Sheets strip before evaluating what follows —
# omitting those is a known sanitizer bypass. Matches the established guard in
# app/routers/crm/export.py + app/routers/sightings.py. Prefixing with a single quote
# neutralises CSV-injection without visibly altering the value in most tools.
_FORMULA_TRIGGERS = ("=", "+", "-", "@", "\t", "\r")


def safe_cell(value: object) -> str:
    """Stringify a cell value, neutralising CSV formula-injection.

    ``None`` → empty string. A value whose first character is one of ``= + - @`` is
    prefixed with a single quote so a spreadsheet treats it as text, not a formula.
    """
    if value is None:
        return ""
    s = str(value)
    if s and s[0] in _FORMULA_TRIGGERS:
        return "'" + s
    return s


def stream_csv(
    filename: str,
    header: Sequence[str],
    rows: Iterable[Sequence[object]],
) -> StreamingResponse:
    """Stream ``rows`` as a CSV file download (attachment), formula-injection-safe.

    ``rows`` is consumed lazily (use a generator / ``yield_per`` query for large sets so
    the whole result never materialises in memory). Every cell passes through
    :func:`safe_cell`.
    """

    def _generate() -> Iterable[str]:
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow([safe_cell(h) for h in header])
        yield buf.getvalue()
        buf.seek(0)
        buf.truncate(0)
        for row in rows:
            writer.writerow([safe_cell(c) for c in row])
            yield buf.getvalue()
            buf.seek(0)
            buf.truncate(0)

    return StreamingResponse(
        _generate(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
