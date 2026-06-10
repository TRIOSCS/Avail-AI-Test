"""Brand/manufacturer name normalization + dual-brand gating constants.

What: ``normalize_brand_name(db, value)`` canonicalizes a brand/maker name through the
      ``manufacturers`` lookup table (``canonical_name`` + ``aliases`` JSON), e.g.
      ``HP`` → ``Hewlett Packard Enterprise``, ``SEAGATE`` → ``Seagate Technology``.
      Case-insensitive; a miss returns the input verbatim with ``.strip()`` only —
      a source-backed verbatim value is truthful, inventing a canonicalization would be
      a guess (composite makers like ``Hitachi/IBM`` stay verbatim, their own facet
      value). The table map is cached per-process (loaded lazily; never invalidated —
      the table is seed-only, a restart refreshes; under TESTING=1 it reloads per call
      so each test's isolated DB is honored). Also home of the dual-brand gating
      constants: ``OEM_BRANDS`` (the only values ever routed to ``brand`` by
      regex/reclassification), ``OEM_TRAILING_RE`` (trailing description token → brand)
      and ``MAKER_TRAILING_RE`` (trailing description token → manufacturer).
Called by: app/services/spec_tiers.py (set_brand / set_manufacturer — writers never
      normalize themselves), app/services/source_ingest/clean.py (trailing-token
      routing), app/management/backfill_dual_brand.py (B1/B3 gating).
Depends on: app.models.Manufacturer (lazy import — avoids a model↔service import
      cycle); the seeds in app/startup.py:_seed_manufacturers.
"""

from __future__ import annotations

import os
import re

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
    """Build (or return the cached) lowercase → canonical-name map."""
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
    _canonical_by_lower = mapping
    return mapping


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
