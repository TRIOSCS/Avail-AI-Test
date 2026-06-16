"""Worker adapter: extract each card's description specs and persist via record_spec.

Runs in the enrichment worker's second pass (await-free, shared session), between the
mpn-decode pass and the AI spec extractor. Run order is NOT load-bearing: record_spec's
F1 tier ladder (app/services/spec_tiers.py) arbitrates every write — desc_parse is
tier 83, so it can never overwrite an mpn_decode (85) / vendor-API (90) / trio_source
(95) value and always beats AI spec_extraction (60), regardless of which pass ran
first. (The old per-writer confidence pre-gate is gone — the ladder owns arbitration.)

The SPEC stage (``extract_and_record`` / ``extract_and_record_specs``) only fills facets
for cards ALREADY categorized to a handled commodity (SPEC_COMMODITIES: hdd/ssd/dram/
power_supplies/displays/tape_drives/gpu/motherboards/cpu — record_spec requires a
category). The phase-2/3 commodities have no MPN decoders, so desc_parse is their top
non-vendor source by confidence.

The CATEGORIZE stage (``categorize_and_record`` — opt-in; only the one-shot CLI / ingest
call it, the worker still runs spec-only) closes the gap for UNCATEGORIZED cards: a strict
lead/body grammar (desc_extractor/categorizer.py) infers the commodity KEY from the
description and, ONLY when ``card.category IS NULL``, fills it via ``set_category`` at
``desc_parse``/tier 83 (no new source name — reuse the desc_parse identity) before the same
spec extraction runs for the freshly-set category. Grammar discipline is "a wrong category
is worse than a missing one": ambiguous / foreign / conflicting descriptions return None
and the card stays uncategorized. Does not commit — the caller manages the txn.

Called by: app/services/enrichment_worker/worker.py (run_one_batch, second pass, SPEC
           stage only, gated by settings.desc_parse_enabled);
           app/management/categorize_from_desc.py (the one-shot CATEGORIZE run).
Depends on: desc_extractor.extract_desc + categorizer.categorize_from_desc (pure),
           spec_tiers.set_category, spec_write_service.record_spec.
"""

from loguru import logger
from sqlalchemy.orm import Session

from app.models import MaterialCard
from app.services.desc_extractor import extract_desc
from app.services.desc_extractor._common import DESC_CONFIDENCE, DESC_SOURCE, SPEC_COMMODITIES, SpecDict
from app.services.desc_extractor.categorizer import categorize_from_desc
from app.services.spec_tiers import set_category
from app.services.spec_write_service import load_schema_cache, record_spec


def _write_specs(db: Session, card_id: int, specs: SpecDict, source: str, confidence: float, schema_cache: dict) -> int:
    """Record every (key, value) in *specs* via record_spec; return the count written.

    No pre-gate here: record_spec's F1 tier ladder rejects any write that loses to a
    higher-(tier, confidence, updated_at) prior (e.g. mpn_decode at tier 85 > 83). The
    caller owns the surrounding ``begin_nested()`` SAVEPOINT.
    """
    written = 0
    for spec_key, value in specs.items():
        if record_spec(
            db,
            card_id,
            spec_key,
            value,
            source=source,
            confidence=confidence,
            schema_cache=schema_cache,
        ):
            written += 1
    return written


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
    with db.begin_nested():
        return _write_specs(db, int(card.id), result.specs, DESC_SOURCE, result.confidence, schema_cache)


def categorize_and_record(
    db: Session,
    card: MaterialCard,
    *,
    description: str | None = None,
    source: str = DESC_SOURCE,
    confidence: float = DESC_CONFIDENCE,
    schema_cache: dict | None = None,
) -> tuple[bool, int]:
    """CATEGORIZE an UNCATEGORIZED card from a description, then fill its facets.

    Returns ``(categorized, specs_written)``. No-op (``(False, 0)``) when the card already
    has a category (categorization is fill-only — never reclassifies), when *description*
    is empty, or when the grammar declines to name a commodity.

    *description* defaults to ``card.description``; the FRU-desc channel passes a linked
    ``fru_links.description`` with ``source="fru_desc_parse"`` (tier 82) so a card with no
    own description still categorizes from its FRU's prose. ``set_category`` writes the
    canonical commodity at *source*/*confidence* via the F1 ladder (existing=None always
    loses, so the fill wins; we gate on NULL first regardless). After a successful set, the
    SAME spec extraction runs for the new category.

    All writes are wrapped in a per-card ``begin_nested()`` SAVEPOINT: a DB-level failure
    rolls back ONLY this card (category + facets together — a facet failure must not strand
    a category with no facets, and vice versa) and re-raises, keeping the caller's shared
    transaction usable for the rest of the batch.
    """
    if (card.category or "").strip():
        return (False, 0)  # already categorized — fill-only, never reclassify
    text = (description if description is not None else card.description or "").strip()
    if not text:
        return (False, 0)
    commodity = categorize_from_desc(text)
    if commodity is None:
        return (False, 0)

    with db.begin_nested():
        if not set_category(card, commodity, source=source, confidence=confidence):
            # The canonical commodity lost the ladder or normalized to None. Off-vocab is
            # impossible (categorize_from_desc only returns canonical keys), so this is a
            # category that appeared concurrently — nothing else to do (no facets without
            # a category). Roll back the empty savepoint.
            return (False, 0)
        # record_spec needs the now-set category. category + facets are one atomic unit
        # under this savepoint; the freshly-categorized commodity gets desc_parse facets
        # immediately, in the SAME transaction. Facets carry the SAME provenance as the
        # category they were unlocked by (desc_parse/83 for the own description,
        # fru_desc_parse/82 for a linked FRU description) — the ladder arbitrates.
        if schema_cache is None:
            schema_cache = load_schema_cache(db, commodity)
        written = 0
        result = extract_desc(text, commodity_hint=commodity)
        if result is not None and result.specs:
            written = _write_specs(db, int(card.id), result.specs, source, confidence, schema_cache)
    return (True, written)


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
