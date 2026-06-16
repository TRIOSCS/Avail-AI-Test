"""Reconcile deterministic facet rows against the FIXED extractors + plausibility gates.

What: generalized facet-accuracy reconciler (trust architecture, 2026-06-12;
      originally the 2026-06-10 audit hotfix companion). Scope is selectable:
      ``--sources`` (default: ALL deterministic facet sources — mpn_decode,
      desc_parse, fru_matrix_decode, fru_desc_parse) × ``--keys`` (default: every
      spec_key present in commodity_spec_schemas). Per row:
        * mpn_decode / desc_parse — re-runs the corrected MPN decoder / description
          extractor and reconciles the stored value: a DIFFERENT value ->
          record_spec the correction (same source — the F1 ladder's
          newest-timestamp tie-break lets the re-run win over the stale same-tier
          entry); NOTHING for a previously-recorded key -> DELETE the facet row and
          its specs_structured entry (the old value was a misdecode — wrong is
          worse than missing, provenance stays honest).
        * fru_matrix_decode / fru_desc_parse — no deterministic recompute channel
          exists (the crosswalk depends on fru_links workbook state, not the card
          alone), so these rows ride a capacity PLAUSIBILITY-GRID gate instead: an
          hdd capacity_gb off the shipped-capacity grid
          (mpn_decoder.storage.HDD_SHIPPED_CAPACITY_GB — HDD capacities are a
          discrete vendor vocabulary, see that module) is a misread -> DELETE;
          on-grid -> unchanged; everything else is tally-only (skipped_ungated —
          the grid is deliberately HDD-only, SSD/DRAM capacities are
          near-continuous).
      Dry-run by default with per-failure-class tallies — round 1: legacy_wd /
      legacy_seagate / stmicro_gate / gb_bit / rtx_family; round 2 (re-audit
      2026-06-10): wd_revision_digit (modern WD final-digit revision markers read as
      tenths-of-TB), capacity_grid (hdd capacity the decoder now drops as off the
      shipped-capacity grid), seagate_envelope (modern structured-tail Seagate shapes,
      now per-family-envelope gated), nand_density (bare-"G" gigaBIT die densities
      recorded as GB); fru gate: fru_capacity_grid / fru_ungated. --apply writes.
      SAVEPOINT per card so one bad card never poisons the batch. Every run's tallies
      (dry-run AND apply) persist to the reconcile_runs table via
      ``record_reconcile_run`` — the first two rounds' tallies were log-only and are
      unrecoverable.
Usage: python -m app.management.reconcile_decoded_facets [--apply] [--limit N]
      [--sources csv] [--keys csv]
Called by: admin manually after extractor fixes / on the trust-review cadence.
Depends on: mpn_decoder.decode_mpn (+ storage.HDD_SHIPPED_CAPACITY_GB),
      desc_extractor.extract_desc, fru_crosswalk_enrich source tags,
      spec_write_service.record_spec/spec_would_write/load_schema_cache,
      MaterialCard + MaterialSpecFacet + ReconcileRun (+ CommoditySpecSchema for the
      default key set), normalize_mpn.
"""

import argparse
import re
from collections import Counter, defaultdict
from collections.abc import Sequence

from loguru import logger
from sqlalchemy.orm import Session

from app.models import CommoditySpecSchema, MaterialCard, MaterialSpecFacet, ReconcileRun
from app.services.desc_extractor import extract_desc
from app.services.desc_extractor._common import DESC_CONFIDENCE, DESC_SOURCE, SPEC_COMMODITIES, nand_die_context
from app.services.fru_crosswalk_enrich import FRU_DECODE_SOURCE, FRU_DESC_SOURCE
from app.services.mpn_decoder import decode_mpn
from app.services.mpn_decoder._common import DECODE_CONFIDENCE, DECODE_SOURCE, DROP_OUT_OF_ENVELOPE
from app.services.mpn_decoder.storage import _SEAGATE, _STMICRO_DENY, _WD_MODERN, HDD_SHIPPED_CAPACITY_GB
from app.services.spec_write_service import load_schema_cache, record_spec, spec_would_write
from app.utils.normalization import normalize_mpn

