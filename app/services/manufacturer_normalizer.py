"""Brand/manufacturer name normalization + dual-brand gating constants.

What: ``normalize_brand_name(db, value)`` canonicalizes a brand/maker name through the
      ``manufacturers`` lookup table (``canonical_name`` + ``aliases`` JSON), e.g.
      ``HP`` → ``HPE``, ``SEAGATE`` → ``Seagate Technology``.
      Case-insensitive; a miss returns the input verbatim with ``.strip()`` only —
      a source-backed verbatim value is truthful, inventing a canonicalization would be
      a guess (composite makers like ``Hitachi/IBM`` stay verbatim, their own facet
      value). The table map is cached per-process (loaded lazily; a NON-EMPTY load is
      never invalidated — the table is seed-only, a restart refreshes — but an EMPTY
      load is never memoized, so a worker/CLI that races the app's seeding self-heals;
      under TESTING=1 it reloads per call so each test's isolated DB is honored).
      ``is_garbage_brand_value(value)`` classifies strings that can never be a real
      brand/maker name (len<2 after strip, or unbalanced parentheses — the comma-split
      fragments of parenthesized MPN packing suffixes, e.g. the ``F)``/``LF(T`` residue
      of Toshiba ordering codes like ``TLP781(D4-GR-TP6,F)``). Also
      home of the dual-brand gating
      constants: ``OEM_BRANDS`` (the only values ever routed to ``brand`` by
      regex/reclassification), ``OEM_TRAILING_RE`` (trailing description token → brand)
      and ``MAKER_TRAILING_RE`` (trailing description token → manufacturer).
Called by: app/services/spec_tiers.py (set_brand / set_manufacturer — writers never
      normalize themselves; the garbage gate rejects fragment values at the ladder),
      app/services/source_ingest/clean.py (trailing-token routing + candidate
      plausibility), app/management/backfill_dual_brand.py (B1/B3 gating),
      app/management/normalize_manufacturers.py (one-shot canonicalization backfill).
Depends on: app.models.Manufacturer (lazy import — avoids a model↔service import
      cycle); the seeds in app/startup.py:_seed_manufacturers.
"""

from __future__ import annotations

import os
import re

from loguru import logger
from sqlalchemy.orm import Session

# The ONLY values ever routed to `brand` by regex/reclassification: the four observed
# OEM labels + HP aliases. Fujitsu/Hitachi/Samsung are NOT here — they appear as makers
# in fru_links. Lowercase membership test: `value.lower() in OEM_BRANDS`.
OEM_BRANDS = {"ibm", "dell", "hp", "hpe", "hewlett packard enterprise", "lenovo"}

# Trailing description token → brand (OEM label). Literal list only; anything else is
# never written ("HDD, 300GB, 2.5\" SED, 15K RPM, IBM" → brand IBM).
OEM_TRAILING_RE = re.compile(r",\s*(IBM|Dell|HP|HPE|Lenovo)\s*$", re.IGNORECASE)

# Trailing description token → manufacturer (actual maker). Literal list only.
MAKER_TRAILING_RE = re.compile(r",\s*(Seagate|Kingston|Samsung)\s*$", re.IGNORECASE)

# Per-process lowercase-name → canonical-name map. None = not yet loaded. Loaded lazily
# from the manufacturers table on first use; never invalidated (table is seed-only —
# restart refreshes). Canonical names override aliases on key collisions ("Toshiba" the
# canonical wins over "Toshiba" the alias of "Toshiba Electronic Devices").
_canonical_by_lower: dict[str, str] | None = None


def _load_map(db: Session) -> dict[str, str]:
    """Build (or return the cached) lowercase → canonical-name map.

    An EMPTY query result is treated as a cache MISS (returned but never memoized):
    the enrichment worker / a CLI can race the app container's ``_seed_manufacturers``
    on first deploy, and freezing a pre-seed empty map for the process lifetime would
    silently split the brand facet ("Kingston" vs "Kingston Technology") until a
    restart. A non-empty map IS memoized forever — the table is seed-only, a restart
    refreshes.
    """
    global _canonical_by_lower
    if _canonical_by_lower is not None and not os.environ.get("TESTING"):
        return _canonical_by_lower
    from app.models import Manufacturer

    rows = db.query(Manufacturer.canonical_name, Manufacturer.aliases).all()
    mapping: dict[str, str] = {}
    for canonical, aliases in rows:
        for alias in aliases or []:
            key = str(alias).strip().lower()
            if key:
                mapping[key] = canonical
    # Canonical names second, so a canonical always maps to itself even when it collides
    # with another row's alias.
    for canonical, _aliases in rows:
        key = str(canonical).strip().lower()
        if key:
            mapping[key] = canonical
    if not mapping:
        # Pre-seed race (manufacturers table not seeded yet in THIS process's view) —
        # use the empty map for this call but do NOT memoize it, so the cache
        # self-heals once the seeds land.
        logger.warning(
            "manufacturer_normalizer: manufacturers table is empty — alias map NOT cached "
            "(pre-seed race?); values pass through verbatim until the seeds are visible"
        )
        return mapping
    _canonical_by_lower = mapping
    logger.info("manufacturer_normalizer: loaded {} alias mapping(s) from manufacturers table", len(mapping))
    return mapping


def is_garbage_brand_value(value: str | None) -> bool:
    """True when *value* can never be a real brand/maker name.

    Two empirically-validated shapes (live audit 2026-06-12, 2,340 cards — every one a
    parser fragment or empty residue, zero legitimate names lost):

    - shorter than 2 characters once stripped (includes the empty-string residue;
      no 0/1-char manufacturer exists — 2-char names like ``TI``/``WD`` stay valid);
    - unbalanced parentheses — the comma-split fragments of parenthesized MPN packing
      suffixes (``F)``, ``LF(T``, ``TSOP)``, ``Ind.)`` …) that the pre-fix
      ``extract_trailing_oem`` carved out of Toshiba-style ordering codes like
      ``TLP781(D4-GR-TP6,F)``. Balanced parentheticals (``Texas Instruments (TI)``)
      are NOT garbage — they normalize via the alias table instead.
    """
    s = str(value or "").strip()
    if len(s) < 2:
        return True
    return s.count("(") != s.count(")")


def normalize_brand_name(db: Session | None, value: str) -> str:
    """Canonicalize *value* through the manufacturers table, verbatim-strip on miss.

    Case-insensitive lookup against ``canonical_name`` + ``aliases``. Hit → the
    canonical name. Miss (or no usable session) → the input with ``.strip()`` only:
    never invent a canonicalization for a source-backed verbatim value.
    """
    stripped = str(value).strip()
    if not stripped or db is None:
        return stripped
    return _load_map(db).get(stripped.lower(), stripped)
