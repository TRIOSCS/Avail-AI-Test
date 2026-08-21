"""Shared text-parsing helpers for search worker result parsers.

One home for the small parsing utilities every marketplace result parser
needs, so a format fix lands once instead of per-worker.

Called by: ics_worker/nc_worker/tbf_worker result parsers
Depends on: stdlib only
"""


def parse_quantity(text: str) -> int | None:
    """Parse a marketplace quantity string to int.

    Handles commas, '+' suffix, empty.
    """
    if not text:
        return None
    cleaned = text.strip().rstrip("+").replace(",", "")
    try:
        return int(cleaned)
    except (ValueError, TypeError):
        return None
