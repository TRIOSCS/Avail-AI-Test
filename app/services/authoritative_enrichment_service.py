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
from app.services.enrichment_worker.oem_classifier import classify_oem_vendor
from app.services.enrichment_worker.oem_extractor import (
    CrossRefResult,
    OemExtractResult,
    cross_reference_mpn,
    extract_oem_description,
)
from app.services.enrichment_worker.web_extractor import WebExtractResult, extract_part_from_web
from app.utils.claude_errors import ClaudeError
from app.utils.normalization import normalize_mpn_key

_RATE_COOLDOWN_SECONDS = 300  # 5 min

# Cost-optimized: free distributor APIs first, paid Nexar last (gaps only).
SOURCE_ORDER = ["digikey", "mouser", "element14", "oemsecrets", "nexar"]
# Nexar source_type is reported as "octopart" by its connector.
_SOURCE_TYPE_ALIASES = {"octopart": "nexar"}

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


def apply_authoritative(
    card: MaterialCard,
    merged: dict,
    provenance: dict,
    contributors: list[str],
) -> None:
    """Write merged authoritative fields + provenance onto the card."""
    for field, value in merged.items():
        setattr(card, field, value)
    card.enrichment_provenance = provenance
    card.enrichment_source = contributors[0] if contributors else card.enrichment_source
    card.enrichment_status = MaterialEnrichmentStatus.VERIFIED
    card.enriched_at = datetime.now(timezone.utc)


