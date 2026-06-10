"""Deterministic DRAM description→spec extraction (TRIO inventory grammar).

What: reads capacity / DDR generation / speed / ECC / module form / rank out of
      compact human memory descriptions like ``Mem, 16GB DDR4 2Rx4 PC4-2400T RDIMM``
      — NO network, NO LLM. Every emitted value is a seeded dram enum member /
      in-range numeric per app/data/commodity_seeds.json; record_spec independently
      re-validates enum members and skips unseeded keys, but numeric ranges are
      enforced ONLY here (record_spec performs no numeric_range check) — the drift
      guard in tests/test_desc_extractor_routing.py pins both against the seeds.
Called by: app/services/desc_extractor/__init__.py (extract_desc routing).
Depends on: _common (SpecDict alias + unique_or_none helper) — pure functions.

CONSERVATIVE by design (a wrong facet value is worse than a missing one):
- capacity_gb requires an explicit GB/G token and the seeded 1-512 range (MB-era
  modules and bandwidth-per-second tokens never match). GigaBIT component-density
  tokens ("2Gb, 128*16" — lowercase b) are neutralized to "2GBIT" by extract_desc's
  pre-uppercase _BIT_UNITS rewrite, so bits can never be recorded as bytes.
- ddr_type from explicit DDR/DDR2/DDR3/DDR3L/DDR4/DDR5 tokens, or the PC3-/PC3L-/
  PC4-<digits> prefixes (PC3→DDR3, PC3L→DDR3L, PC4→DDR4). Mixed generations ⇒ omit.
- speed_mhz only from an explicit MHz token (seed range 800-8400, so "DDR-333MHz"
  legacy speeds are dropped) or the closed deterministic PC4 speed-grade map
  (PC4-2400T→2400, PC4-2666V→2666, PC4-3200AA→3200). Bare "DDR4 2400" and
  bandwidth codes (PC3-10600, PC4-19200) are deliberately NOT decoded.
- ecc: True from a non-negated ECC token or an RDIMM/LRDIMM form (registered /
  load-reduced modules are ECC by JEDEC definition — same rule the MPN decoders
  use); False from "Non-ECC". Contradictory signals ⇒ omit.
- form_factor: RDIMM/UDIMM/LRDIMM/SO-DIMM tokens verbatim; a bare "DIMM" token maps
  to the seeded generic "DIMM" only when no specific form is present.
- rank: verbatim 1Rx4/1Rx8/2Rx4/2Rx8/4Rx4/8Rx4 tokens only — the seeded dram rank
  enum mirrors _RANK_VALID exactly, so record_spec persists it like the other keys.
"""

import re

from app.services.desc_extractor._common import SpecDict, unique_or_none

# Canonical dram enum strings — MUST match the dram entry in app/data/commodity_seeds.json.
RDIMM, LRDIMM, UDIMM, SODIMM, DIMM = "RDIMM", "LRDIMM", "UDIMM", "SO-DIMM", "DIMM"

_CAPACITY = re.compile(r"\b(\d{1,3})\s?G(?:B)?\b")
_MHZ = re.compile(r"\b(\d{1,2},?\d{3})\s?MHZ\b|\b(\d{3})\s?MHZ\b")
_PC4_SPEED_GRADE = re.compile(r"\bPC4[- ]?(2400T|2666V|3200AA)\b")
_PC4_SPEED = {"2400T": 2400, "2666V": 2666, "3200AA": 3200}
_DDR_TOKEN = re.compile(r"\bDDR(3L|[2345])\b")
_DDR_BARE = re.compile(r"\bDDR\b")
_PC_PREFIX = re.compile(r"\bPC(3L|[34])(?=[- ]?\d)")
_PC_GEN = {"3": "DDR3", "3L": "DDR3L", "4": "DDR4"}
_NON_ECC = re.compile(r"\bNON[- ]?ECC\b")
_ECC = re.compile(r"\bECC\b")
_FORM_SPECIFIC = (
    (RDIMM, re.compile(r"\bRDIMM\b")),
    (UDIMM, re.compile(r"\bUDIMM\b")),
    (LRDIMM, re.compile(r"\bLRDIMM\b")),
    (SODIMM, re.compile(r"\bSO-?DIMM\b")),
)
_DIMM_GENERIC = re.compile(r"\bDIMM\b")
_RANK = re.compile(r"\b([1248])RX([48])\b")
_RANK_VALID = {"1Rx4", "1Rx8", "2Rx4", "2Rx8", "4Rx4", "8Rx4"}

