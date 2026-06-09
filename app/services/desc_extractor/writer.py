"""Worker adapter: extract each card's description specs and persist via record_spec.

Runs in the enrichment worker's second pass (await-free, shared session), AFTER the
mpn-decode pass (0.95) and BEFORE the AI spec extractor (0.85), so the deterministic
0.90 description grammar lands between them. Keys an earlier pass already holds at
HIGHER confidence (mpn_decode 0.95, vendor APIs) are skipped here — record_spec's
current cross-source rule is latest-write-wins, so this guard is what keeps the
decode baseline authoritative until the SP2 source-tier ladder lands in record_spec.
Unlike mpn_decoder/writer.py this NEVER writes a category: descriptions are not a
regex-gated commodity proof, so only already-categorized hdd/ssd/dram cards are
processed (record_spec requires a category anyway). Does not commit — the caller
manages the txn.

Called by: app/services/enrichment_worker/worker.py (run_one_batch, second pass,
           gated by settings.desc_parse_enabled).
Depends on: desc_extractor.extract_desc (pure), spec_write_service.record_spec.
"""

from loguru import logger
from sqlalchemy.orm import Session

from app.models import MaterialCard
from app.services.desc_extractor import extract_desc
from app.services.desc_extractor._common import DESC_CONFIDENCE, DESC_SOURCE
from app.services.spec_write_service import load_schema_cache, record_spec

# The only commodities the extractor fills specs for (must mirror extract_desc).
_HANDLED = frozenset({"hdd", "ssd", "dram"})


def extract_and_record(db: Session, card: MaterialCard, schema_cache: dict | None = None) -> int:
    """Extract *card*'s description specs and write them; returns the count written.

    Writes inside a per-card ``begin_nested()`` SAVEPOINT: record_spec flushes, so a
    DB-level failure (constraint, type) would otherwise poison the caller's shared
    transaction — the nested txn rolls back ONLY this card and re-raises, keeping the
    outer transaction usable for the rest of the batch.
    """
    description = (card.description or "").strip()
    category = (card.category or "").lower().strip()
    if not description or category not in _HANDLED:
        return 0
    result = extract_desc(description, commodity_hint=category)
    if result is None or not result.specs:
        return 0
    if schema_cache is None:
        schema_cache = load_schema_cache(db, category)
    prior_specs = card.specs_structured or {}
    written = 0
    with db.begin_nested():
        for spec_key, value in result.specs.items():
            prior = prior_specs.get(spec_key)
            if prior and float(prior.get("confidence") or 0.0) > DESC_CONFIDENCE:
                # mpn_decode (0.95) / vendor-API values outrank this pass — leave them.
                continue
            if record_spec(
                db,
                int(card.id),
                spec_key,
                value,
                source=DESC_SOURCE,
                confidence=result.confidence,
                schema_cache=schema_cache,
            ):
                written += 1
    return written


def extract_and_record_specs(db: Session, card_ids: list[int]) -> dict[str, int]:
    """Desc-parse the descriptions of *card_ids* and write extracted specs.

    Returns {parsed, written}: cards that landed at least one spec, and total specs.
    """
    parsed = 0
    written = 0
    schema_caches: dict[str, dict] = {}  # one schema load per commodity, reused across cards
    for card_id in card_ids:
        # Per-card isolation: a single bad card must never abort the rest of the batch.
        try:
            card = db.get(MaterialCard, card_id)
            if card is None:
                continue
            category = (card.category or "").lower().strip()
            if category not in _HANDLED:
                continue
            cache = schema_caches.get(category)
            if cache is None:
                cache = schema_caches[category] = load_schema_cache(db, category)
            card_written = extract_and_record(db, card, schema_cache=cache)
            if card_written:
                parsed += 1
                written += card_written
        except Exception:
            logger.exception("desc-parse: failed on card_id={}", card_id)
    if written:
        logger.info("desc-parse: wrote {} specs across {} cards", written, parsed)
    return {"parsed": parsed, "written": written}
