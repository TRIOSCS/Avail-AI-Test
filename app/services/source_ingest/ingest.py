"""SP-Ingest ingest — AUGMENT material_cards from ConsolidatedParts via the SP2 ladder.

What: ``ingest`` walks each ConsolidatedPart, finds its MaterialCard by ``normalized_mpn`` and
      AUGMENTS — creating the card when absent (never clobbering an existing description, since
      we have no description-tier yet). Category goes through ``set_category`` (tier
      ``trio_source``=95, or ``trio_source_ai``=88 when AI-inferred); manufacturer (actual
      maker) and brand (OEM label) go through ``set_manufacturer``/``set_brand`` at
      trio_source/0.9 (dual-brand W6 — each no-ops on None); each spec through the
      ``record_spec`` tier ladder so TRIO ground truth (95) beats vendor (90) / decode (85) and
      a later lower-tier write can never overwrite it. Per-card ``begin_nested()`` SAVEPOINTs
      mirror mpn_decoder/writer.py so one bad card never poisons the batch — and, like that
      writer, per-card tallies merge into the stats ONLY after a clean savepoint release, so a
      rolled-back card contributes nothing (the counters stay honest). Failed parts are counted
      (``stats["failed"]`` + a capped mpn sample) so the operator report surfaces them.
      apply=False (DEFAULT) is a true DRY RUN: NO writes — it runs the SAME gates apply-mode
      runs (set_category with write=False; record_spec's schema/enum/ladder checks via
      spec_would_write) so the would-create / would-update / fields-filled tallies cannot drift
      from what --apply will do, plus a sample (dry-run only). apply=True commits in chunks.
Called by: app/management/ingest_source_data.py (the ingest stage).
Depends on: MaterialCard, spec_tiers.set_category/tier_for, spec_write_service.
      load_schema_cache + record_spec + spec_would_write, constants.MaterialCondition, the
      ConsolidatedPart dataclass, and ai_correct.AI_SOURCE.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone

from loguru import logger
from sqlalchemy.orm import Session

from app.constants import MaterialCondition
from app.models import MaterialCard
from app.services.source_ingest.ai_correct import AI_SOURCE
from app.services.source_ingest.models import ConsolidatedPart
from app.services.spec_tiers import set_brand, set_category, set_manufacturer, tier_for
from app.services.spec_write_service import load_schema_cache, record_spec, spec_would_write

# Raw-source provenance tag (top of the ladder, tier 95).
RAW_SOURCE = "trio_source"
# Confidence for the ladder-routed brand/manufacturer writes (dual-brand W6).
BRAND_CONFIDENCE = 0.9
_COMMIT_CHUNK = 500
_SAMPLE_LIMIT = 15
_FAILED_SAMPLE_LIMIT = 10


def _empty_stats() -> dict:
    """The stats shape ingest returns (also the dry-run report's backing data)."""
    return {
        "parts_seen": 0,
        "would_create": 0,
        "would_update": 0,
        "created": 0,
        "updated": 0,
        # Parts that raised and were skipped (per-part isolation). failed_mpns holds up to
        # _FAILED_SAMPLE_LIMIT of their mpns — full tracebacks are in the logs.
        "failed": 0,
        "failed_mpns": [],
        "categories_set": 0,
        "brands_set": 0,
        "manufacturers_set": 0,
        "descriptions_filled": 0,
        "conditions_filled": 0,
        "specs_written": 0,
        # fields filled, keyed by ladder source ("trio_source" / "trio_source_ai").
        "fields_by_source": defaultdict(int),
        "sample": [],  # dry-run only: up to _SAMPLE_LIMIT dicts describing consolidated parts
    }


def _resolve_category(part: ConsolidatedPart) -> tuple[str | None, str, float]:
    """Pick the category to write + its ladder source/confidence.

    Raw source category (tier 95, confidence 1.0) wins when present; else the AI-inferred
    category (tier 88) if ai_correct supplied one. Returns (value, source, confidence) or
    (None, ..., ...) when there is nothing to write.
    """
    if part.category:
        return part.category, RAW_SOURCE, 1.0
    if part.ai_category:
        return part.ai_category, AI_SOURCE, part.ai_category_confidence or 0.5
    return None, RAW_SOURCE, 1.0


def _description_for(part: ConsolidatedPart) -> str | None:
    """The description to fill an EMPTY card with — AI-standardized if present, else
    raw."""
    return part.ai_description or part.description


def _condition_fillable(part_condition: str | None, card_condition: str | None) -> bool:
    """True when the consolidated condition should fill the card column.

    "Unknown" is treated the same as NULL on BOTH sides: a synthetic Unknown is never
    written (it would permanently occupy the fill-only-when-empty column — condition has
    no tier ladder), and an existing Unknown never blocks a real value.
    """
    if not part_condition or part_condition == MaterialCondition.UNKNOWN:
        return False
    existing = (card_condition or "").strip()
    return not existing or existing == MaterialCondition.UNKNOWN


def _ingest_part(db: Session, part: ConsolidatedPart, schema_caches: dict, stats: dict, *, apply: bool) -> None:
    """Augment-ingest one part.

    On apply=False, only tallies (no DB writes).
    """
    card = db.query(MaterialCard).filter(MaterialCard.normalized_mpn == part.normalized_mpn).first()
    is_new = card is None

    if not apply:
        _tally_dry_run(db, part, card, schema_caches, stats)
        return

    # SAVEPOINT per card (mirror mpn_decoder/writer.py): a flush-level failure rolls back ONLY
    # this card, keeping the outer transaction usable. The per-card tallies accumulate in
    # LOCALS and merge into stats only after a clean savepoint release — a rolled-back card
    # must contribute nothing, or the report would claim writes that were undone.
    tally = {
        "categories_set": 0,
        "brands_set": 0,
        "manufacturers_set": 0,
        "descriptions_filled": 0,
        "conditions_filled": 0,
        "specs_written": 0,
    }
    by_source: dict[str, int] = defaultdict(int)
    with db.begin_nested():
        if card is None:
            # Manufacturer is NOT written in the constructor — it goes through the
            # set_manufacturer ladder below so its provenance columns are stamped.
            card = MaterialCard(
                normalized_mpn=part.normalized_mpn,
                display_mpn=part.raw_mpn,
            )
            db.add(card)
            db.flush()  # assign card.id before record_spec needs it

        cat_value, cat_source, cat_conf = _resolve_category(part)
        if cat_value and set_category(card, cat_value, source=cat_source, confidence=cat_conf):
            tally["categories_set"] += 1
            by_source[cat_source] += 1

        # Dual-brand W6: maker + OEM label through the ladder at trio_source/0.9.
        # Each no-ops on None/empty; a lower-tier value can never displace a higher one.
        if set_manufacturer(card, part.manufacturer, RAW_SOURCE, BRAND_CONFIDENCE):
            tally["manufacturers_set"] += 1
            by_source[RAW_SOURCE] += 1
        if set_brand(card, part.brand, RAW_SOURCE, BRAND_CONFIDENCE):
            tally["brands_set"] += 1
            by_source[RAW_SOURCE] += 1

        # Description: fill ONLY when empty — there is no description-tier yet, so we must not
        # clobber an existing description (documented design choice; see module docstring).
        desc = _description_for(part)
        if desc and not (card.description or "").strip():
            card.description = desc[:1000]
            tally["descriptions_filled"] += 1
            by_source[RAW_SOURCE] += 1

        # Condition: fill only with a real value, never "Unknown" (see _condition_fillable).
        if _condition_fillable(part.condition, card.condition):
            card.condition = part.condition
            tally["conditions_filled"] += 1
            by_source[RAW_SOURCE] += 1

        # Specs through the ladder — raw specs at trio_source(95), AI specs at trio_source_ai(88).
        cache = _schema_cache_for(db, card.category, schema_caches)
        if cache is not None:
            tally["specs_written"] += _write_specs(db, card.id, part, cache, by_source)

    # Reached only on a clean savepoint release — merge the card's tallies.
    for key, count in tally.items():
        stats[key] += count
    for source, count in by_source.items():
        stats["fields_by_source"][source] += count
    if is_new:
        stats["created"] += 1
    else:
        stats["updated"] += 1


def _schema_cache_for(db: Session, category: str | None, schema_caches: dict) -> dict | None:
    """Lazily load (and cache) the commodity schema for *category*."""
    category = (category or "").lower().strip()
    if not category:
        return None
    cache = schema_caches.get(category)
    if cache is None:
        cache = schema_caches[category] = load_schema_cache(db, category)
    return cache


def _write_specs(db: Session, card_id: int, part: ConsolidatedPart, cache: dict, by_source: dict) -> int:
    """Write raw specs (trio_source, conf 1.0) then AI specs (trio_source_ai).

    Returns count.
    """
    written = 0
    for key, value in part.specs.items():
        if record_spec(db, card_id, key, value, source=RAW_SOURCE, confidence=1.0, schema_cache=cache):
            written += 1
            by_source[RAW_SOURCE] += 1
    for key, spec in part.ai_specs.items():
        conf = spec.get("confidence", 0.5)
        if record_spec(db, card_id, key, spec["value"], source=AI_SOURCE, confidence=conf, schema_cache=cache):
            written += 1
            by_source[AI_SOURCE] += 1
    return written


def _tally_dry_run(
    db: Session, part: ConsolidatedPart, card: MaterialCard | None, schema_caches: dict, stats: dict
) -> None:
    """Dry-run accounting: NO writes — runs the SAME gates apply-mode runs.

    Category goes through ``set_category(write=False)`` (full ladder, no mutation), so an
    existing low-tier category that --apply WOULD correct is counted, and a higher-tier one
    that would block the write is not. Specs go through ``spec_would_write`` (schema
    existence, enum/numeric validation, ladder vs the card's existing entries) with an
    overlay simulating apply-mode's sequential writes (a raw trio_source spec blocks the
    same-key AI spec, exactly as in apply mode). The dry-run report is the operator's
    go/no-go gate — its numbers must match what --apply will do.
    """
    cat_value, cat_source, cat_conf = _resolve_category(part)
    desc = _description_for(part)
    if card is None:
        stats["would_create"] += 1
        # Fresh card: the ladder trivially wins (existing=None); clean.py already
        # canonicalized the category, so a non-empty value is writable.
        category_would = bool(cat_value)
        effective_category = cat_value
        existing_specs: dict = {}
        desc_would = bool(desc)
        cond_would = _condition_fillable(part.condition, None)
        # Brand/manufacturer: on a fresh card any non-empty value wins (existing=None);
        # set_brand/set_manufacturer reject empties, mirrored by the bool() gate here.
        mfr_would = bool(part.manufacturer and str(part.manufacturer).strip())
        brand_would = bool(part.brand and str(part.brand).strip())
    else:
        stats["would_update"] += 1
        category_would = bool(cat_value) and set_category(card, cat_value, cat_source, cat_conf, write=False)
        mfr_would = set_manufacturer(card, part.manufacturer, RAW_SOURCE, BRAND_CONFIDENCE, write=False)
        brand_would = set_brand(card, part.brand, RAW_SOURCE, BRAND_CONFIDENCE, write=False)
        # Specs are validated against the category the card WOULD have after --apply.
        effective_category = cat_value if category_would else card.category
        existing_specs = dict(card.specs_structured or {})
        if category_would and card.category and cat_value and card.category != cat_value:
            # Apply-mode's category flip purges the old commodity's facet rows + JSONB
            # mirrors (spec_tiers._purge_stale_commodity_data) — mirror that here so the
            # spec ladder below compares against the same post-purge state --apply would see.
            from app.models import MaterialSpecFacet

            stale_rows = (
                db.query(MaterialSpecFacet.spec_key)
                .filter(
                    MaterialSpecFacet.material_card_id == card.id,
                    MaterialSpecFacet.category != cat_value,
                )
                .all()
            )
            for (stale_key,) in stale_rows:
                existing_specs.pop(stale_key, None)
        desc_would = bool(desc) and not (card.description or "").strip()
        cond_would = _condition_fillable(part.condition, card.condition)

    if category_would:
        stats["categories_set"] += 1
        stats["fields_by_source"][cat_source] += 1
    if mfr_would:
        stats["manufacturers_set"] += 1
        stats["fields_by_source"][RAW_SOURCE] += 1
    if brand_would:
        stats["brands_set"] += 1
        stats["fields_by_source"][RAW_SOURCE] += 1
    if desc_would:
        stats["descriptions_filled"] += 1
        stats["fields_by_source"][RAW_SOURCE] += 1
    if cond_would:
        stats["conditions_filled"] += 1
        stats["fields_by_source"][RAW_SOURCE] += 1

    effective_category = (effective_category or "").lower().strip()
    cache = _schema_cache_for(db, effective_category, schema_caches)
    if cache is not None:
        now_iso = datetime.now(timezone.utc).isoformat()
        overlay = existing_specs  # already a copy — safe to annotate with simulated wins
        for key, value in part.specs.items():
            if spec_would_write(
                db,
                category=effective_category,
                existing_specs=overlay,
                spec_key=key,
                value=value,
                source=RAW_SOURCE,
                confidence=1.0,
                schema_cache=cache,
            ):
                stats["specs_written"] += 1
                stats["fields_by_source"][RAW_SOURCE] += 1
                overlay[key] = {
                    "source": RAW_SOURCE,
                    "confidence": 1.0,
                    "tier": tier_for(RAW_SOURCE),
                    "updated_at": now_iso,
                }
        for key, spec in part.ai_specs.items():
            conf = spec.get("confidence", 0.5)
            if spec_would_write(
                db,
                category=effective_category,
                existing_specs=overlay,
                spec_key=key,
                value=spec["value"],
                source=AI_SOURCE,
                confidence=conf,
                schema_cache=cache,
            ):
                stats["specs_written"] += 1
                stats["fields_by_source"][AI_SOURCE] += 1
                overlay[key] = {
                    "source": AI_SOURCE,
                    "confidence": conf,
                    "tier": tier_for(AI_SOURCE),
                    "updated_at": now_iso,
                }

    if len(stats["sample"]) < _SAMPLE_LIMIT:
        stats["sample"].append(
            {
                "normalized_mpn": part.normalized_mpn,
                "display_mpn": part.raw_mpn,
                "action": "create" if card is None else "update",
                "manufacturer": part.manufacturer,
                "brand": part.brand,
                "category": cat_value,
                "category_source": cat_source if cat_value else None,
                "description": (desc[:80] + "…") if desc and len(desc) > 80 else desc,
                "condition": part.condition,
                "specs": dict(part.specs),
                "ai_specs": {k: v["value"] for k, v in part.ai_specs.items()},
                "record_count": part.record_count,
            }
        )


def ingest(db: Session, parts: list[ConsolidatedPart], *, apply: bool) -> dict:
    """AUGMENT-ingest *parts* into material_cards via the SP2 ladder.

    apply=False (DEFAULT) is a DRY RUN — makes NO writes, returns would-create/would-update +
    fields-filled tallies (computed through the same ladder/schema gates as apply mode) and a
    sample (dry-run only). apply=True creates/augments cards and commits in chunks of
    _COMMIT_CHUNK. Each card is wrapped in a SAVEPOINT for per-card isolation; a part that
    raises is skipped, counted in ``stats["failed"]`` (mpn sampled in ``failed_mpns``), and
    never silently absent from the report. ``fields_by_source`` in the returned stats is
    converted to a plain dict.
    """
    stats = _empty_stats()
    schema_caches: dict[str, dict] = {}
    for idx, part in enumerate(parts, start=1):
        stats["parts_seen"] += 1
        try:
            _ingest_part(db, part, schema_caches, stats, apply=apply)
        except Exception:
            stats["failed"] += 1
            if len(stats["failed_mpns"]) < _FAILED_SAMPLE_LIMIT:
                stats["failed_mpns"].append(part.normalized_mpn)
            logger.exception("ingest: failed on mpn={} — skipping", part.normalized_mpn)
        if apply and idx % _COMMIT_CHUNK == 0:
            db.commit()
    if apply:
        db.commit()
    stats["fields_by_source"] = dict(stats["fields_by_source"])
    logger.info(
        "ingest(apply={}): seen={} create={} update={} failed={} specs={}",
        apply,
        stats["parts_seen"],
        stats["created"] or stats["would_create"],
        stats["updated"] or stats["would_update"],
        stats["failed"],
        stats["specs_written"],
    )
    return stats
