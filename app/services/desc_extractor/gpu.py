"""Deterministic GPU description→spec extraction (TRIO inventory grammar).

What: reads GPU family / memory size out of compact human graphics-card
      descriptions like ``SPS-PCA, NVIDIA Tesla V100 32GB Module`` or
      ``MSI, RTX3080, 10G/D6X/3DP/H`` — NO network, NO LLM. Every emitted value
      is a seeded gpu enum member / in-range numeric per app/data/
      commodity_seeds.json; record_spec independently re-validates enum members
      and skips unseeded keys, but numeric ranges are enforced ONLY here — the
      drift guard in tests/test_desc_extractor_routing.py pins both against the
      seeds.
Called by: app/services/desc_extractor/__init__.py (extract_desc routing).
Depends on: _common (SpecDict alias + unique_or_none helper) — pure functions.

CONSERVATIVE by design (a wrong facet value is worse than a missing one):
- gpu_family collects all marketing/chip tokens (Quadro, GeForce, GTX⇒GeForce,
  RTX, Tesla + datacenter chip list incl. T4, Radeon (Pro), A-/H-series) and
  emits a unique member or omits. Subsumption: an explicit GEFORCE token absorbs
  its own GTX/RTX sub-brand tokens ("GeForce RTX/GTX …" is one family, not a
  conflict). A-/H-series chip tokens reject hyphen-glued silicon steppings
  ("N17M-Q3-A2", "GK104-400-A2") via a lookbehind — those mark a chip revision,
  never an Ampere/Hopper card. Architecture names (PASCAL spans Tesla AND
  Quadro) and bare models ("2080TI") are deliberately unmapped.
- memory_gb REQUIRES a GPU-context token in the text (any family hit, or
  NVIDIA/NVD/GDDRx/HBMx) — this is the cross-commodity GB guard: "Emulex, 10GB,
  SFP+Mezza Card" and RAID flash-module rows inside the GC bucket emit nothing.
  A bare context token is NOT enough when a DRAM-module token (DIMM/DDRx) is
  present — "SODIMM 16GB DDR4 for NVIDIA DGX Station" is a DIMM whose GB
  belongs to the module (an explicit family hit still unlocks memory_gb).
  Speed/bandwidth defenses: "100GbE"/"25GbE" never match (no boundary after
  GB); "608GB/S" bandwidth tokens are skipped explicitly via the trailing-"/S"
  check (mirrors storage.py/memory.py — also kills decimal bandwidths like
  "14.4GB/S" whose fractional digit would otherwise capture); and a NIC clause
  (ConnectX/(Q)SFP/Ethernet/dual-port) disqualifies memory_gb outright —
  NVIDIA-branded Mellanox adapters carry spaced "25Gb" link speeds that
  uppercase into the GB shape. The glued "…@6G/D6/DP/H" OEM grammar is decoded
  via its /D5|/D6 (GDDR) lookahead. Candidates filtered to the seeded 1-96
  range; unique-or-omit.
- NOT extracted: gpu_model free-text (not seeded), memory_type (phase 3),
  interface PCIe gen, tdp_watts.
"""

import re

from app.services.desc_extractor._common import SpecDict, unique_or_none

# Canonical gpu_family enum strings — MUST match the gpu entry in
# app/data/commodity_seeds.json (drift-guarded).
GEFORCE, QUADRO, RTX = "GeForce", "Quadro", "RTX"
RADEON, RADEON_PRO, TESLA = "Radeon", "Radeon Pro", "Tesla"
A_SERIES, H_SERIES = "A-series", "H-series"

_FAMILY_PATTERNS = (
    (QUADRO, re.compile(r"\bQUADRO\b")),
    (TESLA, re.compile(r"\bTESLA\b|\b(?:V100|P100|P40|P4|T4|K20X?|K40|K80|M40|M60)\b")),
    (RADEON_PRO, re.compile(r"RADEON PRO|\bFIREPRO\b")),
    (RADEON, re.compile(r"\bRADEON\b(?! PRO)|\bRX\d{3,4}\b")),
    # (?<![\w-]) rejects hyphen-glued silicon steppings ("N17M-Q3-A2",
    # "GK104-400-A2") that a plain \b would accept — the corpus carries real
    # "-A2"-stepped GeForce chip markings that are NOT Ampere cards.
    (A_SERIES, re.compile(r"(?<![\w-])A(?:100|40|30|16|10|2)\b")),
    (H_SERIES, re.compile(r"(?<![\w-])H(?:100|200|800)\b")),
)
_GEFORCE = re.compile(r"\bGEFORCE\b")
# GTX/RTX hug their model digits in the corpus ("RTX3080", "GTX1660Super") — the
# lookahead accepts a glued digit OR a plain word boundary.
_GTX = re.compile(r"\bGTX(?=\d|\b)")
_RTX = re.compile(r"\bRTX(?=\d|\b)")

