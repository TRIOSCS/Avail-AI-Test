"""Deterministic description→spec field extraction (storage, memory, PSU, displays,
tape, GPU, motherboards).

What: reads parametric specs straight out of TRIO's compact *human description*
      strings (part master + inventory sheets — e.g. ``HD, 450GB, 15KRPM, 3.5",
      Fibre Channel``, ``Mem, 16GB DDR4 2Rx4 PC4-2400T RDIMM``, ``PSU, 1460W
      240V/200V AC Hot Swap``) with NO network and NO LLM — zero hallucination.
      Descriptions are the only parametric signal for OEM/FRU cards whose spare
      numbers no MPN decoder recognizes. Extraction is gated on an explicit
      commodity signal (the TRIO ``<Label>,`` lead, a whole-word grammar token,
      or the caller's commodity hint); anything else returns None (never
      guessed). Extracted values map to the seeded commodity_spec_schemas facet
      keys/enum values; record_spec independently re-validates enum members and
      skips unseeded keys, while numeric ranges are enforced only inside the
      extractors themselves.
Called by: the enrichment worker's second pass via desc_extractor/writer.py
      (between the mpn-decode pass at 0.95 and the AI spec reader at 0.85).
Depends on: desc_extractor.{_common,storage,memory,power,display,tape,gpu,board}
      (pure functions).

Coverage is deliberately CONSERVATIVE: a field is emitted only when the description
grammar expresses it unambiguously; conflicting signals omit the key, a foreign
commodity label (``Other,``/``Tray,``/``Card,``/``Library,``…) suppresses extraction
entirely. Packaging-word and brand leads (``ASSY,``/``FRU,``/``MSI,``/``SPS-…``)
are NEUTRAL — they fall through to body-token + hint arbitration instead.
"""

import re

from app.services.desc_extractor._common import DESC_CONFIDENCE, SPEC_COMMODITIES, DescResult
from app.services.desc_extractor.board import extract_board
from app.services.desc_extractor.display import extract_display
from app.services.desc_extractor.gpu import extract_gpu
from app.services.desc_extractor.memory import extract_memory
from app.services.desc_extractor.power import extract_psu
from app.services.desc_extractor.storage import extract_storage
from app.services.desc_extractor.tape import extract_tape

_FAMILY = {
    "hdd": "storage",
    "ssd": "storage",
    "dram": "dram",
    "motherboards": "board",
    "power_supplies": "power",
    "displays": "display",
    "tape_drives": "tape",
    "gpu": "gpu",
    "cpu": "other",
}

# TRIO part-master grammar is "<Commodity label>, <details>, <OEM>". A leading label
# we don't handle ("Other", "Tray", "Card", "Library", …) means TRIO classified the
# part as something else — extraction is suppressed even if drive tokens appear later
# ("Other, 3.5\" Server HDD Hard Drive tray"). Labels are pure alpha (so "DDR2," or
# "MC2X3-3.3," fall through to the body scan instead). "CARD," stays FOREIGN — the
# corpus GC bucket is heavily contaminated (only ~38% of "Card," rows are real GPUs:
# "Card, PCI-X Quad Channel Ultra4 Controller", "Card, Adapter Card, ConnectX-6").
_LEAD = re.compile(r"^([A-Z][A-Z /.&-]{0,30}?)\s*[,<]")
_LEAD_MAP = {
    "HD": "hdd",
    "HDD": "hdd",
    "SSD": "ssd",
    "MB": "motherboards",
    "MAIN BOARD": "motherboards",
    "MAINBOARD": "motherboards",
    "MOTHERBOARD": "motherboards",
    "BDPLANAR": "motherboards",
    "BDPLANAR WIN": "motherboards",
    "MEM": "dram",
    "MEMORY": "dram",
    "PSU": "power_supplies",
    "POWER SUPPLY": "power_supplies",
    "PWR SPLY": "power_supplies",
    "PWR SUPPLY": "power_supplies",
    "AC ADAPTER": "power_supplies",
    "AC ADAPTERS": "power_supplies",
    "LCD": "displays",
    "LCD PANEL": "displays",
    "PNL": "displays",
    "PANEL": "displays",
    "DISPLAY": "displays",
    "DSPLY": "displays",
    "MONITOR": "displays",
    "HU": "displays",  # HP display-unit lead: "HU, FHD AG LED UWVA 13 TS"
    "TAPE": "tape_drives",
    "TAPE DRIVE": "tape_drives",
    "TD": "tape_drives",
    "GPU": "gpu",
    "GPU CARD": "gpu",
    "GC": "gpu",
    "GRAPHICS CARD": "gpu",
    "GRAPHIC CARD": "gpu",
    "VIDEO CARD": "gpu",
    "VIDEO BOARD": "gpu",
    "CPU": "cpu",
    "PROCESSOR": "cpu",
}
_FOREIGN = "__foreign__"

