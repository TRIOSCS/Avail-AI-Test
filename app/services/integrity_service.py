"""Material Card Data Integrity Service.

Provides continuous integrity monitoring and self-healing for the material card
linkage system.  Designed to catch and repair data issues before they cost deals.

Checks run every 6 hours via scheduler.  Any orphaned records (have MPN but no
material_card_id) are automatically re-linked.  Alert-level log lines use the
prefix INTEGRITY_ALERT for easy monitoring.

Called by: scheduler.py (_job_integrity_check)
Depends on: models (MaterialCard, Requirement, Sighting, Offer), search_service
"""

import logging
from datetime import datetime, timezone

from sqlalchemy import func, text
from sqlalchemy.orm import Session

from ..models import MaterialCard, Offer, Requirement, Sighting
from ..utils.normalization import normalize_mpn_key

log = logging.getLogger(__name__)


# ── Integrity Checks ─────────────────────────────────────────────────


def check_orphaned_requirements(db: Session) -> int:
    """Count requirements that have an MPN but no material_card_id."""
    return (
        db.query(func.count(Requirement.id))
        .filter(
            Requirement.primary_mpn.isnot(None),
            Requirement.primary_mpn != "",
            Requirement.material_card_id.is_(None),
        )
        .scalar()
    ) or 0


def check_orphaned_sightings(db: Session) -> int:
    """Count sightings that have an MPN but no material_card_id."""
    return (
        db.query(func.count(Sighting.id))
        .filter(
            Sighting.mpn_matched.isnot(None),
            Sighting.mpn_matched != "",
            Sighting.material_card_id.is_(None),
        )
        .scalar()
    ) or 0


def check_orphaned_offers(db: Session) -> int:
    """Count offers that have a part number but no material_card_id."""
    return (
        db.query(func.count(Offer.id))
        .filter(
            Offer.mpn.isnot(None),
            Offer.mpn != "",
            Offer.material_card_id.is_(None),
        )
        .scalar()
    ) or 0


def check_dangling_fks(db: Session) -> dict:
    """Count records pointing to non-existent material cards."""
    results = {}
    for model, name in [
        (Requirement, "requirements"),
        (Sighting, "sightings"),
        (Offer, "offers"),
    ]:
        count = (
            db.query(func.count(model.id))
            .outerjoin(MaterialCard, model.material_card_id == MaterialCard.id)
            .filter(
                model.material_card_id.isnot(None),
                MaterialCard.id.is_(None),
            )
            .scalar()
        ) or 0
        results[name] = count
    return results


def check_duplicate_cards(db: Session) -> int:
    """Count normalized_mpn values that appear on more than one card.

    Should always be 0 given the UNIQUE constraint, but defense in depth.
    """
    rows = (
        db.query(MaterialCard.normalized_mpn, func.count(MaterialCard.id))
        .group_by(MaterialCard.normalized_mpn)
        .having(func.count(MaterialCard.id) > 1)
        .all()
    )
    return len(rows)


# ── Self-Healing Re-Linker ───────────────────────────────────────────


def heal_orphaned_records(db: Session, batch_size: int = 500) -> dict:
    """Re-link records that have an MPN but no material_card_id.

    Uses resolve_material_card (find-or-create) so cards are created if needed.
    Processes in batches to limit memory usage on large backlogs.

    Returns counts of healed records per entity type.
    """
    from ..search_service import resolve_material_card

    healed = {"requirements": 0, "sightings": 0, "offers": 0}

    # --- Requirements ---
    orphans = (
        db.query(Requirement)
        .filter(
            Requirement.primary_mpn.isnot(None),
            Requirement.primary_mpn != "",
            Requirement.material_card_id.is_(None),
        )
        .limit(batch_size)
        .all()
    )
    for r in orphans:
        try:
            card = resolve_material_card(r.primary_mpn, db)
            if card:
                r.material_card_id = card.id
                healed["requirements"] += 1
        except Exception as e:
            log.warning("INTEGRITY_HEAL_FAIL: requirement id=%s mpn=%s error=%s", r.id, r.primary_mpn, e)
            db.rollback()

    # --- Sightings ---
    orphans = (
        db.query(Sighting)
        .filter(
            Sighting.mpn_matched.isnot(None),
            Sighting.mpn_matched != "",
            Sighting.material_card_id.is_(None),
        )
        .limit(batch_size)
        .all()
    )
    for s in orphans:
        try:
            card = resolve_material_card(s.mpn_matched, db)
            if card:
                s.material_card_id = card.id
                healed["sightings"] += 1
        except Exception as e:
            log.warning("INTEGRITY_HEAL_FAIL: sighting id=%s mpn=%s error=%s", s.id, s.mpn_matched, e)
            db.rollback()

    # --- Offers ---
    orphans = (
        db.query(Offer)
        .filter(
            Offer.mpn.isnot(None),
            Offer.mpn != "",
            Offer.material_card_id.is_(None),
        )
        .limit(batch_size)
        .all()
    )
    for o in orphans:
        try:
            card = resolve_material_card(o.mpn, db)
            if card:
                o.material_card_id = card.id
                healed["offers"] += 1
        except Exception as e:
            log.warning("INTEGRITY_HEAL_FAIL: offer id=%s mpn=%s error=%s", o.id, o.mpn, e)
            db.rollback()

    if any(v > 0 for v in healed.values()):
        db.commit()
        log.info(
            "INTEGRITY_HEALED: requirements=%d sightings=%d offers=%d",
            healed["requirements"],
            healed["sightings"],
            healed["offers"],
        )

    return healed


