"""FRU crosswalk decode enrichment — FRU cards inherit specs from approved models.

What: for each batch card whose key-normalized MPN matches ``fru_links.fru_norm``,
decodes every linked ``mfg_model`` manufacturer model with the existing pure MPN
decoders (zero LLM, zero network), STRICT-INTERSECTS the results (only spec keys
present in every decode with equal values survive; a commodity disagreement skips
the card entirely), and writes the agreed specs onto the FRU card via
``record_spec(source="fru_matrix_decode", confidence=0.93)`` — between mpn_decode
(0.95) and desc_parse (0.90) on the confidence ladder. Keys a prior pass already
holds at STRICTLY higher confidence are pre-gated here, never overwritten — this
file is one of the three pre-gate sites enumerated in record_spec's ARBITRATION
MODEL registry (app/services/spec_write_service.py), which the SP2 source-tier
ladder will consolidate. Category is filled from the agreed commodity ONLY when the
card has none (an existing category is authoritative and a mismatch skips the
card); the card's manufacturer is never touched. Reverse direction (a card that IS
a mfg_model) gains nothing here — its own MPN already decodes directly at 0.95.
Does not commit — the caller manages the txn.

Called by: app/services/enrichment_worker/worker.py (run_one_batch, second pass,
           gated by settings.fru_crosswalk_enrich_enabled, over the FULL batch ids).
Depends on: models.FruLink, models.MaterialCard, constants.FruLinkKind,
            mpn_decoder.decode_mpn (pure), utils.normalization.normalize_mpn_key,
            spec_write_service.record_spec / load_schema_cache.
"""

from collections import Counter

from loguru import logger
from sqlalchemy.orm import Session

from app.constants import FruLinkKind
from app.models import FruLink, MaterialCard
from app.services.mpn_decoder import DecodeResult, decode_mpn
from app.services.spec_write_service import load_schema_cache, record_spec
from app.utils.normalization import normalize_mpn_key

# Source tag + confidence for everything this pass writes (see record_spec). Sits
# between mpn_decode (0.95 — first-party decode of the card's own MPN is strictly
# stronger evidence) and desc_parse (0.90). DecodeResult.confidence (0.95) is
# deliberately ignored: a one-hop workbook mapping plus decode is weaker than a
# first-party decode.
FRU_DECODE_SOURCE = "fru_matrix_decode"
FRU_DECODE_CONFIDENCE = 0.93


def intersect_decodes(
    results: list[DecodeResult],
) -> tuple[str | None, dict[str, str | int | float | bool], int]:
    """Strict-intersect the decodes of a FRU's approved substitute models (pure, no DB).

    Returns ``(agreed_commodity_or_None, agreed_specs, dropped_count)``:
    - ``None`` commodity signals a genuine commodity conflict — the substitutes
      can't agree on what they ARE, so nothing may be asserted.
    - ``agreed_specs`` keeps only keys present in EVERY decode with equal values
      (``==`` exact — decoders emit canonical enum strings / numbers). A key missing
      from any one decode is dropped silently (absence is not agreement); a key
      present everywhere with differing values is dropped AND counted.

    Raises ``ValueError`` on empty *results* — the caller must filter no-evidence
    FRUs (zero decodable substitutes) BEFORE calling, so a ``None`` commodity here
    always means contradicting evidence, never missing evidence.
    """
    if not results:
        raise ValueError("intersect_decodes requires at least one DecodeResult")
    commodities = {r.commodity for r in results}
    if len(commodities) > 1:
        return (None, {}, 0)
    commodity = results[0].commodity
    agreed: dict[str, str | int | float | bool] = {}
    dropped = 0
    for spec_key, first_value in results[0].specs.items():
        rest = results[1:]
        if any(spec_key not in r.specs for r in rest):
            continue  # absent from a substitute — not agreement, not a value conflict
        if all(r.specs[spec_key] == first_value for r in rest):
            agreed[spec_key] = first_value
        else:
            dropped += 1
    return (commodity, agreed, dropped)


