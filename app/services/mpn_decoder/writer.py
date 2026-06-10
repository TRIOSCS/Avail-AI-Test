"""Worker adapter: decode each card's MPN and persist the specs via record_spec.

Runs in the enrichment worker's second pass (await-free, shared session), BEFORE the AI
spec extractor, so the deterministic 0.95-confidence decode is the baseline the 0.85
description-mined pass cannot overwrite. Does not commit — the caller manages the txn.
"""

from collections import Counter

from loguru import logger
from sqlalchemy.orm import Session

from app.models import MaterialCard
from app.services.mpn_decoder import decode_mpn
from app.services.mpn_decoder._common import DECODE_SOURCE
from app.services.spec_write_service import load_schema_cache, record_spec


def decode_and_record_specs(db: Session, card_ids: list[int]) -> dict[str, int]:
    """Decode the MPNs of *card_ids* and write decoded specs.

    Returns {decoded, written, categorized}.
    """
    decoded_cards = 0
    written = 0
    categorized = 0
    schema_caches: dict[str, dict] = {}  # one schema load per commodity, reused across cards
    # "commodity.spec_key" -> count of decoder-emitted values with NO schema row. record_spec
    # drops those at DEBUG only (invisible at the production INFO level), so the discard of
    # decoder output is surfaced here as an aggregate WARNING after the batch.
    dropped_no_schema: Counter = Counter()
    for card_id in card_ids:
        # Per-card isolation: a single bad card must never abort decode for the rest of the batch.
        try:
            card = db.get(MaterialCard, card_id)
            if card is None:
                continue
            result = decode_mpn(card.display_mpn, card.manufacturer)
            if result is None:
                continue
            card_cat = (card.category or "").lower().strip()
            if card_cat and card_cat != result.commodity:
                # Decoded commodity conflicts with the card's category — skip. A shared spec_key
                # (capacity_gb exists for hdd/ssd/dram) must not write a drive's capacity onto a
                # differently-categorized card. An existing category is authoritative.
                continue
            cache = schema_caches.get(result.commodity)
            if cache is None:
                cache = schema_caches[result.commodity] = load_schema_cache(db, result.commodity)
            for spec_key in result.specs:
                if (result.commodity, spec_key) not in cache:
                    dropped_no_schema[f"{result.commodity}.{spec_key}"] += 1
            # SAVEPOINT per card: record_spec flushes, so a DB-level failure (constraint, type)
            # would otherwise poison the shared transaction — swallowed here, it would surface
            # later as a failed/rolled-back commit with the counters still claiming success. The
            # nested txn rolls back ONLY this card, keeping the outer transaction usable and the
            # totals honest (incremented after a clean release).
            with db.begin_nested():
                if not card_cat:
                    # The decoder's commodity is regex-gated against the strict manufacturer
                    # scheme (e.g. an `M393A…` part is unambiguously a Samsung DDR4 RDIMM ⇒ dram),
                    # so an un-categorized card can be categorized FROM the decode. Only ever SET a
                    # missing category — an existing one is authoritative and never overwritten.
                    card.category = result.commodity
                card_written = sum(
                    1
                    for spec_key, value in result.specs.items()
                    if record_spec(
                        db,
                        card_id,
                        spec_key,
                        value,
                        source=DECODE_SOURCE,
                        confidence=result.confidence,
                        schema_cache=cache,
                    )
                )
            # Reached only on a clean savepoint release — so a rolled-back card contributes nothing.
            decoded_cards += 1
            written += card_written
            if not card_cat:
                categorized += 1
        except Exception:
            logger.exception("mpn-decode: failed on card_id={}", card_id)
    if dropped_no_schema:
        logger.warning(
            "mpn-decode: {} decoded spec values dropped — no commodity_spec_schemas row for {} "
            "(decoder and commodity_seeds.json have drifted; see tests/test_mpn_decoder_seed_sync.py)",
            sum(dropped_no_schema.values()),
            dict(dropped_no_schema),
        )
    if written or categorized:
        logger.info(
            "mpn-decode: wrote {} specs across {} cards ({} newly categorized)",
            written,
            decoded_cards,
            categorized,
        )
    return {"decoded": decoded_cards, "written": written, "categorized": categorized}
