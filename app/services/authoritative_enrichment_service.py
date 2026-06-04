"""Verified, source-attributed enrichment for MaterialCards.

Queries existing connectors in cost-optimized priority order, accepts a source's data
ONLY on an exact normalized-MPN match, and merges core attributes first-non-null-by-
priority while recording per-field provenance. Parts with no authoritative hit fall
through to a flagged Opus 4.8 inference (see ai_inference_fallback) — never a silent
guess.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from loguru import logger
from sqlalchemy.orm import Session

from app.models import MaterialCard
from app.utils.normalization import normalize_mpn_key

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


def _source_of(hit: dict) -> str:
    st = str(hit.get("source_type", "")).lower()
    return _SOURCE_TYPE_ALIASES.get(st, st)


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
    by_name = {}
    for c in conns:
        name = _SOURCE_TYPE_ALIASES.get(c.source_name, c.source_name)
        by_name.setdefault(name, c)
    return [by_name[n] for n in SOURCE_ORDER if n in by_name]


async def fetch_authoritative(display_mpn: str, normalized_mpn: str, connectors: list) -> dict[str, list[dict]]:
    """Query connectors in priority order; short-circuit before paid Nexar once
    adequate."""
    results: dict[str, list[dict]] = {}
    for conn in connectors:
        name = _SOURCE_TYPE_ALIASES.get(conn.source_name, conn.source_name)
        if name == "nexar":
            merged, _, _ = merge_authoritative(normalized_mpn, results)
            if all(f in merged for f in _ADEQUATE):
                logger.debug("AUTH_ENRICH: {} adequately resolved, skipping nexar", normalized_mpn)
                break
        try:
            results[name] = await conn.search(display_mpn)
        except Exception as e:  # connector-level failure is non-fatal for this MPN
            logger.warning("AUTH_ENRICH: {} failed for {}: {}", name, normalized_mpn, type(e).__name__)
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
    card.enrichment_status = "verified"
    card.enriched_at = datetime.now(timezone.utc)
