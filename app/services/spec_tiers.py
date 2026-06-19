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
      app/management/backfill_dual_brand.py (B1-B3 dual-brand backfill), the
      enrichment category writers (enrichment.py, authoritative_enrichment_service.py —
      whose apply_* writers route BOTH category and manufacturer through the ladder at
      {connector}_api/90, oem_official/80 and web_search/70 —
      material_enrichment_service.py), and the manual edit endpoints
      (routers/materials.py update_material/add_material,
      routers/htmx_views.py update_material_card — category + manufacturer at
      manual/100).
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
    "cpu_pollution_fix": 96,  # deterministic re-classification of the polluted `cpu` catch-all:
    # beats the trio_source 'cpu' DEFAULT (95, an un-coded SFDC dump), below manual (100).
    # Only ever written by app/management/fix_cpu_pollution.py on category='cpu' cards.
    "trio_source": 95,
    "digikey_api": 90,
    "mouser_api": 90,
    "nexar_api": 90,
    "element14_api": 90,
    "oemsecrets_api": 90,
    "trio_source_ai": 88,
    "mpn_decode": 85,
    "fru_matrix_decode": 84,
    # PartSurfer = the OEM's own authoritative description → outranks the card's own
    # desc_parse (83); still a description-parse, so it loses to the deterministic
    # decoders (mpn_decode 85). Ties fru_matrix_decode (84) — different vendors, the tie
    # is not load-bearing.
    "connector_desc": 84,  # distributor (DigiKey/Mouser/…) description parsed by the desc grammar:
    # more authoritative than the card's own desc_parse (83), below the deterministic decoders (85).
    "partsurfer_desc": 84,
    "desc_parse": 83,
    # eBay listing titles are external marketplace free-text part descriptions — the same
    # desc grammar run over an eBay Browse title we fetch. Same evidence class as the
    # card's own desc_parse (83): below the curated distributor description (connector_desc
    # 84) and the deterministic decoders (mpn_decode 85). See eBay-title-mining.
    "ebay_title": 83,
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

# Detached-card brand/maker writes are warned ONCE per process (same rationale): a writer
# operating on a session-less card skips alias canonicalization for EVERY row it touches,
# so the first occurrence must be visible at production log levels without per-row spam.
_warned_detached_normalize = False


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


def values_contradict(kept_value, incoming_value) -> bool:
    """True iff the two values DIFFER under the ladder's conflict normalization.

    The single comparison both contradiction detectors (``record_validation_conflict``,
    ``record_evidence_dissent``) and the rejection counters use, so "corroboration vs
    contradiction" can never be classified differently across them.
    """
    return _conflict_value_key(kept_value) != _conflict_value_key(incoming_value)


def _is_conflict_for(conflict: dict, key: str, source: str) -> bool:
    """True iff *conflict* is this card's recorded entry for ``(key, evidence.source)``.

    The single de-dup predicate both recorders share: a card holds at most one conflict
    entry per ``(spec key, contributing source)``, so newest evidence replaces and a
    same-source corroboration drops the stale entry.
    """
    return conflict.get("key") == key and (conflict.get("evidence") or {}).get("source") == source


def _drop_same_source_conflicts(card: "MaterialCard", key: str, source: str) -> None:
    """Drop *card*'s conflict entries for ``(key, source)`` and recompute the flag.

    Called from the corroboration branch of both recorders: a deterministic source that
    re-fires and now AGREES must not leave its earlier contradiction/dissent on the card.
    No-op (leaves the JSONB column untouched) when nothing matched, so a fixed decoder's
    agreeing pass doesn't churn the column on every card.
    """
    conflicts = card.validation_conflicts or []
    kept = [c for c in conflicts if not _is_conflict_for(c, key, source)]
    if len(kept) != len(conflicts):
        card.validation_conflicts = kept
        card.has_validation_conflict = bool(kept)