def clear_dangling_fks(db: Session) -> dict:
    """Set material_card_id to NULL where the referenced card no longer exists.

    These records will then be picked up by heal_orphaned_records on the next
    cycle and re-linked to the correct (or a new) card.
    """
    cleared = {}
    for model, name in [
        (Requirement, "requirements"),
        (Sighting, "sightings"),
        (Offer, "offers"),
    ]:
        danglers = (
            db.query(model)
            .outerjoin(MaterialCard, model.material_card_id == MaterialCard.id)
            .filter(
                model.material_card_id.isnot(None),
                MaterialCard.id.is_(None),
            )
            .all()
        )
        for rec in danglers:
            rec.material_card_id = None
        cleared[name] = len(danglers)

    if any(v > 0 for v in cleared.values()):
        db.commit()
        log.warning(
            "INTEGRITY_CLEARED_DANGLING: requirements=%d sightings=%d offers=%d",
            cleared["requirements"],
            cleared["sightings"],
            cleared["offers"],
        )

    return cleared


# ── Full Check + Heal Orchestrator ───────────────────────────────────


def run_integrity_check(db: Session) -> dict:
    """Run all integrity checks and self-heal if needed.

    Returns a report dict suitable for the /api/admin/integrity endpoint.
    """
    now = datetime.now(timezone.utc)

    # --- Run checks ---
    orphaned_req = check_orphaned_requirements(db)
    orphaned_sight = check_orphaned_sightings(db)
    orphaned_offer = check_orphaned_offers(db)
    dangling = check_dangling_fks(db)
    dup_cards = check_duplicate_cards(db)

    total_orphaned = orphaned_req + orphaned_sight + orphaned_offer
    total_dangling = sum(dangling.values())

    # --- Log check results ---
    if total_orphaned == 0 and total_dangling == 0 and dup_cards == 0:
        log.info("INTEGRITY_CHECK: all checks passed")
    else:
        level = "critical" if (total_orphaned > 50 or total_dangling > 0 or dup_cards > 0) else "warning"
        msg = (
            "INTEGRITY_ALERT: orphaned_req=%d orphaned_sight=%d orphaned_offer=%d "
            "dangling_req=%d dangling_sight=%d dangling_offer=%d dup_cards=%d"
        )
        args = (
            orphaned_req, orphaned_sight, orphaned_offer,
            dangling["requirements"], dangling["sightings"], dangling["offers"],
            dup_cards,
        )
        if level == "critical":
            log.critical(msg, *args)
        else:
            log.warning(msg, *args)

    # --- Self-heal ---
    heal_result = {"requirements": 0, "sightings": 0, "offers": 0}
    clear_result = {"requirements": 0, "sightings": 0, "offers": 0}

    # Fix dangling FKs first (sets to NULL), then heal (re-links)
    if total_dangling > 0:
        clear_result = clear_dangling_fks(db)

    if total_orphaned > 0 or total_dangling > 0:
        heal_result = heal_orphaned_records(db)

    # --- Compute linkage percentages ---
    linkage = _compute_linkage_coverage(db)

    # --- Determine overall status ---
    residual_orphans = (
        check_orphaned_requirements(db)
        + check_orphaned_sightings(db)
        + check_orphaned_offers(db)
    )
    if residual_orphans == 0 and total_dangling == 0 and dup_cards == 0:
        status = "healthy"
    elif residual_orphans <= 50 and dup_cards == 0:
        status = "degraded"
    else:
        status = "critical"

    report = {
        "status": status,
        "last_check": now.isoformat(),
        "checks": {
            "orphaned_requirements": orphaned_req,
            "orphaned_sightings": orphaned_sight,
            "orphaned_offers": orphaned_offer,
            "dangling_requirements": dangling["requirements"],
            "dangling_sightings": dangling["sightings"],
            "dangling_offers": dangling["offers"],
            "duplicate_cards": dup_cards,
        },
        "healed": heal_result,
        "cleared_dangling": clear_result,
        "linkage_coverage": linkage,
        "material_cards_total": db.query(func.count(MaterialCard.id)).scalar() or 0,
    }

    return report


def _compute_linkage_coverage(db: Session) -> dict:
    """Compute percentage of records linked to material cards."""
    coverage = {}
    for model, mpn_col, name in [
        (Requirement, Requirement.primary_mpn, "requirements"),
        (Sighting, Sighting.mpn_matched, "sightings"),
        (Offer, Offer.mpn, "offers"),
    ]:
        total = (
            db.query(func.count(model.id))
            .filter(mpn_col.isnot(None), mpn_col != "")
            .scalar()
        ) or 0
        linked = (
            db.query(func.count(model.id))
            .filter(
                mpn_col.isnot(None),
                mpn_col != "",
                model.material_card_id.isnot(None),
            )
            .scalar()
        ) or 0
        pct = f"{(linked / total * 100):.1f}%" if total > 0 else "N/A"
        coverage[name] = {"total": total, "linked": linked, "pct": pct}
    return coverage
