"""Source→tier provenance ladder (SP2 / shared foundation F1+F2).

What: Defines the single authoritative rule for "which data source wins" so that good
      data always beats guesses and source-execution ORDER is no longer load-bearing.
      ``SOURCE_TIER`` ranks every registered writer (writers MUST register their source
      string here — ``tier_for`` maps unknown sources to tier 0 with a WARNING, so an
      unregistered writer loses every conflict); ``tier_for`` looks a source up;
      ``resolve`` decides whether an incoming provenance tuple beats an existing one;
      ``set_category`` / ``set_brand`` / ``set_manufacturer`` apply that ladder to a
      MaterialCard's category / brand (OEM label) / manufacturer (actual maker) columns
      (the DB-touching helpers — all three delegate to the generic
      ``_set_provenanced_column``).
Called by: app/services/spec_write_service.record_spec (spec conflict resolution),
      app/services/mpn_decoder/writer.py (decode category + maker writes) +
      app/services/fru_crosswalk_enrich.py (decode category writes),
      app/services/source_ingest/ingest.py (TRIO part-master ingest: category at
      trio_source/trio_source_ai; brand/manufacturer at trio_source),
      app/management/backfill_dual_brand.py (B1-B3 dual-brand backfill), and the
      enrichment category writers (enrichment.py, authoritative_enrichment_service.py,
      material_enrichment_service.py).
Depends on: app.services.category_normalizer.normalize_category and
      app.services.manufacturer_normalizer.normalize_brand_name (lazy imports inside
      the setters to avoid model↔service import cycles), MaterialCard's category
      provenance columns (migration 096_spec_provenance) and brand/manufacturer
      provenance columns (migration 097_dual_brand).

The ladder rule (F1): incoming wins iff its ``(tier, confidence, updated_at)`` tuple is
strictly greater than the existing one. Higher tier always overrides; equal tier → higher
confidence; exact (tier, confidence) tie → newer updated_at; full tie → existing kept
(no churn). A ``None`` existing always loses (incoming wins).
"""

from datetime import datetime, timezone
from typing import TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    from app.models import MaterialCard

# Source → tier ranking. Higher tier always overrides a lower one (F1). ``trio_source`` is
# TRIO's own authoritative inventory/part-master data (ground truth) so it sits ABOVE the
# vendor distributor APIs (tier 90); its AI-corrected variant ``trio_source_ai`` sits just
# below vendor APIs but above the deterministic MPN decode (85). Vendor distributor APIs
# share tier 90; the deterministic MPN decode is 85; the FRU crosswalk decode
# (``fru_matrix_decode`` — a one-hop workbook mapping plus decode, weaker than a
# first-party decode) is 84; the deterministic description→spec grammar (``desc_parse``)
# is 83 — preserving the relative order mpn_decode > fru_matrix_decode > desc_parse >
# spec_extraction the worker's run-order + writer pre-gates used to enforce by hand; the
# same grammar run over a FRU's LINKED qual-sheet descriptions (``fru_desc_parse`` — a
# one-hop fru_links row's prose, weaker than the card's OWN description) is 82; OEM
# pages map to 80 — the named scrapers (``partsurfer``/``psref``) and the broader
# ``oem_official`` umbrella (authoritative_enrichment_service's OEM-domain extractor)
# are the same evidence class; ``legacy_backfill`` (50) marks pre-ladder data
# whose true source is unknown — above the AI guesses (a stray guess can't flip legacy
# data) but below every real source; AI free-text mining sits at the bottom (Haiku batch
# categorization ``claude_haiku`` ranks with the other AI guesses). ``manual`` (a human
# edit) tops it.
#
# NOTE: migration 096_spec_provenance carries a SQL CASE snapshot of this map for the
# one-shot facet-tier backfill — tests/test_migration_096_spec_provenance.py asserts the
# two stay in sync, so adding a source here requires updating the migration literal too.
SOURCE_TIER: dict[str, int] = {
    "manual": 100,
    "trio_source": 95,
    "digikey_api": 90,
    "mouser_api": 90,
    "nexar_api": 90,
    "element14_api": 90,
    "oemsecrets_api": 90,
    "trio_source_ai": 88,
    "mpn_decode": 85,
    "fru_matrix_decode": 84,
    "desc_parse": 83,
    "fru_desc_parse": 82,
    "partsurfer": 80,
    "psref": 80,
    "oem_official": 80,
    "web_search": 70,
    "brokerbin": 65,
    "spec_extraction": 60,
    "legacy_backfill": 50,
    "ai_guess": 40,
    "claude_opus_inferred": 40,
    "claude_haiku": 40,
}