def _replace_conflict(card: "MaterialCard", key: str, source: str, entry: dict) -> None:
    """Replace *card*'s conflict entry for ``(key, source)`` with *entry* and flag it.

    The append path both recorders share: drop any prior entry for this
    ``(key, source)`` (newest replaces) and store *entry*, then set
    ``has_validation_conflict``.
    """
    conflicts = [c for c in (card.validation_conflicts or []) if not _is_conflict_for(c, key, source)]
    conflicts.append(entry)
    card.validation_conflicts = conflicts
    card.has_validation_conflict = True


# Daily ladder-rejection counters survive ~a month so weekly trust reviews can read
# them back; the date-keyed hash self-partitions, so a longer TTL only costs bytes.
REJECTION_COUNTER_TTL_DAYS = 35.0


def count_ladder_rejection(winner_source: str, loser_source: str, *, contradiction: bool) -> None:
    """Bump the persistent per-day ladder-rejection counter. NEVER raises.

    Redis hash ``intel:ladder:rejections:{date}`` (via app.cache.intel_cache, the same
    backend the enrichment-worker daily counters use), field
    ``{winner_source}|{loser_source}|{corroboration|contradiction}``. Before this
    counter, rejections were log-only and died with container rotation — and
    corroborations vs contradictions were indistinguishable without re-parsing logs.
    Telemetry must never break the write path: every failure is swallowed and logged
    at DEBUG.
    """
    try:
        from app.cache import intel_cache

        kind = "contradiction" if contradiction else "corroboration"
        date = datetime.now(timezone.utc).date().isoformat()
        intel_cache.incr_hash_count(
            f"ladder:rejections:{date}",
            f"{winner_source or 'unknown'}|{loser_source or 'unknown'}|{kind}",
            ttl_days=REJECTION_COUNTER_TTL_DAYS,
        )
    except Exception as exc:
        logger.debug("count_ladder_rejection: counter write failed (non-fatal): {}", exc)


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
    ``_set_provenanced_column`` (category, brand, manufacturer) — i.e. after the F1
    ladder already KEPT the existing value. Gates (all enforced here so call sites
    stay dumb):
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
        _drop_same_source_conflicts(card, key, incoming_source)
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
    _replace_conflict(card, key, incoming_source, entry)
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