# Every deterministic facet source the reconciler knows how to handle. mpn_decode /
# desc_parse rows are RECOMPUTED against the fixed extractors; the fru sources have no
# card-local recompute channel and ride the capacity plausibility-grid gate instead.
DEFAULT_SOURCES = (DECODE_SOURCE, DESC_SOURCE, FRU_DECODE_SOURCE, FRU_DESC_SOURCE)
_RECOMPUTE_SOURCES = frozenset({DECODE_SOURCE, DESC_SOURCE})
_FRU_SOURCES = frozenset({FRU_DECODE_SOURCE, FRU_DESC_SOURCE})

_CONFIDENCE = {DECODE_SOURCE: DECODE_CONFIDENCE, DESC_SOURCE: DESC_CONFIDENCE}

# Raw-casing bit token ("2Gb", "512Mb") — classification only (the extractor fix
# neutralizes these before upper-casing; see desc_extractor._BIT_UNITS).
_BIT_TOKEN = re.compile(r"\b\d+(?:\.\d+)?\s?[KMGT]b(?![A-Za-z])")


def _classify(facet: MaterialSpecFacet, card: MaterialCard) -> str:
    """Audit failure-class bucket for a targeted facet row (tally key, not behavior).

    Round-2 buckets (re-audit 2026-06-10): capacity_grid / seagate_envelope via the
    decoder's own dropped channel when it carries the key — checked first, it is the
    most specific signal, and DecodeResult.drop_reasons splits the two gates so a grid-
    emptied capacity-only decode (legacy WD, family-unmapped Seagate) tallies as
    capacity_grid and an envelope rejection as seagate_envelope, never misattributed to
    a shape-regex bucket. wd_revision_digit / seagate_envelope also cover rows in the
    modern WD / modern Seagate grammar branch that the round-2 fixes re-gated (the
    legacy_* buckets keep covering the round-1 legacy shapes), and nand_density covers
    bare-"G" gigaBIT die densities on a capacity_gb row.
    """
    if facet.source == DECODE_SOURCE:
        mpn = normalize_mpn(card.display_mpn) or ""
        if _STMICRO_DENY.match(mpn):
            return "stmicro_gate"
        result = decode_mpn(card.display_mpn, card.manufacturer)
        if result is not None and facet.spec_key in result.dropped:
            if result.drop_reasons.get(facet.spec_key) == DROP_OUT_OF_ENVELOPE:
                return "seagate_envelope"
            return "capacity_grid"
        if mpn.startswith("WD"):
            return "wd_revision_digit" if _WD_MODERN.match(mpn) else "legacy_wd"
        if mpn.startswith("ST"):
            return "seagate_envelope" if _SEAGATE.match(mpn) else "legacy_seagate"
        return "other_mpn_decode"
    if facet.spec_key == "gpu_family":
        return "rtx_family"
    description = re.sub(r"\s+", " ", card.description or "").strip().upper()
    if facet.spec_key == "capacity_gb" and nand_die_context(description):
        return "nand_density"
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


def _all_schema_keys(db: Session) -> tuple[str, ...]:
    """Every spec_key present in commodity_spec_schemas — the ``--keys`` default."""
    rows = db.query(CommoditySpecSchema.spec_key).distinct().order_by(CommoditySpecSchema.spec_key).all()
    return tuple(key for (key,) in rows)


def _delete_facet_row(db: Session, card: MaterialCard, facet: MaterialSpecFacet, *, apply: bool) -> str:
    """Delete *facet* + its specs_structured mirror (apply mode); returns the action.

    Guard shared by every delete branch: the JSONB entry and the facet row mirror by
    construction, so a provenance mismatch between them means drift — skip rather
    than delete data some other source now owns.
    """
    specs = dict(card.specs_structured or {})
    entry = specs.get(facet.spec_key)
    if entry is not None and entry.get("source") != facet.source:
        return "skipped_provenance_mismatch"
    if apply:
        specs.pop(facet.spec_key, None)
        card.specs_structured = specs
        db.delete(facet)
    return "deleted"


