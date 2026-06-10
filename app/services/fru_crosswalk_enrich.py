"""FRU crosswalk decode enrichment — FRU cards inherit specs from approved models.

What: for each batch card whose key-normalized MPN matches ``fru_links.fru_norm``,
decodes every linked ``mfg_model`` manufacturer model with the existing pure MPN
decoders (zero LLM, zero network), STRICT-INTERSECTS the results (only spec keys
present in every decode with equal values survive; a commodity disagreement skips
the card entirely), and writes the agreed specs onto the FRU card via
``record_spec(source="fru_matrix_decode", confidence=0.93)`` — between mpn_decode
(0.95) and desc_parse (0.90) on the confidence ladder. Category is filled from the
agreed commodity ONLY when the card has none (an existing category is authoritative
and a mismatch skips the card); the card's manufacturer is never touched. Reverse
direction (a card that IS a mfg_model) gains nothing here — its own MPN already
decodes directly at 0.95. Does not commit — the caller manages the txn.

Called by: app/services/enrichment_worker/worker.py (run_one_batch, second pass,
           gated by settings.fru_crosswalk_enrich_enabled, over the FULL batch ids).
Depends on: models.FruLink, constants.FruLinkKind, mpn_decoder.decode_mpn (pure),
            utils.normalization.normalize_mpn_key, spec_write_service.record_spec.
"""

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


def intersect_decodes(results: list[DecodeResult]) -> tuple[str | None, dict, int]:
    """Strict-intersect the decodes of a FRU's approved substitute models (pure, no DB).

    Returns ``(agreed_commodity_or_None, agreed_specs, dropped_count)``:
    - ``None`` commodity signals a commodity conflict (or an empty input) — the
      substitutes can't agree on what they ARE, so nothing may be asserted.
    - ``agreed_specs`` keeps only keys present in EVERY decode with equal values
      (``==`` exact — decoders emit canonical enum strings / numbers). A key missing
      from any one decode is dropped silently (absence is not agreement); a key
      present everywhere with differing values is dropped AND counted.
    """
    if not results:
        return (None, {}, 0)
    commodities = {r.commodity for r in results}
    if len(commodities) > 1:
        return (None, {}, 0)
    commodity = results[0].commodity
    agreed: dict = {}
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

    Returns {matched, decoded, written, categorized, dropped_conflict,
    commodity_conflict, category_mismatch}: cards with ≥1 mfg_model link; matched cards
    with ≥1 successful model decode; specs persisted; NULL-category cards categorized
    from the agreed commodity; spec keys discarded by the value intersection; cards
    skipped on commodity disagreement; cards skipped because their existing category
    contradicts the agreed commodity.
    """
    stats = {
        "matched": 0,
        "decoded": 0,
        "written": 0,
        "categorized": 0,
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
    for fru_norm, models in models_by_fru.items():
        # Decode + intersect once per FRU (pure, no DB); sorted for a deterministic
        # result order. None results (unrecognized schemes) contribute no evidence.
        results = [r for raw, mfg in sorted(models, key=lambda m: m[0]) if (r := decode_mpn(raw, mfg)) is not None]
        commodity, agreed, dropped = intersect_decodes(results)

        for card_id in key_to_card_ids[fru_norm]:
            stats["matched"] += 1
            if not results:
                continue
            # Per-card isolation: a single bad card must never abort the rest of the batch.
            try:
                if commodity is None:
                    # Substitutes disagree on what they ARE — assert nothing (D2).
                    stats["decoded"] += 1
                    stats["commodity_conflict"] += 1
                    continue
                card = db.get(MaterialCard, card_id)
                card_cat = (card.category or "").lower().strip()
                if card_cat and card_cat != commodity:
                    # An existing category is authoritative — never overwritten (D3).
                    stats["decoded"] += 1
                    stats["category_mismatch"] += 1
                    continue
                cache = schema_caches.get(commodity)
                if cache is None:
                    cache = schema_caches[commodity] = load_schema_cache(db, commodity)
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
                stats["dropped_conflict"] += dropped
                if not card_cat:
                    stats["categorized"] += 1
            except Exception:
                logger.exception("fru-crosswalk: failed on card_id={}", card_id)

    if stats["written"] or stats["categorized"]:
        logger.info(
            "fru-crosswalk: wrote {} specs across {} cards ({} newly categorized, {} keys dropped by intersection)",
            stats["written"],
            stats["decoded"],
            stats["categorized"],
            stats["dropped_conflict"],
        )
    return stats
