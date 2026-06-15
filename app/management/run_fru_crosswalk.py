"""Targeted FRU-graph drain CLI (OPTIMIZATION_PLAN §2.6 — zero-network, zero-LLM).

What: exploits the existing FRU-links graph (mfg_model + drive_pn edges) to categorize/
      facet/maker-stamp more cards through the F1 ladder, all deterministically. Two
      phases, each dry-run by DEFAULT (``--apply`` writes):

  PHASE A — targeted drain (``drain``): runs ``crosswalk_and_record_specs`` over the
    EXISTING cards that have a mfg_model/drive_pn FRU link but are still UNFACETED
    (no MaterialSpecFacet rows) or UNCATEGORIZED (category NULL/blank). No targeted
    runner existed before this — the worker only crosswalks whatever happens to be in
    its current batch. Dry-run wraps the writer in a SAVEPOINT and rolls it back, so the
    returned stats are a REAL yield report (the writer's own ladder/savepoint logic runs
    unchanged) with nothing persisted.

  PHASE B — dangling-card creation (``create``): creates MaterialCards (category=None,
    enrichment_status=unenriched) for two dangling populations so the worker's existing
    tier-84 crosswalk / tier-85 mpn_decode passes FIRE on them on the next loop:
      (b1) dangling enrichable FRUs — distinct ``fru_norm`` (mfg_model/drive_pn links)
           with NO card whose linked models DECODE or whose link descriptions EXTRACT;
      (b2) dangling canonical models — distinct ``related_norm`` (mfg_model/drive_pn,
           NEVER lenovo_ppn) with NO card whose ``related_raw`` DECODES to a recognized
           vendor (Seagate/WD/Samsung/Micron/… via the regex-gated decoders).
    The ~30,985 lenovo_ppn danglers are EXPLICITLY OUT OF SCOPE (display-only value;
    OPTIMIZATION_PLAN §5 kill-list) and are never created.

  ``--measure-drive-pn``: the §2.6(c) GATE. Decodes a sample of drive_pn related parts
    and reports the OEM-firmware-suffix MISREAD rate (a decode whose commodity/specs
    contradict the linked qual-sheet description — the description is ground truth for a
    drive_pn row). Widening the decode channel to drive_pn is enabled by default
    (settings.fru_crosswalk_drive_pn_decode_enabled) iff that rate is ≤2%.

Usage:
  python -m app.management.run_fru_crosswalk                      # dry-run BOTH phases
  python -m app.management.run_fru_crosswalk --apply              # write BOTH phases
  python -m app.management.run_fru_crosswalk drain --apply        # drain only
  python -m app.management.run_fru_crosswalk create --apply       # create only
  python -m app.management.run_fru_crosswalk --measure-drive-pn   # §2.6(c) gate report
Called by: admin manually; the deploy orchestrator runs ``--apply`` post-deploy.
Depends on: fru_crosswalk_enrich.crosswalk_and_record_specs (drain), mpn_decoder.decode_mpn
      + desc_extractor.extract_desc (enrichability + the drive_pn misread gate),
      MaterialCard / MaterialSpecFacet / FruLink models, normalize_mpn(_key),
      app.database.SessionLocal.
"""

from __future__ import annotations

import argparse
from collections import Counter

from loguru import logger
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.constants import FruLinkKind, MaterialEnrichmentStatus
from app.models import FruLink, MaterialCard, MaterialSpecFacet
from app.services.desc_extractor import extract_desc
from app.services.fru_crosswalk_enrich import crosswalk_and_record_specs
from app.services.mpn_decoder import decode_mpn
from app.utils.normalization import normalize_mpn, normalize_mpn_key

# Card-creation phase scopes the FRU-graph edges that point at real, enrichable parts:
# mfg_model = manufacturer model / MPN, drive_pn = bare drive PN (qual lists). lenovo_ppn
# is EXPLICITLY excluded (§2.6(b) / §5 kill-list) — its danglers are display-only.
CARD_CREATE_REL_KINDS = (FruLinkKind.MFG_MODEL.value, FruLinkKind.DRIVE_PN.value)

# §2.6(c) gate threshold: drive_pn decode widening stays default-ON iff the measured
# OEM-firmware-suffix misread rate is at or below this.
DRIVE_PN_MISREAD_GATE_PCT = 2.0
DRIVE_PN_MEASURE_SAMPLE = 100


# ──────────────────────────────────────────────────────────────────────────────────
# Shared helpers
# ──────────────────────────────────────────────────────────────────────────────────


