"""Verified, source-attributed enrichment for MaterialCards.

Queries existing connectors in cost-optimized priority order, accepts a source's data
ONLY on an exact normalized-MPN match, and merges core attributes first-non-null-by-
priority while recording per-field provenance. Parts with no authoritative hit fall
through to a flagged Opus 4.8 inference (see ai_inference_fallback) — never a silent
guess.
"""

from __future__ import annotations

import asyncio
import time as _time
from datetime import datetime, timezone
from typing import Any

from loguru import logger
from sqlalchemy.orm import Session

from app.connectors.errors import ConnectorAuthError, ConnectorQuotaError, ConnectorRateLimitError
from app.constants import MaterialEnrichmentStatus
from app.models import MaterialCard
from app.services.enrichment_types import WebMeter
from app.services.enrichment_worker.oem_classifier import HIGH_PRECISION_VENDORS, classify_oem_vendor
from app.services.enrichment_worker.oem_extractor import (
    CrossRefResult,
    OemExtractResult,
    cross_reference_mpn,
    extract_oem_description,
)
from app.services.enrichment_worker.web_extractor import WebExtractResult, extract_part_from_web
from app.services.spec_tiers import set_category, set_manufacturer
from app.utils.claude_errors import ClaudeError
from app.utils.normalization import normalize_mpn_key

_RATE_COOLDOWN_SECONDS = 300  # 5 min

# Cost-optimized: free distributor APIs first, paid Nexar last (gaps only).
SOURCE_ORDER = ["digikey", "mouser", "element14", "oemsecrets", "nexar"]
# Nexar source_type is reported as "octopart" by its connector.
_SOURCE_TYPE_ALIASES = {"octopart": "nexar"}


def _vendor_ladder_source(connector_name: str) -> str:
    """Map a connector name to its registered F1-ladder source string.

    The five distributor APIs are registered in spec_tiers.SOURCE_TIER as ``{name}_api``
    (all tier 90) — same mapping enrichment.py uses for its connector category writes.
    """
    return f"{connector_name}_api"


CORE_FIELDS = [
    "description",
    "manufacturer",
    "category",
    "lifecycle_status",
    "package_type",
    "rohs_status",
    "pin_count",
    "datasheet_url",
]
# A part is "adequately resolved" (skip paid Nexar) once these are present.
_ADEQUATE = ("description", "manufacturer", "category")


def merge_authoritative(
    normalized_mpn: str, results_by_source: dict[str, list[dict]]
) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    """Merge connector results into (fields, provenance, contributors).

    Only exact normalized-MPN matches are considered. For each CORE_FIELD, the first
    source (in SOURCE_ORDER) with a non-null value wins.
    """
    merged: dict[str, Any] = {}
    provenance: dict[str, Any] = {}
    contributors: list[str] = []
    now = datetime.now(timezone.utc).isoformat()

    for source in SOURCE_ORDER:
        hits = results_by_source.get(source) or []
        exact = [h for h in hits if normalize_mpn_key(h.get("mpn_matched")) == normalized_mpn]
        if not exact:
            continue
        contributed = False
        for hit in exact:
            for field in CORE_FIELDS:
                if field in merged:
                    continue
                val = hit.get(field)
                if val is None or (isinstance(val, str) and not val.strip()):
                    continue
                merged[field] = val
                provenance[field] = {
                    "source": source,
                    "confidence": 1.0,
                    "fetched_at": now,
                    "matched_mpn": hit.get("mpn_matched"),
                }
                contributed = True
        if contributed and source not in contributors:
            contributors.append(source)
    return merged, provenance, contributors


def _connectors_in_order(db: Session) -> list:
    """Return enabled connectors filtered + ordered to SOURCE_ORDER."""
    from app.search_service import _build_connectors

    conns, _, _ = _build_connectors(db)
    by_name: dict[str, Any] = {}
    for c in conns:
        name = _SOURCE_TYPE_ALIASES.get(c.source_name, c.source_name)
        by_name.setdefault(name, c)
    return [by_name[n] for n in SOURCE_ORDER if n in by_name]


