"""OEM crosswalk enrichment — spare-PN cards inherit category + specs from their cached
PartSurfer/PSREF resolution (oem_crosswalk rows), zero network.

What: ONE deterministic writer pass (Pass B of the OEM web-resolution feature), gated by
``settings.oem_crosswalk_enrich_enabled``, structurally cloned from
fru_crosswalk_enrich.py: ONE batched query joins the batch cards'
``normalize_mpn_key(display_mpn)`` to ``oem_crosswalk`` rows with status='resolved';
per-card SAVEPOINT; does not commit (the worker's batch-final commit persists
everything together). Per matched card, with ``source = "partsurfer"`` (vendor hpe) or
``"psref"`` (vendor lenovo) — both ALREADY registered in spec_tiers.SOURCE_TIER at
tier 80, so the F1 ladder arbitrates every write (mpn_decode 85 / vendor APIs 90 /
trio_source 95 always beat this pass; it always beats web_search 70 / spec_extraction
60 / ai_guess 40 — no per-writer pre-gates):

1. **Agreement gate**: multiple resolved rows for the spare_norm that disagree on
   ``canonical_mpn_norm`` → skip the card (counted ``canonical_conflict`` —
   strict-intersect spirit).
2. **Decode channel**: ``decode_mpn(canonical_mpn_raw, canonical_manufacturer)`` →
   ``record_spec(source=source, confidence=0.90)`` per spec; category from the decode
   commodity via ``set_category(card, commodity, source, 0.90)`` ONLY when the card has
   none — an existing DIFFERENT category skips the card entirely (``category_mismatch``).
3. **Title channel**: ``extract_desc(f"{title} {canonical_mpn_raw}",
   commodity_hint=card.category if in SPEC_COMMODITIES else None)`` →
   ``record_spec(source=source, confidence=0.85)`` — intra-tier-80, the decode
   channel's 0.90 wins conflicts via the ladder. This is the CPU path today: resolved
   Xeon/Core model strings hit desc_extractor/cpu.py + cpu_model_specs.json → all six
   facets. NEVER writes a category.
4. **Cross-reference**: appends {"mpn", "manufacturer", "source"} to
   ``card.cross_references``, deduped on normalized mpn + source.
5. **Status**: if (category written OR ≥1 spec written) AND the card is not VERIFIED:
   upgrade to OEM_SOURCED + stamp enrichment_source/enriched_at and merge an
   ``enrichment_provenance["oem_crosswalk"]`` audit entry. Service spares are the
   population distributors miss by construction; the upgrade short-circuits
   enrich_card's early-return and saves up to 3 web calls per card. EXCEPTION:
   ``-B\\d{2}`` OPTION KITS (OPTION_KIT_RE) are widely distributor-catalogued, so an
   UNENRICHED option kit only takes the spec/xref writes (counted
   ``option_kit_deferred``) and keeps its free tier-90 connector pass — it accepts the
   uplift once a connector attempt has already missed (any non-UNENRICHED status).

Freshness (negative cache) also lives here: ``resolved`` rows are PERMANENT;
``no_match`` rows block re-resolution for NO_MATCH_RETRY_DAYS (90) from
``looked_up_at``; a stale no_match row is updated in place on retry.
``pending_resolution`` is the shared selector — and ``apply_resolution`` the shared
row writer (the single keeper of the status×canonical nullability invariant and the
no_match ``source_domain=''`` sentinel) — for Pass A and the backfill CLI.

Called by: app/services/enrichment_worker/worker.py (run_one_batch — Pass B over the
           FULL batch ids BEFORE the per-card core loop; pending_resolution feeds
           Pass A), app/management/backfill_oem_crosswalk.py (pending_resolution).
Depends on: models.OemCrosswalk, models.MaterialCard, constants.OemCrosswalkStatus,
            constants.MaterialEnrichmentStatus, mpn_decoder.decode_mpn (pure),
            desc_extractor.extract_desc (pure), utils.normalization.normalize_mpn_key,
            spec_tiers.set_category, spec_write_service.record_spec/load_schema_cache.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from loguru import logger
from sqlalchemy.orm import Session

from app.constants import MaterialEnrichmentStatus, OemCrosswalkStatus
from app.models import MaterialCard, OemCrosswalk
from app.services.desc_extractor import extract_desc
from app.services.desc_extractor._common import SPEC_COMMODITIES
from app.services.enrichment_worker.oem_classifier import OPTION_KIT_RE
from app.services.mpn_decoder import decode_mpn
from app.services.spec_tiers import set_category
from app.services.spec_write_service import load_schema_cache, record_spec
from app.utils.normalization import normalize_mpn_key

if TYPE_CHECKING:
    from app.services.enrichment_worker.oem_crosswalk_resolver import OemResolveResult

# Negative-cache retry window: a no_match row blocks re-resolution for 90 days from
# looked_up_at (uncatalogued OEM service parts rarely become catalogued). resolved
# rows are permanent — never re-fetched.
NO_MATCH_RETRY_DAYS = 90

# vendor → SOURCE_TIER source string (both already registered at tier 80 — no
# SOURCE_TIER edit). The ladder, not run order or these confidences, arbitrates.
SOURCE_BY_VENDOR: dict[str, str] = {"hpe": "partsurfer", "lenovo": "psref"}

# Decode channel: deterministic decode of the resolved canonical MPN.
OEM_DECODE_CONFIDENCE = 0.90
# Title channel: desc-grammar parse of the OEM page title — intra-tier-80, the decode
# channel's 0.90 wins conflicting keys via the ladder's confidence tie-break.
OEM_TITLE_CONFIDENCE = 0.85


def pending_resolution(
    db: Session,
    spare_norms: Iterable[str],
    vendor: str,
    now: datetime | None = None,
) -> dict[str, OemCrosswalk | None]:
    """Return ``{spare_norm: stale_no_match_row_or_None}`` for norms needing resolution.

    A norm is EXCLUDED when it has a fresh row for *vendor*: any ``resolved`` row
    (permanent), or a ``no_match`` row younger than NO_MATCH_RETRY_DAYS. A norm with
    only STALE no_match rows maps to the stale row (the caller updates it in place —
    upsert on the unique key); a never-looked-up norm maps to ``None`` (insert).
    """
    norms = [n for n in dict.fromkeys(spare_norms) if n]
    if not norms:
        return {}
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=NO_MATCH_RETRY_DAYS)

    rows_by_norm: dict[str, list[OemCrosswalk]] = {}
    rows = db.query(OemCrosswalk).filter(OemCrosswalk.spare_norm.in_(norms), OemCrosswalk.vendor == vendor).all()
    for row in rows:
        rows_by_norm.setdefault(row.spare_norm, []).append(row)

    pending: dict[str, OemCrosswalk | None] = {}
    for norm in norms:
        existing = rows_by_norm.get(norm, [])
        if any(r.status == OemCrosswalkStatus.RESOLVED for r in existing):
            continue  # permanent positive cache
        no_matches = [r for r in existing if r.status == OemCrosswalkStatus.NO_MATCH]
        fresh = [r for r in no_matches if r.looked_up_at is not None and _aware(r.looked_up_at) > cutoff]
        if fresh:
            continue  # fresh negative cache — blocked for 90 days
        pending[norm] = min(no_matches, key=lambda r: r.id) if no_matches else None
    return pending


def _aware(dt: datetime) -> datetime:
    """Coerce a naive UTC timestamp (SQLite round-trip) to aware for comparison."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def apply_resolution(
    row: OemCrosswalk | None,
    result: OemResolveResult,
    *,
    display_mpn: str,
    spare_norm: str,
    vendor: str,
    now: datetime | None = None,
) -> OemCrosswalk:
    """Build (``row=None``) or refresh in place (a stale no_match row) the
    ``oem_crosswalk`` row for one resolver outcome.

    The SINGLE row writer shared by worker Pass A and the backfill CLI — the one place
    the status×canonical nullability invariant (ck_oem_crosswalk_status_canonical) and
    the no_match ``source_domain=''`` sentinel (what lets uq_oem_crosswalk_edge dedupe
    negatives) are maintained, so the two writers cannot drift. String fields are
    clamped to their column widths: the values are LLM output, and PostgreSQL raises
    DataError on overflow where the SQLite suite silently passes. The caller owns
    ``db.add`` (when new) and flush/commit.
    """
    if row is None:
        row = OemCrosswalk(spare_raw=display_mpn[:64], spare_norm=spare_norm[:64], vendor=vendor)
    resolved = result.status == OemCrosswalkStatus.RESOLVED
    row.status = result.status
    if resolved:
        # __post_init__ guarantees canonical_mpn/source_url are present and the
        # resolver's shape guard caps canonical length — the clamps are belt-and-braces.
        row.canonical_mpn_raw = (result.canonical_mpn or "")[:64]
        row.canonical_mpn_norm = normalize_mpn_key(result.canonical_mpn)[:64]
        row.canonical_manufacturer = (result.manufacturer or "")[:128] or None
        row.title = result.title
        row.confidence = result.confidence
        row.source_url = result.source_url
        row.source_domain = (result.source_domain or "")[:128]
    else:
        row.canonical_mpn_raw = None
        row.canonical_mpn_norm = None
        row.canonical_manufacturer = None
        row.title = None
        row.confidence = None
        row.source_url = None
        row.source_domain = ""  # NOT NULL sentinel — see the model docstring
    row.payload = result.payload
    row.looked_up_at = now or datetime.now(timezone.utc)
    return row


