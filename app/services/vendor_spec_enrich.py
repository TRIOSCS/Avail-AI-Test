"""Vendor-API parametric enrichment WRITER — Mouser result → category + spec facets.

What: Mouser's API returns a rich, consistent DESCRIPTION (e.g. "Multilayer Ceramic
      Capacitors MLCC - SMD/SMT 16V 0.1uF X7R 0402 10%") but NOT structured parametric
      attributes. So the backbone is: distributor category + description → the
      ``desc_extractor`` grammar parses the description into spec facets, and the result
      is written through the F1 ladder at ``connector_desc`` / tier 84 — the SAME source
      identity + tier the shipped connector-description harvest uses
      (app/services/enrichment.py:_harvest_connector_enrichment).

      Reuse vs. replicate: the spec-write loop is the canonical
      ``desc_extractor/writer.py:_write_specs`` (record_spec per (key, value), ladder
      arbitrated, caller owns the SAVEPOINT) — reused directly, not re-implemented. The
      CATEGORIZE step is replicated minimally rather than calling
      ``writer.categorize_and_record`` because that helper (a) is fill-only/no-op when a
      category already exists and (b) categorizes from the DESCRIPTION grammar only —
      whereas this path is told to consider the distributor's own ``category`` string
      first (the measured-design rule), falling back to the description grammar (which
      ``categorize_and_record`` itself trusts) when the distributor phrase is off-vocab.
      Both paths write via ``set_category`` at connector_desc/0.9, so the ladder still
      owns arbitration. Mouser's cap/resistor category phrases ("…MLCC - SMD/SMT") have
      no normalize_category alias, so the grammar fallback is what categorizes them.

Called by: app/management/backfill_vendor_specs.py (the paced backfill CLI).
Depends on: spec_tiers.set_category, category_normalizer.normalize_category,
      desc_extractor.{extract_desc, categorizer.categorize_from_desc, writer._write_specs},
      spec_write_service.load_schema_cache. Does NOT commit — the caller owns the txn.
"""

from __future__ import annotations

from loguru import logger
from sqlalchemy.orm import Session

from app.models import MaterialCard
from app.services.category_normalizer import normalize_category
from app.services.desc_extractor import extract_desc
from app.services.desc_extractor.categorizer import categorize_from_desc
from app.services.desc_extractor.writer import _write_specs
from app.services.spec_tiers import set_category
from app.services.spec_write_service import load_schema_cache

# Same source identity + confidence the shipped connector-description harvest uses
# (desc_extractor/_common.CONNECTOR_DESC_SOURCE / CONNECTOR_DESC_CONFIDENCE = 0.90):
# a distributor description parsed by the desc grammar is more authoritative than the
# card's own desc_parse (83), below the deterministic decoders (85).
_SOURCE = "connector_desc"
_CONFIDENCE = 0.9


def _first_usable(results: list[dict]) -> dict | None:
    """The first result carrying a non-empty description (the only parametric
    signal)."""
    for r in results or []:
        if (r.get("description") or "").strip():
            return r
    return None


def _resolve_commodity(result: dict) -> str | None:
    """Canonical commodity for *result*: the distributor ``category`` string first
    (normalized like ``set_category`` does), then the description grammar fallback.

    Mouser's parametric-category phrases ("Multilayer Ceramic Capacitors MLCC - SMD/SMT")
    have no normalize_category alias and return None — the description grammar
    (categorize_from_desc, the same router writer.categorize_and_record trusts) recovers
    them as canonical keys the ladder will never drop as off-vocab.
    """
    return normalize_category(result.get("category")) or categorize_from_desc(result.get("description") or "")


def enrich_card_from_mouser(db: Session, card: MaterialCard, results: list[dict]) -> dict:
    """Enrich *card* from a Mouser search result list (category + spec facets).

    Returns ``{"categorized": int, "specs_written": int}``. Categorizes the card to our
    commodity taxonomy at connector_desc/tier 84 (fill-only: set_category through the F1
    ladder, where an existing higher-tier category wins), then parses the result's
    DESCRIPTION into spec facets under that commodity and records each via record_spec at
    the same source/tier. No-op (``{"categorized": 0, "specs_written": 0}``) when no
    result carries a description, or when no commodity can be resolved. Does NOT commit —
    the caller owns the transaction.
    """
    summary = {"categorized": 0, "specs_written": 0}
    result = _first_usable(results)
    if result is None:
        return summary

    commodity = _resolve_commodity(result)
    if commodity is None:
        logger.debug(
            "vendor-spec-enrich: card={} ({}) — no commodity from category={!r} / desc — skipping",
            card.id,
            card.display_mpn,
            result.get("category"),
        )
        return summary

    if set_category(card, commodity, source=_SOURCE, confidence=_CONFIDENCE):
        summary["categorized"] = 1

    # record_spec needs the now-set category; if set_category lost the ladder the card may
    # already carry a (higher-tier) category — fall back to it to still fill facets.
    category = (card.category or "").lower().strip()
    if not category:
        return summary  # no schema without a category — facets can't be validated

    extracted = extract_desc(result.get("description") or "", commodity_hint=category)
    if extracted is None or not extracted.specs:
        return summary

    schema_cache = load_schema_cache(db, category)
    with db.begin_nested():
        summary["specs_written"] = _write_specs(db, int(card.id), extracted.specs, _SOURCE, _CONFIDENCE, schema_cache)
    return summary
