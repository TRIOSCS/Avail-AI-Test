"""services/dedup_decision_service.py — persisted dedup decisions (dismiss / un-dismiss
/ merge audit).

The Data Ops dedup surface emits keeper-first pair tokens ("<keeperId>-<loserId>"),
which are NOT sorted — every function here canonicalizes to (min, max) on write AND
lookup so one pair can never store under two keys. All functions are commit-free:
the router owns the transaction (via _dedup_single_action / _dedup_bulk).

audited_merge / audited_delete_both REUSE the existing hand-maintained merge
services — they never re-implement a merge.

Called by: routers/htmx/settings.py (Data Ops pane), services/auto_dedup_service.py
    (nightly dismissed-pair skip)
Depends on: app.models (DedupDecision, DedupMergeAudit, VendorCard, Company,
    SiteContact, User); vendor/company/contact merge services (lazy imports)
"""

from sqlalchemy import delete, or_, select
from sqlalchemy.orm import Session

from ..models import Company, DedupDecision, DedupMergeAudit, SiteContact, User, VendorCard

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


def _display_name(db: Session, entity_type: str, entity_id: int) -> str | None:
    """Live display name for an entity id (None if the row no longer exists)."""
    if entity_type == "vendor":
        row = db.get(VendorCard, entity_id)
        val = row.display_name if row else None
    elif entity_type == "company":
        row = db.get(Company, entity_id)
        val = row.name if row else None
    else:
        row = db.get(SiteContact, entity_id)
        val = row.full_name if row else None
    return val[:255] if val else None


def _prune_decisions(db: Session, entity_type: str, ids: list[int]) -> None:
    """Delete DedupDecision rows referencing ANY of the given ids (stale after a
    merge/delete removed the entity).

    Bulk delete — audit_listeners never fire on bulk ops, which is exactly why the
    caller writes an explicit audit row.
    """
    db.execute(
        delete(DedupDecision)
        .where(
            DedupDecision.entity_type == entity_type,
            or_(DedupDecision.id_a.in_(ids), DedupDecision.id_b.in_(ids)),
        )
        .execution_options(synchronize_session=False)
    )


def audited_merge(db: Session, entity_type: str, keep_id: int, remove_id: int, actor_id: int | None) -> dict:
    """Run the entity's EXISTING merge service + append a DedupMergeAudit row + prune
    stale DedupDecision rows.

    Commit-free; returns the service's result dict unchanged so the router's
    success_msg_fn keeps working. Names are captured BEFORE the merge because the merge
    deletes the loser row.
    """
    _require_entity_type(entity_type)
    if entity_type == "vendor":
        from .vendor_merge_service import merge_vendor_cards as merge_fn
    elif entity_type == "company":
        from .company_merge_service import merge_companies as merge_fn
    else:
        from .contact_merge_service import merge_contacts as merge_fn

    kept_name = _display_name(db, entity_type, keep_id)
    removed_name = _display_name(db, entity_type, remove_id)
    result = merge_fn(keep_id, remove_id, db)
    db.add(
        DedupMergeAudit(
            actor_id=actor_id,
            entity_type=entity_type,
            action="merge",
            kept_id=keep_id,
            kept_name=kept_name,
            removed_id=remove_id,
            removed_name=removed_name,
        )
    )
    _prune_decisions(db, entity_type, [keep_id, remove_id])
    return result


def audited_delete_both(db: Session, entity_type: str, id_a: int, id_b: int, actor_id: int | None) -> dict:
    """Run the entity's EXISTING delete-both service + append TWO delete_both audit rows
    (one per removed entity, kept_id NULL) + prune stale decisions.

    Vendor and company only — there is no contact delete-both.
    """
    if entity_type == "vendor":
        from .vendor_merge_service import delete_vendor_cards as delete_fn
    elif entity_type == "company":
        from .company_merge_service import delete_companies as delete_fn
    else:
        raise ValueError(f"delete-both is not supported for entity type {entity_type!r}")

    name_a = _display_name(db, entity_type, id_a)
    name_b = _display_name(db, entity_type, id_b)
    result = delete_fn(id_a, id_b, db)
    for removed_id, removed_name in ((id_a, name_a), (id_b, name_b)):
        db.add(
            DedupMergeAudit(
                actor_id=actor_id,
                entity_type=entity_type,
                action="delete_both",
                kept_id=None,
                kept_name=None,
                removed_id=removed_id,
                removed_name=removed_name,
            )
        )
    _prune_decisions(db, entity_type, [id_a, id_b])
    return result


def list_dismissals(db: Session) -> list[dict]:
    """All persisted dismissals for the Ignored-pairs section, newest first.

    Display names resolve LIVE per entity type; ids that no longer resolve render as
    "deleted #<id>" (the row still offers Un-dismiss, so stale rows are self-cleaning).
    decided_by falls back to "unknown" (deleted user → SET NULL).
    """
    rows = (
        db.execute(select(DedupDecision).order_by(DedupDecision.created_at.desc(), DedupDecision.id.desc()))
        .scalars()
        .all()
    )
    if not rows:
        return []

    ids: dict[str, set[int]] = {t: set() for t in ENTITY_TYPES}
    for r in rows:
        if r.entity_type in ids:
            ids[r.entity_type].update((r.id_a, r.id_b))
    # NOTE: .all() before dict() — a raw Result has .keys(), so dict(result) would
    # treat it as a mapping and crash; a list of 2-tuple Rows builds the map cleanly.
    names: dict[str, dict[int, str]] = {t: {} for t in ENTITY_TYPES}
    if ids["vendor"]:
        names["vendor"] = dict(
            db.execute(select(VendorCard.id, VendorCard.display_name).where(VendorCard.id.in_(ids["vendor"]))).all()
        )
    if ids["company"]:
        names["company"] = dict(
            db.execute(select(Company.id, Company.name).where(Company.id.in_(ids["company"]))).all()
        )
    if ids["contact"]:
        names["contact"] = dict(
            db.execute(select(SiteContact.id, SiteContact.full_name).where(SiteContact.id.in_(ids["contact"]))).all()
        )

    user_ids = {r.decided_by_id for r in rows if r.decided_by_id}
    users = {u.id: u for u in db.execute(select(User).where(User.id.in_(user_ids))).scalars()} if user_ids else {}

    out: list[dict] = []
    for r in rows:
        entity_names = names.get(r.entity_type, {})
        decider = users.get(r.decided_by_id)
        out.append(
            {
                "entity_type": r.entity_type,
                "id_a": r.id_a,
                "id_b": r.id_b,
                "name_a": entity_names.get(r.id_a) or f"deleted #{r.id_a}",
                "name_b": entity_names.get(r.id_b) or f"deleted #{r.id_b}",
                "decided_by": decider.display_name if decider else "unknown",
                "created_at": r.created_at,
            }
        )
    return out