def _gate_fru_facet(
    db: Session, card: MaterialCard, facet: MaterialSpecFacet, category: str, *, apply: bool
) -> tuple[str, str]:
    """Plausibility gate for a fru_matrix_decode / fru_desc_parse row → (class, action).

    No recompute channel exists for the fru sources (the crosswalk depends on
    fru_links workbook state, not the card alone), so the only deterministic check is
    the shipped-capacity grid: an hdd capacity_gb off HDD_SHIPPED_CAPACITY_GB is a
    misread (the documented 373,455 GB class) — wrong is worse than missing → delete.
    The grid is deliberately HDD-only (SSD/DRAM capacities are near-continuous, see
    mpn_decoder/storage.py), and non-capacity keys have no plausibility vocabulary —
    those rows are tally-only (``fru_ungated`` / ``skipped_ungated``) so coverage
    gaps stay visible in the persisted run report instead of silently passing.
    """
    if facet.spec_key == "capacity_gb" and category == "hdd":
        try:
            on_grid = float(facet.value_numeric) in HDD_SHIPPED_CAPACITY_GB
        except (TypeError, ValueError):
            on_grid = False
        if on_grid:
            return "fru_capacity_grid", "unchanged"
        return "fru_capacity_grid", _delete_facet_row(db, card, facet, apply=apply)
    return "fru_ungated", "skipped_ungated"


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