async def fetch_authoritative(
    display_mpn: str,
    normalized_mpn: str,
    connectors: list,
    disabled: set[str] | None = None,
    cooldown: dict[str, float] | None = None,
) -> dict[str, list[dict]]:
    """Query connectors in priority order; short-circuit before paid Nexar once
    adequate.

    A source that hits a quota or auth wall is added to ``disabled`` and skipped
    for the rest of the run (so we don't keep burning failed calls), and surfaced
    loudly. A source that hits a transient rate limit is added to ``cooldown`` (not
    permanently disabled) and skipped until the cooldown window expires.
    Transient per-MPN failures are logged and treated as no result.
    """
    results: dict[str, list[dict]] = {}
    now = _time.monotonic()
    for conn in connectors:
        name = _SOURCE_TYPE_ALIASES.get(conn.source_name, conn.source_name)
        if disabled is not None and name in disabled:
            continue
        if cooldown is not None and cooldown.get(name, 0) > now:
            continue  # rate-limit cooldown active
        if name == "nexar":
            merged, _, _ = merge_authoritative(normalized_mpn, results)
            if all(f in merged for f in _ADEQUATE):
                logger.debug("AUTH_ENRICH: {} adequately resolved, skipping nexar", normalized_mpn)
                break
        try:
            results[name] = await conn.search(display_mpn)
        except (ConnectorQuotaError, ConnectorAuthError) as e:
            # Source is unusable for the rest of the run — disable + surface loudly.
            if disabled is not None:
                disabled.add(name)
            logger.error("AUTH_ENRICH: {} DISABLED for run ({}): {}", name, type(e).__name__, e)
            results[name] = []
        except ConnectorRateLimitError as e:
            if cooldown is not None:
                cooldown[name] = _time.monotonic() + _RATE_COOLDOWN_SECONDS
            logger.warning(
                "AUTH_ENRICH: {} rate-limited for {} (cooldown {}s): {}",
                name,
                normalized_mpn,
                _RATE_COOLDOWN_SECONDS,
                e,
            )
            results[name] = []
        except Exception as e:  # transient connector failure — non-fatal for this MPN
            logger.warning("AUTH_ENRICH: {} failed for {}: {}: {}", name, normalized_mpn, type(e).__name__, e)
            results[name] = []
    return results


def _apply_merged_core_fields(card: MaterialCard, merged: dict, provenance: dict) -> dict:
    """Write the merged connector fields onto *card*; return the provenance to persist.

    ``category`` and ``manufacturer`` are PROVENANCED columns — they NEVER go through
    raw setattr (a raw write leaves the old ``category_*``/``manufacturer_*`` columns
    attached to the new value and bypasses arbitration). They route through the F1
    ladder (``set_category`` / ``set_manufacturer``) at the contributing connector's
    registered ``{name}_api`` source (tier 90) with the merge confidence (1.0 — exact
    normalized-MPN match). The ladder decides how aggressively this tier overwrites:
    90 displaces decode (85) / OEM pages (80) / web (70) / AI guesses (40) but never
    trio_source (95) or a manual edit (100). A write the ladder rejects has its
    per-field provenance entry DROPPED so ``enrichment_provenance`` never claims a
    write that did not land (the LOSE branch still records a validation conflict when
    it contradicts a manual value). All other core fields keep their direct writes —
    they are not provenanced columns.
    """
    prov = dict(provenance)
    for field, value in merged.items():
        if field in ("category", "manufacturer"):
            entry = prov.get(field) or {}
            source = _vendor_ladder_source(entry.get("source", ""))
            confidence = float(entry.get("confidence") or 1.0)
            setter = set_category if field == "category" else set_manufacturer
            if not setter(card, value, source, confidence):
                prov.pop(field, None)
        else:
            setattr(card, field, value)
    return prov


