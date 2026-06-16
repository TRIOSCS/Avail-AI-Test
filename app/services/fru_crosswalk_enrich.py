"""FRU crosswalk enrichment — FRU cards inherit specs from approved models AND from the
qual-sheet descriptions stored on their fru_links rows.

What: ONE pass, two deterministic evidence channels, ONE batched fru_links query
(rel_kind IN mfg_model + drive_pn), gated together by
settings.fru_crosswalk_enrich_enabled:

1. **Model decode** (``fru_matrix_decode``, tier 84, confidence 0.93): for each batch
   card whose key-normalized MPN matches ``fru_links.fru_norm``, decodes every linked
   ``mfg_model`` manufacturer model (and ``drive_pn`` links when
   ``settings.fru_crosswalk_drive_pn_decode_enabled`` — the gated widening, measured 0%
   misread) with the existing pure MPN decoders (zero LLM, zero network),
   STRICT-INTERSECTS the results (only spec keys present in every decode with equal
   values survive; a commodity disagreement skips the card entirely), and writes the
   agreed specs via record_spec. Category is filled from the agreed commodity ONLY when
   the card has none (via ``spec_tiers.set_category``, stamping tier-84 provenance; an
   existing DIFFERENT category skips the card before any write). The card's MANUFACTURER
   is filled (via ``spec_tiers.set_manufacturer`` at tier 84) ONLY when every decoded
   substitute identifies the SAME maker — the decoder's regex gate is
   manufacturer-scheme-specific, so a unanimous vendor is a DETERMINISTIC maker, never a
   prose inference (D4: the prohibition was on prose inference, not deterministic decode).
2. **Linked-description parse** (``fru_desc_parse``, tier 82, confidence 0.88): the
   FRU matrix qual sheets stored prose on fru_links rows (drive_pn rows carry strings
   like ``18TB 3.5 HDD 7.2K 12 Gb/s SAS``, mfg_model rows carry bare-drive prose like
   ``SSD; 2.5; 1.92 TB Samsung PM1733``). Every non-null description on the FRU's
   mfg_model/drive_pn links runs through the deterministic desc_extractor grammar
   (``extract_desc(description, commodity_hint=card.category)`` — the channel is
   pre-gated on the category being a SPEC_COMMODITIES member, so the hint is ALWAYS a
   SPEC_COMMODITIES member and extract_desc's hint-less path is unreachable from this
   module). Commodity agreement is judged over ALL extractions — a spec-less result
   (bare ``HDD, Hot Swap`` prose extracts commodity-only) is still commodity
   evidence: a commodity disagreement among the descriptions skips the desc channel
   for the card (counted in desc_commodity_conflict), and a UNANIMOUS commodity that
   contradicts the card's category skips it too (desc_category_mismatch — reachable
   only as hdd<->ssd via extract_desc's same-family lead refinement; the decode
   channel's "an existing category is authoritative, never written-around" rule
   applies to desc evidence identically). Spec-less extractions are then EXCLUDED
   from the per-key intersection (under intersect_decodes' absence-is-not-agreement
   rule one barren qual-sheet row would otherwise veto every key of its rich
   siblings; the first-party writer treats ``not result.specs`` as a no-op
   contribution for the same reason) and the survivors intersect with the SAME
   ``intersect_decodes`` contract: conflicting values are dropped and counted
   (desc_dropped_conflict, per card), a single extracting description passes all its
   specs. Runs in its OWN per-card SAVEPOINT after the decode channel's savepoint has
   RELEASED — a desc-side failure (counted in desc_failed, never in failed) can not
   take the card's decode writes or category fill with it, while a category the
   decode just filled still routes the extraction in the same batch. The desc channel
   NEVER writes a category (linked prose is not a regex-gated commodity proof) — a
   card with no category after the decode channel gets nothing from it (record_spec
   requires a category anyway).

record_spec's F1 source-tier ladder (app/services/spec_tiers.py) arbitrates every
write — fru_matrix_decode 84 sits between mpn_decode (85) and desc_parse (83);
fru_desc_parse 82 sits between desc_parse (83 — the card's OWN description outranks
a linked row's prose) and partsurfer (80). Neither channel can overwrite an
mpn_decode/desc_parse/vendor-API/trio_source prior and both always beat AI
spec_extraction (60), so there is NO per-writer confidence pre-gate here. The card's
manufacturer is written ONLY by the decode channel's deterministic maker propagation
(unanimous decoded vendor; the desc channel never writes a maker). Reverse direction
(a card that IS a mfg_model) gains
nothing here — its own MPN already decodes directly at tier 85 and its own
description already desc-parses at tier 83. Does not commit — the caller manages
the txn.

Called by: app/services/enrichment_worker/worker.py (run_one_batch, second pass,
           gated by settings.fru_crosswalk_enrich_enabled, over the FULL batch ids).
Depends on: models.FruLink, models.MaterialCard, constants.FruLinkKind,
            mpn_decoder.decode_mpn (pure), desc_extractor.extract_desc (pure),
            utils.normalization.normalize_mpn_key,
            spec_write_service.record_spec / load_schema_cache.
"""

