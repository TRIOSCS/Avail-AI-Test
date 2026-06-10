"""SP-Ingest cleaning — turn a raw parsed SourceRecord into a normalized one (or drop
it).

What: ``clean_record`` strips MPN suffixes (` - Pull` / ` - New` / `-x`) and normalizes the
      MPN to its dedup key; scrubs ``_x000D_`` / control chars and collapses whitespace in the
      free-text fields; canonicalizes ``condition`` to the MaterialCondition vocabulary
      (constants.py — None when the source carries none); maps the source commodity
      to an app-canonical category (None if unmappable) with a CPU-bucket pollution deny-list
      (the SFDC master's 'CPU' code is ~14% non-CPU parts — see _CPU_POLLUTION_RE); and DROPS
      the record when the MPN is too short / falsy or the description says "DO NOT USE".
      ``extract_trailing_oem`` pulls the embedded ", IBM"/", EMC"/", HP" trailing token out
      of a sheet description. Dual-brand routing (SPEC_DUAL_BRAND_FILTERS §2 W6): a trailing
      token matching OEM_TRAILING_RE (IBM/Dell/HP/HPE/Lenovo — an OEM LABEL, not a maker)
      fills ``record.brand``; any other plausible trailing token keeps the legacy behavior
      and fills ``manufacturer`` when absent. Brand is NEVER inferred beyond that regex.
Called by: app/management/ingest_source_data.py (between parse and consolidate).
Depends on: app.utils.normalization.normalize_mpn_key (the dedup-key normalizer the app uses
      for material_cards.normalized_mpn) + normalize_mpn (display form); app.services.
      category_normalizer.normalize_trio_category (the SFDC ingest entry point — consults
      the TRIO-scoped Commodity_Code__c vocabulary first, e.g. bare "Memory"→dram, then
      falls back to the global alias map); the SourceRecord dataclass.
"""

from __future__ import annotations

import re

from app.constants import MaterialCondition
from app.services.category_normalizer import normalize_trio_category
from app.services.manufacturer_normalizer import OEM_TRAILING_RE
from app.services.source_ingest.models import SourceRecord
from app.utils.normalization import normalize_mpn, normalize_mpn_key

# Source condition string (lowercased, substring) -> MaterialCard.condition canonical
# value (constants.MaterialCondition — the column's documented vocabulary). Ordered
# most-specific first so "recertified"/"refurbished" are not swallowed by a looser
# pattern. Absent/unmatched input canonicalizes to None, NEVER to "Unknown": a synthetic
# Unknown would (a) outvote a real sheet condition in consolidate's modal vote and
# (b) permanently occupy the fill-only-when-empty card column.
_CONDITION_MAP: list[tuple[str, str]] = [
    ("recert", MaterialCondition.RECERTIFIED),
    ("refurb", MaterialCondition.REFURBISHED),
    ("recondition", MaterialCondition.REFURBISHED),
    ("pull", MaterialCondition.PULLED),
    ("used", MaterialCondition.USED),
    ("surplus", MaterialCondition.USED),
    ("new", MaterialCondition.NEW),
    ("factory", MaterialCondition.NEW),
]

# MPN suffixes that mark stock state, not the part identity: " - Pull", " - New", "-x"/"-X".
# Stripped (case-insensitively) before normalizing the MPN.
_MPN_SUFFIX_RE = re.compile(r"\s*-\s*(?:pull|new)\s*$|-[xX]\s*$", re.IGNORECASE)

# Control chars + the literal Excel CR token ``_x000D_`` that leaks into exported text.
_X000D_RE = re.compile(r"_x000[dD]_")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_WS_RE = re.compile(r"\s+")

# CPU-bucket pollution guard (CATALOG.md ingest warning + docs/CPU_DECODE_FEASIBILITY.md
# step 0): ~14% of the SFDC master's Commodity_Code__c='CPU' rows are NON-CPU parts. A
# tier-95 ``set_category("cpu")`` on a Murata MLCC would be near-unoverridable (only
# manual=100 beats trio_source), so MPNs matching the empirically-found false-positive
# shapes get their category BLANKED (the card stays uncategorizable from this source —
# never miscategorized). Deny-list, anchored at the start of the MPN:
#   GRM…            Murata MLCC chip caps           EEE…/EEU…  Panasonic aluminum caps
#   SN74…           TI logic ICs                    SMAJ…      TVS diodes
#   B72…            EPCOS/TDK varistors             C0603/C0805/C1206… chip-cap shapes
#   #####A###XAT…   AVX chip caps (06035A101JAT2A)
#   ######-# / #-######-#  TE Connectivity connector PNs (640456-9, 1-640456-0) — single
#   trailing digit, distinct from HP spares' three-char dash suffix (732505-001).
_CPU_POLLUTION_RE = re.compile(
    r"^(?:"
    r"GRM\d|EEE[A-Z0-9]|EEU[A-Z0-9]|SN74|SMAJ|B72\d|C0603|C0805|C1206"
    r"|\d{5}A\d{3}[A-Z]AT"
    r"|\d{6}-\d$|\d-\d{6}-\d$"
    r")",
    re.IGNORECASE,
)