def crosswalk_and_record_specs(db: Session, card_ids: list[int]) -> dict[str, int]:
    """Crosswalk-decode the FRU cards among *card_ids* and write the agreed specs.

    Returns {matched, decoded, written, categorized, failed, dropped_conflict,
    commodity_conflict, category_mismatch}:

    - matched: cards with ≥1 mfg_model link.
    - decoded: matched cards whose substitutes produced ≥1 decode AND that were not
      lost to a failure/rollback (conflict-/mismatch-skipped cards count — their
      decode reached a verdict; rolled-back cards do not).
    - written: specs persisted.
    - categorized: NULL-category cards categorized from the agreed commodity.
    - failed: cards lost to an exception — a raising decode fails every card on its
      FRU, a write failure only its own card. Per-failure tracebacks are logged
      below, but the aggregate must surface failures too, so the worker's one-line
      stats can distinguish a healthy no-op batch from a crashed one.
    - dropped_conflict: spec keys discarded by the value intersection, counted ONCE
      per FRU (not per card sharing the key, and independent of per-card skips).
    - commodity_conflict: cards skipped on commodity disagreement.
    - category_mismatch: cards skipped because their existing category contradicts
      the agreed commodity.
    """
    stats = {
        "matched": 0,
        "decoded": 0,
        "written": 0,
        "categorized": 0,
        "failed": 0,
        "dropped_conflict": 0,
        "commodity_conflict": 0,
        "category_mismatch": 0,
    }

    # db.get per id is an identity-map hit (the worker loaded the batch on this
    # session). Distinct normalized_mpns can share one key, hence the list values.
    key_to_card_ids: dict[str, list[int]] = {}
    for card_id in card_ids:
        card = db.get(MaterialCard, card_id)
        if card is None:
            continue
        key = normalize_mpn_key(card.normalized_mpn)
        if key:
            key_to_card_ids.setdefault(key, []).append(int(card_id))
    if not key_to_card_ids:
        return stats

    # ONE link query for the whole batch (no N+1), grouped by FRU into a set of
    # (related_raw, manufacturer) so cross-sheet duplicates decode once.
    links = (
        db.query(FruLink)
        .filter(
            FruLink.fru_norm.in_(key_to_card_ids.keys()),
            FruLink.rel_kind == FruLinkKind.MFG_MODEL.value,
        )
        .all()
    )
    models_by_fru: dict[str, set[tuple[str, str | None]]] = {}
    for link in links:
        models_by_fru.setdefault(link.fru_norm, set()).add((link.related_raw, link.manufacturer))

    schema_caches: dict[str, dict] = {}  # one schema load per commodity, reused across cards
    # record_spec drops BOTH vocabulary-drift cases at DEBUG only (invisible at the
    # production INFO level), so the discard of crosswalk output is surfaced below as
    # an aggregate WARNING after the batch, exactly like mpn_decoder/writer.py:
    #   dropped_no_schema   "commodity.spec_key"       -> value with NO schema row
    #   dropped_out_of_enum "commodity.spec_key=value" -> enum value outside the LIVE
    #                       enum_values (the worker decodes against live DB rows,
    #                       which can lag a deploy's reseed or drift after a
    #                       failed/manual reseed).
    dropped_no_schema: Counter = Counter()
    dropped_out_of_enum: Counter = Counter()
    for fru_norm, models in models_by_fru.items():
        fru_card_ids = key_to_card_ids[fru_norm]
        stats["matched"] += len(fru_card_ids)
        # Per-FRU isolation: one raising decoder on a weird workbook string must
        # fail ONLY this FRU's cards, never abort the remaining FRUs or lose stats.
        try:
            # Decode + intersect once per FRU (pure, no DB); sorted for a deterministic
            # result order. None results (unrecognized schemes) contribute no evidence.
            results = [r for raw, mfg in sorted(models, key=lambda m: m[0]) if (r := decode_mpn(raw, mfg)) is not None]
        except Exception:
            stats["failed"] += len(fru_card_ids)
            logger.exception("fru-crosswalk: decode failed for fru_norm={}", fru_norm)
            continue
        if not results:
            continue  # zero decodable substitutes — no evidence, nothing asserted
        commodity, agreed, dropped = intersect_decodes(results)
        stats["dropped_conflict"] += dropped  # once per FRU, independent of card outcomes
        if commodity is None:
            # Substitutes disagree on what they ARE — assert nothing.
            stats["decoded"] += len(fru_card_ids)
            stats["commodity_conflict"] += len(fru_card_ids)
            continue

        for card_id in fru_card_ids:
            # Per-card isolation: a single bad card must never abort the rest of the batch.
            try:
                card = db.get(MaterialCard, card_id)
                if card is None:
                    continue
                card_cat = (card.category or "").lower().strip()
                if card_cat and card_cat != commodity:
                    # An existing category is authoritative — never overwritten,
                    # never written-around: the card is skipped entirely.
                    stats["decoded"] += 1
                    stats["category_mismatch"] += 1
                    continue
                cache = schema_caches.get(commodity)
                if cache is None:
                    cache = schema_caches[commodity] = load_schema_cache(db, commodity)
                for spec_key, value in agreed.items():
                    schema = cache.get((commodity, spec_key))
                    if schema is None:
                        dropped_no_schema[f"{commodity}.{spec_key}"] += 1
                    elif schema.data_type == "enum" and schema.enum_values and str(value) not in schema.enum_values:
                        dropped_out_of_enum[f"{commodity}.{spec_key}={value}"] += 1
                prior_specs = card.specs_structured or {}
                # SAVEPOINT per card: record_spec flushes, so a DB-level failure would
                # otherwise poison the shared batch transaction. The nested txn rolls back
                # ONLY this card (including a categorize-from-null); counters increment
                # after a clean release so a rolled-back card contributes nothing.
                with db.begin_nested():
                    if not card_cat:
                        # The agreed commodity is regex-gated against strict manufacturer
                        # schemes AND agreed across ALL decoded substitutes — a safe FILL
                        # for a missing category, never an overwrite. record_spec requires
                        # a category, so this precedes the loop.
                        card.category = commodity
                    card_written = 0
                    for spec_key, value in agreed.items():
                        prior = prior_specs.get(spec_key)
                        # Pre-SP2 overwrite guard: record_spec's cross-source rule is
                        # latest-write-wins, so keys already held at STRICTLY higher
                        # confidence (mpn_decode 0.95, vendor APIs) are skipped here.
                        # Registered in record_spec's ARBITRATION MODEL docstring.
                        if prior and float(prior.get("confidence") or 0.0) > FRU_DECODE_CONFIDENCE:
                            continue
                        if record_spec(
                            db,
                            card_id,
                            spec_key,
                            value,
                            source=FRU_DECODE_SOURCE,
                            confidence=FRU_DECODE_CONFIDENCE,
                            schema_cache=cache,
                        ):
                            card_written += 1
                # Reached only on a clean savepoint release.
                stats["decoded"] += 1
                stats["written"] += card_written
                if not card_cat:
                    stats["categorized"] += 1
            except Exception:
                stats["failed"] += 1
                logger.exception("fru-crosswalk: failed on card_id={}", card_id)

    if dropped_no_schema or dropped_out_of_enum:
        logger.warning(
            "fru-crosswalk: {} crosswalk spec values dropped — no commodity_spec_schemas row for {}; "
            "value outside the live enum_values for {} "
            "(decoder and seeded schemas have drifted; see tests/test_mpn_decoder_seed_sync.py)",
            sum(dropped_no_schema.values()) + sum(dropped_out_of_enum.values()),
            dict(dropped_no_schema),
            dict(dropped_out_of_enum),
        )
    if stats["written"] or stats["categorized"] or stats["failed"]:
        logger.info(
            "fru-crosswalk: wrote {} specs across {} decoded cards "
            "({} newly categorized, {} keys dropped by intersection, {} cards failed)",
            stats["written"],
            stats["decoded"],
            stats["categorized"],
            stats["dropped_conflict"],
            stats["failed"],
        )
    return stats
