"""Reconcile mpn_decode / desc_parse facet rows against the FIXED deterministic
extractors.

What: facet-accuracy hotfix companion (audit 2026-06-10). Re-runs the corrected MPN
      decoder and description extractor over every card owning a facet row with
      source IN (mpn_decode, desc_parse) for the audit-affected spec_keys
      (capacity_gb / gpu_family / memory_gb) and reconciles the stored value:
        * fixed extractor yields a DIFFERENT value  -> record_spec the corrected
          value (same source — the F1 ladder's newest-timestamp tie-break lets the
          re-run win over the stale same-tier entry);
        * fixed extractor yields NOTHING for a previously-recorded key -> DELETE the
          facet row and its specs_structured entry (the old value was a misdecode —
          wrong is worse than missing, provenance stays honest).
      Dry-run by default with per-failure-class tallies (legacy_wd / legacy_seagate /
      stmicro_gate / gb_bit / rtx_family); --apply writes. SAVEPOINT per card so one
      bad card never poisons the batch.
Usage: python -m app.management.reconcile_decoded_facets [--apply] [--limit N]
Called by: admin manually after deploying the facet-accuracy hotfix.
Depends on: mpn_decoder.decode_mpn, desc_extractor.extract_desc,
      spec_write_service.record_spec/spec_would_write/load_schema_cache,
      MaterialCard + MaterialSpecFacet, normalize_mpn.
"""

import argparse
import re
from collections import Counter, defaultdict

from loguru import logger
from sqlalchemy.orm import Session

from app.models import MaterialCard, MaterialSpecFacet
from app.services.desc_extractor import extract_desc
from app.services.desc_extractor._common import DESC_CONFIDENCE, DESC_SOURCE, SPEC_COMMODITIES
from app.services.mpn_decoder import decode_mpn
from app.services.mpn_decoder._common import DECODE_CONFIDENCE, DECODE_SOURCE
from app.services.mpn_decoder.storage import _STMICRO_DENY
from app.services.spec_write_service import load_schema_cache, record_spec, spec_would_write
from app.utils.normalization import normalize_mpn

# The audit's wrong rows live in these (source, spec_key) cells: legacy WD/Seagate +
# STMicro misdecodes (mpn_decode × capacity_gb), Gb-vs-GB bit coercion (desc_parse ×
# capacity_gb, and the same reader fix touches gpu memory_gb), and RTX family
# fragmentation (desc_parse × gpu_family).
TARGET_SOURCES = (DECODE_SOURCE, DESC_SOURCE)
TARGET_SPEC_KEYS = ("capacity_gb", "gpu_family", "memory_gb")

_CONFIDENCE = {DECODE_SOURCE: DECODE_CONFIDENCE, DESC_SOURCE: DESC_CONFIDENCE}

# Raw-casing bit token ("2Gb", "512Mb") — classification only (the extractor fix
# neutralizes these before upper-casing; see desc_extractor._BIT_UNITS).
_BIT_TOKEN = re.compile(r"\b\d+(?:\.\d+)?\s?[KMGT]b(?![A-Za-z])")


def _classify(facet: MaterialSpecFacet, card: MaterialCard) -> str:
    """Audit failure-class bucket for a targeted facet row (tally key, not behavior)."""
    if facet.source == DECODE_SOURCE:
        mpn = normalize_mpn(card.display_mpn) or ""
        if _STMICRO_DENY.match(mpn):
            return "stmicro_gate"
        if mpn.startswith("WD"):
            return "legacy_wd"
        if mpn.startswith("ST"):
            return "legacy_seagate"
        return "other_mpn_decode"
    if facet.spec_key == "gpu_family":
        return "rtx_family"
    if _BIT_TOKEN.search(card.description or ""):
        return "gb_bit"
    return "other_desc_parse"


def _recompute(card: MaterialCard, sources: set[str]) -> dict[str, dict]:
    """Fixed-extractor output per source for *card* — {} means "yields nothing now".

    Mirrors the writers' own gates exactly: the decode contributes only when its
    commodity matches the card's category (mpn_decoder/writer.py's cross-commodity
    guard), and desc extraction only runs for a non-empty description on a
    SPEC_COMMODITIES card (desc_extractor/writer.py).
    """
    category = (card.category or "").lower().strip()
    out: dict[str, dict] = {}
    if DECODE_SOURCE in sources:
        result = decode_mpn(card.display_mpn, card.manufacturer)
        out[DECODE_SOURCE] = dict(result.specs) if result is not None and result.commodity == category else {}
    if DESC_SOURCE in sources:
        specs: dict = {}
        description = (card.description or "").strip()
        if description and category in SPEC_COMMODITIES:
            desc_result = extract_desc(description, commodity_hint=category)
            if desc_result is not None:
                specs = dict(desc_result.specs)
        out[DESC_SOURCE] = specs
    return out


def _facet_matches(facet: MaterialSpecFacet, schema, new_value) -> bool:
    """Does the stored facet projection already equal the fixed extractor's value?"""
    if schema is not None and schema.data_type == "numeric":
        if facet.value_numeric is None:
            return False
        try:
            return abs(float(new_value) - float(facet.value_numeric)) < 1e-9
        except (TypeError, ValueError):
            return False
    if schema is not None and schema.data_type == "boolean":
        return facet.value_text == ("true" if new_value else "false")
    return facet.value_text == str(new_value)


