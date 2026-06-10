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
Depends on: _common (constants only) — pure functions.

CONSERVATIVE by design (a wrong facet value is worse than a missing one):
- gpu_family collects all marketing/chip tokens (Quadro, GeForce, GTX⇒GeForce,
  RTX, Tesla + datacenter chip list, Radeon (Pro), A-/H-series) and emits a
  unique member or omits. Subsumption: an explicit GEFORCE token absorbs its own
  GTX/RTX sub-brand tokens ("GeForce RTX/GTX …" is one family, not a conflict).
  Architecture names (PASCAL spans Tesla AND Quadro) and bare models ("2080TI")
  are deliberately unmapped.
- memory_gb REQUIRES a GPU-context token in the text (any family hit, or
  NVIDIA/NVD/GDDRx/HBMx) — this is the cross-commodity GB guard: "Emulex, 10GB,
  SFP+Mezza Card" and RAID flash-module rows inside the GC bucket emit nothing.
  "100GbE"/"25GbE" NIC speeds never match (no boundary after GB); the glued
  "…@6G/D6/DP/H" OEM grammar is decoded via its /D5|/D6 (GDDR) lookahead.
  Candidates filtered to the seeded 1-96 range; unique-or-omit.
- NOT extracted: gpu_model free-text (not seeded), memory_type (phase 3),
  interface PCIe gen, tdp_watts.
"""

import re

# Canonical gpu_family enum strings — MUST match the gpu entry in
# app/data/commodity_seeds.json (drift-guarded).
GEFORCE, QUADRO, RTX = "GeForce", "Quadro", "RTX"
RADEON, RADEON_PRO, TESLA = "Radeon", "Radeon Pro", "Tesla"
A_SERIES, H_SERIES = "A-series", "H-series"

_FAMILY_PATTERNS = (
    (QUADRO, re.compile(r"\bQUADRO\b")),
    (TESLA, re.compile(r"\bTESLA\b|\b(?:V100|P100|P40|P4|K20X?|K40|K80|M40|M60)\b")),
    (RADEON_PRO, re.compile(r"RADEON PRO|\bFIREPRO\b")),
    (RADEON, re.compile(r"\bRADEON\b(?! PRO)|\bRX\d{3,4}\b")),
    (A_SERIES, re.compile(r"\b(?:A100|A40|A30|A16|A10|A2)\b")),
    (H_SERIES, re.compile(r"\b(?:H100|H200|H800)\b")),
)
_GEFORCE = re.compile(r"\bGEFORCE\b")
# GTX/RTX hug their model digits in the corpus ("RTX3080", "GTX1660Super") — the
# lookahead accepts a glued digit OR a plain word boundary.
_GTX = re.compile(r"\bGTX(?=\d|\b)")
_RTX = re.compile(r"\bRTX(?=\d|\b)")

_CONTEXT = re.compile(r"\bNVIDIA\b|\bNVD\b|\bGDDR\dX?\b|\bHBM\d?\b")
_MEM_GB = re.compile(r"\b(\d{1,3})\s?GB\b")
_MEM_GLUED = re.compile(r"\b(\d{1,2})G(?=/D[56])")

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
    values = {int(m.group(1)) for m in _MEM_GB.finditer(text)}
    values |= {int(m.group(1)) for m in _MEM_GLUED.finditer(text)}
    values = {v for v in values if _MEM_MIN <= v <= _MEM_MAX}
    return values.pop() if len(values) == 1 else None


def extract_gpu(text: str) -> dict[str, str | int]:
    """Extract gpu specs from an upper-cased, whitespace-collapsed description."""
    specs: dict[str, str | int] = {}
    members = _family_members(text)
    if len(members) == 1:
        specs["gpu_family"] = next(iter(members))
    if members or _CONTEXT.search(text):
        # A family token still counts as GB context when the family KEY was
        # omitted for conflict — the description is provably about a GPU.
        memory = _memory_gb(text)
        if memory is not None:
            specs["memory_gb"] = memory
    return specs