# Leads that are packaging words or brand names, NOT commodity labels — treated as
# *no lead* (fall through to body-token + hint arbitration) instead of FOREIGN.
# Any "SPS…"-prefixed lead is neutral too ("SPS-PCA, NVIDIA Tesla V100 32GB Module"
# must not die foreign), and so is a label whose LAST word is neutral ("SUPERMICRO
# FRU," is brand+packaging). Phase-1 guards still hold behind a neutral lead: a
# brand-led body mixing HDD+DIMM tokens hard-conflicts to None as before.
_NEUTRAL_LEADS = frozenset(
    {
        # structural / packaging words
        "ASSY",
        "FRU",
        "KIT",
        "PCA",
        "CRD",
        "MODULE",
        "SXM",
        "SXM ASSY",
        # brand names
        "IBM",
        "HP",
        "HPE",
        "DELL",
        "LENOVO",
        "ACER",
        "ASUS",
        "MSI",
        "NVIDIA",
        "INTEL",
        "SAMSUNG",
        "LG",
        "SHARP",
        "INNOLUX",
        "AUO",
        "BOE",
        "TANDBERG",
        "QUANTUM",
        "DELTA",
        "ACBEL",
        "ASTEC",
        "ZIPPY",
        "CISCO",
        "EMULEX",
    }
)

# Comma-less descriptions ("SSD 480GB 7mmH …", "Tape LTO-9, FH" — the digit in
# "LTO-9" blocks the lead regex): the bare first token routes only for the
# unambiguous labels (a bare leading "HD"/"Mem" without the comma is too loose —
# those require the `<Label>,` form).
_FIRST_TOKEN_MAP = {
    "HDD": "hdd",
    "SSD": "ssd",
    "MB": "motherboards",
    "BDPLANAR": "motherboards",
    "MAINBOARD": "motherboards",
    "MEM": "dram",
    "MEMORY": "dram",
    "PSU": "power_supplies",
    "LCD": "displays",
    "PNL": "displays",
    "DISPLAY": "displays",
    "DSPLY": "displays",
    "MONITOR": "displays",
    "TAPE": "tape_drives",
    "GPU": "gpu",
    "GC": "gpu",
    "CPU": "cpu",
}