def apply_authoritative(
    card: MaterialCard,
    merged: dict,
    provenance: dict,
    contributors: list[str],
) -> None:
    """Write merged authoritative fields + provenance onto the card.

    category/manufacturer route through the F1 ladder at ``{connector}_api``/90 — see
    ``_apply_merged_core_fields`` (ladder-rejected writes are dropped from the persisted
    provenance).
    """
    card.enrichment_provenance = _apply_merged_core_fields(card, merged, provenance)
    card.enrichment_source = contributors[0] if contributors else card.enrichment_source
    card.enrichment_status = MaterialEnrichmentStatus.VERIFIED
    card.enriched_at = datetime.now(timezone.utc)


def _apply_evidence_fields(
    card: MaterialCard,
    fields: dict[str, Any],
    *,
    source: str,
    confidence: float,
    prov: dict,
    fetched_at: str,
) -> None:
    """Write non-empty evidence-sourced *fields* onto *card* under one *source* string.

    category/manufacturer route through the F1 ladder at *source*'s registered tier (the
    ladder decides whether the write lands and rejects off-vocab categories); every other
    field is a direct write. *prov* (the caller's top-level provenance block) is mutated in
    place: each field that was actually written gets a per-field provenance entry; a
    ladder-rejected write gets NO entry. Shared by the web/OEM evidence tiers.
    """
    for f, v in fields.items():
        if not v:
            continue
        if f == "category":
            if not set_category(card, v, source, confidence):
                continue
        elif f == "manufacturer":
            if not set_manufacturer(card, v, source, confidence):
                continue
        else:
            setattr(card, f, v)
        prov[f] = {"source": source, "confidence": confidence, "fetched_at": fetched_at}


def apply_web_sourced(card: MaterialCard, result: WebExtractResult) -> None:
    """Write web-sourced fields + provenance onto the card.

    Only sets non-empty fields. Records per-field provenance entries for every field
    that was written, plus top-level metadata (web_sourced, confidence, source_urls,
    source_domains, fetched_at). category/manufacturer route through the F1 ladder at
    ``web_search``/70 (the ladder decides — web evidence fills empty cards and
    displaces AI guesses (40) but never decode (85) / vendor-API (90) / trio (95) /
    manual (100) provenance, and off-vocab categories are rejected, never persisted);
    a ladder-rejected write gets NO per-field provenance entry.
    """
    now = datetime.now(timezone.utc)
    iso = now.isoformat()
    prov: dict = {
        "web_sourced": True,
        "confidence": result.confidence,
        "source_urls": result.source_urls,
        "source_domains": result.source_domains,
        "fetched_at": iso,
    }
    _apply_evidence_fields(
        card,
        {
            "description": result.description,
            "manufacturer": result.manufacturer,
            "category": result.category,
            "datasheet_url": result.datasheet_url,
        },
        source="web_search",
        confidence=result.confidence,
        prov=prov,
        fetched_at=iso,
    )
    card.enrichment_source = "web_search"
    card.enrichment_status = MaterialEnrichmentStatus.WEB_SOURCED
    card.enrichment_provenance = prov
    card.enriched_at = now


def apply_cross_ref_verified(
    card: MaterialCard,
    merged: dict,
    provenance: dict,
    contributors: list[str],
    xr: CrossRefResult,
) -> None:
    """Write the resolved commodity MPN's distributor data onto an OEM/FRU card.

    Status becomes ``verified`` (the resolved MPN was independently confirmed against a
    distributor). Records the FRU<->MPN linkage in ``cross_references`` and a top-level
    ``cross_ref`` provenance block so the whole chain is auditable. The merged dict is
    the same distributor evidence apply_authoritative consumes, so category/manufacturer
    route through the F1 ladder identically (``{connector}_api``/90 — see
    ``_apply_merged_core_fields``; ladder-rejected writes are dropped from the persisted
    provenance).
    """
    now = datetime.now(timezone.utc)
    provenance = _apply_merged_core_fields(card, merged, provenance)
    xrefs = list(card.cross_references or [])
    xrefs.append({"mpn": xr.resolved_mpn, "manufacturer": xr.manufacturer})
    card.cross_references = xrefs
    confirmer = contributors[0] if contributors else None
    prov = dict(provenance)
    prov["cross_ref"] = {
        "oem_part": card.display_mpn,
        "resolved_mpn": xr.resolved_mpn,
        "linkage_source_url": xr.linkage_source_url,
        "linkage_source_domain": xr.linkage_source_domain,
        "confirmed_by": confirmer,
        "confidence": xr.confidence,
    }
    card.enrichment_provenance = prov
    card.enrichment_source = confirmer or "cross_ref"
    card.enrichment_status = MaterialEnrichmentStatus.VERIFIED
    card.enriched_at = now