from collections import Counter

from loguru import logger
from sqlalchemy.orm import Session

from app.config import settings
from app.constants import FruLinkKind
from app.models import FruLink, MaterialCard
from app.services.desc_extractor import DescResult, extract_desc
from app.services.desc_extractor._common import SPEC_COMMODITIES
from app.services.mpn_decoder import DecodeResult, decode_mpn
from app.services.spec_tiers import set_category, set_manufacturer
from app.services.spec_write_service import load_schema_cache, record_spec
from app.utils.normalization import normalize_mpn_key

# Source tag + confidence for the model-decode channel (see record_spec). Tier 84 on
# the F1 ladder (spec_tiers.SOURCE_TIER) — between mpn_decode (85: first-party decode of
# the card's own MPN is strictly stronger evidence) and desc_parse (83).
# DecodeResult.confidence (0.95) is deliberately ignored: a one-hop workbook mapping plus
# decode is weaker than a first-party decode. The ladder, not this confidence, arbitrates.
FRU_DECODE_SOURCE = "fru_matrix_decode"
FRU_DECODE_CONFIDENCE = 0.93

# Source tag + confidence for the linked-description channel. Tier 82 on the F1 ladder —
# between desc_parse (83: the card's OWN description is strictly stronger evidence than
# a linked qual-sheet row's prose) and partsurfer (80). DescResult.confidence (0.90) is
# deliberately ignored for the same one-hop reason as above; the ladder arbitrates.
FRU_DESC_SOURCE = "fru_desc_parse"
FRU_DESC_CONFIDENCE = 0.88

# Confidence for the FRU-side MAKER write (D4 maker propagation). The decode's vendor IS a
# DETERMINISTIC maker claim — every decoder's regex gate is manufacturer-scheme-specific
# (an ``M393A…`` part is unambiguously Samsung, an ``ST…`` part unambiguously Seagate), so a
# unanimous vendor across a FRU's decoded canonical models is a deterministic maker, never a
# prose inference (the D4 prohibition was about prose inference, not deterministic decode).
# One notch below the per-spec decode confidence (0.93), exactly like mpn_decoder/writer.py's
# MAKER_CONFIDENCE sits below DECODE_CONFIDENCE; the F1 ladder (set_manufacturer at tier 84)
# arbitrates, never this confidence.
FRU_MAKER_CONFIDENCE = 0.9


def _tally_schema_drop(
    commodity: str,
    spec_key: str,
    value: object,
    schema,
    dropped_no_schema: Counter,
    dropped_out_of_enum: Counter,
) -> None:
    """Tally a crosswalk spec value that record_spec will silently drop (DEBUG-only
    there).

    Mirrors record_spec's own schema/enum gate so the discard is surfaced as an
    aggregate WARNING after the batch (see the module docstring). Pure observability —
    no DB write, no effect on what record_spec persists. Shared by both write channels
    so the decode-side and desc-side tallies cannot drift.
    """
    if schema is None:
        dropped_no_schema[f"{commodity}.{spec_key}"] += 1
    elif schema.data_type == "enum" and schema.enum_values and str(value) not in schema.enum_values:
        dropped_out_of_enum[f"{commodity}.{spec_key}={value}"] += 1