# Provenance stamped on valued-but-unprovenanced data (categories that pre-date the
# ladder, or rows written by a not-yet-routed writer). Mid-tier by design: a real future
# source (decode 85, vendor 90) overrides it, but a stray AI guess (40) cannot silently
# flip it. Shared by migration 096's category backfill and set_category's runtime
# default so "existed at migration time" and "written a minute later" rank identically.
LEGACY_BACKFILL_SOURCE = "legacy_backfill"
LEGACY_BACKFILL_TIER = SOURCE_TIER[LEGACY_BACKFILL_SOURCE]
LEGACY_BACKFILL_CONFIDENCE = 0.5

# Unknown sources are warned ONCE per process (a misregistered writer fails 100% of its
# writes — that must be visible at production log levels, but not once per row).
_warned_unknown_sources: set[str] = set()


def tier_for(source: str) -> int:
    """Return the ladder tier for *source*.

    Unknown source → tier 0: it can never beat a known one, and anything it does write
    is clobberable by the lowest-tier guess. That is a writer bug (every write silently
    loses), so the first occurrence per source is logged at WARNING.
    """
    tier = SOURCE_TIER.get(source, 0)
    if tier == 0 and source not in _warned_unknown_sources:
        _warned_unknown_sources.add(source)
        logger.warning(
            "tier_for: unknown source {!r} → tier 0 — every write from this source loses "
            "all conflicts. Register it in spec_tiers.SOURCE_TIER (warned once per source).",
            source,
        )
    return tier


def _prov_key(entry: dict) -> tuple[int, float, str]:
    """Coerce a provenance dict to its comparable ``(tier, confidence, updated_at)``
    key.

    Defensive at the boundary: explicit ``None`` values (hand-edited JSONB, a writer
    passing confidence=None) coerce to the defaults instead of raising TypeError inside
    the tuple comparison, and confidence is clamped to [0, 1] so a bogus percent-style
    value (95) cannot dominate every same-tier comparison.
    """
    return (
        int(entry.get("tier") or 0),
        min(max(float(entry.get("confidence") or 0.0), 0.0), 1.0),
        str(entry.get("updated_at") or ""),
    )


def resolve(existing: dict | None, incoming: dict) -> bool:
    """Return ``True`` iff *incoming* wins over *existing* under the F1 ladder.

    Each arg is a provenance dict with keys ``tier`` (int), ``confidence`` (float in
    [0, 1]), and ``updated_at`` (ISO-8601 string, lexicographically sortable). Missing or
    ``None`` values coerce to (0, 0.0, "") and confidence is clamped to [0, 1] — see
    ``_prov_key``. ``existing is None`` → incoming always wins. Otherwise incoming wins
    iff its ``(tier, confidence, updated_at)`` tuple is strictly greater. Pure function —
    no DB, no side effects.
    """
    if existing is None:
        return True
    return _prov_key(incoming) > _prov_key(existing)


def _purge_stale_commodity_data(card: "MaterialCard", new_category: str, source: str) -> None:
    """Purge facet rows + their JSONB mirrors left over from the OLD commodity.

    Called when set_category CHANGES an existing category: the card's MaterialSpecFacet
    rows carry the commodity they were written under, so after a flip the old commodity's
    rows would keep matching that commodity's deep-filters (a now-hdd card still
    answering dram parametric filters — silent cross-commodity filter corruption). Every
    facet row whose ``category`` differs from the new one is deleted, and the matching
    ``specs_structured`` entries (record_spec always writes both) are dropped so the
    winning sources re-assert their specs under the new commodity's schema.

    No-op when the card has no session (a brand-new, not-yet-added card has no facet
    rows to purge).
    """
    from sqlalchemy.orm import Session

    from app.models import MaterialSpecFacet

    db = Session.object_session(card)
    if db is None or card.id is None:
        return
    stale = (
        db.query(MaterialSpecFacet)
        .filter(
            MaterialSpecFacet.material_card_id == card.id,
            MaterialSpecFacet.category != new_category,
        )
        .all()
    )
    if not stale:
        return
    stale_keys = sorted({f.spec_key for f in stale})
    for facet in stale:
        db.delete(facet)
    specs = dict(card.specs_structured or {})
    removed = [k for k in stale_keys if specs.pop(k, None) is not None]
    card.specs_structured = specs
    logger.info(
        "set_category: card={} re-categorized {!r} → {!r} (source={}) — purged {} stale "
        "facet row(s) {} and {} matching specs_structured entr(ies) from the old commodity",
        card.id,
        card.category,
        new_category,
        source,
        len(stale),
        stale_keys,
        len(removed),
    )


