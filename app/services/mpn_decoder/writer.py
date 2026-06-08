"""Worker adapter: decode each card's MPN and persist the specs via record_spec.

Runs in the enrichment worker's second pass (await-free, shared session), BEFORE the AI
spec extractor, so the deterministic 0.95-confidence decode is the baseline the 0.85
description-mined pass cannot overwrite. Does not commit — the caller manages the txn.
"""

from loguru import logger
from sqlalchemy.orm import Session

from app.models import MaterialCard
from app.services.mpn_decoder import decode_mpn
from app.services.mpn_decoder._common import DECODE_SOURCE
from app.services.spec_write_service import load_schema_cache, record_spec


def decode_and_record_specs(db: Session, card_ids: list[int]) -> dict[str, int]:
    """Decode the MPNs of *card_ids* and write decoded specs.

    Returns {decoded, written}.
    """
    decoded_cards = 0
    written = 0
    schema_caches: dict[str, dict] = {}  # one schema load per commodity, reused across cards
    for card_id in card_ids:
        # Per-card isolation: a single malformed MPN must never abort decode for the batch.
        try:
            card = db.get(MaterialCard, card_id)
            if card is None:
                continue
            result = decode_mpn(card.display_mpn, card.manufacturer)
            if result is None:
                continue
            # The decoded commodity MUST match the card's actual category — otherwise a shared
            # spec_key (e.g. capacity_gb exists for hdd/ssd/dram) could write a drive's capacity
            # onto a mis-categorized card.
            if result.commodity != (card.category or "").lower().strip():
                continue
            cache = schema_caches.get(result.commodity)
            if cache is None:
                cache = schema_caches[result.commodity] = load_schema_cache(db, result.commodity)
            decoded_cards += 1
            for spec_key, value in result.specs.items():
                if record_spec(
                    db, card_id, spec_key, value, source=DECODE_SOURCE, confidence=result.confidence, schema_cache=cache
                ):
                    written += 1
        except Exception:
            logger.exception("mpn-decode: failed on card_id={}", card_id)
    if written:
        logger.info("mpn-decode: wrote {} specs across {} cards", written, decoded_cards)
    return {"decoded": decoded_cards, "written": written}