# Whole-word body tokens (word boundaries make "16MB", "20HD", "SODIMM bracket"-style
# substrings safe). Order is irrelevant — all matches are collected. GPU family words
# (RTX/QUADRO/GTX…) are extractor-level vocabulary, NOT routing tokens — "SPS-MB DSC
# GTX1050 4GB i7-7700HQ WIN" must stay a motherboard.
_BODY_TOKENS = (
    ("hdd", re.compile(r"\bHDD\b|\bHARD DRIVE\b|\bHARD DISK\b")),
    ("ssd", re.compile(r"\bSSD\b")),
    ("dram", re.compile(r"\bMEMORY\b|\b(?:R|U|LR)DIMM\b|\bSO-?DIMM\b|\bDIMM\b|\bDDR(?:3L|[2345])?\b")),
    (
        "motherboards",
        re.compile(r"\bMOTHERBOARD\b|\bMAIN BOARD\b|\bMB\b|\bMAINBOARD\b|\bBDPLANAR\b|\bPLANAR\b"),
    ),
    (
        "power_supplies",
        re.compile(r"\bPSU\b|\bP/S\b|POWER SUPPLY|POWERSUPPLY|PWR[ _]SPLY|PWR[ _]?SUPPLY|\bAC ADAPTER\b"),
    ),
    # NOT bare "PANEL" — too generic for a body token (lead-position only).
    ("displays", re.compile(r"\bLCD\b|\bDISPLAY\b|\bDSPLY\b|\bMONITOR\b|\bPNL\b")),
    # Covers glued "LTO9 CANIS" / "JAG6 DRIVE" generation tokens.
    ("tape_drives", re.compile(r"\bLTO[- ]?\d\b|\bULTRIUM\b|TAPE DRIVE|\b3592\b|\bJAG ?\d\b")),
    ("gpu", re.compile(r"\bGPU\b|\bGFX\b|\bGRPHC\b|GRAPHICS? CARD|GRAPHICS? BOARD")),
    ("cpu", re.compile(r"\bCPU\b|\bPROCESSOR\b")),
)


def _is_neutral_lead(label: str) -> bool:
    return label.startswith("SPS") or label in _NEUTRAL_LEADS or label.rsplit(" ", 1)[-1] in _NEUTRAL_LEADS


def _lead_commodity(text: str) -> str | None:
    """Mapped commodity for a `<Label>,` lead, _FOREIGN for an unhandled label, None for
    a NEUTRAL lead (packaging word / brand — body tokens + hint arbitrate), else the
    _FIRST_TOKEN_MAP commodity for an unambiguous comma-less first token (e.g. ``SSD
    480GB 7mmH …``), else None."""
    m = _LEAD.match(text)
    if m:
        # Dot-strip normalization: "PSU., 750W TT ITIC, Acbel…" captures "PSU.".
        label = m.group(1).strip().rstrip(".")
        commodity = _LEAD_MAP.get(label)
        if commodity:
            return commodity
        return None if _is_neutral_lead(label) else _FOREIGN
    first = re.match(r"([A-Z]+)\b", text)
    if first and first.group(1) in _FIRST_TOKEN_MAP:
        return _FIRST_TOKEN_MAP[first.group(1)]
    return None


def extract_desc(description: str, commodity_hint: str | None = None) -> DescResult | None:
    """Extract a DescResult from a human description, or None if nothing is safe to say.

    ``commodity_hint`` is the caller's authoritative commodity (typically the card's
    existing category): it routes extraction even when the text carries no commodity
    token, and a hint outside SPEC_COMMODITIES (hdd/ssd/dram/power_supplies/displays/
    tape_drives/gpu/motherboards) returns None. The returned ``commodity`` is a HINT
    for callers — nothing here ever writes a category.
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
        # The HARD cross-family conflict is storage×dram only (both read bare-GB
        # capacity) — e.g. "Memory, 256GB, LiteOn SSD, M.2 2280" or a drive
        # description on a DIMM-categorized card. Never pick a side. gpu also reads
        # GB but is defended inside the gpu module by the GPU-context-token guard,
        # not here — a gpu-hinted card with body-only dram/hdd tokens routes gpu and
        # then extracts nothing.
        return None

    if hint:
        if lead and _FAMILY[lead] != _FAMILY[hint]:
            # TRIO's own label contradicts the card category ("MEMORY," lead on a
            # gpu-hinted card) — never extract from a contradicted description.
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
    elif effective == "power_supplies":
        specs = extract_psu(text)
    elif effective == "displays":
        specs = extract_display(text)
    elif effective == "tape_drives":
        specs = extract_tape(text)
    elif effective == "gpu":
        specs = extract_gpu(text)
    elif effective == "motherboards":
        specs = extract_board(text)
    else:
        specs = {}  # cpu — commodity hint only (the wattage-vs-TDP structural guard)
    return DescResult(commodity=effective, specs=specs, confidence=DESC_CONFIDENCE)


__all__ = ["DescResult", "extract_desc"]