def apply_oem_sourced(card: MaterialCard, result: OemExtractResult) -> None:
    """Write OEM-official description/category onto the card (status ``oem_sourced``).

    Description + category + datasheet only — never structured specs.
    category/manufacturer route through the F1 ladder at ``oem_official``/80 (the
    ladder decides — OEM-page evidence displaces web (70) / AI (40) provenance but
    never decode (85) / vendor-API (90) / trio (95) / manual (100), and off-vocab
    categories are rejected); a ladder-rejected write gets NO per-field provenance
    entry.
    """
    now = datetime.now(timezone.utc)
    iso = now.isoformat()
    prov: dict = {
        "oem_sourced": True,
        "confidence": result.confidence,
        "source_urls": result.source_urls,
        "source_domains": result.source_domains,
        "fetched_at": iso,
    }
    _apply_evidence_fields(
        card,
        {
            "description": result.description,
            "category": result.category,
            "datasheet_url": result.datasheet_url,
            "manufacturer": result.manufacturer,
        },
        source="oem_official",
        confidence=result.confidence,
        prov=prov,
        fetched_at=iso,
    )
    card.enrichment_source = "oem_official"
    card.enrichment_status = MaterialEnrichmentStatus.OEM_SOURCED
    card.enrichment_provenance = prov
    card.enriched_at = now


