"""Deterministic description→spec field extraction (storage, memory, PSU, displays,
tape, GPU, motherboards, CPU).

What: reads parametric specs straight out of TRIO's compact *human description*
      strings (part master + inventory sheets — e.g. ``HD, 450GB, 15KRPM, 3.5",
      Fibre Channel``, ``Mem, 16GB DDR4 2Rx4 PC4-2400T RDIMM``, ``PSU, 1460W
      240V/200V AC Hot Swap``, ``SPS-CPU BDW E5-2650L V4 14C 1_7GHZ 65W``) with
      NO network and NO LLM — zero hallucination.
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
Depends on: desc_extractor.{_common,storage,memory,power,display,tape,gpu,board,
      cpu} (pure functions).

Coverage is deliberately CONSERVATIVE: a field is emitted only when the description
grammar expresses it unambiguously; conflicting signals omit the key, a foreign
commodity label (``Other,``/``Tray,``/``Card,``/``Library,``…) suppresses extraction
entirely. Packaging-word and brand leads (``ASSY,``/``FRU,``/``MSI,``/``SPS-…``)
are NEUTRAL — they fall through to body-token + hint arbitration instead. Under a
commodity hint, a description whose lead or strong body tokens ALL belong to a
different family than the hint returns None (contradiction guard — a motherboard
FRU in the SFDC CPU bucket must not take cpu facets).
"""

import re

from app.services.desc_extractor._common import DESC_CONFIDENCE, SPEC_COMMODITIES, DescResult, SpecDict
from app.services.desc_extractor.board import extract_board
from app.services.desc_extractor.cpu import extract_cpu, is_cpu_pollution
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
    "cpu": "cpu",
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
    # Packaging-suffixed display labels are mapped EXPLICITLY — _is_neutral_lead
    # requires every label word to be neutral, so "LCD ASSY,"/"PNL KIT," would
    # otherwise die foreign ("LCD"/"PNL" are commodity words, not packaging).
    "LCD ASSY": "displays",
    "PNL KIT": "displays",
    "PANEL KIT": "displays",
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
    "PROC": "cpu",
}
_FOREIGN = "__foreign__"

# HP board-IC grammar lead: "IC,uP,CFL,i5-8400,2.8GHz,65W,9MB" / "IC, uP,i5-7500T,…"
# / space form "IC uP KBL i7-7600U 2.8GHz 15W". The gate requires the FULL "IC,uP"
# prefix — a bare "IC," lead is the SFDC general-components bin (logic ICs, analog…)
# and stays FOREIGN via the unhandled-label rule below.
_IC_UP_LEAD = re.compile(r"^IC[, ] ?UP[, ]")

# Leads that are packaging words or brand names, NOT commodity labels — treated as
# *no lead* (fall through to body-token + hint arbitration) instead of FOREIGN.
# Any "SPS…"-prefixed lead is neutral too ("SPS-PCA, NVIDIA Tesla V100 32GB Module"
# must not die foreign). A multi-word label is neutral only when EVERY word is
# neutral ("SUPERMICRO FRU," is brand+packaging); a label mixing a foreign word
# with packaging ("CBL ASSY,"/"DRIVE TRAY KIT,") stays FOREIGN — an accessory row
# must never take the facets of the part it fits ("Cable Assy, for LCD 15.6" FHD
# panel" describes the panel the cable serves, not the cable). Phase-1 guards
# still hold behind a neutral lead: a brand-led body mixing HDD+DIMM tokens
# hard-conflicts to None as before.
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
        "SUPERMICRO",
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
# GTX1050 4GB i7-7700HQ WIN" must stay a motherboard. The same principle keeps CPU
# family/model words (XEON, i7-7700HQ…) OUT of this table: boards and servers name
# their CPUs constantly, so those are SUBORDINATE tokens (_CPU_WEAK below) that
# route only when nothing else claims the row.
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

# SUBORDINATE cpu routing tokens — CPU family words and model strings appear inside
# motherboard/server/GPU rows all the time ("SPS-MB DSC GTX1050 4GB i7-7700HQ WIN"),
# so they must never out-vote another commodity's token: they route cpu ONLY when no
# lead matched and NO other body token fired ("Xeon GOLD 6134 3.2G 8C 130W",
# "SPS-PROC HSW E5-1630v3 4C 3.7GHz 140W", "I7-7600U PROCESSOR" already routes via
# the strong \bPROCESSOR\b token). The Scalable shape requires a 3-9xxx model number
# so 80-PLUS "PLATINUM 1100W" PSU grades never match, and it never claims a Pentium
# Gold / Athlon Gold-Silver consumer part — those route via their own brand words
# (cpu.py suppresses the Scalable interpretation for them entirely).
_CPU_WEAK = re.compile(
    r"\bXEON\b|\bEPYC\b|\bRYZEN\b|\bPROC\b|\bPENTIUM\b|\bATHLON\b"
    r"|\bE[357]-?\d{4}"
    r"|\bI[3579]-\d{4,5}"
    r"|(?<!PENTIUM )(?<!ATHLON )\b(?:GOLD|SILVER|PLATINUM|BRONZE)[ -]?[3-9]\d{3}[A-VX-Z]?\b"
)