_CONTEXT = re.compile(r"\bNVIDIA\b|\bNVD\b|\bGDDR\dX?\b|\bHBM\d?\b")
_MEM_GB = re.compile(r"\b(\d{1,3})\s?GB\b")
_MEM_GLUED = re.compile(r"\b(\d{1,2})G(?=/D[56])")
# DRAM-module body tokens (mirrors the dram routing vocabulary in __init__.py):
# a bare NVIDIA/GDDR context token must not unlock memory_gb on a DIMM row —
# "SODIMM 16GB DDR4 for NVIDIA DGX Station" — the GB belongs to the module.
# GDDR never matches (no boundary inside "GDDR4"). An explicit family hit wins.
_DRAM_BODY = re.compile(r"\b(?:R|U|LR)DIMM\b|\bSO-?DIMM\b|\bDIMM\b|\bDDR(?:3L|[2345])?\b")
# NIC clause: Mellanox adapters inside the GC bucket are NVIDIA-branded, and their
# spaced "25Gb"/"56Gb" link speeds uppercase into the \bGB\b shape — any GB hit on
# a row carrying NIC vocabulary is a link speed, never a VRAM size.
_NIC_CLAUSE = re.compile(r"\bCONNECTX\b|\bQ?SFP\d*\b|\bETHERNET\b|\b(?:DUAL|SINGLE|QUAD)[- ]PORT\b")

# Seeded gpu.memory_gb numeric_range — the only range gate (record_spec performs
# no numeric_range check); pinned against the seeds by the drift guard.
_MEM_MIN, _MEM_MAX = 1, 96


def _family_members(text: str) -> set[str]:
    """All family members whose tokens appear (post-subsumption)."""
    members = {member for member, pattern in _FAMILY_PATTERNS if pattern.search(text)}
    if _GEFORCE.search(text):
        # Explicit GEFORCE absorbs its GTX/RTX sub-brand tokens — "NVIDIA GeForce
        # GTX"/"GeForce RTX 3080" is one family, not a GeForce-vs-RTX conflict.
        members.add(GEFORCE)
    else:
        if _GTX.search(text):
            members.add(GEFORCE)  # GTX is definitionally GeForce
        if _RTX.search(text):
            members.add(RTX)
    return members


def _memory_gb(text: str) -> int | None:
    """Distinct surviving memory candidate in the seeded range, or None."""
    values: set[int] = set()
    for m in _MEM_GB.finditer(text):
        if text[m.end() : m.end() + 2] == "/S":
            continue  # "608GB/S" is memory bandwidth, not a size (mirrors storage.py)
        values.add(int(m.group(1)))
    values |= {int(m.group(1)) for m in _MEM_GLUED.finditer(text)}
    values = {v for v in values if _MEM_MIN <= v <= _MEM_MAX}
    return unique_or_none(values)


def extract_gpu(text: str) -> SpecDict:
    """Extract gpu specs from an upper-cased, whitespace-collapsed description."""
    specs: SpecDict = {}
    members = _family_members(text)
    if len(members) == 1:
        specs["gpu_family"] = next(iter(members))
    # A family token still counts as GB context when the family KEY was omitted
    # for conflict — the description is provably about a GPU. A bare context
    # token does NOT count on a DRAM-module row (the GB is the DIMM's), and a
    # NIC clause disqualifies memory_gb outright (the GB is a link speed).
    gb_context = bool(members) or bool(_CONTEXT.search(text) and not _DRAM_BODY.search(text))
    if gb_context and not _NIC_CLAUSE.search(text):
        memory = _memory_gb(text)
        if memory is not None:
            specs["memory_gb"] = memory
    return specs