def record_evidence_dissent(
    card: "MaterialCard",
    key: str,
    kept_prov: dict | None,
    incoming_prov: dict,
    incoming_value,
) -> bool:
    """Record that authoritative evidence dissents from a kept NON-manual value.

    The companion of ``record_validation_conflict`` for the case it structurally never
    covers: that helper fires only when the KEPT value is ``manual`` (tier 100), so an
    authoritative-vs-authoritative contradiction (trio_source category vs mpn_decode,
    a FRU 373TB capacity vs the description's 36.4 GB) resolved silently by tier with
    no review artifact — "wrong facet worse than missing" had no detector above the
    ladder itself. Called from the same LOSE branches (``record_spec``'s
    ``_incoming_loses`` path and ``_set_provenanced_column``); gates (all here, call
    sites stay dumb):
      - the kept value is NOT ``manual`` (the manual-winner case stays with
        ``record_validation_conflict``, unchanged);
      - the LOSING source ranks in the authoritative band (tier >=
        ``CONFLICT_EVIDENCE_MIN_TIER`` = 80);
      - the two values actually differ under ``values_contradict`` (corroboration is
        not stored — and, mirroring record_validation_conflict, a same-source
        observation that now AGREES removes that source's stale entry).

    Persists into the SAME ``card.validation_conflicts`` JSONB +
    ``has_validation_conflict`` flag (de-duped per ``(key, evidence.source)``, newest
    replaces) so dissents surface in the already-wired needs-review filter and detail
    rendering with zero UI work. Shape note: the kept side is stored under the
    ``manual`` sub-dict key for compatibility with the rendered conflict rows and the
    accept endpoint, but carries honest ``source``/``tier`` fields and the entry is
    tagged ``kind: "dissent"``. Returns True iff an entry was written.
    """
    kept_prov = kept_prov or {}
    incoming_prov = incoming_prov or {}
    kept_source = kept_prov.get("source") or ""
    if kept_source == "manual":
        return False
    incoming_source = incoming_prov.get("source", "")
    incoming_tier = incoming_prov.get("tier")
    if incoming_tier is None:
        incoming_tier = tier_for(incoming_source)
    if int(incoming_tier) < CONFLICT_EVIDENCE_MIN_TIER:
        return False
    kept_value = kept_prov.get("value")
    if not values_contradict(kept_value, incoming_value):
        # Corroboration: this source's NEWEST observation agrees with the kept value —
        # drop any stale dissent it recorded earlier and recompute the flag
        # (deterministic sources re-fire every pass; a fixed decoder must not leave
        # the card flagged forever).
        _drop_same_source_conflicts(card, key, incoming_source)
        return False

    kept_tier = kept_prov.get("tier")
    if kept_tier is None and kept_source:
        kept_tier = tier_for(kept_source)
    entry = {
        "key": key,
        "kind": "dissent",
        "manual": {
            "value": kept_value,
            "updated_at": kept_prov.get("updated_at") or "",
            "source": kept_source,
            "tier": int(kept_tier) if kept_tier is not None else None,
        },
        "evidence": {
            "source": incoming_source,
            "tier": int(incoming_tier),
            "confidence": incoming_prov.get("confidence"),
            "value": incoming_value,
            "observed_at": datetime.now(timezone.utc).isoformat(),
        },
    }
    _replace_conflict(card, key, incoming_source, entry)
    logger.info(
        "evidence dissent: card={} key={} kept {!r} ({}, tier {}); {} (tier {}) reported {!r}",
        getattr(card, "id", None),
        key,
        kept_value,
        kept_source,
        kept_tier,
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

    LOSE branch (write=True only): when the kept value is a ``manual`` edit and the
    losing source is authoritative (tier >= ``CONFLICT_EVIDENCE_MIN_TIER``) reporting a
    DIFFERENT value, the contradiction is persisted via ``record_validation_conflict``
    for human review. The hook lives here because this is where ladder losses for ALL
    provenanced columns (category, brand, manufacturer) are decided.
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
        # The KEPT side's effective provenance: a valued-but-unprovenanced existing
        # ranked as legacy_backfill above, so it must be NAMED that here too (the
        # dissent record, the rejection counter, and the log line all describe the
        # same decision the ladder just made).
        kept_source = existing_source if existing_source else (LEGACY_BACKFILL_SOURCE if existing else "")
        if write:
            # The ladder kept the existing value. If that value is a manual edit and
            # the loser is an authoritative source reporting something ELSE, persist
            # the contradiction for human review (the helper gates manual/tier>=80/
            # value-differs). This hook lives HERE — the single point where ladder
            # losses for provenanced columns are decided — so it fires for category
            # AND brand/manufacturer: manual maker edits exist (update_material routes
            # them through set_brand/set_manufacturer with source="manual"), so they
            # carry the same contradiction-flagging contract as category and spec keys.
            record_validation_conflict(
                card,
                attr,
                {
                    "source": existing_source,
                    "value": current,
                    "updated_at": existing["updated_at"] if existing else "",
                },
                {"source": source, **incoming},
                value,
            )
            # Non-manual kept values get the same contradiction artifact via the
            # dissent channel (the helper gates kept!=manual / loser tier>=80 /
            # value-differs — exactly one of the two recorders can fire per loss).
            record_evidence_dissent(
                card,
                attr,
                {
                    "source": kept_source,
                    "value": current,
                    "updated_at": existing["updated_at"] if existing else "",
                    "tier": existing["tier"] if existing else None,
                },
                {"source": source, **incoming},
                value,
            )
            # Persistent telemetry: every real (write-mode) rejection counts once,
            # classified corroboration vs contradiction. Never raises (wrapped inside).
            count_ladder_rejection(kept_source, source, contradiction=values_contradict(current, value))
        # Visibility rule (mirrors record_spec._incoming_loses): a writer that
        # systematically loses arbitration must be visible at production log levels,
        # so NON-manual rejections log at INFO for EVERY provenanced column
        # (category, brand, manufacturer). Some writers ALSO surface aggregate
        # conflict WARNINGs (mpn_decoder's skipped_maker_conflict, ingest's conflict
        # tallies), but the W8 enrichment writers (apply_authoritative /
        # apply_cross_ref_verified / apply_oem_sourced / apply_web_sourced) have no
        # such counter — a DEBUG-only maker loss there would be production-invisible
        # (no validation conflict either unless the kept value is manual and the
        # loser is tier >= 80). Only manual submissions stay at DEBUG: the human
        # gets endpoint feedback (toast/422) and the no-op re-assert paths are
        # deliberate. The line names BOTH sides — winner source+tier AND loser —
        # so a rejection is diagnosable from the log alone.
        log = logger.info if source != "manual" else logger.debug
        log(
            "set_{}: card={} kept existing {}={!r} ({}, tier {}) — incoming {!r}@{} (tier {}) lost",
            attr,
            getattr(card, "id", None),
            attr,
            current,
            kept_source,
            existing["tier"] if existing else None,
            value,
            source,
            incoming["tier"],
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

    # value is already canonical here; MaterialCard's @validates("category") guard
    # (models/intelligence.py) hardens every OTHER path — an off-vocab direct
    # assignment raises instead of bypassing the ladder.
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
    """Shared body of set_brand/set_manufacturer: reject empties + garbage fragments
    (is_garbage_brand_value — unbalanced-paren / single-char shapes, logged at WARNING),
    normalize, ladder.

    PRECONDITION: *card* should be session-attached — the alias lookup derives its DB
    session via ``Session.object_session(card)``. A detached/transient card cannot be
    canonicalized, so the verbatim strip is written instead (full provenance, ladder-
    protected) — that fragments the brand facet ("Seagate" vs "Seagate Technology"),
    so the first such write per process is logged at WARNING.
    """
    if value is None or not str(value).strip():
        # A write can never blank a value — None/empty/whitespace is a no-op.
        return False

    from sqlalchemy.orm import Session

    from app.services.manufacturer_normalizer import is_garbage_brand_value, normalize_brand_name

    if is_garbage_brand_value(str(value)):
        # Fragment shapes (unbalanced parens / single char) can never be a maker name —
        # the source_ingest parser rejects them at extraction, but the ladder is the
        # single arbitration point for ALL writers, so junk dies here too (mirrors
        # set_category's off-vocab WARNING).
        logger.warning(
            "set_{}: card={} rejecting garbage value {!r} (source={}) — fragment/too-short, not a maker name",
            attr,
            getattr(card, "id", None),
            value,
            source,
        )
        return False

    db = Session.object_session(card)
    if db is None:
        global _warned_detached_normalize
        if not _warned_detached_normalize:
            _warned_detached_normalize = True
            logger.warning(
                "set_{}: card={} is not session-attached — alias canonicalization SKIPPED, "
                "writing the verbatim strip of {!r}. A detached-card writer fragments the "
                "brand facet; attach (add+flush) the card before calling set_brand/"
                "set_manufacturer (warned once per process).",
                attr,
                getattr(card, "id", None),
                value,
            )
    normalized = normalize_brand_name(db, str(value))
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
    ``write=False`` is the read-only dry-run twin. *card* must be session-attached
    (add+flush first) — a detached card skips canonicalization, with a once-per-process
    WARNING (see ``_set_brand_or_maker``).
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
    dry-run twin. *card* must be session-attached (add+flush first) — a detached card
    skips canonicalization, with a once-per-process WARNING (see ``_set_brand_or_maker``).
    """
    return _set_brand_or_maker(card, "manufacturer", value, source, confidence, write=write)