def _scrub_text(value: str | None) -> str | None:
    """Strip ``_x000D_``/control chars, trim, and collapse internal whitespace."""
    if value is None:
        return None
    s = _X000D_RE.sub(" ", str(value))
    s = _CONTROL_RE.sub(" ", s)
    s = _WS_RE.sub(" ", s).strip()
    return s or None


def canonicalize_condition(raw: str | None) -> str | None:
    """Map a source condition string to the MaterialCondition vocabulary, or None.

    Returns one of {New, Recertified, Refurbished, Used, Pulled} — the column's
    documented canon (constants.MaterialCondition) — or ``None`` when the source said
    nothing / nothing recognizable. "No data" stays None (the column stays NULL) so a
    later real value can still fill it; it is never collapsed into "Unknown".
    """
    if not raw:
        return None
    s = str(raw).strip().lower()
    for pattern, canon in _CONDITION_MAP:
        if pattern in s:
            return str(canon)
    return None


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
    "DO NOT USE" (case-insensitive). Returns a NEW, cleaned SourceRecord (the input is
    never modified): scrubs text fields, sets ``normalized_mpn`` (dedup key) and
    ``raw_mpn`` (display form), canonicalizes condition (None when the source had none),
    routes a trailing OEM-label token (OEM_TRAILING_RE) to ``brand``, fills manufacturer
    from any OTHER plausible trailing token when absent, and maps the source category to
    a canonical key (None if unmappable).
    """
    display_mpn = strip_mpn_suffix(rec.raw_mpn)
    norm_key = normalize_mpn_key(display_mpn)
    # normalize_mpn returns None for <3 chars; normalize_mpn_key returns "" for empty input.
    if not norm_key or len(norm_key) < 3 or normalize_mpn(display_mpn) is None:
        return None

    description = _scrub_text(rec.description)
    if description and "do not use" in description.lower():
        return None

    # Dual-brand routing: a trailing token in the literal OEM-label list is BRAND
    # evidence ("HDD, ..., IBM" → brand IBM), never a maker. Anything else keeps the
    # legacy behavior: fill manufacturer when the source carried none.
    brand = _scrub_text(rec.brand)
    oem_match = OEM_TRAILING_RE.search(description) if description else None
    if oem_match and not brand:
        brand = oem_match.group(1)

    manufacturer = _scrub_text(rec.manufacturer)
    if not manufacturer and not oem_match:
        manufacturer = extract_trailing_oem(description)

    # All source_ingest data is TRIO's own export, so the TRIO-scoped vocabulary
    # (e.g. bare "Memory" → dram) applies before the global alias map.
    category = normalize_trio_category(rec.category)
    if category == "cpu" and _CPU_POLLUTION_RE.match(display_mpn):
        # Known non-CPU MPN shape inside the polluted CPU bucket — blank, never write.
        category = None
    if category == "other":
        # TRIO's 'Other' code is the ABSENCE of classification, not ground truth — writing
        # the canonical "other" bucket at tier 95 would permanently block the decode (85) /
        # desc-parse (83) / AI lanes from ever re-homing the ~18k re-homeable rows the
        # facet-curation analysis identified (HDD trays, CPUs, SSDs, …). Blank it: the card
        # stays in the no-commodity bucket, open to real categorization.
        category = None

    return SourceRecord(
        raw_mpn=normalize_mpn(display_mpn) or display_mpn,
        normalized_mpn=norm_key,
        manufacturer=manufacturer,
        brand=brand,
        description=description,
        condition=canonicalize_condition(rec.condition),
        quantity=rec.quantity,
        category=category,
        specs=dict(rec.specs),
        source_file=rec.source_file,
        source_kind=rec.source_kind,
    )
