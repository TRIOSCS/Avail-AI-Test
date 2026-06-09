"""Source→tier provenance ladder (SP2 / shared foundation F1+F2).

What: Defines the single authoritative rule for "which data source wins" so that good
      data always beats guesses and source-execution ORDER is no longer load-bearing.
      ``SOURCE_TIER`` ranks every writer; ``tier_for`` looks a source up; ``resolve``
      decides whether an incoming provenance tuple beats an existing one; ``set_category``
      applies that ladder to a MaterialCard's category column (the one DB-touching helper).
Called by: app/services/spec_write_service.record_spec (spec conflict resolution),
      app/services/mpn_decoder/writer.py (category write), and — in sibling SPs — the
      other category writers (enrichment, authoritative, material enrichment).
Depends on: app.services.category_normalizer.normalize_category (lazy import inside
      set_category to avoid a model↔service import cycle), MaterialCard's category
      provenance columns (added by migration 092_spec_provenance).

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

# Source → tier ranking. Higher tier always overrides a lower one (F1). Vendor distributor
# APIs share tier 90; the deterministic MPN decode is 85; OEM scrapers (PartSurfer/PSREF)
# map to 80; AI free-text mining sits at the bottom. ``manual`` (a human edit) tops it.
SOURCE_TIER: dict[str, int] = {
    "manual": 100,
    "digikey_api": 90,
    "mouser_api": 90,
    "nexar_api": 90,
    "element14_api": 90,
    "oemsecrets_api": 90,
    "mpn_decode": 85,
    "partsurfer": 80,
    "psref": 80,
    "web_search": 70,
    "brokerbin": 65,
    "spec_extraction": 60,
    "ai_guess": 40,
    "claude_opus_inferred": 40,
}


def tier_for(source: str) -> int:
    """Return the ladder tier for *source* (unknown source → 0, can never beat a known
    one)."""
    tier = SOURCE_TIER.get(source, 0)
    if tier == 0:
        logger.debug("tier_for: unknown source {!r} → tier 0", source)
    return tier


def resolve(existing: dict | None, incoming: dict) -> bool:
    """Return ``True`` iff *incoming* wins over *existing* under the F1 ladder.

    Each arg is a provenance dict with keys ``tier`` (int), ``confidence`` (float), and
    ``updated_at`` (ISO-8601 string, lexicographically sortable). ``existing is None`` →
    incoming always wins. Otherwise incoming wins iff its ``(tier, confidence, updated_at)``
    tuple is strictly greater. Pure function — no DB, no side effects.
    """
    if existing is None:
        return True
    incoming_key = (
        incoming.get("tier", 0),
        incoming.get("confidence", 0.0),
        incoming.get("updated_at", ""),
    )
    existing_key = (
        existing.get("tier", 0),
        existing.get("confidence", 0.0),
        existing.get("updated_at", ""),
    )
    return incoming_key > existing_key


def set_category(card: "MaterialCard", value: str | None, source: str, confidence: float) -> bool:
    """Set ``card.category`` (+ provenance) through the F1 ladder. Return ``True`` if
    written.

    Normalizes *value* to a canonical commodity key (off-vocab → ``None``, never persisted
    as junk). If it normalizes to ``None`` the call is a no-op and returns ``False``. Builds
    the incoming provenance from *source*/*confidence* and compares it (via ``resolve``)
    against the card's existing category provenance — a lower-tier source can never
    overwrite a higher-tier category. On a win it sets ``category`` and the three
    ``category_*`` provenance columns and returns ``True``; otherwise leaves the card
    untouched and returns ``False``.
    """
    from app.services.category_normalizer import normalize_category

    canonical = normalize_category(value)
    if canonical is None:
        # Off-vocab / empty / None — never persist junk, never blank an existing category.
        if value:
            logger.warning("set_category: off-vocab value {!r} (source={}) — not writing", value, source)
        return False

    now_iso = datetime.now(timezone.utc).isoformat()
    incoming = {"tier": tier_for(source), "confidence": confidence, "updated_at": now_iso}

    if card.category is None:
        existing = None
    else:
        existing = {
            "tier": card.category_tier if card.category_tier is not None else 0,
            "confidence": card.category_confidence if card.category_confidence is not None else 0.0,
            "updated_at": card.updated_at.isoformat() if card.updated_at is not None else "",
        }

    if not resolve(existing, incoming):
        logger.debug(
            "set_category: card={} kept existing category={!r} (incoming {!r}@{} lost)",
            getattr(card, "id", None),
            card.category,
            canonical,
            source,
        )
        return False

    card.category = canonical  # already canonical; SP3's @validates("category") hardens other paths
    card.category_source = source
    card.category_confidence = confidence
    card.category_tier = incoming["tier"]
    return True
