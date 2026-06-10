"""SP-Ingest cleaning — turn a raw parsed SourceRecord into a normalized one (or drop
it).

What: ``clean_record`` strips MPN suffixes (` - Pull` / ` - New` / `-x`) and normalizes the
      MPN to its dedup key; scrubs ``_x000D_`` / control chars and collapses whitespace in the
      free-text fields; canonicalizes ``condition`` to a small enum; maps the source commodity
      to an app-canonical category (None if unmappable); and DROPS the record when the MPN is
      too short / falsy or the description says "DO NOT USE". ``extract_trailing_oem`` pulls the
      embedded ", IBM"/", EMC"/", HP" manufacturer token out of a sheet description.
Called by: app/management/ingest_source_data.py (between parse and consolidate).
Depends on: app.utils.normalization.normalize_mpn_key (the dedup-key normalizer the app uses
      for material_cards.normalized_mpn) + normalize_mpn (display form); app.services.
      category_normalizer.normalize_category; the SourceRecord dataclass.
"""

from __future__ import annotations

import re

from app.services.category_normalizer import normalize_category
from app.services.source_ingest.models import SourceRecord
from app.utils.normalization import normalize_mpn, normalize_mpn_key

# Condition canon enum (task-mandated): the small set we persist downstream.
CONDITION_NEW = "New"
CONDITION_PULL = "Pull"
CONDITION_REFURBISHED = "Refurbished"
CONDITION_USED = "Used"
CONDITION_UNKNOWN = "Unknown"

# Source condition string (lowercased, substring) -> canonical enum. Ordered most-specific
# first so "refurbished" is not swallowed by a looser pattern.
_CONDITION_MAP: list[tuple[str, str]] = [
    ("refurb", CONDITION_REFURBISHED),
    ("recondition", CONDITION_REFURBISHED),
    ("pull", CONDITION_PULL),
    ("used", CONDITION_USED),
    ("surplus", CONDITION_USED),
    ("new", CONDITION_NEW),
    ("factory", CONDITION_NEW),
]

# MPN suffixes that mark stock state, not the part identity: " - Pull", " - New", "-x"/"-X".
# Stripped (case-insensitively) before normalizing the MPN.
_MPN_SUFFIX_RE = re.compile(r"\s*-\s*(?:pull|new)\s*$|-[xX]\s*$", re.IGNORECASE)

# Control chars + the literal Excel CR token ``_x000D_`` that leaks into exported text.
_X000D_RE = re.compile(r"_x000[dD]_")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_WS_RE = re.compile(r"\s+")


def _scrub_text(value: str | None) -> str | None:
    """Strip ``_x000D_``/control chars, trim, and collapse internal whitespace."""
    if value is None:
        return None
    s = _X000D_RE.sub(" ", str(value))
    s = _CONTROL_RE.sub(" ", s)
    s = _WS_RE.sub(" ", s).strip()
    return s or None


def canonicalize_condition(raw: str | None) -> str:
    """Map a source condition string to {New, Pull, Refurbished, Used, Unknown}."""
    if not raw:
        return CONDITION_UNKNOWN
    s = str(raw).strip().lower()
    for pattern, canon in _CONDITION_MAP:
        if pattern in s:
            return canon
    return CONDITION_UNKNOWN


def strip_mpn_suffix(raw_mpn: str) -> str:
    """Strip a trailing stock-state suffix (` - Pull`/` - New`/`-x`) from an MPN."""
    return _MPN_SUFFIX_RE.sub("", str(raw_mpn)).strip()


def extract_trailing_oem(description: str | None) -> str | None:
    """Pull the trailing manufacturer token from a sheet description.

    The operational sheets embed the OEM as the last comma-token, e.g. "HDD, 6Gbps 1.2TB
    10K 2.5 Inch HDD, IBM" → "IBM". Returns None when the last token is not a plausible
    manufacturer (too long / sentence-like) so we never mistake spec text for an OEM.
    """
    if not description:
        return None
    parts = [p.strip() for p in str(description).split(",")]
    if len(parts) < 2:
        return None
    candidate = parts[-1]
    # A manufacturer token is short, has no digits-only payload, and isn't a spec phrase.
    if not candidate or len(candidate) > 40:
        return None
    word_count = len(candidate.split())
    if word_count > 4:
        return None
    # Reject obvious non-OEM trailers (pure measurements/connectors left after a comma).
    if re.fullmatch(r"[\d.\s\"'/-]+", candidate):
        return None
    return candidate


def clean_record(rec: SourceRecord) -> SourceRecord | None:
    """Clean one SourceRecord. Returns the cleaned record, or None if it must be
    dropped.

    Drops when: the MPN normalizes to falsy / <3 chars, OR the description contains
    "DO NOT USE" (case-insensitive). Mutates a copy-in-place: scrubs text fields, sets
    ``normalized_mpn`` (dedup key) and ``raw_mpn`` (display form), canonicalizes condition,
    fills manufacturer from the trailing OEM token when absent, and maps the source category
    to a canonical key (None if unmappable).
    """
    display_mpn = strip_mpn_suffix(rec.raw_mpn)
    norm_key = normalize_mpn_key(display_mpn)
    # normalize_mpn returns None for <3 chars; normalize_mpn_key returns "" for empty input.
    if not norm_key or len(norm_key) < 3 or normalize_mpn(display_mpn) is None:
        return None

    description = _scrub_text(rec.description)
    if description and "do not use" in description.lower():
        return None

    manufacturer = _scrub_text(rec.manufacturer)
    if not manufacturer:
        manufacturer = extract_trailing_oem(description)

    return SourceRecord(
        raw_mpn=normalize_mpn(display_mpn) or display_mpn,
        normalized_mpn=norm_key,
        manufacturer=manufacturer,
        description=description,
        condition=canonicalize_condition(rec.condition),
        quantity=rec.quantity,
        category=normalize_category(rec.category),
        specs=dict(rec.specs),
        source_file=rec.source_file,
        source_kind=rec.source_kind,
    )
