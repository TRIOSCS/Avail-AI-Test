"""services/dedup_decision_service.py — persisted dedup decisions (dismiss / un-dismiss
/ merge audit).

The Data Ops dedup surface emits keeper-first pair tokens ("<keeperId>-<loserId>"),
which are NOT sorted — every function here canonicalizes to (min, max) on write AND
lookup so one pair can never store under two keys. All functions are commit-free:
the router owns the transaction (via _dedup_single_action / _dedup_bulk).

audited_merge / audited_delete_both (added in a later task of the same workstream)
REUSE the existing hand-maintained merge services — they never re-implement a merge.

Called by: routers/htmx/settings.py (Data Ops pane), services/auto_dedup_service.py
    (nightly dismissed-pair skip)
Depends on: app.models (DedupDecision, DedupMergeAudit, VendorCard, Company,
    SiteContact, User); vendor/company/contact merge services (lazy imports)
"""

from sqlalchemy import delete, or_, select  # noqa: F401
from sqlalchemy.orm import Session

from ..models import Company, DedupDecision, DedupMergeAudit, SiteContact, User, VendorCard  # noqa: F401

ENTITY_TYPES: tuple[str, ...] = ("vendor", "company", "contact")


def _require_entity_type(entity_type: str) -> None:
    if entity_type not in ENTITY_TYPES:
        raise ValueError(f"Unknown dedup entity type {entity_type!r}")


def canonical_pair(id_a: int, id_b: int) -> tuple[int, int]:
    """Canonical storage/lookup key for a pair: (min, max)."""
    return (id_a, id_b) if id_a < id_b else (id_b, id_a)


def record_dismissals(db: Session, entity_type: str, pairs: list[tuple[int, int]], decided_by_id: int | None) -> int:
    """Persist dismissals for the given pairs (idempotent). Returns rows ADDED.

    Self-pairs (a == b) are ignored. Existing rows are skipped rather than violating
    uq_dedup_decision_pair — re-dismissing is a no-op, not an error.
    """
    _require_entity_type(entity_type)
    wanted = {canonical_pair(a, b) for a, b in pairs if a != b}
    if not wanted:
        return 0
    existing = {
        (id_a, id_b)
        for id_a, id_b in db.execute(
            select(DedupDecision.id_a, DedupDecision.id_b).where(DedupDecision.entity_type == entity_type)
        )
    }
    new = sorted(wanted - existing)
    for id_a, id_b in new:
        db.add(DedupDecision(entity_type=entity_type, id_a=id_a, id_b=id_b, decided_by_id=decided_by_id))
    return len(new)


def remove_dismissal(db: Session, entity_type: str, id_a: int, id_b: int) -> int:
    """Delete the persisted dismissal for one pair (un-dismiss).

    Raises if absent.
    """
    _require_entity_type(entity_type)
    lo, hi = canonical_pair(id_a, id_b)
    deleted: int = db.execute(
        delete(DedupDecision)
        .where(
            DedupDecision.entity_type == entity_type,
            DedupDecision.id_a == lo,
            DedupDecision.id_b == hi,
        )
        .execution_options(synchronize_session=False)
    ).rowcount
    if not deleted:
        raise ValueError(f"No dismissal recorded for {entity_type} pair {lo}-{hi}")
    return deleted


def load_dismissed_pairs(db: Session) -> dict[str, set[tuple[int, int]]]:
    """All dismissed pairs, grouped by entity type, in ONE query.

    Keys always include all three entity types (empty set when none) so callers can
    index without .get().
    """
    out: dict[str, set[tuple[int, int]]] = {t: set() for t in ENTITY_TYPES}
    for entity_type, id_a, id_b in db.execute(
        select(DedupDecision.entity_type, DedupDecision.id_a, DedupDecision.id_b)
    ):
        out.setdefault(entity_type, set()).add((id_a, id_b))
    return out


def filter_dismissed_pairs(pairs: list[dict], dismissed: set[tuple[int, int]], key: str) -> list[dict]:
    """Drop dismissed pairs from a finder's candidate list (router-level post-filter).

    ``key`` is the finder's dict prefix: candidates carry ``{key}_a`` / ``{key}_b``
    sub-dicts each holding an ``id`` (vendor_utils / company_utils /
    contact_dedup_candidates all share this nested shape).
    """
    if not dismissed:
        return pairs
    return [p for p in pairs if canonical_pair(p[f"{key}_a"]["id"], p[f"{key}_b"]["id"]) not in dismissed]
