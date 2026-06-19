"""Vendor-API parametric enrichment WRITER — distributor result → category + spec
facets.

What: Two distributor paths, both ladder-arbitrated (spec_tiers.SOURCE_TIER), both
      commit-free (the caller owns the txn):

      MOUSER (``enrich_card_from_mouser``) — Mouser's API returns a rich, consistent
      DESCRIPTION (e.g. "Multilayer Ceramic Capacitors MLCC - SMD/SMT 16V 0.1uF X7R 0402
      10%") but NOT structured parametric attributes (measured: ProductAttributes carry
      only Packaging). So the backbone is: distributor category + description → the
      ``desc_extractor`` grammar parses the description into spec facets, written at
      ``connector_desc`` / tier 84 — the SAME source identity + tier the shipped
      connector-description harvest uses (enrichment.py:_harvest_connector_enrichment).

      ELEMENT14 (``enrich_card_from_element14``) — Element14's API ``attributes`` ARE
      structured parametrics (Capacitance / Tolerance / …); the connector
      (element14.py:_parse via _vendor_spec_map) already maps them to seeded spec keys in
      the result's ``specs`` dict. So this path records each spec DIRECTLY via record_spec
      at ``element14_api`` / tier 90 (distributor structured parametrics are high-trust,
      above the description grammar and the deterministic decoders), with no desc parse.
      Element14 rate-limits hard, so it is a bounded top-demand SUPPLEMENT to Mouser.

      Reuse vs. replicate: both paths use the canonical
      ``desc_extractor/writer.py:_write_specs`` (record_spec per (key, value), ladder
      arbitrated, caller owns the SAVEPOINT) for the spec write loop. The CATEGORIZE step
      is replicated minimally (rather than calling ``writer.categorize_and_record``, which
      is fill-only and categorizes from the DESCRIPTION grammar only) so both paths can
      consider the distributor's own ``category`` string first (the measured-design rule),
      falling back to the description grammar when the distributor phrase is off-vocab.

Called by: app/management/backfill_vendor_specs.py (the paced backfill CLI).
Depends on: spec_tiers.set_category, category_normalizer.normalize_category,
      desc_extractor.{extract_desc, categorizer.categorize_from_desc, writer._write_specs},
      spec_write_service.{load_schema_cache, record_spec}. Does NOT commit — the caller
      owns the txn.
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

# Element14 STRUCTURED parametrics are recorded at the distributor vendor-API tier
# (spec_tiers.SOURCE_TIER: element14_api = 90) — above the description grammar (84/83) and
# the deterministic decoders (85), below trio_source (95) / manual (100). Confidence
# mirrors the structured-field harvest in enrichment.py (0.95): a typed distributor
# parametric is high-trust. Arbitration is the F1 ladder; this is provenance metadata.
_E14_SOURCE = "element14_api"
_E14_CONFIDENCE = 0.95


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


def _first_with_specs(results: list[dict]) -> dict | None:
    """The first result carrying structured ``specs`` (Element14's mapped parametrics);
    falls back to the first result with a resolvable category so a categorize-only hit
    (no parametrics but a known category) still enriches."""
    usable = [r for r in (results or []) if _resolve_commodity(r) is not None]
    if not usable:
        return None
    return next((r for r in usable if r.get("specs")), usable[0])


def enrich_card_from_element14(db: Session, card: MaterialCard, results: list[dict]) -> dict:
    """Enrich *card* from an Element14 search result list (category + STRUCTURED specs).

    Returns ``{"categorized": int, "specs_written": int}``. Element14's connector already
    mapped its structured ``attributes`` to seeded spec keys in each result's ``specs``
    dict (_vendor_spec_map). Categorizes the card at element14_api/tier 90 (fill-only via
    the F1 ladder — a higher-tier category wins), then records each ``specs`` value
    DIRECTLY via record_spec at the same source/tier (no description grammar — the values
    are already structured). record_spec's enum/numeric+unit schema gate is the final
    arbiter, so an off-enum value is dropped, never coerced. No-op when no result carries a
    resolvable commodity. Does NOT commit — the caller owns the transaction.
    """
    summary = {"categorized": 0, "specs_written": 0}
    result = _first_with_specs(results)
    if result is None:
        return summary

    # Surface coverage gaps: attributes Element14 returned that mapped to no seeded key
    # (the connector collects them in `dropped`). This log IS the consumer the field was
    # added for — without it, unmapped parametrics are an invisible coverage signal that
    # tells us which aliases to add to VENDOR_SPEC_MAP next.
    dropped = result.get("dropped") or {}
    if dropped:
        logger.info(
            "element14 card={}: {} unmapped attribute(s) (spec-map coverage gap): {}",
            card.id,
            len(dropped),
            sorted(dropped),
        )

    commodity = _resolve_commodity(result)
    if set_category(card, commodity, source=_E14_SOURCE, confidence=_E14_CONFIDENCE):
        summary["categorized"] = 1

    # record_spec needs the now-set category; if set_category lost the ladder the card may
    # already carry a (higher-tier) category. Only fill facets when the card's CURRENT
    # category matches the result's commodity — a capacitor result must not write capacitor
    # facets onto a card that a higher-tier source categorized as something else.
    category = (card.category or "").lower().strip()
    specs = result.get("specs") or {}
    if not category or category != commodity or not specs:
        return summary

    schema_cache = load_schema_cache(db, category)
    with db.begin_nested():
        summary["specs_written"] = _write_specs(db, int(card.id), specs, _E14_SOURCE, _E14_CONFIDENCE, schema_cache)
    return summary
