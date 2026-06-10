"""SP-Ingest ingest — AUGMENT material_cards from ConsolidatedParts via the SP2 ladder.

What: ``ingest`` walks each ConsolidatedPart, finds its MaterialCard by ``normalized_mpn`` and
      AUGMENTS — creating the card when absent (never clobbering an existing description, since
      we have no description-tier yet). Category goes through ``set_category`` (tier
      ``trio_source``=95, or ``trio_source_ai``=88 when AI-inferred); each spec through the
      ``record_spec`` tier ladder so TRIO ground truth (95) beats vendor (90) / decode (85) and
      a later lower-tier write can never overwrite it. Per-card ``begin_nested()`` SAVEPOINTs
      mirror mpn_decoder/writer.py so one bad card never poisons the batch.
      apply=False (DEFAULT) is a true DRY RUN: NO writes — it tallies would-create / would-update
      / fields-filled and a sample. apply=True commits in chunks.
Called by: app/management/ingest_source_data.py (the ingest stage).
Depends on: MaterialCard, spec_tiers.set_category, spec_write_service.load_schema_cache +
      record_spec, the ConsolidatedPart dataclass, and ai_correct.AI_SOURCE.
"""

from __future__ import annotations

from collections import defaultdict

from loguru import logger
from sqlalchemy.orm import Session

from app.models import MaterialCard
from app.services.source_ingest.ai_correct import AI_SOURCE
from app.services.source_ingest.models import ConsolidatedPart
from app.services.spec_tiers import set_category
from app.services.spec_write_service import load_schema_cache, record_spec

# Raw-source provenance tag (top of the ladder, tier 95).
RAW_SOURCE = "trio_source"
_COMMIT_CHUNK = 500


def _empty_stats() -> dict:
    """The stats shape ingest returns (also the dry-run report's backing data)."""
    return {
        "parts_seen": 0,
        "would_create": 0,
        "would_update": 0,
        "created": 0,
        "updated": 0,
        "categories_set": 0,
        "descriptions_filled": 0,
        "conditions_filled": 0,
        "specs_written": 0,
        # fields filled, keyed by ladder source ("trio_source" / "trio_source_ai").
        "fields_by_source": defaultdict(int),
        "sample": [],  # up to _SAMPLE_LIMIT dicts describing consolidated parts
    }


_SAMPLE_LIMIT = 15


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


def _ingest_part(db: Session, part: ConsolidatedPart, schema_caches: dict, stats: dict, *, apply: bool) -> None:
    """Augment-ingest one part.

    On apply=False, only tallies (no DB writes).
    """
    card = db.query(MaterialCard).filter(MaterialCard.normalized_mpn == part.normalized_mpn).first()
    is_new = card is None

    if not apply:
        _tally_dry_run(db, part, card, stats)
        return

    # SAVEPOINT per card (mirror mpn_decoder/writer.py): a flush-level failure rolls back ONLY
    # this card, keeping the outer transaction usable and the counters honest.
    with db.begin_nested():
        if card is None:
            card = MaterialCard(
                normalized_mpn=part.normalized_mpn,
                display_mpn=part.raw_mpn,
                manufacturer=part.manufacturer,
            )
            db.add(card)
            db.flush()  # assign card.id before record_spec needs it

        cat_value, cat_source, cat_conf = _resolve_category(part)
        if cat_value and set_category(card, cat_value, source=cat_source, confidence=cat_conf):
            stats["categories_set"] += 1
            stats["fields_by_source"][cat_source] += 1

        # Description: fill ONLY when empty — there is no description-tier yet, so we must not
        # clobber an existing description (documented design choice; see module docstring).
        desc = _description_for(part)
        if desc and not (card.description or "").strip():
            card.description = desc[:1000]
            stats["descriptions_filled"] += 1
            stats["fields_by_source"][RAW_SOURCE] += 1

        # Condition: fill the column only when empty and a condition was consolidated.
        if part.condition and not (card.condition or "").strip():
            card.condition = part.condition
            stats["conditions_filled"] += 1
            stats["fields_by_source"][RAW_SOURCE] += 1

        # Specs through the ladder — raw specs at trio_source(95), AI specs at trio_source_ai(88).
        cache = _schema_cache_for(db, card, schema_caches)
        if cache is not None:
            stats["specs_written"] += _write_specs(db, card.id, part, cache, stats)

    if is_new:
        stats["created"] += 1
    else:
        stats["updated"] += 1


