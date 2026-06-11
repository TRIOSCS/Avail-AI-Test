"""Worker adapter: decode each card's MPN and persist the specs via record_spec.

Runs in the enrichment worker's second pass (await-free, shared session). The deterministic
0.95-confidence decode (tier 85) is NOT protected by run-order: the F1 tier ladder in
record_spec / set_category (app/services/spec_tiers.py) is authoritative. A later, lower-tier
spec_extraction (tier 60) pass can never overwrite a decode value regardless of which ran
first, and the decode's category write only wins over a lower-tier category. Does not commit
— the caller manages the txn.
"""

from collections import Counter

from loguru import logger
from sqlalchemy.orm import Session

from app.models import MaterialCard
from app.services.manufacturer_normalizer import normalize_brand_name
from app.services.mpn_decoder import decode_mpn
from app.services.mpn_decoder._common import DECODE_SOURCE, DROP_OUT_OF_ENVELOPE
from app.services.spec_tiers import set_category, set_manufacturer
from app.services.spec_write_service import load_schema_cache, record_spec

# Confidence for the decode's MAKER write (dual-brand W4 — the vendor gate is the same
# deterministic regex as the specs, but the maker claim is one notch below the per-spec
# decode confidence by design: spec'd at 0.9, tier mpn_decode/85).
MAKER_CONFIDENCE = 0.9