def agree_vendor(results: "list[DecodeResult]") -> str | None:
    """Return the single vendor every decode agrees on, else ``None`` (pure, no DB).

    Maker propagation (D4) is deterministic-only: a maker is written for the FRU-side card
    ONLY when EVERY decoded canonical model identifies the SAME maker via its regex-gated
    scheme. A vendor disagreement (Seagate vs Western Digital among a FRU's substitutes) or
    an empty *results* list yields ``None`` — assert no maker rather than guess one. Callers
    must filter spec-less / unrecognized decodes before calling (same contract as
    ``intersect_decodes``: the caller already drops ``decode_mpn`` results without specs).
    """
    vendors = {r.vendor for r in results if r.vendor}
    return next(iter(vendors)) if len(vendors) == 1 else None


def intersect_decodes(
    results: "list[DecodeResult] | list[DescResult]",
) -> tuple[str | None, dict[str, str | int | float | bool], int]:
    """Strict-intersect per-FRU evidence results (pure, no DB).

    Accepts the decodes of a FRU's approved substitute models (DecodeResult) or the
    desc_extractor extractions of its linked qual-sheet descriptions (DescResult) —
    both carry the same ``commodity`` + ``specs`` shape, and the agreement rule is
    identical for both channels.

    Returns ``(agreed_commodity_or_None, agreed_specs, dropped_count)``:
    - ``None`` commodity signals a genuine commodity conflict — the evidence rows
      can't agree on what the part IS, so nothing may be asserted.
    - ``agreed_specs`` keeps only keys present in EVERY result with equal values
      (``==`` exact — decoders and extractors emit canonical enum strings / numbers).
      A key missing from any one result is dropped silently (absence is not
      agreement); a key present everywhere with differing values is dropped AND
      counted.

    Raises ``ValueError`` on empty *results* — the caller must filter no-evidence
    FRUs (zero decodable substitutes / zero extracting descriptions) BEFORE calling,
    so a ``None`` commodity here always means contradicting evidence, never missing
    evidence.

    Shared contract: every member must carry NON-EMPTY ``specs``. A spec-less member
    makes the strict intersection vacuously empty (absence is not agreement — it
    would silently veto every key of its rich siblings with dropped_count 0). BOTH
    callers must filter: decode_mpn can return a specs-empty result whose only
    content is the observability ``dropped`` dict (every value failed a plausibility
    gate), and desc_extractor returns commodity-only DescResults for spec-less
    prose (the desc caller keeps those in its own commodity-agreement check) —
    each caller drops spec-less members from the key intersection before calling.
    """
    if not results:
        raise ValueError("intersect_decodes requires at least one result")
    commodities = {r.commodity for r in results}
    if len(commodities) > 1:
        return (None, {}, 0)
    commodity = results[0].commodity
    agreed: dict[str, str | int | float | bool] = {}
    dropped = 0
    for spec_key, first_value in results[0].specs.items():
        rest = results[1:]
        if any(spec_key not in r.specs for r in rest):
            continue  # absent from a sibling result — not agreement, not a value conflict
        if all(r.specs[spec_key] == first_value for r in rest):
            agreed[spec_key] = first_value
        else:
            dropped += 1
    return (commodity, agreed, dropped)