def apply_web_sourced(card: MaterialCard, result: WebExtractResult) -> None:
    """Write web-sourced fields + provenance onto the card.

    Only sets non-empty fields. Records per-field provenance entries for every field
    that was written, plus top-level metadata (web_sourced, confidence, source_urls,
    source_domains, fetched_at).
    """
    now = datetime.now(timezone.utc)
    fields = {
        "description": result.description,
        "manufacturer": result.manufacturer,
        "category": result.category,
        "datasheet_url": result.datasheet_url,
    }
    prov: dict = {
        "web_sourced": True,
        "confidence": result.confidence,
        "source_urls": result.source_urls,
        "source_domains": result.source_domains,
        "fetched_at": now.isoformat(),
    }
    for f, v in fields.items():
        if v:
            setattr(card, f, v)
            prov[f] = {
                "source": "web_search",
                "confidence": result.confidence,
                "fetched_at": now.isoformat(),
            }
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
    ``cross_ref`` provenance block so the whole chain is auditable.
    """
    now = datetime.now(timezone.utc)
    for field_name, value in merged.items():
        setattr(card, field_name, value)
    xrefs = list(card.cross_references or [])
    xrefs.append({"mpn": xr.resolved_mpn, "manufacturer": xr.manufacturer})
    card.cross_references = xrefs
    prov = dict(provenance)
    prov["cross_ref"] = {
        "oem_part": card.display_mpn,
        "resolved_mpn": xr.resolved_mpn,
        "linkage_source_url": xr.linkage_source_url,
        "linkage_source_domain": xr.linkage_source_domain,
        "confirmed_by": contributors[0] if contributors else None,
        "confidence": xr.confidence,
    }
    card.enrichment_provenance = prov
    card.enrichment_source = contributors[0] if contributors else "cross_ref"
    card.enrichment_status = MaterialEnrichmentStatus.VERIFIED
    card.enriched_at = now


def apply_oem_sourced(card: MaterialCard, result: OemExtractResult) -> None:
    """Write OEM-official description/category onto the card (status ``oem_sourced``).

    Description + category + datasheet only — never structured specs.
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
    fields = {
        "description": result.description,
        "category": result.category,
        "datasheet_url": result.datasheet_url,
        "manufacturer": result.manufacturer,
    }
    for f, v in fields.items():
        if v:
            setattr(card, f, v)
            prov[f] = {"source": "oem_official", "confidence": result.confidence, "fetched_at": iso}
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
    web_meter: dict | None = None,
) -> str:
    """Enrich one card: authoritative -> flagged AI inference -> not_found.

    Returns the resulting enrichment_status. Does not commit (caller controls txn).
    ``disabled`` accumulates sources that hit a quota/auth wall (skipped run-wide).
    ``cooldown`` accumulates sources that hit a transient rate limit (skipped until
    the cooldown window expires, not permanently disabled).

    ``web_meter`` (optional ``{"web_calls": int, "claude_ok": bool}``) is updated in place:
    ``web_calls`` counts each billable web-search call made (distributor web, cross-ref, OEM
    description); ``claude_ok`` is set True after ANY Claude call (incl. infer_part) returns
    without raising. The worker uses ``web_calls`` for the daily budget and ``claude_ok`` to
    reset its circuit breaker. Default None = no metering.

    CONCURRENCY INVARIANT: safe to run over a shared Session via asyncio.gather
    because, after its first ``await``, this function performs NO DB query/flush —
    only in-memory attribute writes on ``card`` — so synchronous session ops never
    interleave across awaits on the single-threaded event loop. Do NOT add a
    db.query()/db.flush() after the await without switching callers to per-card
    sessions, or concurrent runs (import script, enrichment worker) will corrupt
    the identity map. The cross-ref re-verification (``fetch_authoritative`` on the
    resolved MPN) is pure async connector I/O — no DB query/flush — so the invariant
    holds.
    """
    if card.enrichment_status == MaterialEnrichmentStatus.VERIFIED and not refresh:
        return MaterialEnrichmentStatus.VERIFIED

    conns = connectors if connectors is not None else _connectors_in_order(db)
    results = await fetch_authoritative(card.display_mpn, card.normalized_mpn, conns, disabled, cooldown)
    merged, provenance, contributors = merge_authoritative(card.normalized_mpn, results)

    if merged:
        apply_authoritative(card, merged, provenance, contributors)
        return MaterialEnrichmentStatus.VERIFIED

    web_enabled = not (disabled and "web_search" in disabled)

    # Distributor / manufacturer web tier.
    # CONCURRENCY INVARIANT: this await is pure async (no DB) — no DB query/flush follows it;
    # see the docstring above for the full invariant.
    if web_enabled:
        web = await extract_part_from_web(card.display_mpn, card.normalized_mpn)
        if web_meter is not None:
            web_meter["web_calls"] = web_meter.get("web_calls", 0) + 1
            web_meter["claude_ok"] = True
        if web.status == "web_sourced":
            apply_web_sourced(card, web)
            return MaterialEnrichmentStatus.WEB_SOURCED

    # OEM tiers — only for recognised OEM/FRU codes, only when the web budget is live.
    vendor = classify_oem_vendor(card.display_mpn)
    oem_attempted = False
    if vendor and web_enabled:
        oem_attempted = True
        # Tier 3: cross-reference, then INDEPENDENTLY re-verify against distributors.
        xr = await cross_reference_mpn(card.display_mpn, card.normalized_mpn, vendor)
        if web_meter is not None:
            web_meter["web_calls"] = web_meter.get("web_calls", 0) + 1
            web_meter["claude_ok"] = True
        if xr.status == "resolved" and xr.resolved_mpn:
            resolved_key = normalize_mpn_key(xr.resolved_mpn)
            xr_results = await fetch_authoritative(xr.resolved_mpn, resolved_key, conns, disabled, cooldown)
            xr_merged, xr_prov, xr_contrib = merge_authoritative(resolved_key, xr_results)
            if xr_merged:
                apply_cross_ref_verified(card, xr_merged, xr_prov, xr_contrib, xr)
                return MaterialEnrichmentStatus.VERIFIED
        # Tier 4: OEM-official description (single authoritative page).
        oem = await extract_oem_description(card.display_mpn, card.normalized_mpn, vendor)
        if web_meter is not None:
            web_meter["web_calls"] = web_meter.get("web_calls", 0) + 1
            web_meter["claude_ok"] = True
        if oem.status == "oem_sourced":
            apply_oem_sourced(card, oem)
            return MaterialEnrichmentStatus.OEM_SOURCED

    # No authoritative hit -> flagged inference.
    from app.services.ai_inference_fallback import infer_part

    inf = await infer_part(card.display_mpn)
    if web_meter is not None:
        web_meter["claude_ok"] = True
    now = datetime.now(timezone.utc)
    card.enriched_at = now
    if inf.status == "ai_inferred":
        card.description = inf.description
        card.category = inf.category
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

    # Terminal: not_catalogued only when an OEM pattern matched AND the OEM tiers ran.
    card.enrichment_status = (
        MaterialEnrichmentStatus.NOT_CATALOGUED if (vendor and oem_attempted) else MaterialEnrichmentStatus.NOT_FOUND
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
        MaterialEnrichmentStatus.AI_INFERRED: 0,
        MaterialEnrichmentStatus.NOT_FOUND: 0,
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