def _existing_card_keys(db: Session) -> set[str]:
    """normalize_mpn_key of every active card's normalized_mpn — the "has a card" set.

    The FRU norms (fru_norm / related_norm) are themselves normalize_mpn_key outputs
    (see ingest_fru_matrix), so comparing on this key matches a dangling part to its
    card 1:1 even across raw-vs-canonical spelling.
    """
    keys: set[str] = set()
    for (normalized_mpn,) in db.execute(
        select(MaterialCard.normalized_mpn).where(MaterialCard.deleted_at.is_(None))
    ).yield_per(5000):
        key = normalize_mpn_key(normalized_mpn)
        if key:
            keys.add(key)
    return keys


# ──────────────────────────────────────────────────────────────────────────────────
# Phase A — targeted drain
# ──────────────────────────────────────────────────────────────────────────────────


def select_drain_card_ids(db: Session, *, limit: int = 0) -> list[int]:
    """Active cards with a mfg_model/drive_pn FRU link that are UNFACETED or
    UNCATEGORIZED.

    A card is in scope when its key-normalized MPN equals a ``fru_links.fru_norm`` of an
    in-scope rel_kind AND (it has no MaterialSpecFacet row OR its category is NULL/blank).
    Ordered by id for a deterministic, resumable run. ``limit`` 0 = all.
    """
    fru_norms = set(
        db.execute(select(FruLink.fru_norm.distinct()).where(FruLink.rel_kind.in_(CARD_CREATE_REL_KINDS))).scalars()
    )
    if not fru_norms:
        return []
    has_facet = select(MaterialSpecFacet.id).where(MaterialSpecFacet.material_card_id == MaterialCard.id).exists()
    has_category = func.coalesce(func.trim(MaterialCard.category), "") != ""
    rows = db.execute(
        select(MaterialCard.id, MaterialCard.normalized_mpn, ~has_facet, ~has_category).where(
            MaterialCard.deleted_at.is_(None),
            MaterialCard.is_internal_part.is_(False),
        )
    ).all()
    card_ids: list[int] = []
    for card_id, normalized_mpn, unfaceted, uncategorized in rows:
        if not (unfaceted or uncategorized):
            continue
        if normalize_mpn_key(normalized_mpn) in fru_norms:
            card_ids.append(int(card_id))
    card_ids.sort()
    return card_ids[:limit] if limit else card_ids


def run_drain(db: Session, *, apply: bool = False, limit: int = 0) -> dict:
    """Run the FRU crosswalk over the linked-but-unfaceted/uncategorized cards.

    Dry-run (default) wraps the writer in a SAVEPOINT and rolls it back so the returned
    stats are a REAL yield report (the writer's ladder + per-card savepoints all run) with
    nothing persisted. ``--apply`` keeps the writes and commits. Returns a summary dict.
    """
    card_ids = select_drain_card_ids(db, limit=limit)
    if not card_ids:
        summary: dict[str, object] = {"mode": "apply" if apply else "dry-run", "candidates": 0, "stats": {}}
        logger.info("run-fru-crosswalk drain [{}]: no linked-but-unfaceted/uncategorized cards", summary["mode"])
        return summary

    if apply:
        stats = crosswalk_and_record_specs(db, card_ids)
        db.commit()
    else:
        # Dry-run: the writer flushes via record_spec, so isolate every write in ONE
        # savepoint and roll it back — the returned stats are the true yield, persisted
        # nothing. (begin_nested needs an active transaction; SessionLocal/tests provide one.)
        savepoint = db.begin_nested()
        try:
            stats = crosswalk_and_record_specs(db, card_ids)
        finally:
            savepoint.rollback()

    summary = {"mode": "apply" if apply else "dry-run", "candidates": len(card_ids), "stats": dict(stats)}
    logger.info("run-fru-crosswalk drain [{}]: {}", summary["mode"], summary)
    return summary


# ──────────────────────────────────────────────────────────────────────────────────
# Phase B — dangling-card creation
# ──────────────────────────────────────────────────────────────────────────────────


def _decodes(raw: str | None, manufacturer: str | None) -> bool:
    """True iff ``raw`` decodes to non-empty specs (the part is mpn_decode-
    enrichable)."""
    result = decode_mpn(raw, manufacturer)
    return result is not None and bool(result.specs)