# Families whose STRONG body tokens are routine vocabulary inside another family's
# descriptions — under that hint they refine, never contradict. cpu descriptions
# state their supported memory constantly ("Intel i5-9400 2.9GHz/6C/9M 65W DDR4
# 2666" — a real CPU-bucket row), the exact inverse of boards naming their CPUs
# (_CPU_WEAK subordination above). Corpus-validated: dram-under-cpu is the only
# pair whose exemption restores real extractions without re-admitting a wrong
# facet (the storage×dram HARD conflict below still applies unconditionally).
_SUBORDINATE_UNDER: dict[str, frozenset[str]] = {"cpu": frozenset({"dram"})}


def _is_neutral_lead(label: str) -> bool:
    if label.startswith("SPS"):
        return True
    # EVERY word must be neutral — a single-word check would miss "SUPERMICRO FRU",
    # while any looser rule (e.g. last-word-only) re-opens the foreign-lead guard
    # for accessory labels like "CBL ASSY,"/"DRIVE TRAY KIT,".
    return all(word in _NEUTRAL_LEADS for word in label.split())


def _lead_commodity(text: str) -> str | None:
    """Mapped commodity for a `<Label>,` lead, _FOREIGN for an unhandled label, None for
    a NEUTRAL lead (packaging word / brand — body tokens + hint arbitrate), else the
    _FIRST_TOKEN_MAP commodity for an unambiguous comma-less first token (e.g. ``SSD
    480GB 7mmH …``), else None."""
    if _IC_UP_LEAD.match(text):
        return "cpu"  # full "IC,uP" prefix only — bare "IC," falls through to FOREIGN
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
    tape_drives/gpu/motherboards/cpu) returns None. The returned ``commodity`` is a
    HINT for callers — nothing here ever writes a category.
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
    if not found and lead is None and _CPU_WEAK.search(text):
        # Subordinate cpu tokens (family words / model strings) only claim a row
        # nothing else claimed — see the _CPU_WEAK comment.
        found.add("cpu")
    if lead:
        found.add(lead)

    found_families = {_FAMILY[c] for c in found}
    families = found_families | ({_FAMILY[hint]} if hint else set())
    if "storage" in families and "dram" in families:
        # The HARD cross-family conflict is storage×dram only (both read bare-GB
        # capacity) — e.g. "Memory, 256GB, LiteOn SSD, M.2 2280" or a drive
        # description on a DIMM-categorized card. Never pick a side. gpu also reads
        # GB but is defended inside the gpu module, not here: memory_gb requires a
        # GPU-context token, and a DRAM-module body token without a gpu_family hit
        # disqualifies it (a bare NVIDIA token on a DIMM row is not GB context).
        return None

    if hint:
        if lead and _FAMILY[lead] != _FAMILY[hint]:
            # TRIO's own label contradicts the card category ("MEMORY," lead on a
            # gpu-hinted card) — never extract from a contradicted description.
            return None
        contradicting = found_families - {_FAMILY[hint]} - _SUBORDINATE_UNDER.get(_FAMILY[hint], frozenset())
        if found_families and _FAMILY[hint] not in found_families and contradicting:
            # Every strong signal the text carries (lead and/or body tokens) belongs
            # to a DIFFERENT family than the hinted card category — e.g. the
            # CPU-bucket motherboard FRU "SPS-MB UMA I5-8265U 8GB W/HEATSINK WIN"
            # under a cpu hint. Same contradiction class as the lead guard above:
            # never write the hint's facets onto a part whose own tokens say it is
            # something else (a wrong facet value is worse than a missing one).
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

    # Every extractor returns the canonical _common.SpecDict (dict is invariant in
    # its value type — a narrower per-module union would fail this dispatch).
    specs: SpecDict
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
    else:  # cpu — the only remaining SPEC_COMMODITIES member
        if is_cpu_pollution(text):
            # Step-0 pollution guard (docs/CPU_DECODE_FEASIBILITY.md): the SFDC CPU
            # bucket carries MLCCs/connectors/tape parts — a denied row extracts
            # NOTHING, not even the commodity hint.
            return None
        specs = extract_cpu(text)
    return DescResult(commodity=effective, specs=specs, confidence=DESC_CONFIDENCE)


__all__ = ["DescResult", "extract_desc"]