def _set_provenanced_column(
    card: "MaterialCard",
    attr: str,
    value: str,
    source: str,
    confidence: float,
    *,
    write: bool = True,
    on_change=None,
) -> bool:
    """Set ``card.<attr>`` (+ its four ``<attr>_*`` provenance columns) through the F1
    ladder. Return ``True`` iff the incoming write wins.

    Generic over the column-name prefix: ``attr`` ∈ {"category", "brand",
    "manufacturer"}, provenance columns are ``f"{attr}_source"`` / ``_confidence`` /
    ``_tier`` / ``_updated_at``. *value* must already be canonical — the public setters
    own normalization (set_category via normalize_category, set_brand/set_manufacturer
    via normalize_brand_name).

    Ladder semantics (identical for all three columns): incoming provenance is
    ``(tier_for(source), clamped confidence, now)``; an existing VALUE with NULL
    provenance columns (pre-ladder data, or a write that bypassed these helpers) ranks
    at the migration-backfill mid-tier (``LEGACY_BACKFILL_TIER`` = 50, logged at INFO),
    so it cannot be flipped by an AI guess but yields to decode/vendor sources. The
    existing timestamp for the tie-break is ``<attr>_updated_at`` ("" when NULL).

    ``on_change`` (optional) is called as ``on_change(card, new_value, source)`` just
    before a win that CHANGES an existing value is written — set_category uses it to
    purge the old commodity's stale facet data. ``write=False`` is the read-only twin
    for dry-run accounting: every check runs, the same bool returns, nothing is mutated.
    """
    confidence = min(max(float(confidence or 0.0), 0.0), 1.0)
    now = datetime.now(timezone.utc)
    incoming = {"tier": tier_for(source), "confidence": confidence, "updated_at": now.isoformat()}

    current = getattr(card, attr)
    existing_tier = getattr(card, f"{attr}_tier")
    existing_source = getattr(card, f"{attr}_source")
    if current is None:
        existing = None
    elif existing_tier is None and existing_source is None:
        # Valued but unprovenanced — written before the ladder or by an un-routed writer.
        # Rank it exactly like the migration backfill (legacy_backfill / 0.5 / 50) so the
        # same data doesn't rank 50 if it existed at migration time but 0 a minute later.
        logger.info(
            "set_{}: card={} existing {}={!r} has no provenance — treating as {} (tier {}); an un-routed writer set it",
            attr,
            getattr(card, "id", None),
            attr,
            current,
            LEGACY_BACKFILL_SOURCE,
            LEGACY_BACKFILL_TIER,
        )
        existing = {
            "tier": LEGACY_BACKFILL_TIER,
            "confidence": LEGACY_BACKFILL_CONFIDENCE,
            "updated_at": "",
        }
    else:
        existing_conf = getattr(card, f"{attr}_confidence")
        existing_ts = getattr(card, f"{attr}_updated_at")
        existing = {
            "tier": existing_tier if existing_tier is not None else 0,
            "confidence": existing_conf if existing_conf is not None else 0.0,
            "updated_at": existing_ts.isoformat() if existing_ts is not None else "",
        }

    if not resolve(existing, incoming):
        logger.debug(
            "set_{}: card={} kept existing {}={!r} (incoming {!r}@{} lost)",
            attr,
            getattr(card, "id", None),
            attr,
            current,
            value,
            source,
        )
        return False

    if not write:
        return True

    if on_change is not None and current is not None and current != value:
        on_change(card, value, source)

    setattr(card, attr, value)
    setattr(card, f"{attr}_source", source)
    setattr(card, f"{attr}_confidence", confidence)
    setattr(card, f"{attr}_tier", incoming["tier"])
    setattr(card, f"{attr}_updated_at", now)
    return True