def _schema_cache_for(db: Session, card: MaterialCard, schema_caches: dict) -> dict | None:
    """Lazily load (and cache) the commodity schema for a card's category."""
    category = (card.category or "").lower().strip()
    if not category:
        return None
    cache = schema_caches.get(category)
    if cache is None:
        cache = schema_caches[category] = load_schema_cache(db, category)
    return cache


def _write_specs(db: Session, card_id: int, part: ConsolidatedPart, cache: dict, stats: dict) -> int:
    """Write raw specs (trio_source, conf 1.0) then AI specs (trio_source_ai).

    Returns count.
    """
    written = 0
    for key, value in part.specs.items():
        if record_spec(db, card_id, key, value, source=RAW_SOURCE, confidence=1.0, schema_cache=cache):
            written += 1
            stats["fields_by_source"][RAW_SOURCE] += 1
    for key, spec in part.ai_specs.items():
        conf = spec.get("confidence", 0.5)
        if record_spec(db, card_id, key, spec["value"], source=AI_SOURCE, confidence=conf, schema_cache=cache):
            written += 1
            stats["fields_by_source"][AI_SOURCE] += 1
    return written


def _tally_dry_run(db: Session, part: ConsolidatedPart, card: MaterialCard | None, stats: dict) -> None:
    """Dry-run accounting: NO writes — count would-create / would-update / fields filled."""
    cat_value, cat_source, _ = _resolve_category(part)
    desc = _description_for(part)
    if card is None:
        stats["would_create"] += 1
        fills = {
            "category": bool(cat_value),
            "description": bool(desc),
            "condition": bool(part.condition),
            "specs": len(part.specs) + len(part.ai_specs),
        }
    else:
        stats["would_update"] += 1
        fills = {
            "category": bool(cat_value) and not card.category,
            "description": bool(desc) and not (card.description or "").strip(),
            "condition": bool(part.condition) and not (card.condition or "").strip(),
            "specs": len(part.specs) + len(part.ai_specs),
        }
    if fills["category"]:
        stats["categories_set"] += 1
        stats["fields_by_source"][cat_source] += 1
    if fills["description"]:
        stats["descriptions_filled"] += 1
    if fills["condition"]:
        stats["conditions_filled"] += 1
    stats["specs_written"] += fills["specs"]

    if len(stats["sample"]) < _SAMPLE_LIMIT:
        stats["sample"].append(
            {
                "normalized_mpn": part.normalized_mpn,
                "display_mpn": part.raw_mpn,
                "action": "create" if card is None else "update",
                "manufacturer": part.manufacturer,
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
    fields-filled tallies and a sample. apply=True creates/augments cards and commits in chunks
    of _COMMIT_CHUNK. Each card is wrapped in a SAVEPOINT for per-card isolation. ``fields_by_source``
    in the returned stats is converted to a plain dict.
    """
    stats = _empty_stats()
    schema_caches: dict[str, dict] = {}
    for idx, part in enumerate(parts, start=1):
        stats["parts_seen"] += 1
        try:
            _ingest_part(db, part, schema_caches, stats, apply=apply)
        except Exception:
            logger.exception("ingest: failed on mpn={} — skipping", part.normalized_mpn)
        if apply and idx % _COMMIT_CHUNK == 0:
            db.commit()
    if apply:
        db.commit()
    stats["fields_by_source"] = dict(stats["fields_by_source"])
    logger.info(
        "ingest(apply={}): seen={} create={} update={} specs={}",
        apply,
        stats["parts_seen"],
        stats["created"] or stats["would_create"],
        stats["updated"] or stats["would_update"],
        stats["specs_written"],
    )
    return stats