def reconcile(db: Session, *, apply: bool = False, limit: int = 0) -> dict:
    """Reconcile the targeted facet rows; returns the tally dict (see module docstring).

    Dry-run (default) classifies every row through the exact gates apply-mode uses
    (spec_would_write is record_spec's read-only twin) and writes NOTHING; --apply
    mutates inside a per-card SAVEPOINT and commits at the end.
    """
    rows = (
        db.query(MaterialSpecFacet)
        .filter(
            MaterialSpecFacet.source.in_(TARGET_SOURCES),
            MaterialSpecFacet.spec_key.in_(TARGET_SPEC_KEYS),
        )
        .order_by(MaterialSpecFacet.material_card_id, MaterialSpecFacet.spec_key)
        .all()
    )
    by_card: dict[int, list[MaterialSpecFacet]] = defaultdict(list)
    for row in rows:
        by_card[row.material_card_id].append(row)

    card_ids = sorted(by_card)
    if limit:
        card_ids = card_ids[:limit]

    tallies: dict[str, Counter] = defaultdict(Counter)
    totals: Counter = Counter()
    schema_caches: dict[str, dict] = {}

    for card_id in card_ids:
        facets = by_card[card_id]
        try:
            card = db.get(MaterialCard, card_id)
            if card is None:
                totals["skipped"] += len(facets)
                continue
            category = (card.category or "").lower().strip()
            cache = schema_caches.get(category)
            if cache is None and category:
                cache = schema_caches[category] = load_schema_cache(db, category)
            recomputed = _recompute(card, {f.source for f in facets})

            # Per-card SAVEPOINT (apply mode): record_spec flushes, so a DB-level
            # failure would otherwise poison the shared transaction for every later
            # card. Dry-run opens none — it never writes.
            ctx = db.begin_nested() if apply else None
            try:
                for facet in facets:
                    klass = _classify(facet, card)
                    new_value = recomputed.get(facet.source, {}).get(facet.spec_key)
                    schema = (cache or {}).get((category, facet.spec_key))
                    if new_value is None:
                        # The fixed extractor yields nothing for this key — the stored
                        # value is a stale misdecode. Delete facet + JSONB entry, but
                        # only when the JSONB provenance still agrees with the facet's
                        # (they mirror by construction; a mismatch means drift — skip).
                        specs = dict(card.specs_structured or {})
                        entry = specs.get(facet.spec_key)
                        if entry is not None and entry.get("source") != facet.source:
                            tallies[klass]["skipped_provenance_mismatch"] += 1
                            totals["skipped"] += 1
                            continue
                        if apply:
                            specs.pop(facet.spec_key, None)
                            card.specs_structured = specs
                            db.delete(facet)
                        tallies[klass]["deleted"] += 1
                        totals["deleted"] += 1
                    elif _facet_matches(facet, schema, new_value):
                        tallies[klass]["unchanged"] += 1
                        totals["unchanged"] += 1
                    else:
                        confidence = _CONFIDENCE[facet.source]
                        if apply:
                            written = record_spec(
                                db,
                                card_id,
                                facet.spec_key,
                                new_value,
                                source=facet.source,
                                confidence=confidence,
                                schema_cache=cache,
                            )
                        else:
                            written = spec_would_write(
                                db,
                                category=category,
                                existing_specs=card.specs_structured,
                                spec_key=facet.spec_key,
                                value=new_value,
                                source=facet.source,
                                confidence=confidence,
                                schema_cache=cache,
                            )
                        action = "corrected" if written else "skipped_ladder"
                        tallies[klass][action] += 1
                        totals["corrected" if written else "skipped"] += 1
                if ctx is not None:
                    ctx.commit()
            except BaseException:
                if ctx is not None:
                    ctx.rollback()
                raise
            totals["cards"] += 1
        except Exception:
            totals["failed"] += 1
            logger.exception("reconcile-decoded-facets: failed on card_id={}", card_id)

    if apply:
        db.commit()

    summary = {
        "mode": "apply" if apply else "dry-run",
        "cards": totals["cards"],
        "facets": sum(len(by_card[c]) for c in card_ids),
        "corrected": totals["corrected"],
        "deleted": totals["deleted"],
        "unchanged": totals["unchanged"],
        "skipped": totals["skipped"],
        "failed": totals["failed"],
        "by_class": {klass: dict(counter) for klass, counter in sorted(tallies.items())},
    }
    logger.info("reconcile-decoded-facets [{}]: {}", summary["mode"], summary)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Reconcile mpn_decode/desc_parse facets against the fixed extractors")
    parser.add_argument("--apply", action="store_true", help="Write the corrections/deletes (default: dry-run)")
    parser.add_argument("--limit", type=int, default=0, help="Max cards to process (0 = all)")
    args = parser.parse_args()

    from app.database import SessionLocal

    db = SessionLocal()
    try:
        if args.apply:
            logger.warning(
                "reconcile-decoded-facets: --apply will rewrite/DELETE mpn_decode/desc_parse facet rows "
                "for spec_keys {} — ensure a recent db-backup exists (pg_dump runs every 6h; "
                "scripts/restore.sh to roll back)",
                TARGET_SPEC_KEYS,
            )
        reconcile(db, apply=args.apply, limit=args.limit)
        if not args.apply:
            db.rollback()  # belt-and-braces: dry-run leaves no writes behind
    finally:
        db.close()


if __name__ == "__main__":
    main()