async def enrich_card(
    card: MaterialCard,
    db: Session,
    *,
    connectors: list | None = None,
    refresh: bool = False,
    disabled: set[str] | None = None,
    cooldown: dict[str, float] | None = None,
    web_meter: WebMeter | None = None,
    full_pipeline: bool = True,
) -> str:
    """Authoritative -> web -> OEM cross-ref/description -> flagged AI inference ->
    not_catalogued/not_found.

    Returns the resulting enrichment_status. Does not commit (caller controls txn).
    ``disabled`` accumulates sources that hit a quota/auth wall (skipped run-wide).
    ``cooldown`` accumulates sources that hit a transient rate limit (skipped until
    the cooldown window expires, not permanently disabled).

    ``full_pipeline=False`` is the worker's BULK lane (enrich_requested_at IS NULL,
    settings.enrichment_lane_split_enabled): only the FREE connector tier runs — the
    web tier, the OEM tiers and the Opus infer_part fallback are all skipped and a
    connector miss goes straight to the terminal branch (not_found — the OEM tiers
    never ran, so not_catalogued can never be concluded). This is CALL ROUTING ONLY:
    every write that does happen still arbitrates through the F1 ladder; no write
    pre-gates are added anywhere. Independently,
    settings.enrichment_skip_web_for_oem_mpns skips ONLY the web tier for
    OEM/FRU-shaped MPNs (any classify_oem_vendor hit) on every lane — the measured
    ~95% no-trusted-source reject class — while the OEM tiers + Opus fallback still
    run when ``full_pipeline`` is True.

    ``web_meter`` (optional :class:`WebMeter`) is updated in place: ``web_calls`` counts each
    web-search-enabled Claude tier attempt (distributor / cross-ref / OEM-description),
    reserved before dispatch so a call that bills then raises is still counted; ``claude_ok``
    is latched True after ANY Claude call (incl. infer_part) returns without raising. The
    worker uses ``web_calls`` for the daily budget and ``claude_ok`` to reset its circuit
    breaker. Default None = no metering.

    CONCURRENCY INVARIANT: safe to run over a shared Session via asyncio.gather
    because, after its first ``await``, every session op this function performs is
    SYNCHRONOUS (it contains no await), so it runs atomically between awaits on the
    single-threaded event loop and can never interleave mid-operation with another
    card's enrichment. The post-await session ops are the F1-ladder setters
    (``set_category``/``set_manufacturer``) called from the apply_* helpers AND from
    the ai_inferred branch's ``set_category`` below: ``set_manufacturer``'s
    alias-table SELECT (cached per-process after the first non-empty load) and
    ``set_category``'s stale-facet purge SELECT/DELETE, which fires only on a win
    that CHANGES an existing category.
    Do NOT add AWAITED DB work, a flush-and-read-back sequence, or anything that
    expires/refreshes shared ORM state after the first await without switching
    callers to per-card sessions — concurrent runs (import script, enrich_cards)
    would corrupt the identity map. The cross-ref re-verification
    (``fetch_authoritative`` on the resolved MPN) is pure async connector I/O — no
    DB query/flush — so the invariant holds.
    """
    if (
        card.enrichment_status in (MaterialEnrichmentStatus.VERIFIED, MaterialEnrichmentStatus.OEM_SOURCED)
        and not refresh
    ):
        return card.enrichment_status

    conns = connectors if connectors is not None else _connectors_in_order(db)
    results = await fetch_authoritative(card.display_mpn, card.normalized_mpn, conns, disabled, cooldown)
    merged, provenance, contributors = merge_authoritative(card.normalized_mpn, results)

    if merged:
        apply_authoritative(card, merged, provenance, contributors)
        return MaterialEnrichmentStatus.VERIFIED

    from app.config import settings

    web_enabled = not (disabled and "web_search" in disabled)
    vendor = classify_oem_vendor(card.display_mpn)
    # OEM/FRU-shaped MPNs skip the web tier on EVERY lane (reseller-only pages — the
    # measured ~95% no-trusted-source reject class); the OEM tiers below still run.
    skip_web_for_oem = vendor is not None and settings.enrichment_skip_web_for_oem_mpns

    # Distributor / manufacturer web tier.
    # CONCURRENCY INVARIANT: this await is pure async (no DB); the apply_web_sourced
    # that follows performs only SYNCHRONOUS F1-ladder session ops (set_manufacturer's
    # alias-table SELECT, set_category's stale-facet purge SELECT/DELETE) — see the
    # docstring above for the full invariant.
    if web_enabled and full_pipeline and not skip_web_for_oem:
        if web_meter is not None:
            web_meter.reserve_web_call()
        web = await extract_part_from_web(card.display_mpn, card.normalized_mpn)
        if web_meter is not None:
            web_meter.mark_claude_ok()
        if web.status == "web_sourced":
            apply_web_sourced(card, web)
            return MaterialEnrichmentStatus.WEB_SOURCED

    # OEM tiers — only for recognised OEM/FRU codes, only when the web budget is live.
    oem_attempted = False
    if vendor and web_enabled and full_pipeline:
        oem_attempted = True
        # Tier 3: cross-reference, then INDEPENDENTLY re-verify against distributors.
        if web_meter is not None:
            web_meter.reserve_web_call()
        xr = await cross_reference_mpn(card.display_mpn, card.normalized_mpn, vendor)
        if web_meter is not None:
            web_meter.mark_claude_ok()
        if xr.status == "resolved" and xr.resolved_mpn:
            resolved_key = normalize_mpn_key(xr.resolved_mpn)
            xr_results = await fetch_authoritative(xr.resolved_mpn, resolved_key, conns, disabled, cooldown)
            xr_merged, xr_prov, xr_contrib = merge_authoritative(resolved_key, xr_results)
            if xr_merged:
                apply_cross_ref_verified(card, xr_merged, xr_prov, xr_contrib, xr)
                return MaterialEnrichmentStatus.VERIFIED
        # Tier 4: OEM-official description (single authoritative page).
        if web_meter is not None:
            web_meter.reserve_web_call()
        oem = await extract_oem_description(card.display_mpn, card.normalized_mpn, vendor)
        if web_meter is not None:
            web_meter.mark_claude_ok()
        if oem.status == "oem_sourced":
            apply_oem_sourced(card, oem)
            return MaterialEnrichmentStatus.OEM_SOURCED

    # No authoritative hit -> flagged inference (full pipeline only — the bulk lane
    # skips the Opus fallback: measured 165 calls/day, 0 ladder-accepted writes ever).
    if full_pipeline:
        from app.services.ai_inference_fallback import infer_part

        inf = await infer_part(card.display_mpn)
        if web_meter is not None:
            web_meter.mark_claude_ok()
        now = datetime.now(timezone.utc)
        card.enriched_at = now
        if inf.status == "ai_inferred":
            card.description = inf.description
            # Through the F1 ladder: an Opus inference (claude_opus_inferred, tier 40) fills
            # an empty category but can never overwrite decode/vendor/TRIO provenance, and
            # off-vocab junk is rejected instead of persisted.
            set_category(card, inf.category, "claude_opus_inferred", inf.confidence)
            card.enrichment_source = "claude_opus_inferred"
            card.enrichment_status = MaterialEnrichmentStatus.AI_INFERRED
            # >= 0.95-confidence guess: added, but flagged for human reconfirmation so it
            # is never mistaken for verified data.
            card.enrichment_provenance = {
                "reconfirm_needed": True,
                "description": {
                    "source": "claude_opus_inferred",
                    "confidence": inf.confidence,
                    "fetched_at": now.isoformat(),
                },
            }
            return MaterialEnrichmentStatus.AI_INFERRED
    else:
        card.enriched_at = datetime.now(timezone.utc)

    # Terminal: not_catalogued only when a HIGH-PRECISION OEM pattern matched AND the OEM
    # tiers ran. The broad Dell 5-char pattern is excluded (see HIGH_PRECISION_VENDORS) so a
    # generic 5-char part missing every tier stays not_found (22h retry) instead of being
    # parked for ~30 days. `vendor in HIGH_PRECISION_VENDORS` is False when vendor is None.
    card.enrichment_status = (
        MaterialEnrichmentStatus.NOT_CATALOGUED
        if (vendor in HIGH_PRECISION_VENDORS and oem_attempted)
        else MaterialEnrichmentStatus.NOT_FOUND
    )
    card.enrichment_source = None
    card.enrichment_provenance = None
    return card.enrichment_status