def collect_creatable_cards(db: Session) -> dict[str, dict]:
    """Plan the dangling cards to create, keyed by normalize_mpn_key (deduped, no card
    yet).

    Returns ``{key: {"display_mpn", "manufacturer", "kinds": set, "reason": str}}`` for:
      (b1) dangling ENRICHABLE FRUs — a fru_norm with no card whose linked models DECODE
           or whose link descriptions EXTRACT (so the tier-84 crosswalk pass will fire);
      (b2) dangling CANONICAL models — a related_norm (mfg_model/drive_pn, NOT lenovo_ppn)
           with no card whose related_raw DECODES (so the tier-85 mpn_decode pass fires).
    lenovo_ppn danglers are never collected (the query is scoped to CARD_CREATE_REL_KINDS,
    which excludes it). A part that is both keeps the first reason seen and unions its kinds.
    """
    existing = _existing_card_keys(db)
    links = db.execute(
        select(
            FruLink.fru_norm,
            FruLink.fru_raw,
            FruLink.related_norm,
            FruLink.related_raw,
            FruLink.rel_kind,
            FruLink.manufacturer,
            FruLink.description,
        ).where(FruLink.rel_kind.in_(CARD_CREATE_REL_KINDS))
    ).all()

    # FRU side (b1): gather each fru_norm's decodable models + extractable descriptions.
    fru_raw_by_norm: dict[str, str] = {}
    fru_models: dict[str, list[tuple[str, str | None]]] = {}
    fru_descs: dict[str, list[str]] = {}
    # Related side (b2): the canonical model parts.
    related: dict[str, tuple[str, str | None]] = {}  # related_norm -> (related_raw, manufacturer)
    for fru_norm, fru_raw, related_norm, related_raw, rel_kind, manufacturer, description in links:
        if fru_norm:
            fru_raw_by_norm.setdefault(fru_norm, fru_raw)
            if rel_kind == FruLinkKind.MFG_MODEL.value:
                fru_models.setdefault(fru_norm, []).append((related_raw, manufacturer))
            if description and description.strip():
                fru_descs.setdefault(fru_norm, []).append(description.strip())
        if related_norm and related_norm not in related:
            related[related_norm] = (related_raw, manufacturer)

    plan: dict[str, dict] = {}

    # (b1) dangling enrichable FRUs
    for fru_norm in sorted(set(fru_models) | set(fru_descs)):
        if fru_norm in existing:
            continue
        enrichable = any(_decodes(raw, mfg) for raw, mfg in fru_models.get(fru_norm, [])) or any(
            extract_desc(d) is not None for d in fru_descs.get(fru_norm, [])
        )
        if not enrichable:
            continue
        display = normalize_mpn(fru_raw_by_norm[fru_norm]) or fru_raw_by_norm[fru_norm]
        plan[fru_norm] = {
            "display_mpn": display,
            "manufacturer": None,  # FRU side: maker is set deterministically by the crosswalk pass
            "kinds": {"fru"},
            "reason": "enrichable_fru",
        }

    # (b2) dangling canonical models (decode-enrichable)
    for related_norm in sorted(related):
        if related_norm in existing or related_norm in plan:
            continue
        related_raw, manufacturer = related[related_norm]
        if not _decodes(related_raw, manufacturer):
            continue
        display = normalize_mpn(related_raw) or related_raw
        plan[related_norm] = {
            "display_mpn": display,
            "manufacturer": None,  # mpn_decode writes the deterministic maker on the next loop
            "kinds": {"canonical_model"},
            "reason": "canonical_model",
        }

    return plan


def run_create(db: Session, *, apply: bool = False, limit: int = 0) -> dict:
    """Create cards for the dangling enrichable FRUs + canonical models (skip
    lenovo_ppn).

    Dry-run (default) only counts. ``--apply`` inserts each card (category=None,
    unenriched) with a per-card flush guarded against the unique-MPN race (a concurrent
    ingest may have just created it). Returns a summary dict with per-reason counts.
    """
    plan = collect_creatable_cards(db)
    keys = sorted(plan)
    if limit:
        keys = keys[:limit]

    by_reason: Counter = Counter()
    for key in keys:
        by_reason[plan[key]["reason"]] += 1

    created = 0
    skipped_existing = 0
    if apply:
        for key in keys:
            spec = plan[key]
            card = MaterialCard(
                normalized_mpn=key,
                display_mpn=spec["display_mpn"],
                category=None,
                enrichment_status=MaterialEnrichmentStatus.UNENRICHED.value,
            )
            db.add(card)
            try:
                db.flush()
                created += 1
            except IntegrityError:
                # A concurrent ingest created the same normalized_mpn between the plan and
                # the insert — not an error, just nothing to create.
                db.rollback()
                skipped_existing += 1
        db.commit()

    summary = {
        "mode": "apply" if apply else "dry-run",
        "creatable": len(keys),
        "by_reason": dict(by_reason),
        "created": created,
        "skipped_existing": skipped_existing,
    }
    logger.info("run-fru-crosswalk create [{}]: {}", summary["mode"], summary)
    return summary


