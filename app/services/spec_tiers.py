"""Source→tier provenance ladder (SP2 / shared foundation F1+F2).

What: Defines the single authoritative rule for "which data source wins" so that good
      data always beats guesses and source-execution ORDER is no longer load-bearing.
      ``SOURCE_TIER`` ranks every registered writer (writers MUST register their source
      string here — ``tier_for`` maps unknown sources to tier 0 with a WARNING, so an
      unregistered writer loses every conflict); ``tier_for`` looks a source up;
      ``resolve`` decides whether an incoming provenance tuple beats an existing one;
      ``set_category`` applies that ladder to a MaterialCard's category column (the one
      DB-touching helper).
Called by: app/services/spec_write_service.record_spec (spec conflict resolution),
      app/services/mpn_decoder/writer.py + app/services/fru_crosswalk_enrich.py (decode
      category writes), app/services/source_ingest/ingest.py (TRIO part-master ingest:
      category at trio_source/trio_source_ai), and the enrichment category writers
      (enrichment.py, authoritative_enrichment_service.py, material_enrichment_service.py).
Depends on: app.services.category_normalizer.normalize_category (lazy import inside
      set_category to avoid a model↔service import cycle), MaterialCard's category
      provenance columns (added by migration 096_spec_provenance).

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


# ── Validation conflicts (on-add enrichment, migration 099) ──────────────────────────
# A manual (tier 100) value is NEVER overwritten by the system — but when a source in
# the deterministic/authoritative band contradicts it, that contradiction is recorded on
# the card (validation_conflicts JSONB + has_validation_conflict flag) so a human can
# review it. Tier >= 80 = the authoritative band (trio_source 95, connector APIs 90,
# trio_source_ai 88, mpn_decode 85, fru_matrix_decode 84, desc_parse 83,
# fru_desc_parse 82, partsurfer/psref 80). web_search 70 / brokerbin 65 / ai_guess 40
# never flag — too noisy to challenge a human.
CONFLICT_EVIDENCE_MIN_TIER = 80


def _conflict_value_key(value) -> str:
    """Normalize a value for conflict comparison.

    Numerics compare as floats (so 16 == 16.0 == "16"); everything else compares case-
    insensitively with whitespace stripped (so "DDR4" == "ddr4", True == "true").
    """
    text = str(value).strip()
    try:
        return repr(float(text))
    except (TypeError, ValueError):
        return text.casefold()


def record_validation_conflict(
    card: "MaterialCard",
    key: str,
    existing_prov: dict | None,
    incoming_prov: dict,
    incoming_value,
) -> bool:
    """Record that authoritative evidence contradicts a manual value. Single choke
    point.

    Called from the LOSE branches of ``record_spec`` (spec_write_service) and
    ``set_category`` — i.e. after the F1 ladder already KEPT the existing value. Gates
    (all enforced here so call sites stay dumb):
      - the existing entry is a ``manual`` (tier 100) value;
      - the incoming source ranks in the authoritative band (tier >= 80);
      - the two values actually differ after normalization (corroboration is not stored).

    Persists an entry into ``card.validation_conflicts`` de-duped per
    ``(key, evidence.source)`` — newest evidence replaces, and a same-source
    observation that now AGREES with the manual value removes that source's stale
    entry (deterministic sources re-fire on every pass; a fixed decoder must not
    leave the card flagged forever) — and sets ``card.has_validation_conflict``.
    Returns True iff an entry was written.
    """
    existing_prov = existing_prov or {}
    incoming_prov = incoming_prov or {}
    if existing_prov.get("source") != "manual":
        return False
    incoming_source = incoming_prov.get("source", "")
    incoming_tier = incoming_prov.get("tier")
    if incoming_tier is None:
        incoming_tier = tier_for(incoming_source)
    if int(incoming_tier) < CONFLICT_EVIDENCE_MIN_TIER:
        return False
    manual_value = existing_prov.get("value")
    if _conflict_value_key(manual_value) == _conflict_value_key(incoming_value):
        # Corroboration: this source's NEWEST observation agrees with the manual
        # value. "Newest evidence replaces" includes replacing-with-nothing — drop
        # any stale contradiction this source recorded earlier and recompute the flag.
        stale = [
            c
            for c in (card.validation_conflicts or [])
            if c.get("key") == key and (c.get("evidence") or {}).get("source") == incoming_source
        ]
        if stale:
            kept = [c for c in (card.validation_conflicts or []) if c not in stale]
            card.validation_conflicts = kept
            card.has_validation_conflict = bool(kept)
        return False

    entry = {
        "key": key,
        "manual": {
            "value": manual_value,
            "updated_at": existing_prov.get("updated_at") or "",
        },
        "evidence": {
            "source": incoming_source,
            "tier": int(incoming_tier),
            "confidence": incoming_prov.get("confidence"),
            "value": incoming_value,
            "observed_at": datetime.now(timezone.utc).isoformat(),
        },
    }
    conflicts = [
        c
        for c in (card.validation_conflicts or [])
        if not (c.get("key") == key and (c.get("evidence") or {}).get("source") == incoming_source)
    ]
    conflicts.append(entry)
    card.validation_conflicts = conflicts
    card.has_validation_conflict = True
    logger.info(
        "validation conflict: card={} key={} manual={!r} kept; {} (tier {}) reported {!r}",
        getattr(card, "id", None),
        key,
        manual_value,
        incoming_source,
        incoming_tier,
        incoming_value,
    )
    return True


def clear_validation_conflicts(card: "MaterialCard", key: str) -> bool:
    """Drop every conflict entry for *key* (a human re-asserted or accepted a value).

    Recomputes ``has_validation_conflict`` (empty list → False). Returns True iff
    anything was removed.
    """
    conflicts = list(card.validation_conflicts or [])
    kept = [c for c in conflicts if c.get("key") != key]
    if len(kept) == len(conflicts):
        return False
    card.validation_conflicts = kept
    card.has_validation_conflict = bool(kept)
    return True


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

    ``validation_conflicts`` entries keyed by the purged spec keys are dropped too
    (and ``has_validation_conflict`` recomputed): the manual values they reference no
    longer exist on the card, the new commodity has no schema for them, so accepting
    one could never write anything — they would be dead review items. ``category``
    entries are untouched (never a facet spec key).

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
    # Conflict entries for purged spec keys are orphans — the manual value they point
    # at was just removed, and the new commodity has no schema for the key (accepting
    # would no-op). Drop them with the specs they describe; recompute the flag.
    conflicts = list(card.validation_conflicts or [])
    if conflicts:
        stale_key_set = set(stale_keys)
        kept_conflicts = [c for c in conflicts if c.get("key") not in stale_key_set]
        if len(kept_conflicts) != len(conflicts):
            card.validation_conflicts = kept_conflicts
            card.has_validation_conflict = bool(kept_conflicts)
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
    as junk). If it normalizes to ``None`` the call is a no-op and returns ``False``. Builds
    the incoming provenance from *source*/*confidence* and compares it (via ``resolve``)
    against the card's existing category provenance — a lower-tier source can never
    overwrite a higher-tier category. An existing category with NULL provenance columns
    (pre-ladder data, or a write that bypassed this helper) ranks at the same mid-tier the
    migration backfill uses (``LEGACY_BACKFILL_TIER`` = 50), so it cannot be flipped by an
    AI guess but yields to decode/vendor sources. The existing timestamp for the
    tie-break is ``category_updated_at`` (stamped on every win; "" when NULL).

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

    confidence = min(max(float(confidence or 0.0), 0.0), 1.0)
    now = datetime.now(timezone.utc)
    incoming = {"tier": tier_for(source), "confidence": confidence, "updated_at": now.isoformat()}

    if card.category is None:
        existing = None
    elif card.category_tier is None and card.category_source is None:
        # Valued but unprovenanced — written before the ladder or by an un-routed writer.
        # Rank it exactly like the migration backfill (legacy_backfill / 0.5 / 50) so the
        # same data doesn't rank 50 if it existed at migration time but 0 a minute later.
        logger.info(
            "set_category: card={} existing category={!r} has no provenance — treating as "
            "{} (tier {}); an un-routed writer set it",
            getattr(card, "id", None),
            card.category,
            LEGACY_BACKFILL_SOURCE,
            LEGACY_BACKFILL_TIER,
        )
        existing = {
            "tier": LEGACY_BACKFILL_TIER,
            "confidence": LEGACY_BACKFILL_CONFIDENCE,
            "updated_at": "",
        }
    else:
        existing = {
            "tier": card.category_tier if card.category_tier is not None else 0,
            "confidence": card.category_confidence if card.category_confidence is not None else 0.0,
            "updated_at": (card.category_updated_at.isoformat() if card.category_updated_at is not None else ""),
        }

    if not resolve(existing, incoming):
        if write:
            # The ladder kept the existing category. If that category is a manual value
            # and the loser is an authoritative source reporting something ELSE, persist
            # the contradiction for human review (the helper gates manual/tier>=80/diff).
            record_validation_conflict(
                card,
                "category",
                {
                    "source": card.category_source,
                    "value": card.category,
                    "updated_at": existing["updated_at"] if existing else "",
                },
                {"source": source, **incoming},
                canonical,
            )
        logger.debug(
            "set_category: card={} kept existing category={!r} (incoming {!r}@{} lost)",
            getattr(card, "id", None),
            card.category,
            canonical,
            source,
        )
        return False

    if not write:
        return True

    if card.category is not None and card.category != canonical:
        _purge_stale_commodity_data(card, canonical, source)

    card.category = canonical  # already canonical; SP3's @validates("category") hardens other paths
    card.category_source = source
    card.category_confidence = confidence
    card.category_tier = incoming["tier"]
    card.category_updated_at = now
    return True