def reconcile(
    db: Session,
    *,
    apply: bool = False,
    limit: int = 0,
    sources: Sequence[str] | None = None,
    keys: Sequence[str] | None = None,
) -> dict:
    """Reconcile the targeted facet rows; returns the tally dict (see module docstring).

    *sources* / *keys* default to ALL deterministic sources / every schema'd spec_key
    (an unknown source raises — the reconciler only knows the four deterministic
    channels). Dry-run (default) classifies every row through the exact gates
    apply-mode uses (spec_would_write is record_spec's read-only twin) and writes
    NOTHING; --apply mutates inside a per-card SAVEPOINT and commits at the end. The
    caller persists the returned summary via ``record_reconcile_run`` (main() does).
    """
    sources = tuple(sources) if sources else DEFAULT_SOURCES
    unknown = sorted(set(sources) - set(DEFAULT_SOURCES))
    if unknown:
        raise ValueError(f"reconcile: unsupported source(s) {unknown} — supported: {list(DEFAULT_SOURCES)}")
    keys = tuple(keys) if keys else _all_schema_keys(db)

    rows = (
        db.query(MaterialSpecFacet)
        .filter(
            MaterialSpecFacet.source.in_(sources),
            MaterialSpecFacet.spec_key.in_(keys),
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
            recomputed = _recompute(card, {f.source for f in facets} & _RECOMPUTE_SOURCES)

            # Per-card SAVEPOINT (apply mode): record_spec flushes, so a DB-level
            # failure would otherwise poison the shared transaction for every later
            # card. Dry-run opens none — it never writes. Tallies are BUFFERED and
            # merged only after a clean release, so a mid-card failure (savepoint
            # rolled back) can never leave the counters claiming work that was
            # undone — same honesty rule as mpn_decoder/writer.py.
            pending: list[tuple[str, str]] = []  # (class, action) per facet
            ctx = db.begin_nested() if apply else None
            try:
                for facet in facets:
                    if facet.source in _FRU_SOURCES:
                        # No recompute channel — plausibility-grid gate only.
                        pending.append(_gate_fru_facet(db, card, facet, category, apply=apply))
                        continue
                    klass = _classify(facet, card)
                    new_value = recomputed.get(facet.source, {}).get(facet.spec_key)
                    schema = (cache or {}).get((category, facet.spec_key))
                    if new_value is None:
                        # The fixed extractor yields nothing for this key — the stored
                        # value is a stale misdecode. Delete facet + JSONB entry, but
                        # only when the JSONB provenance still agrees with the facet's
                        # (they mirror by construction; a mismatch means drift — skip).
                        pending.append((klass, _delete_facet_row(db, card, facet, apply=apply)))
                    elif _facet_matches(facet, schema, new_value):
                        pending.append((klass, "unchanged"))
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
                        pending.append((klass, "corrected" if written else "skipped_ladder"))
                if ctx is not None:
                    ctx.commit()
            except BaseException:
                if ctx is not None:
                    ctx.rollback()
                raise
            # Reached only on a clean savepoint release (or dry-run) — merge the buffer.
            for klass, action in pending:
                tallies[klass][action] += 1
                totals[action if action in ("corrected", "deleted", "unchanged") else "skipped"] += 1
            totals["cards"] += 1
        except Exception:
            totals["failed"] += 1
            logger.exception("reconcile-decoded-facets: failed on card_id={}", card_id)

    if apply:
        db.commit()

    summary = {
        "mode": "apply" if apply else "dry-run",
        "sources": list(sources),
        "keys": list(keys),
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


def record_reconcile_run(db: Session, summary: dict) -> ReconcileRun:
    """Persist a reconcile *summary* to the reconcile_runs table (durable telemetry).

    Both prior reconcile rounds' per-class apply tallies were runtime-log-only and died
    with container rotation — every run (dry-run AND apply) now leaves a queryable row.
    Flushes but does not commit; the caller owns the transaction (main() commits AFTER
    the dry-run rollback so the report row is the only thing a dry-run persists).
    """
    run = ReconcileRun(
        mode=summary["mode"],
        sources=summary["sources"],
        keys=summary["keys"],
        by_class=summary["by_class"],
        totals={k: summary[k] for k in ("cards", "facets", "corrected", "deleted", "unchanged", "skipped", "failed")},
    )
    db.add(run)
    db.flush()
    return run


def main() -> None:
    parser = argparse.ArgumentParser(description="Reconcile deterministic facet rows against the fixed extractors")
    parser.add_argument("--apply", action="store_true", help="Write the corrections/deletes (default: dry-run)")
    parser.add_argument("--limit", type=int, default=0, help="Max cards to process (0 = all)")
    parser.add_argument(
        "--sources",
        default=",".join(DEFAULT_SOURCES),
        help=f"Comma-separated facet sources to reconcile (default: {','.join(DEFAULT_SOURCES)})",
    )
    parser.add_argument(
        "--keys",
        default="",
        help="Comma-separated spec_keys to reconcile (default: every spec_key in commodity_spec_schemas)",
    )
    args = parser.parse_args()
    sources = tuple(s.strip() for s in args.sources.split(",") if s.strip())
    keys = tuple(k.strip() for k in args.keys.split(",") if k.strip()) or None

    from app.database import SessionLocal

    db = SessionLocal()
    try:
        if args.apply:
            logger.warning(
                "reconcile-decoded-facets: --apply will rewrite/DELETE facet rows for sources {} / "
                "spec_keys {} — ensure a recent db-backup exists (pg_dump runs every 6h; "
                "scripts/restore.sh to roll back)",
                sources,
                args.keys or "ALL schema'd",
            )
        summary = reconcile(db, apply=args.apply, limit=args.limit, sources=sources, keys=keys)
        if not args.apply:
            db.rollback()  # belt-and-braces: dry-run leaves no facet writes behind
        # The durable run report is persisted for BOTH modes — after the dry-run
        # rollback, the ReconcileRun row is the only write this commit contains.
        record_reconcile_run(db, summary)
        db.commit()
    finally:
        db.close()


if __name__ == "__main__":
    main()