# ──────────────────────────────────────────────────────────────────────────────────
# §2.6(c) — drive_pn decode-widening misread gate
# ──────────────────────────────────────────────────────────────────────────────────


def measure_drive_pn_misreads(db: Session, *, sample: int = DRIVE_PN_MEASURE_SAMPLE) -> dict:
    """Measure the OEM-firmware-suffix MISREAD rate of decoding drive_pn related parts.

    For up to *sample* drive_pn links that DECODE (deterministic id order, resumable), a
    decode is a MISREAD when it contradicts the linked qual-sheet description — the
    description is the ground truth for a drive_pn row, which carries rich prose like
    ``18TB 3.5 HDD 7.2K 12 Gb/s SAS``. A contradiction is a different commodity, or a
    shared spec key whose decoded value differs from the description's extracted value.
    A decode with no description to check against is counted as ``unverifiable`` (neither
    pass nor misread — it cannot be a false read either way). The gate is misread / decoded.

    Returns {decoded, misread, unverifiable, misread_pct, gate_pct, passes, sample, scanned}.
    """
    links = db.execute(
        select(FruLink.related_raw, FruLink.manufacturer, FruLink.description)
        .where(FruLink.rel_kind == FruLinkKind.DRIVE_PN.value)
        .order_by(FruLink.id)
    ).all()

    decoded = 0
    misread = 0
    unverifiable = 0
    scanned = 0
    for related_raw, manufacturer, description in links:
        if decoded >= sample:
            break
        scanned += 1
        result = decode_mpn(related_raw, manufacturer)
        if result is None or not result.specs:
            continue
        decoded += 1
        prose = (description or "").strip()
        truth = extract_desc(prose) if prose else None
        if truth is None:
            unverifiable += 1
            continue
        contradicts = result.commodity != truth.commodity or any(
            key in truth.specs and str(truth.specs[key]) != str(value) for key, value in result.specs.items()
        )
        if contradicts:
            misread += 1

    misread_pct = round(100.0 * misread / decoded, 2) if decoded else 0.0
    summary = {
        "decoded": decoded,
        "misread": misread,
        "unverifiable": unverifiable,
        "misread_pct": misread_pct,
        "gate_pct": DRIVE_PN_MISREAD_GATE_PCT,
        "passes": misread_pct <= DRIVE_PN_MISREAD_GATE_PCT,
        "sample": sample,
        "scanned": scanned,
    }
    logger.info("run-fru-crosswalk measure-drive-pn: {}", summary)
    return summary


# ──────────────────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(description="Targeted FRU-graph drain + dangling-card creation (§2.6)")
    parser.add_argument(
        "phase",
        nargs="?",
        choices=("drain", "create", "all"),
        default="all",
        help="Which phase to run (default: all)",
    )
    parser.add_argument("--apply", action="store_true", help="Persist the writes/creations (default: dry-run)")
    parser.add_argument("--limit", type=int, default=0, help="Max cards per phase (0 = all)")
    parser.add_argument(
        "--measure-drive-pn",
        action="store_true",
        help="Only report the §2.6(c) drive_pn decode-widening misread rate; no writes",
    )
    args = parser.parse_args()

    from app.database import SessionLocal

    db = SessionLocal()
    try:
        if args.measure_drive_pn:
            measure_drive_pn_misreads(db)
            db.rollback()
            return
        if args.apply:
            logger.warning(
                "run-fru-crosswalk: --apply will write specs/categories/manufacturers via the F1 ladder "
                "and CREATE dangling cards — ensure a recent db-backup exists (pg_dump every 6h; "
                "scripts/restore.sh to roll back)"
            )
        if args.phase in ("drain", "all"):
            run_drain(db, apply=args.apply, limit=args.limit)
        if args.phase in ("create", "all"):
            run_create(db, apply=args.apply, limit=args.limit)
        if not args.apply:
            db.rollback()  # belt-and-braces: dry-run leaves no writes behind
    finally:
        db.close()


if __name__ == "__main__":
    main()
