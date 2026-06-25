"""PartSurfer description negative-cache selector + writer (DB-backed, zero network).

The enrichment worker's ``_partsurfer_desc_pass`` fetches HP/HPE spares' verbatim
descriptions live from partsurfer.hpe.com to categorize the ~70k uncategorized HP cards.
Without a negative cache it re-fetched every dead/ungrammatical spare on EVERY batch
forever (145k of the 743k catalog are not_found) -- pure wasted throughput. This module
is the durable record of those misses, structured after ``oem_crosswalk_enrich``'s
``pending_resolution`` / ``apply_resolution`` pair but on its OWN ``partsurfer_desc_negative``
table (a PartSurfer-description miss is a different sub-resource from the spare->canonical
crosswalk -- see the model docstring for why they are NOT merged).

- ``blocked_spare_norms`` -- the selector: given candidate spare norms, return the subset
  whose negative row is still FRESH (``retry_after`` in the future). The worker drops those
  candidates so they are never re-fetched within the window.
- ``record_negative`` -- the writer: upsert one miss. ``reason='no_result'`` (the fetch
  returned no description) gets the long ``PARTSURFER_NO_RESULT_RETRY_DAYS`` window; an
  ``reason='ungrammatical'`` (a description came back but the grammar declined it) gets the
  SHORT ``PARTSURFER_UNGRAMMATICAL_RETRY_DAYS`` window -- a parse miss is not evidence the
  OEM lacks the part, so it must NOT lock the spare out for the long window. A stale row is
  refreshed in place (upsert on the unique ``spare_norm``); the caller owns commit.

Throttle/outage misses (``PartSurferTransient``) are NEVER recorded -- only genuine
``None`` no-results and grammar declines.

Called by: app/services/enrichment_worker/worker.py (``_partsurfer_desc_pass``).
Depends on: app.models.PartsurferDescNegative, app.utils.normalization.normalize_mpn_key.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from typing import Literal

from sqlalchemy.orm import Session

from app.models import PartsurferDescNegative

# Long window: a genuine no-result (PartSurfer catalogs no description for the spare) --
# matches oem_crosswalk no_match (uncataloged service parts rarely become cataloged).
PARTSURFER_NO_RESULT_RETRY_DAYS = 90
# Short window: a description came back but the desc-grammar could not categorize it.
# NOT evidence the OEM lacks the part -- the grammar improves, so re-check sooner.
PARTSURFER_UNGRAMMATICAL_RETRY_DAYS = 14

_RETRY_DAYS_BY_REASON: dict[str, int] = {
    "no_result": PARTSURFER_NO_RESULT_RETRY_DAYS,
    "ungrammatical": PARTSURFER_UNGRAMMATICAL_RETRY_DAYS,
}


def _aware(dt: datetime) -> datetime:
    """Coerce a naive UTC timestamp (SQLite round-trip) to aware for comparison."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def blocked_spare_norms(
    db: Session,
    spare_norms: Iterable[str],
    now: datetime | None = None,
) -> set[str]:
    """Return the subset of *spare_norms* whose negative row is still FRESH.

    A norm is BLOCKED (re-fetch suppressed) when it has a ``partsurfer_desc_negative``
    row whose ``retry_after`` is in the future. A stale row (``retry_after`` <= now) is
    NOT blocked -- the worker re-fetches and the writer refreshes the row in place.
    Empty/falsy norms are ignored.
    """
    norms = [n for n in dict.fromkeys(spare_norms) if n]
    if not norms:
        return set()
    now = now or datetime.now(timezone.utc)
    rows = (
        db.query(PartsurferDescNegative.spare_norm, PartsurferDescNegative.retry_after)
        .filter(PartsurferDescNegative.spare_norm.in_(norms))
        .all()
    )
    return {norm for norm, retry_after in rows if retry_after is not None and _aware(retry_after) > now}


def record_negative(
    db: Session,
    spare_raw: str,
    spare_norm: str,
    reason: Literal["no_result", "ungrammatical"],
    now: datetime | None = None,
) -> PartsurferDescNegative | None:
    """Upsert one PartSurfer-description miss for *spare_norm*; return the row.

    ``reason`` picks the retry window: ``no_result`` -> 90 days, ``ungrammatical`` -> 14
    days. ``retry_after`` is stored = ``looked_up_at`` + the window so the selector is one
    indexed comparison and the policy is auditable per row. An existing row (any reason)
    is refreshed in place -- the latest observation, and its window, win. The caller owns
    flush/commit. A blank *spare_norm* is a no-op (returns ``None``).
    """
    norm = (spare_norm or "").strip()
    if not norm:
        return None
    now = now or datetime.now(timezone.utc)
    retry_after = now + timedelta(days=_RETRY_DAYS_BY_REASON[reason])

    row = db.query(PartsurferDescNegative).filter_by(spare_norm=norm[:64]).one_or_none()
    if row is None:
        row = PartsurferDescNegative(spare_norm=norm[:64])
        db.add(row)
    row.spare_raw = (spare_raw or "")[:64]
    row.reason = reason
    row.looked_up_at = now
    row.retry_after = retry_after
    return row