# Seeded dram numeric_ranges (speed_mhz, capacity_gb) — record_spec does NOT validate
# numeric ranges, so these constants are the only range gate; the drift guard in
# tests/test_desc_extractor_routing.py asserts them against commodity_seeds.json.
_SPEED_MIN, _SPEED_MAX = 800, 8400
_CAP_MIN, _CAP_MAX = 1, 512


def _capacity_gb(text: str) -> int | None:
    values: set[int] = set()
    for m in _CAPACITY.finditer(text):
        if text[m.end() : m.end() + 2] == "/S":
            continue
        value = int(m.group(1))
        if _CAP_MIN <= value <= _CAP_MAX:
            values.add(value)
    return unique_or_none(values)


def _ddr_type(text: str) -> str | None:
    generations = {f"DDR{m.group(1)}" for m in _DDR_TOKEN.finditer(text)}
    generations |= {_PC_GEN[m.group(1)] for m in _PC_PREFIX.finditer(text)}
    if not generations and _DDR_BARE.search(text):
        generations.add("DDR")  # generation-less legacy "DDR-333MHz" grammar
    return unique_or_none(generations)


def _speed_mhz(text: str) -> int | None:
    speeds: set[int] = set()
    for m in _MHZ.finditer(text):
        value = int((m.group(1) or m.group(2)).replace(",", ""))
        if _SPEED_MIN <= value <= _SPEED_MAX:
            speeds.add(value)
    speeds |= {_PC4_SPEED[m.group(1)] for m in _PC4_SPEED_GRADE.finditer(text)}
    return unique_or_none(speeds)


def _form_factor(text: str) -> str | None:
    forms = {name for name, pattern in _FORM_SPECIFIC if pattern.search(text)}
    if len(forms) > 1:
        return None  # conflicting specific module types
    if forms:
        return forms.pop()
    return DIMM if _DIMM_GENERIC.search(text) else None


def _ecc(text: str, form_factor: str | None) -> bool | None:
    signals: set[bool] = set()
    if _NON_ECC.search(text):
        signals.add(False)
    if _ECC.search(_NON_ECC.sub(" ", text)):  # ECC tokens outside any Non-ECC phrase
        signals.add(True)
    if form_factor in (RDIMM, LRDIMM):
        signals.add(True)  # registered / load-reduced modules are always ECC
    return unique_or_none(signals)


def _rank(text: str) -> str | None:
    ranks = {f"{m.group(1)}Rx{m.group(2)}" for m in _RANK.finditer(text)}
    ranks &= _RANK_VALID
    return unique_or_none(ranks)


def extract_memory(text: str) -> SpecDict:
    """Extract dram specs from an upper-cased, whitespace-collapsed description."""
    specs: SpecDict = {}
    capacity = _capacity_gb(text)
    if capacity is not None:
        specs["capacity_gb"] = capacity
    ddr_type = _ddr_type(text)
    if ddr_type is not None:
        specs["ddr_type"] = ddr_type
    speed = _speed_mhz(text)
    if speed is not None:
        specs["speed_mhz"] = speed
    form_factor = _form_factor(text)
    if form_factor is not None:
        specs["form_factor"] = form_factor
    ecc = _ecc(text, form_factor)
    if ecc is not None:
        specs["ecc"] = ecc
    rank = _rank(text)
    if rank is not None:
        specs["rank"] = rank
    return specs
