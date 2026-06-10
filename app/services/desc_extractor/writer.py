"""Worker adapter: extract each card's description specs and persist via record_spec.

Runs in the enrichment worker's second pass (await-free, shared session), between the
mpn-decode pass and the AI spec extractor. Run order is NOT load-bearing: record_spec's
F1 tier ladder (app/services/spec_tiers.py) arbitrates every write — desc_parse is
tier 83, so it can never overwrite an mpn_decode (85) / vendor-API (90) / trio_source
(95) value and always beats AI spec_extraction (60), regardless of which pass ran
first. (The old per-writer confidence pre-gate is gone — the ladder owns arbitration.)
Unlike mpn_decoder/writer.py this NEVER writes a category: descriptions are not a
regex-gated commodity proof, so only cards already categorized to a handled
commodity (SPEC_COMMODITIES: hdd/ssd/dram/power_supplies/displays/tape_drives/gpu/
motherboards — the spec'd _HANDLED set) are processed (record_spec requires a
category anyway). The five phase-2 commodities have no MPN decoders, so desc_parse
is their top non-vendor source by confidence. Does not commit — the caller manages
the txn.

Called by: app/services/enrichment_worker/worker.py (run_one_batch, second pass,
           gated by settings.desc_parse_enabled).
Depends on: desc_extractor.extract_desc (pure), spec_write_service.record_spec.
"""

from loguru import logger
from sqlalchemy.orm import Session

from app.models import MaterialCard
from app.services.desc_extractor import extract_desc
from app.services.desc_extractor._common import DESC_SOURCE, SPEC_COMMODITIES
from app.services.spec_write_service import load_schema_cache, record_spec


def extract_and_record(db: Session, card: MaterialCard, schema_cache: dict | None = None) -> int:
    """Extract *card*'s description specs and write them; returns the count written.

    Writes inside a per-card ``begin_nested()`` SAVEPOINT: record_spec flushes, so a
    DB-level failure (constraint, type) would otherwise poison the caller's shared
    transaction — the nested txn rolls back ONLY this card and re-raises, keeping the
    outer transaction usable for the rest of the batch.
    """
    description = (card.description or "").strip()
    category = (card.category or "").lower().strip()
    if not description or category not in SPEC_COMMODITIES:
        return 0
    result = extract_desc(description, commodity_hint=category)
    if result is None or not result.specs:
        return 0
    if schema_cache is None:
        schema_cache = load_schema_cache(db, category)
    written = 0
    with db.begin_nested():
        # No pre-gate here: record_spec's F1 tier ladder rejects any write that loses to a
        # higher-(tier, confidence, updated_at) prior (e.g. mpn_decode at tier 85 > 83).
        for spec_key, value in result.specs.items():
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

    Returns {parsed, written, failed}: cards that landed at least one spec, total specs,
    and cards that raised — so the worker's summary line distinguishes a healthy no-op
    batch from a fully-crashed one (the per-card tracebacks are logged below, but the
    aggregate must surface failures too).
    """
    parsed = 0
    written = 0
    failed = 0
    schema_caches: dict[str, dict] = {}  # one schema load per commodity, reused across cards
    for card_id in card_ids:
        # Per-card isolation: a single bad card must never abort the rest of the batch.
        try:
            card = db.get(MaterialCard, card_id)
            if card is None:
                continue
            category = (card.category or "").lower().strip()
            if category not in SPEC_COMMODITIES:
                continue
            cache = schema_caches.get(category)
            if cache is None:
                cache = schema_caches[category] = load_schema_cache(db, category)
            card_written = extract_and_record(db, card, schema_cache=cache)
            if card_written:
                parsed += 1
                written += card_written
        except Exception:
            failed += 1
            logger.exception("desc-parse: failed on card_id={}", card_id)
    if written or failed:
        logger.info("desc-parse: wrote {} specs across {} cards ({} cards failed)", written, parsed, failed)
    return {"parsed": parsed, "written": written, "failed": failed}