def decode_and_record_specs(db: Session, card_ids: list[int]) -> dict[str, int]:
    """Decode the MPNs of *card_ids* and write decoded specs.

    Returns {decoded, written, categorized, manufacturers_set, failed,
    skipped_category_conflict, skipped_maker_conflict}. ``failed`` counts cards LOST
    to an exception (the per-card isolation below — rolled back, contributing
    nothing); mirroring the desc/crosswalk writers, it is surfaced in the summary log
    so the worker's one-line stats can distinguish a healthy no-op batch from a
    crashed one.
    """
    decoded_cards = 0
    written = 0
    categorized = 0
    manufacturers_set = 0
    failed = 0
    schema_caches: dict[str, dict] = {}  # one schema load per commodity, reused across cards
    # record_spec drops BOTH vocabulary-drift cases at DEBUG only (invisible at the
    # production INFO level), so the discard of decoder output is surfaced here as an
    # aggregate WARNING after the batch:
    #   dropped_no_schema   "commodity.spec_key"       -> value with NO schema row
    #   dropped_out_of_enum "commodity.spec_key=value" -> enum value outside the LIVE
    #                       enum_values (CI pins decoder constants against the JSON seeds,
    #                       but the worker decodes against live DB rows, which can lag a
    #                       deploy's reseed or drift after a failed/manual reseed).
    #   dropped_off_grid    "commodity.spec_key=value" -> value the DECODER itself refused
    #                       to emit (DecodeResult.dropped, reason off_grid — the hdd
    #                       shipped-capacity grid in storage.decode_storage). The decoder
    #                       drop is silent by construction (pure function), so it is
    #                       surfaced here alongside record_spec's vocabulary drops.
    #   dropped_out_of_envelope  same channel, reason out_of_envelope — a modern-shaped
    #                       Seagate capacity outside its family envelope (or an unlisted
    #                       family). Counted separately from off_grid so an over-tight
    #                       envelope is distinguishable from an incomplete grid.
    # Both decoder-drop counters are tallied EVEN when the drop emptied result.specs
    # (capacity-only decodes: legacy WD, family-unmapped Seagate) — that empty-specs
    # path writes nothing but must never be silent.
    dropped_no_schema: Counter = Counter()
    dropped_out_of_enum: Counter = Counter()
    dropped_off_grid: Counter = Counter()
    dropped_out_of_envelope: Counter = Counter()
    # "card_category->decoded_commodity" -> cards whose decoded commodity LOST the category
    # ladder (set_category) against a different existing category — the decoded specs are then
    # rejected by record_spec's schema lookup AND the maker write is skipped (same cross-
    # commodity guard), so the decode contributes nothing. A recurring pair is the signal
    # that the category alias map (app/services/category_normalizer.py) needs another entry.
    skipped_category_conflict: Counter = Counter()
    # "existing_maker->decoded_vendor" -> cards whose decode maker LOST the manufacturer
    # ladder against a DIFFERENT existing value (e.g. decode says Samsung, card holds Hynix
    # at vendor tier). set_manufacturer's losing path logs at DEBUG only (invisible at the
    # production INFO level), so — exactly like skipped_category_conflict — the loss is
    # surfaced here as an aggregate WARNING after the batch. Same-value losses (existing
    # higher tier already agrees) are NOT conflicts and are not counted.
    skipped_maker_conflict: Counter = Counter()
    for card_id in card_ids:
        # Per-card isolation: a single bad card must never abort decode for the rest of the batch.
        try:
            card = db.get(MaterialCard, card_id)
            if card is None:
                continue
            result = decode_mpn(card.display_mpn, card.manufacturer)
            if result is None:
                continue
            for spec_key, value in result.dropped.items():
                counter = (
                    dropped_out_of_envelope
                    if result.drop_reasons.get(spec_key) == DROP_OUT_OF_ENVELOPE
                    else dropped_off_grid
                )
                counter[f"{result.commodity}.{spec_key}={value}"] += 1
            if not result.specs:
                # Every decoded value failed its plausibility gate (counted above):
                # nothing trustworthy remains — no category/maker/spec writes, and the
                # card is not counted as decoded (the decode contributed nothing).
                continue
            card_cat = (card.category or "").lower().strip()
            cache = schema_caches.get(result.commodity)
            if cache is None:
                cache = schema_caches[result.commodity] = load_schema_cache(db, result.commodity)
            for spec_key, value in result.specs.items():
                schema = cache.get((result.commodity, spec_key))
                if schema is None:
                    dropped_no_schema[f"{result.commodity}.{spec_key}"] += 1
                elif schema.data_type == "enum" and schema.enum_values and str(value) not in schema.enum_values:
                    dropped_out_of_enum[f"{result.commodity}.{spec_key}={value}"] += 1
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
                # (purging the old commodity's stale facets) but never overwrites a vendor/manual
                # category.
                did_categorize = set_category(card, result.commodity, DECODE_SOURCE, result.confidence)
                # EXPLICIT cross-commodity guard, shared by the specs AND the W4 maker write:
                # if the card's category (post-ladder) is not the decoded commodity, the decode's
                # commodity claim LOST arbitration — which means the regex match itself is suspect
                # (a false-positive scheme hit on a higher-tier-categorized card), so the decode
                # contributes NOTHING: no specs (a drive's capacity must never land on a non-drive
                # card) and no maker (the maker claim rides the same suspect match). Do not rely
                # on schema-cache keying to enforce the spec half: the schemas overlap across
                # commodities (capacity_gb exists for hdd/ssd/dram), so a cache-miss is an
                # accident of plumbing, not an invariant.
                category_agrees = (card.category or "").lower().strip() == result.commodity
                did_set_maker = False
                maker_conflict_pair = None
                if category_agrees:
                    # Dual-brand W4: the decode's vendor IS the actual maker (the regex gate
                    # is manufacturer-scheme-specific), so write it through the maker ladder
                    # at mpn_decode/0.9 — it corrects a legacy OEM label sitting in
                    # `manufacturer` (unprovenanced pre-ladder data, tier 50) but never
                    # overwrites a vendor-API (90 — authoritative_enrichment_service routes
                    # its maker writes through the ladder) / trio_source (95) / manual (100)
                    # value.
                    existing_maker = card.manufacturer
                    did_set_maker = set_manufacturer(card, result.vendor, DECODE_SOURCE, MAKER_CONFIDENCE)
                    if not did_set_maker and existing_maker:
                        incoming_maker = normalize_brand_name(db, result.vendor)
                        if existing_maker != incoming_maker:
                            maker_conflict_pair = f"{existing_maker}->{incoming_maker}"
                if not category_agrees:
                    card_written = 0
                else:
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
            if did_set_maker:
                manufacturers_set += 1
            if maker_conflict_pair:
                skipped_maker_conflict[maker_conflict_pair] += 1
            if did_categorize:
                categorized += 1
            elif card_cat and card_cat != result.commodity:
                # The category write lost the ladder against a DIFFERENT existing category —
                # the decoded specs AND the W4 maker write were skipped too (shared
                # cross-commodity guard), so surface the pair.
                skipped_category_conflict[f"{card_cat}->{result.commodity}"] += 1
        except Exception:
            failed += 1
            logger.exception("mpn-decode: failed on card_id={}", card_id)
    if dropped_no_schema or dropped_out_of_enum or dropped_off_grid or dropped_out_of_envelope:
        logger.warning(
            "mpn-decode: {} decoded spec values dropped — no commodity_spec_schemas row for {}; "
            "value outside the live enum_values for {} "
            "(decoder and seeded schemas have drifted; see tests/test_mpn_decoder_seed_sync.py); "
            "off the shipped-capacity grid for {} (the decoder refused an implausible value — "
            "see storage.HDD_SHIPPED_CAPACITY_GB); outside the Seagate family envelope for {} "
            "(truncated/malformed string or unlisted family — see storage._SEAGATE_ENVELOPE)",
            sum(dropped_no_schema.values())
            + sum(dropped_out_of_enum.values())
            + sum(dropped_off_grid.values())
            + sum(dropped_out_of_envelope.values()),
            dict(dropped_no_schema),
            dict(dropped_out_of_enum),
            dict(dropped_off_grid),
            dict(dropped_out_of_envelope),
        )
    if skipped_category_conflict:
        logger.warning(
            "mpn-decode: {} cards kept their existing category — the decoded commodity lost the "
            "category ladder for {} (a recurring pair may mean category_normalizer.CATEGORY_ALIASES "
            "needs an entry)",
            sum(skipped_category_conflict.values()),
            dict(skipped_category_conflict),
        )
    if skipped_maker_conflict:
        logger.warning(
            "mpn-decode: {} cards kept their existing manufacturer — the decode maker lost the "
            "ladder against a DIFFERENT existing value for {} (a recurring pair is a genuine "
            "data-conflict signal: check the card's higher-tier maker evidence vs the MPN scheme)",
            sum(skipped_maker_conflict.values()),
            dict(skipped_maker_conflict),
        )
    if written or categorized or failed:
        logger.info(
            "mpn-decode: wrote {} specs across {} cards ({} newly categorized, {} cards failed)",
            written,
            decoded_cards,
            categorized,
            failed,
        )
    return {
        "decoded": decoded_cards,
        "written": written,
        "categorized": categorized,
        "manufacturers_set": manufacturers_set,
        "failed": failed,
        "skipped_category_conflict": sum(skipped_category_conflict.values()),
        "skipped_maker_conflict": sum(skipped_maker_conflict.values()),
    }
