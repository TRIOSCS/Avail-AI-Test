"""Deterministic description→spec field extraction (storage + memory).

What: reads parametric specs straight out of TRIO's compact *human description*
      strings (part master + inventory sheets — e.g. ``HD, 450GB, 15KRPM, 3.5",
      Fibre Channel``, ``Mem, 16GB DDR4 2Rx4 PC4-2400T RDIMM``) with NO network and
      NO LLM — zero hallucination. Descriptions are the only parametric signal for
      OEM/FRU cards whose spare numbers no MPN decoder recognizes. Extraction is
      gated on an explicit commodity signal (the TRIO ``<Label>,`` lead, a
      whole-word HDD/SSD/DIMM-grammar token, or the caller's commodity hint);
      anything else returns None (never guessed). Extracted values map to the
      seeded commodity_spec_schemas facet keys/enum values; record_spec
      independently re-validates enum members and skips unseeded keys, while
      numeric ranges are enforced only inside the extractors themselves.
Called by: the enrichment worker's second pass via desc_extractor/writer.py
      (between the mpn-decode pass at 0.95 and the AI spec reader at 0.85).
Depends on: desc_extractor.{_common,storage,memory} (pure functions).

Coverage is deliberately CONSERVATIVE: a field is emitted only when the description
grammar expresses it unambiguously; conflicting signals omit the key, a foreign
commodity label (``Other,``/``Tray,``/``LCD,``…) suppresses extraction entirely.
"""

import re

from app.services.desc_extractor._common import DESC_CONFIDENCE, SPEC_COMMODITIES, DescResult
from app.services.desc_extractor.memory import extract_memory
from app.services.desc_extractor.storage import extract_storage

_FAMILY = {
    "hdd": "storage",
    "ssd": "storage",
    "dram": "dram",
    "motherboards": "other",
    "power_supplies": "other",
    "cpu": "other",
}

# TRIO part-master grammar is "<Commodity label>, <details>, <OEM>". A leading label
# we don't handle ("Other", "Tray", "LCD", "Card", …) means TRIO classified the part
# as something else — extraction is suppressed even if drive tokens appear later
# ("Other, 3.5\" Server HDD Hard Drive tray"). Labels are pure alpha (so "DDR2," or
# "MC2X3-3.3," fall through to the body scan instead).
_LEAD = re.compile(r"^([A-Z][A-Z /.&-]{0,30}?)\s*[,<]")
_LEAD_MAP = {
    "HD": "hdd",
    "HDD": "hdd",
    "SSD": "ssd",
    "MB": "motherboards",
    "MAIN BOARD": "motherboards",
    "MOTHERBOARD": "motherboards",
    "MEM": "dram",
    "MEMORY": "dram",
    "PSU": "power_supplies",
    "POWER SUPPLY": "power_supplies",
    "CPU": "cpu",
    "PROCESSOR": "cpu",
}
_FOREIGN = "__foreign__"

# Comma-less descriptions ("SSD 480GB 7mmH …", "MB C 82L3 WIN …"): the bare first
# token routes only for the unambiguous labels (a bare leading "HD"/"Mem" without
# the comma is too loose — those require the `<Label>,` form).
_FIRST_TOKEN_MAP = {
    "HDD": "hdd",
    "SSD": "ssd",
    "MB": "motherboards",
    "MEM": "dram",
    "MEMORY": "dram",
    "PSU": "power_supplies",
    "CPU": "cpu",
}

# Whole-word body tokens (word boundaries make "16MB", "20HD", "SODIMM bracket"-style
# substrings safe). Order is irrelevant — all matches are collected.
_BODY_TOKENS = (
    ("hdd", re.compile(r"\bHDD\b|\bHARD DRIVE\b|\bHARD DISK\b")),
    ("ssd", re.compile(r"\bSSD\b")),
    ("dram", re.compile(r"\bMEMORY\b|\b(?:R|U|LR)DIMM\b|\bSO-?DIMM\b|\bDIMM\b|\bDDR(?:3L|[2345])?\b")),
    ("motherboards", re.compile(r"\bMOTHERBOARD\b|\bMAIN BOARD\b|\bMB\b")),
    ("power_supplies", re.compile(r"\bPOWER SUPPLY\b")),
    ("cpu", re.compile(r"\bCPU\b|\bPROCESSOR\b")),
)


def _lead_commodity(text: str) -> str | None:
    """Mapped commodity for a `<Label>,` lead, _FOREIGN for an unhandled label, else the
    _FIRST_TOKEN_MAP commodity for an unambiguous comma-less first token (e.g. ``SSD
    480GB 7mmH …``), else None."""
    m = _LEAD.match(text)
    if m:
        return _LEAD_MAP.get(m.group(1).strip(), _FOREIGN)
    first = re.match(r"([A-Z]+)\b", text)
    if first and first.group(1) in _FIRST_TOKEN_MAP:
        return _FIRST_TOKEN_MAP[first.group(1)]
    return None


def extract_desc(description: str, commodity_hint: str | None = None) -> DescResult | None:
    """Extract a DescResult from a human description, or None if nothing is safe to say.

    ``commodity_hint`` is the caller's authoritative commodity (typically the card's
    existing category): it routes extraction even when the text carries no commodity
    token, and a hint outside hdd/ssd/dram returns None (this extractor only speaks
    storage and memory). The returned ``commodity`` is a HINT for callers — nothing
    here ever writes a category.
    """
    if not description:
        return None
    text = re.sub(r"\s+", " ", description).strip().upper()
    if not text:
        return None

    hint = (commodity_hint or "").lower().strip() or None
    if hint is not None and hint not in SPEC_COMMODITIES:
        return None

    lead = _lead_commodity(text)
    if lead == _FOREIGN:
        return None
    found = {commodity for commodity, pattern in _BODY_TOKENS if pattern.search(text)}
    if lead:
        found.add(lead)

    families = {_FAMILY[c] for c in found}
    if hint:
        families.add(_FAMILY[hint])
    if "storage" in families and "dram" in families:
        # Cross-family conflict — e.g. "Memory, 256GB, LiteOn SSD, M.2 2280" or a
        # drive description on a DIMM-categorized card. Never pick a side.
        return None

    if hint:
        if lead and _FAMILY[lead] != _FAMILY[hint]:
            # TRIO's own label contradicts the card category ("MB," lead on a
            # dram-hinted card) — never extract from a contradicted description.
            return None
        # The hint (card category) routes; a same-family lead refines it
        # ("SSD," lead on an hdd-hinted card routes the ssd vocabulary).
        effective = lead or hint
    elif lead:
        effective = lead
    elif len(found) == 1:
        effective = next(iter(found))
    else:
        # No commodity signal at all (degenerate MPN-as-description), or ambiguous
        # body tokens with no lead to arbitrate (e.g. both HDD and SSD).
        return None

    if effective in ("hdd", "ssd"):
        specs = extract_storage(text, effective)
    elif effective == "dram":
        specs = extract_memory(text)
    else:
        specs = {}  # commodity hint only (motherboards / power_supplies / cpu)
    return DescResult(commodity=effective, specs=specs, confidence=DESC_CONFIDENCE)


__all__ = ["DescResult", "extract_desc"]