def crosswalk_and_record_specs(db: Session, card_ids: list[int]) -> dict[str, int]:
    """Crosswalk-enrich the FRU cards among *card_ids*: decode their mfg_model links AND
    parse their linked qual-sheet descriptions; write the agreed specs.

    Returns {matched, decoded, written, categorized, manufacturers_set, desc_parsed,
    desc_written, failed, desc_failed, dropped_conflict, desc_dropped_conflict,
    commodity_conflict, desc_commodity_conflict, category_mismatch,
    desc_category_mismatch}:

    - matched: cards with ≥1 mfg_model or drive_pn link.
    - decoded: matched cards whose substitutes produced ≥1 decode that AGREED on a
      commodity AND that were not lost to a failure/rollback — truly-decoded cards
      only. Mismatch-skipped cards count (the substitutes reached an agreed verdict;
      the card's existing category blocked the writes); commodity-conflict cards do
      NOT (contradicting substitutes never produced a decoded verdict); rolled-back
      cards do not. Desc-only evidence does NOT count as decoded.
    - written: fru_matrix_decode specs persisted.
    - categorized: NULL-category cards categorized from the agreed decode commodity
      (the desc channel never categorizes).
    - manufacturers_set: cards whose manufacturer was written from the FRU's UNANIMOUS
      decoded vendor (D4 deterministic maker propagation — set_manufacturer at
      fru_matrix_decode/84). Only counted on a clean savepoint release, only when the
      decode commodity agrees with the card's category (shared cross-commodity guard),
      and only when every decoded substitute identifies the SAME maker (agree_vendor).
      The desc channel never writes a maker (linked prose is not a deterministic maker
      proof).
    - desc_parsed: cards that landed ≥1 fru_desc_parse spec from their linked
      descriptions.
    - desc_written: fru_desc_parse specs persisted.
    - failed: cards LOST to an exception — a raising decode fails every card on its
      FRU, a decode-channel write failure only its own card; either way the card
      gets nothing from EITHER channel. Per-failure tracebacks are logged below,
      but the aggregate must surface failures too, so the worker's one-line stats
      can distinguish a healthy no-op batch from a crashed one.
    - desc_failed: cards whose desc channel raised (extract/write). NEVER counted in
      failed — the desc channel runs in its own SAVEPOINT after the decode
      channel's has released, so the card keeps its decode writes and category
      fill; only the desc contribution is lost.
    - dropped_conflict: spec keys discarded by the DECODE value intersection —
      counted ONCE per FRU (computed once, before the card loop), independent of
      card outcomes.
    - desc_dropped_conflict: spec keys discarded by the DESC value intersection —
      counted per CARD (its extraction hint is the card's category, so it runs
      inside the card loop: the same conflicting description pair counts once for
      EACH card sharing the fru_norm), independent of the write outcome (a desc
      write failure after the intersection does not uncount).
    - commodity_conflict: cards skipped entirely on decode commodity disagreement
      (the desc channel is skipped for them too — substitutes that can't agree on
      what the part IS poison both channels; never counted in ``decoded``).
    - desc_commodity_conflict: cards whose desc channel was skipped because their
      linked descriptions disagree among THEMSELVES on the commodity (per card,
      like every desc-side counter).
    - category_mismatch: cards skipped because their existing category contradicts
      the agreed decode commodity.
    - desc_category_mismatch: cards whose desc channel was skipped because the
      descriptions' UNANIMOUS commodity contradicts the card's category (hdd<->ssd
      same-family lead refinement — the decode channel's category-mismatch rule
      applied to desc evidence).
    """
    stats = {
        "matched": 0,
        "decoded": 0,
        "written": 0,
        "categorized": 0,
        "manufacturers_set": 0,
        "desc_parsed": 0,
        "desc_written": 0,
        "failed": 0,
        "desc_failed": 0,
        "dropped_conflict": 0,
        "desc_dropped_conflict": 0,
        "commodity_conflict": 0,
        "desc_commodity_conflict": 0,
        "category_mismatch": 0,
        "desc_category_mismatch": 0,
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

    # ONE link query for the whole batch (no N+1) covering BOTH channels: mfg_model
    # rows feed the decode (deduped into a set so cross-sheet duplicates decode once)
    # and the descriptions of mfg_model + drive_pn rows feed the desc parse (deduped
    # so a description repeated across sheets extracts once).
    links = (
        db.query(FruLink)
        .filter(
            FruLink.fru_norm.in_(key_to_card_ids.keys()),
            FruLink.rel_kind.in_([FruLinkKind.MFG_MODEL.value, FruLinkKind.DRIVE_PN.value]),
        )
        .all()
    )
    # (c) drive_pn DECODE widening (gated). The decode channel reads mfg_model links
    # always; drive_pn links join the decode set ONLY under the flag (measured 0% misread —
    # drive_pn related parts are IBM/Lenovo FRU numbers that the regex gates reject, so the
    # widening writes nothing today but catches a future canonical drive_pn entry). The DESC
    # channel reads drive_pn descriptions regardless of this flag (unchanged below).
    decode_rel_kinds = {FruLinkKind.MFG_MODEL.value}
    if settings.fru_crosswalk_drive_pn_decode_enabled:
        decode_rel_kinds.add(FruLinkKind.DRIVE_PN.value)
    linked_frus: set[str] = set()  # every FRU with an in-scope link — the "matched" universe
    models_by_fru: dict[str, set[tuple[str, str | None]]] = {}
    descs_by_fru: dict[str, set[str]] = {}
    for link in links:
        linked_frus.add(link.fru_norm)
        if link.rel_kind in decode_rel_kinds:
            models_by_fru.setdefault(link.fru_norm, set()).add((link.related_raw, link.manufacturer))
        description = (link.description or "").strip()
        if description:
            descs_by_fru.setdefault(link.fru_norm, set()).add(description)

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
    for fru_norm in sorted(linked_frus):
        fru_card_ids = key_to_card_ids[fru_norm]
        stats["matched"] += len(fru_card_ids)
        descriptions = sorted(descs_by_fru.get(fru_norm, set()))
        # Per-FRU isolation: one raising decoder on a weird workbook string must
        # fail ONLY this FRU's cards, never abort the remaining FRUs or lose stats.
        try:
            # Decode + intersect once per FRU (pure, no DB); sorted for a deterministic
            # result order. None results (unrecognized schemes) contribute no evidence.
            models = models_by_fru.get(fru_norm, set())
            # `and r.specs`: decode_mpn can return a specs-EMPTY result whose only
            # content is the observability `dropped` dict (every decoded value failed
            # a plausibility gate) — it carries no evidence and, left in, would
            # vacuously veto every key of the strict intersection below.
            results = [
                r
                for raw, mfg in sorted(models, key=lambda m: m[0])
                if (r := decode_mpn(raw, mfg)) is not None and r.specs
            ]
        except Exception:
            stats["failed"] += len(fru_card_ids)
            logger.exception("fru-crosswalk: decode failed for fru_norm={}", fru_norm)
            continue
        commodity: str | None = None
        agreed: dict[str, str | int | float | bool] = {}
        # (d) D4 maker propagation: the UNANIMOUS maker across the FRU's decoded canonical
        # models (every decoder's regex gate is manufacturer-scheme-specific, so a single
        # agreed vendor is a deterministic maker, never a prose inference). None when the
        # substitutes disagree on the maker or none decoded — assert no maker then.
        vendor: str | None = agree_vendor(results)
        if results:
            commodity, agreed, dropped = intersect_decodes(results)
            stats["dropped_conflict"] += dropped  # once per FRU, independent of card outcomes
            if commodity is None:
                # Substitutes disagree on what the part IS — assert nothing, from
                # EITHER channel (the linked descriptions describe those same
                # contradicting substitutes). NOT counted in `decoded`: contradicting
                # substitutes never produced a decoded verdict, and counting them
                # would make a fully-conflicted batch read as healthily decoded.
                stats["commodity_conflict"] += len(fru_card_ids)
                continue
        elif not descriptions:
            continue  # zero decodable substitutes, zero descriptions — no evidence at all

        for card_id in fru_card_ids:
            # Per-card isolation: a single bad card must never abort the rest of the batch.
            try:
                card = db.get(MaterialCard, card_id)
                if card is None:
                    continue
                card_cat = (card.category or "").lower().strip()
                if commodity is not None and card_cat and card_cat != commodity:
                    # An existing category is authoritative — never overwritten,
                    # never written-around: the card is skipped entirely (the desc
                    # channel too — its prose describes the mismatching substitutes).
                    stats["decoded"] += 1
                    stats["category_mismatch"] += 1
                    continue
                if commodity is not None:
                    cache = schema_caches.get(commodity)
                    if cache is None:
                        cache = schema_caches[commodity] = load_schema_cache(db, commodity)
                    for spec_key, value in agreed.items():
                        _tally_schema_drop(
                            commodity,
                            spec_key,
                            value,
                            cache.get((commodity, spec_key)),
                            dropped_no_schema,
                            dropped_out_of_enum,
                        )
                    card_written = 0
                    did_set_maker = False
                    # SAVEPOINT 1 — decode channel: record_spec flushes, so a DB-level
                    # failure would otherwise poison the shared batch transaction. The
                    # nested txn rolls back ONLY this card's decode writes (including a
                    # categorize-from-null AND the maker write); counters increment after a
                    # clean release so a rolled-back card contributes nothing.
                    with db.begin_nested():
                        if not card_cat:
                            # The agreed commodity is regex-gated against strict manufacturer
                            # schemes AND agreed across ALL decoded substitutes — a safe FILL
                            # for a missing category (set_category stamps tier-84 provenance;
                            # existing=None always loses to the incoming write). record_spec
                            # requires a category, so this precedes the loop.
                            set_category(card, commodity, FRU_DECODE_SOURCE, FRU_DECODE_CONFIDENCE)
                        # (d) D4 maker propagation: the card reached here only because its
                        # category agrees with the decoded commodity (the cross-commodity
                        # guard above), so the unanimous decoded vendor is a SAFE deterministic
                        # maker for this card. set_manufacturer (tier 84) corrects a legacy OEM
                        # label sitting in `manufacturer` (unprovenanced, tier 50) but never
                        # overwrites a vendor-API (90) / trio_source (95) / manual (100) value —
                        # the F1 ladder, not run order, arbitrates. None vendor (substitutes
                        # disagree on the maker) writes nothing — never infer a maker.
                        if vendor is not None:
                            did_set_maker = set_manufacturer(card, vendor, FRU_DECODE_SOURCE, FRU_MAKER_CONFIDENCE)
                        # No pre-gate: record_spec's F1 tier ladder rejects any write that
                        # loses to a higher-(tier, confidence, updated_at) prior (mpn_decode
                        # 85 / vendor 90 / trio_source 95 all outrank tier 84).
                        for spec_key, value in agreed.items():
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
                    if did_set_maker:
                        stats["manufacturers_set"] += 1
            except Exception:
                stats["failed"] += 1
                logger.exception("fru-crosswalk: failed on card_id={}", card_id)
                continue  # the card is lost — the desc channel must not run on it

            # --- Linked-description channel (fru_desc_parse, tier 82) ---
            # Reads card.category AFTER the decode channel's savepoint RELEASED, so a
            # category the decode just filled routes the extraction (commodity_hint).
            # Pre-gated on SPEC_COMMODITIES exactly like the first-party desc writer
            # — the gate guarantees extract_desc always receives a SPEC_COMMODITIES
            # member as its hint here; a category-less card gets nothing (the desc
            # channel NEVER categorizes, and record_spec requires a category).
            # Failures count in desc_failed, NOT failed: the decode writes above are
            # already released and survive, so the card is not "lost".
            try:
                desc_cat = (card.category or "").lower().strip()
                if not descriptions or desc_cat not in SPEC_COMMODITIES:
                    continue
                desc_results = [r for d in descriptions if (r := extract_desc(d, commodity_hint=desc_cat)) is not None]
                if not desc_results:
                    continue
                # Commodity agreement is judged over ALL extractions — a spec-less
                # result (bare "HDD, Hot Swap" prose) is still commodity evidence.
                d_commodities = {r.commodity for r in desc_results}
                if len(d_commodities) > 1:
                    # The linked descriptions disagree on what the part IS (e.g. an
                    # HDD-prose row next to an SSD-prose row) — the desc channel
                    # asserts nothing for this card.
                    stats["desc_commodity_conflict"] += 1
                    logger.debug(
                        "fru-crosswalk: desc commodity conflict for card_id={} (fru_norm={})",
                        card_id,
                        fru_norm,
                    )
                    continue
                d_commodity = next(iter(d_commodities))
                if d_commodity != desc_cat:
                    # Unanimous prose that CONTRADICTS the card's category — reachable
                    # only as hdd<->ssd, via extract_desc's same-family lead refinement
                    # (an SSD-lead description on an hdd-hinted card returns
                    # commodity='ssd'). Mirror the decode channel's category_mismatch
                    # rule: the existing category is authoritative, never
                    # written-around — which also keeps the desc_cat-keyed schema
                    # lookups below correct by construction.
                    stats["desc_category_mismatch"] += 1
                    logger.debug(
                        "fru-crosswalk: desc category mismatch for card_id={} (fru_norm={}, {} prose on a {} card)",
                        card_id,
                        fru_norm,
                        d_commodity,
                        desc_cat,
                    )
                    continue
                # Spec-less extractions joined the commodity check above but must NOT
                # join the per-key intersection: under the absence-is-not-agreement
                # rule one barren row would silently veto EVERY key of its rich
                # siblings (the first-party writer treats `not result.specs` as a
                # no-op contribution for the same reason — desc_extractor/writer.py).
                spec_results = [r for r in desc_results if r.specs]
                if not spec_results:
                    continue
                _, d_agreed, desc_dropped = intersect_decodes(spec_results)
                # Counted before the write savepoint — like the decode channel's
                # per-FRU count, the intersection's verdict stands independent of the
                # write outcome below.
                stats["desc_dropped_conflict"] += desc_dropped
                if not d_agreed:
                    continue
                d_cache = schema_caches.get(desc_cat)
                if d_cache is None:
                    d_cache = schema_caches[desc_cat] = load_schema_cache(db, desc_cat)
                card_desc_written = 0
                # SAVEPOINT 2 — desc channel, isolated from the decode savepoint that
                # already RELEASED above (released-savepoint changes stay in the
                # enclosing transaction): a desc-side rollback can not take the card's
                # decode writes or category fill with it.
                with db.begin_nested():
                    for spec_key, value in d_agreed.items():
                        _tally_schema_drop(
                            desc_cat,
                            spec_key,
                            value,
                            d_cache.get((desc_cat, spec_key)),
                            dropped_no_schema,
                            dropped_out_of_enum,
                        )
                        # Same no-pre-gate rule: the ladder rejects any write
                        # losing to a desc_parse 83 / mpn_decode 85 / vendor
                        # 90 prior (fru_desc_parse is tier 82).
                        if record_spec(
                            db,
                            card_id,
                            spec_key,
                            value,
                            source=FRU_DESC_SOURCE,
                            confidence=FRU_DESC_CONFIDENCE,
                            schema_cache=d_cache,
                        ):
                            card_desc_written += 1
                # Reached only on a clean savepoint release.
                if card_desc_written:
                    stats["desc_parsed"] += 1
                stats["desc_written"] += card_desc_written
            except Exception:
                stats["desc_failed"] += 1
                logger.exception("fru-crosswalk: desc channel failed on card_id={} (decode writes kept)", card_id)

    if dropped_no_schema or dropped_out_of_enum:
        logger.warning(
            "fru-crosswalk: {} crosswalk spec values dropped — no commodity_spec_schemas row for {}; "
            "value outside the live enum_values for {} "
            "(decoder and seeded schemas have drifted; see tests/test_mpn_decoder_seed_sync.py)",
            sum(dropped_no_schema.values()) + sum(dropped_out_of_enum.values()),
            dict(dropped_no_schema),
            dict(dropped_out_of_enum),
        )
    # desc-side conflict/mismatch skips fire the summary too — systematic desc
    # conflicts after a FRU matrix re-ingest must not silently zero the channel.
    if (
        stats["written"]
        or stats["categorized"]
        or stats["manufacturers_set"]
        or stats["desc_written"]
        or stats["failed"]
        or stats["desc_failed"]
        or stats["desc_commodity_conflict"]
        or stats["desc_category_mismatch"]
    ):
        logger.info(
            "fru-crosswalk: wrote {} decode specs across {} decoded cards "
            "({} newly categorized, {} makers set) + {} linked-desc specs across {} desc-parsed cards "
            "({}/{} decode/desc keys dropped by intersection, {} desc commodity conflicts, "
            "{} desc category mismatches, {} cards failed, {} desc channels failed)",
            stats["written"],
            stats["decoded"],
            stats["categorized"],
            stats["manufacturers_set"],
            stats["desc_written"],
            stats["desc_parsed"],
            stats["dropped_conflict"],
            stats["desc_dropped_conflict"],
            stats["desc_commodity_conflict"],
            stats["desc_category_mismatch"],
            stats["failed"],
            stats["desc_failed"],
        )
    return stats