def oem_crosswalk_and_record_specs(db: Session, card_ids: list[int]) -> Counter:
    """OEM-crosswalk-enrich the spare-PN cards among *card_ids* from their cached
    resolved rows; write category/specs/cross-ref/status per the module contract.

    Returns a Counter with:
    - matched: cards with ≥1 resolved oem_crosswalk row for their display_mpn norm.
    - canonical_conflict: cards skipped — resolved rows disagree on the canonical norm.
    - category_mismatch: cards skipped — existing category contradicts the decode
      commodity (an existing category is authoritative: never overwritten, never
      written-around).
    - categorized: NULL-category cards categorized from the decode commodity.
    - decode_written / title_written: specs persisted per channel.
    - xref_added: cross_references entries appended (post-dedupe).
    - status_upgraded: cards upgraded to oem_sourced.
    - option_kit_deferred: UNENRICHED option-kit (-B\\d{2}) cards that took spec/xref
      writes but NOT the status uplift — they keep their free tier-90 connector pass
      (the cohort distributors DO catalogue); they upgrade once a connector attempt
      has missed.
    - failed: cards LOST to an exception — the per-card SAVEPOINT rolls back only that
      card's writes; the rest of the batch proceeds.
    """
    stats: Counter = Counter(
        matched=0,
        canonical_conflict=0,
        category_mismatch=0,
        categorized=0,
        decode_written=0,
        title_written=0,
        xref_added=0,
        status_upgraded=0,
        option_kit_deferred=0,
        failed=0,
    )

    # db.get per id is an identity-map hit (the worker loaded the batch on this
    # session). Distinct display_mpns can share one key, hence the list values.
    key_to_card_ids: dict[str, list[int]] = {}
    for card_id in card_ids:
        card = db.get(MaterialCard, card_id)
        if card is None:
            continue
        key = normalize_mpn_key(card.display_mpn)
        if key:
            key_to_card_ids.setdefault(key, []).append(int(card_id))
    if not key_to_card_ids:
        return stats

    # ONE crosswalk query for the whole batch (no N+1) — resolved rows only.
    rows = (
        db.query(OemCrosswalk)
        .filter(
            OemCrosswalk.spare_norm.in_(key_to_card_ids.keys()),
            OemCrosswalk.status == OemCrosswalkStatus.RESOLVED,
        )
        .all()
    )
    rows_by_norm: dict[str, list[OemCrosswalk]] = {}
    for row in rows:
        rows_by_norm.setdefault(row.spare_norm, []).append(row)
    if not rows_by_norm:
        return stats

    now = datetime.now(timezone.utc)
    schema_caches: dict[str, dict] = {}  # one schema load per commodity, reused across cards
    for spare_norm in sorted(rows_by_norm):
        spare_rows = rows_by_norm[spare_norm]
        spare_card_ids = key_to_card_ids[spare_norm]
        stats["matched"] += len(spare_card_ids)

        # Agreement gate (strict-intersect spirit): resolved rows from different
        # domains must agree on WHAT the spare relabels, or nothing is asserted.
        if len({r.canonical_mpn_norm for r in spare_rows}) > 1:
            stats["canonical_conflict"] += len(spare_card_ids)
            logger.warning(
                "oem-crosswalk: canonical conflict for spare_norm={} ({} rows disagree) — skipping",
                spare_norm,
                len(spare_rows),
            )
            continue
        # Deterministic pick among agreeing rows: highest confidence, then lowest id.
        row = sorted(spare_rows, key=lambda r: (-(r.confidence or 0.0), r.id))[0]
        source = SOURCE_BY_VENDOR.get(row.vendor, "partsurfer")

        for card_id in spare_card_ids:
            # Per-card isolation: a single bad card must never abort the rest.
            try:
                card = db.get(MaterialCard, card_id)
                if card is None:
                    continue
                decode = decode_mpn(row.canonical_mpn_raw, row.canonical_manufacturer)
                card_cat = (card.category or "").lower().strip()
                if decode is not None and card_cat and card_cat != decode.commodity:
                    # An existing category is authoritative — skip the card entirely.
                    stats["category_mismatch"] += 1
                    continue

                categorized = False
                decode_written = 0
                title_written = 0
                xref_added = 0
                status_upgraded = 0
                option_kit_deferred = 0
                # SAVEPOINT — record_spec flushes, so a DB-level failure would
                # otherwise poison the shared batch transaction. The nested txn rolls
                # back ONLY this card's writes; counters apply after a clean release.
                with db.begin_nested():
                    # ── Decode channel (confidence 0.90) ──
                    if decode is not None:
                        if not card_cat:
                            # The decode commodity is regex-gated against strict
                            # manufacturer schemes — a safe FILL for a missing category
                            # (record_spec requires a category, so this precedes the loop).
                            categorized = set_category(card, decode.commodity, source, OEM_DECODE_CONFIDENCE)
                        cache = schema_caches.get(decode.commodity)
                        if cache is None:
                            cache = schema_caches[decode.commodity] = load_schema_cache(db, decode.commodity)
                        # No pre-gate: the F1 ladder rejects any write that loses to a
                        # higher-(tier, confidence, updated_at) prior.
                        for spec_key, value in decode.specs.items():
                            if record_spec(
                                db,
                                card_id,
                                spec_key,
                                value,
                                source=source,
                                confidence=OEM_DECODE_CONFIDENCE,
                                schema_cache=cache,
                            ):
                                decode_written += 1

                    # ── Title channel (confidence 0.85 — decode's 0.90 wins intra-tier
                    # conflicts via the ladder). Reads card.category AFTER the decode
                    # channel may have filled it; NEVER writes a category.
                    title_cat = (card.category or "").lower().strip()
                    title_input = f"{row.title} {row.canonical_mpn_raw}" if row.title else row.canonical_mpn_raw
                    hint = title_cat if title_cat in SPEC_COMMODITIES else None
                    desc = extract_desc(title_input, commodity_hint=hint)
                    if desc is not None and desc.specs and title_cat:
                        t_cache = schema_caches.get(title_cat)
                        if t_cache is None:
                            t_cache = schema_caches[title_cat] = load_schema_cache(db, title_cat)
                        for spec_key, value in desc.specs.items():
                            if record_spec(
                                db,
                                card_id,
                                spec_key,
                                value,
                                source=source,
                                confidence=OEM_TITLE_CONFIDENCE,
                                schema_cache=t_cache,
                            ):
                                title_written += 1

                    # ── Cross-reference (dedupe on normalized mpn + source, like
                    # apply_cross_ref_verified's linkage record) ──
                    canonical_key = row.canonical_mpn_norm or normalize_mpn_key(row.canonical_mpn_raw)
                    xrefs = list(card.cross_references or [])
                    if not any(
                        isinstance(x, dict)
                        and normalize_mpn_key(x.get("mpn")) == canonical_key
                        and x.get("source") == source
                        for x in xrefs
                    ):
                        xrefs.append(
                            {
                                "mpn": row.canonical_mpn_raw,
                                "manufacturer": row.canonical_manufacturer,
                                "source": source,
                            }
                        )
                        card.cross_references = xrefs
                        xref_added = 1

                    # ── Status upgrade: service spares are the population distributors
                    # miss by construction — short-circuits enrich_card's early-return
                    # (saves up to 3 web calls/card). VERIFIED is never downgraded.
                    # OPTION KITS (-B\d{2}) are the exception: distributors DO catalogue
                    # them, so an UNENRICHED kit defers the uplift and keeps its FREE
                    # tier-90 connector pass (enrich_card runs this very batch); any
                    # other status means a connector attempt already happened
                    # (not_found/not_catalogued missed; web/ai ran after the
                    # authoritative tier missed) — the uplift then costs nothing.
                    if (categorized or decode_written or title_written) and (
                        card.enrichment_status != MaterialEnrichmentStatus.VERIFIED
                    ):
                        is_option_kit = bool(OPTION_KIT_RE.match((card.display_mpn or "").strip().upper()))
                        if is_option_kit and card.enrichment_status == MaterialEnrichmentStatus.UNENRICHED:
                            option_kit_deferred = 1
                        else:
                            card.enrichment_status = MaterialEnrichmentStatus.OEM_SOURCED
                            card.enrichment_source = source
                            card.enriched_at = now
                            prov = dict(card.enrichment_provenance or {})
                            prov["oem_crosswalk"] = {
                                "spare": card.display_mpn,
                                "canonical_mpn": row.canonical_mpn_raw,
                                "source_url": row.source_url,
                                "confidence": row.confidence,
                                "fetched_at": _aware(row.looked_up_at).isoformat() if row.looked_up_at else None,
                            }
                            card.enrichment_provenance = prov
                            status_upgraded = 1
                # Reached only on a clean savepoint release.
                stats["categorized"] += int(categorized)
                stats["decode_written"] += decode_written
                stats["title_written"] += title_written
                stats["xref_added"] += xref_added
                stats["status_upgraded"] += status_upgraded
                stats["option_kit_deferred"] += option_kit_deferred
            except Exception:
                stats["failed"] += 1
                logger.exception("oem-crosswalk: failed on card_id={}", card_id)

    if stats["decode_written"] or stats["title_written"] or stats["status_upgraded"] or stats["failed"]:
        logger.info(
            "oem-crosswalk: wrote {} decode + {} title specs across {} matched cards "
            "({} newly categorized, {} status upgrades, {} option-kit uplifts deferred, "
            "{} xrefs, {} canonical conflicts, {} category mismatches, {} failed)",
            stats["decode_written"],
            stats["title_written"],
            stats["matched"],
            stats["categorized"],
            stats["status_upgraded"],
            stats["option_kit_deferred"],
            stats["xref_added"],
            stats["canonical_conflict"],
            stats["category_mismatch"],
            stats["failed"],
        )
    return stats