def set_category(
    card: "MaterialCard",
    value: str | None,
    source: str,
    confidence: float,
    *,
    write: bool = True,
) -> bool:
    """Set ``card.category`` (+ provenance) through the F1 ladder. Return ``True`` if
    written.

    Normalizes *value* to a canonical commodity key (off-vocab → ``None``, never persisted
    as junk). If it normalizes to ``None`` the call is a no-op and returns ``False``. The
    ladder compare + write is delegated to ``_set_provenanced_column`` — a lower-tier
    source can never overwrite a higher-tier category; an existing category with NULL
    provenance ranks at the legacy_backfill mid-tier (50).

    On a win it sets ``category`` and the four ``category_*`` provenance columns and
    returns ``True``; when the win CHANGES an existing category it also purges the old
    commodity's facet rows / specs_structured entries (see _purge_stale_commodity_data)
    and logs the flip at INFO. Otherwise it leaves the card untouched and returns
    ``False``.

    ``write=False`` is the read-only twin for dry-run accounting: every check runs, the
    same bool returns, nothing is mutated.
    """
    from app.services.category_normalizer import normalize_category

    canonical = normalize_category(value)
    if canonical is None:
        # Off-vocab / empty / None — never persist junk, never blank an existing category.
        if value:
            logger.warning("set_category: off-vocab value {!r} (source={}) — not writing", value, source)
        return False

    # value is already canonical here; SP3's @validates("category") hardens other paths.
    return _set_provenanced_column(
        card,
        "category",
        canonical,
        source,
        confidence,
        write=write,
        on_change=_purge_stale_commodity_data,
    )


def _set_brand_or_maker(
    card: "MaterialCard",
    attr: str,
    value: str | None,
    source: str,
    confidence: float,
    *,
    write: bool,
) -> bool:
    """Shared body of set_brand/set_manufacturer: reject empties, normalize, ladder."""
    if value is None or not str(value).strip():
        # A write can never blank a value — None/empty/whitespace is a no-op.
        return False

    from sqlalchemy.orm import Session

    from app.services.manufacturer_normalizer import normalize_brand_name

    normalized = normalize_brand_name(Session.object_session(card), str(value))
    return _set_provenanced_column(card, attr, normalized, source, confidence, write=write)


def set_brand(
    card: "MaterialCard",
    value: str | None,
    source: str,
    confidence: float,
    *,
    write: bool = True,
) -> bool:
    """Set ``card.brand`` (the OEM LABEL — IBM, Dell Technologies, Lenovo) through the
    F1 ladder. Return ``True`` if written.

    ``None``/empty/whitespace input is rejected (no-op, returns ``False`` — a write can
    never blank a value). The value is canonicalized via ``normalize_brand_name``
    (manufacturers-table aliases; miss → verbatim strip) before the ladder compare. An
    existing brand with NULL provenance ranks at the legacy_backfill mid-tier (50).
    ``write=False`` is the read-only dry-run twin.
    """
    return _set_brand_or_maker(card, "brand", value, source, confidence, write=write)


def set_manufacturer(
    card: "MaterialCard",
    value: str | None,
    source: str,
    confidence: float,
    *,
    write: bool = True,
) -> bool:
    """Set ``card.manufacturer`` (the ACTUAL MAKER — Seagate Technology, Kingston
    Technology, Hitachi/IBM) through the F1 ladder. Return ``True`` if written.

    ``None``/empty/whitespace input is rejected (no-op, returns ``False`` — a write can
    never blank a value). The value is canonicalized via ``normalize_brand_name``
    (manufacturers-table aliases; miss → verbatim strip) before the ladder compare. An
    existing manufacturer with NULL provenance (all legacy data) ranks at the
    legacy_backfill mid-tier (50) — so trio_source (95) maker evidence displaces a
    legacy OEM name, but a stray AI guess (40) cannot. ``write=False`` is the read-only
    dry-run twin.
    """
    return _set_brand_or_maker(card, "manufacturer", value, source, confidence, write=write)