async def enrich_cards(card_ids: list[int], db: Session, *, concurrency: int = 5, refresh: bool = False) -> dict:
    """Enrich many cards with bounded concurrency.

    Commits in batches of 50.
    """
    conns = _connectors_in_order(db)
    disabled: set[str] = set()
    cooldown: dict[str, float] = {}
    sem = asyncio.Semaphore(concurrency)
    counts: dict[str, int] = {
        MaterialEnrichmentStatus.VERIFIED: 0,
        MaterialEnrichmentStatus.WEB_SOURCED: 0,
        MaterialEnrichmentStatus.OEM_SOURCED: 0,
        MaterialEnrichmentStatus.AI_INFERRED: 0,
        MaterialEnrichmentStatus.NOT_FOUND: 0,
        MaterialEnrichmentStatus.NOT_CATALOGUED: 0,
    }

    async def _one(cid: int) -> None:
        card = db.get(MaterialCard, cid)
        if card is None:
            return
        async with sem:
            try:
                status = await enrich_card(
                    card, db, connectors=conns, refresh=refresh, disabled=disabled, cooldown=cooldown
                )
            except ClaudeError as e:
                # Claude backend down — leave the card unenriched and keep going so one
                # outage doesn't abort the whole batch. (The worker path records these
                # against its circuit breaker; this short-lived script just tallies them.)
                logger.warning("AUTH_ENRICH: Claude error for {}: {}", card.display_mpn, type(e).__name__)
                counts["claude_error"] = counts.get("claude_error", 0) + 1
                return
        counts[status] = counts.get(status, 0) + 1

    for i in range(0, len(card_ids), 50):
        batch = card_ids[i : i + 50]
        await asyncio.gather(*(_one(c) for c in batch))
        db.commit()
        logger.info("AUTH_ENRICH: committed {}/{} cards", min(i + 50, len(card_ids)), len(card_ids))
    if disabled:
        counts["disabled_sources"] = sorted(disabled)
        logger.error("AUTH_ENRICH: sources disabled this run (quota/auth): {}", sorted(disabled))
    return counts
