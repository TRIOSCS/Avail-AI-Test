"""Worker adapter: decode each card's MPN and persist the specs via record_spec.

Runs in the enrichment worker's second pass (await-free, shared session). The deterministic
0.95-confidence decode (tier 85) is NOT protected by run-order: the F1 tier ladder in
record_spec / set_category (app/services/spec_tiers.py) is authoritative. A later, lower-tier
spec_extraction (tier 60) pass can never overwrite a decode value regardless of which ran
first, and the decode's category write only wins over a lower-tier category. Does not commit
— the caller manages the txn.
"""

from loguru import logger
from sqlalchemy.orm import Session

from app.models import MaterialCard
from app.services.mpn_decoder import decode_mpn
from app.services.mpn_decoder._common import DECODE_SOURCE
from app.services.spec_tiers import set_category
from app.services.spec_write_service import load_schema_cache, record_spec


def decode_and_record_specs(db: Session, card_ids: list[int]) -> dict[str, int]:
    """Decode the MPNs of *card_ids* and write decoded specs.

    Returns {decoded, written, categorized}.
    """
    decoded_cards = 0
    written = 0
    categorized = 0
    schema_caches: dict[str, dict] = {}  # one schema load per commodity, reused across cards
    for card_id in card_ids:
        # Per-card isolation: a single bad card must never abort decode for the rest of the batch.
        try:
            card = db.get(MaterialCard, card_id)
            if card is None:
                continue
            result = decode_mpn(card.display_mpn, card.manufacturer)
            if result is None:
                continue
            cache = schema_caches.get(result.commodity)
            if cache is None:
                cache = schema_caches[result.commodity] = load_schema_cache(db, result.commodity)
            # SAVEPOINT per card: record_spec flushes, so a DB-level failure (constraint, type)
            # would otherwise poison the shared transaction — swallowed here, it would surface
            # later as a failed/rolled-back commit with the counters still claiming success. The
            # nested txn rolls back ONLY this card, keeping the outer transaction usable and the
            # totals honest (incremented after a clean release).
            with db.begin_nested():
                # The decoder's commodity is regex-gated against the strict manufacturer scheme
                # (e.g. an `M393A…` part is unambiguously a Samsung DDR4 RDIMM ⇒ dram), so it is
                # canonical and safe to feed the ladder. set_category (tier 85) writes it iff it
                # beats the card's existing category provenance — it corrects a lower-tier guess
                # but never overwrites a vendor/manual category. If the category write loses, the
                # card keeps its old category and record_spec rejects the decoded commodity's
                # spec_keys (no schema match), so a drive's capacity never lands on a non-drive card.
                did_categorize = set_category(card, result.commodity, DECODE_SOURCE, result.confidence)
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
            if did_categorize:
                categorized += 1
        except Exception:
            logger.exception("mpn-decode: failed on card_id={}", card_id)
    if written or categorized:
        logger.info(
            "mpn-decode: wrote {} specs across {} cards ({} newly categorized)",
            written,
            decoded_cards,
            categorized,
        )
    return {"decoded": decoded_cards, "written": written, "categorized": categorized}
