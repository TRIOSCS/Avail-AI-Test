"""Dual-brand backfill — populate brand (OEM label) + manufacturer (actual maker).

Usage: python -m app.management.backfill_dual_brand [--apply]

DRY-RUN by DEFAULT: prints the same tally --apply would write (the simulation runs the
SAME ladder gates via the write=False twins, with an overlay mirroring apply-mode's
sequential writes so a B1 win is visible to B3's compare — dry-run tallies cannot drift
from --apply). Four ordered passes, SAVEPOINT-per-card in apply mode (mirroring
source_ingest/ingest.py — one bad card never poisons the batch, and a rolled-back card
contributes nothing to the tallies):

  B1 — reclassify legacy OEM-in-manufacturer: manufacturer (lower) ∈ OEM_BRANDS →
       set_brand(card, manufacturer_value, "legacy_backfill", 0.5). manufacturer is NOT
       cleared (lossless — the combined facet ORs both columns, so behavior is unchanged
       until real maker evidence displaces it via the ladder in B2/W4).
  B2 — maker from TRIO master: fru_links rows (rel_kind='mfg_model', manufacturer set,
       material_cards.normalized_mpn = fru_links.related_norm) →
       set_manufacturer(card, fru.manufacturer, "trio_source", 0.9). Tier 95 beats the
       legacy-50 OEM value. Duplicate mfg_model rows apply in deterministic fru_links.id
       order — the ladder tie-break (equal tier/conf, newer ts) keeps the last.
  B3 — trailing-description tokens: OEM_TRAILING_RE → set_brand ("desc_parse", 0.85);
       MAKER_TRAILING_RE → set_manufacturer ("desc_parse", 0.85). Regex-gated to the
       literal lists only; anything else is never written.
  B4 — verification report: prints the known dual-coverage cards with final
       brand/manufacturer + provenance; exits non-zero unless ST300MP0016 ends
       brand=IBM ∧ manufacturer=Seagate Technology.

Called by: an operator (manually, post-deploy of migration 097 — NOT at startup).
Depends on: app.database.SessionLocal, spec_tiers.set_brand/set_manufacturer/resolve/
      tier_for, manufacturer_normalizer (OEM_BRANDS + trailing regexes +
      normalize_brand_name), MaterialCard, FruLink, constants.FruLinkKind.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone

from loguru import logger
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.constants import FruLinkKind
from app.models import FruLink, MaterialCard
from app.services.manufacturer_normalizer import (
    MAKER_TRAILING_RE,
    OEM_BRANDS,
    OEM_TRAILING_RE,
    normalize_brand_name,
)
from app.services.spec_tiers import resolve, set_brand, set_manufacturer, tier_for
from app.utils.normalization import normalize_mpn_key

_COMMIT_CHUNK = 500

# Known dual-coverage cards (brand evidence in the description AND maker evidence in
# fru_links) from the source_ingest analysis — printed in the B4 report when present.
# The first entry is the GATE: the command exits non-zero unless it ends
# brand=IBM ∧ manufacturer=Seagate Technology.
_GATE_MPN = "ST300MP0016"
_GATE_BRAND = "IBM"
_GATE_MANUFACTURER = "Seagate Technology"
_VERIFICATION_MPNS = [_GATE_MPN, "SSDSC2BW180A3L", "HTS721080G9AT00"]

# Overlay key: (card_id, attr) → simulated winning provenance + normalized value.
# Dry-run only — mirrors apply-mode's sequential writes so a pass-B1 win is the
# "existing" for pass-B3's ladder compare (same pattern as ingest.py's spec overlay).
_Overlay = dict[tuple[int, str], dict]


def _ladder_set(
    db: Session,
    card: MaterialCard,
    attr: str,
    value: str | None,
    source: str,
    confidence: float,
    *,
    apply: bool,
    overlay: _Overlay,
) -> bool:
    """Apply (or, dry-run, simulate) one ladder-gated brand/manufacturer write."""
    if value is None or not str(value).strip():
        return False
    setter = set_brand if attr == "brand" else set_manufacturer
    if apply:
        return setter(card, value, source, confidence)

    key = (card.id, attr)
    incoming = {
        "tier": tier_for(source),
        "confidence": min(max(float(confidence or 0.0), 0.0), 1.0),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    prior = overlay.get(key)
    if prior is None:
        # First touch: the write=False twin runs the full ladder against the card's
        # REAL columns (incl. the legacy-NULL-provenance floor).
        won = setter(card, value, source, confidence, write=False)
    else:
        # A previous pass already simulated a win — compare against THAT, exactly as
        # apply mode would compare against the written provenance.
        won = resolve(prior, incoming)
    if won:
        overlay[key] = {**incoming, "value": normalize_brand_name(db, str(value))}
    return won


def _pass_b1(db: Session, *, apply: bool, overlay: _Overlay) -> dict:
    """B1 — copy a legacy OEM label out of `manufacturer` into `brand` (lossless)."""
    tally = {"scanned": 0, "brands_set": 0, "skipped": 0, "failed": 0}
    cards = (
        db.query(MaterialCard)
        .filter(func.lower(MaterialCard.manufacturer).in_(sorted(OEM_BRANDS)))
        .order_by(MaterialCard.id)
        .all()
    )
    for card in cards:
        tally["scanned"] += 1
        try:
            if apply:
                with db.begin_nested():
                    won = _ladder_set(
                        db, card, "brand", card.manufacturer, "legacy_backfill", 0.5, apply=True, overlay=overlay
                    )
            else:
                won = _ladder_set(
                    db, card, "brand", card.manufacturer, "legacy_backfill", 0.5, apply=False, overlay=overlay
                )
        except Exception:
            tally["failed"] += 1
            logger.exception("backfill_dual_brand B1: failed on card id={} — skipping", card.id)
            continue
        tally["brands_set" if won else "skipped"] += 1
        if apply and won and tally["brands_set"] % _COMMIT_CHUNK == 0:
            db.commit()
    if apply:
        db.commit()
    return tally


def _pass_b2(db: Session, *, apply: bool, overlay: _Overlay) -> dict:
    """B2 — maker from TRIO master (fru_links mfg_model rows), trio_source/0.9."""
    tally = {"links_scanned": 0, "manufacturers_set": 0, "skipped": 0, "failed": 0}
    rows = (
        db.query(FruLink, MaterialCard)
        .join(MaterialCard, MaterialCard.normalized_mpn == FruLink.related_norm)
        .filter(
            FruLink.rel_kind == FruLinkKind.MFG_MODEL.value,
            FruLink.manufacturer.isnot(None),
            FruLink.manufacturer != "",
        )
        .order_by(FruLink.id)  # deterministic; ladder tie-break keeps the last duplicate
        .all()
    )
    for link, card in rows:
        tally["links_scanned"] += 1
        try:
            if apply:
                with db.begin_nested():
                    won = _ladder_set(
                        db, card, "manufacturer", link.manufacturer, "trio_source", 0.9, apply=True, overlay=overlay
                    )
            else:
                won = _ladder_set(
                    db, card, "manufacturer", link.manufacturer, "trio_source", 0.9, apply=False, overlay=overlay
                )
        except Exception:
            tally["failed"] += 1
            logger.exception("backfill_dual_brand B2: failed on card id={} (fru_link id={})", card.id, link.id)
            continue
        tally["manufacturers_set" if won else "skipped"] += 1
        if apply and won and tally["manufacturers_set"] % _COMMIT_CHUNK == 0:
            db.commit()
    if apply:
        db.commit()
    return tally


def _pass_b3(db: Session, *, apply: bool, overlay: _Overlay) -> dict:
    """B3 — trailing-description tokens: OEM list → brand, maker list → manufacturer."""
    tally = {"scanned": 0, "brands_set": 0, "manufacturers_set": 0, "skipped": 0, "failed": 0}
    # Two-step to keep the scan cheap and the mutation isolated: stream (id, description)
    # tuples, regex-gate in Python (portable across PG/SQLite), then load matching cards.
    matches: list[tuple[int, str, str]] = []  # (card_id, attr, token)
    rows = (
        db.query(MaterialCard.id, MaterialCard.description)
        .filter(MaterialCard.description.isnot(None), MaterialCard.description.like("%,%"))
        .yield_per(1000)
    )
    for card_id, description in rows:
        oem = OEM_TRAILING_RE.search(description)
        if oem:
            matches.append((card_id, "brand", oem.group(1)))
            continue  # the trailing anchor means at most ONE of the two can match
        maker = MAKER_TRAILING_RE.search(description)
        if maker:
            matches.append((card_id, "manufacturer", maker.group(1)))

    written = 0
    for card_id, attr, token in matches:
        tally["scanned"] += 1
        card = db.get(MaterialCard, card_id)
        if card is None:
            continue
        try:
            if apply:
                with db.begin_nested():
                    won = _ladder_set(db, card, attr, token, "desc_parse", 0.85, apply=True, overlay=overlay)
            else:
                won = _ladder_set(db, card, attr, token, "desc_parse", 0.85, apply=False, overlay=overlay)
        except Exception:
            tally["failed"] += 1
            logger.exception("backfill_dual_brand B3: failed on card id={} — skipping", card_id)
            continue
        if won:
            tally["brands_set" if attr == "brand" else "manufacturers_set"] += 1
            written += 1
            if apply and written % _COMMIT_CHUNK == 0:
                db.commit()
        else:
            tally["skipped"] += 1
    if apply:
        db.commit()
    return tally


def _final_state(db: Session, card: MaterialCard, attr: str, *, apply: bool, overlay: _Overlay) -> tuple:
    """(value, source, tier) a card ends with — overlay-aware so the dry-run report
    shows the post---apply state."""
    if not apply:
        entry = overlay.get((card.id, attr))
        if entry is not None:
            # Simulated win: source is implied by the tier; report the overlay value.
            return entry["value"], f"(simulated, tier {entry['tier']})", entry["tier"]
    return getattr(card, attr), getattr(card, f"{attr}_source"), getattr(card, f"{attr}_tier")


def _pass_b4(db: Session, *, apply: bool, overlay: _Overlay) -> bool:
    """B4 — verification report + the ST300MP0016 gate.

    Returns True iff the gate passes.
    """
    gate_passed = False
    for mpn in _VERIFICATION_MPNS:
        norm = normalize_mpn_key(mpn)
        card = db.query(MaterialCard).filter(MaterialCard.normalized_mpn == norm).first()
        if card is None:
            logger.warning("backfill_dual_brand B4: verification card {} not in DB", mpn)
            continue
        brand, brand_src, brand_tier = _final_state(db, card, "brand", apply=apply, overlay=overlay)
        mfr, mfr_src, mfr_tier = _final_state(db, card, "manufacturer", apply=apply, overlay=overlay)
        logger.info(
            "backfill_dual_brand B4: {} → brand={!r} ({} / tier {}) · manufacturer={!r} ({} / tier {})",
            mpn,
            brand,
            brand_src,
            brand_tier,
            mfr,
            mfr_src,
            mfr_tier,
        )
        if mpn == _GATE_MPN and brand == _GATE_BRAND and mfr == _GATE_MANUFACTURER:
            gate_passed = True
    if not gate_passed:
        logger.error(
            "backfill_dual_brand B4 GATE FAILED: {} must end brand={!r} ∧ manufacturer={!r}",
            _GATE_MPN,
            _GATE_BRAND,
            _GATE_MANUFACTURER,
        )
    return gate_passed


def run_backfill(db: Session, *, apply: bool) -> dict:
    """Run B1→B2→B3→B4.

    Returns the full tally dict (incl. ``gate_passed``).
    """
    overlay: _Overlay = {}
    mode = "APPLY" if apply else "DRY-RUN (no writes — pass --apply to write)"
    logger.info("backfill_dual_brand: starting in {} mode", mode)
    stats = {
        "apply": apply,
        "b1": _pass_b1(db, apply=apply, overlay=overlay),
        "b2": _pass_b2(db, apply=apply, overlay=overlay),
        "b3": _pass_b3(db, apply=apply, overlay=overlay),
    }
    stats["gate_passed"] = _pass_b4(db, apply=apply, overlay=overlay)
    logger.info(
        "backfill_dual_brand [{}]: B1 {} · B2 {} · B3 {} · gate_passed={}",
        mode,
        stats["b1"],
        stats["b2"],
        stats["b3"],
        stats["gate_passed"],
    )
    return stats


def main(argv: list[str] | None = None) -> int:
    """CLI entry point.

    Returns the process exit code (non-zero on gate failure).
    """
    parser = argparse.ArgumentParser(description="Backfill dual-brand columns (dry-run by default).")
    parser.add_argument("--apply", action="store_true", help="Write the backfill (default: dry-run, no writes)")
    args = parser.parse_args(argv)

    from app.database import SessionLocal

    db = SessionLocal()
    try:
        stats = run_backfill(db, apply=args.apply)
    finally:
        db.close()
    return 0 if stats["gate_passed"] else 2


if __name__ == "__main__":
    sys.exit(main())
