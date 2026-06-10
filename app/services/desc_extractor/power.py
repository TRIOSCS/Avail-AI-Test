"""Deterministic power-supply description→spec extraction (TRIO inventory grammar).

What: reads wattage / supply class out of compact human PSU descriptions like
      ``PSU, 1460W 240V/200V AC Hot Swap for EN 62368-1`` or ``PWR SPLY,180W,BRZ,
      D8,ACBL`` — NO network, NO LLM. Every emitted value is a seeded
      power_supplies enum member / in-range numeric per app/data/
      commodity_seeds.json; record_spec independently re-validates enum members
      and skips unseeded keys, but numeric ranges are enforced ONLY here — the
      drift guard in tests/test_desc_extractor_routing.py pins both against the
      seeds.
Called by: app/services/desc_extractor/__init__.py (extract_desc routing).
Depends on: _common (SpecDict alias only) — pure functions.

CONSERVATIVE by design (a wrong facet value is worse than a missing one):
- wattage requires an explicit W/WATT(S) unit token with word boundaries, so
  glued "250WENT17", VA ratings ("240VA"), VAC/VDC voltages and bare numbers
  inside MPNs ("PA-1300-42") never match. Candidates are filtered to the seeded
  30-5000 range; two DIFFERENT surviving values ⇒ omit. The wattage key only
  exists on this route at all — CPU "135W" TDP descriptions are structurally
  unreachable because extract_desc never dispatches cpu here.
- psu_class maps explicit tokens to seeded members (hot-swap/redundant/N+1/
  common-slot ⇒ Server/Redundant, AC adapter/charger ⇒ AC-DC (External/Adapter),
  ATX, DC-DC). Multiple DISTINCT members ⇒ omit. A generic "Power Supply"
  description deliberately emits NO psu_class — the commodity already says PSU,
  so a generic member would carry zero filter information.
"""

import re

from app.services.desc_extractor._common import SpecDict

# Canonical psu_class enum strings — MUST match the power_supplies entry in
# app/data/commodity_seeds.json (drift-guarded).
SERVER_REDUNDANT = "Server/Redundant"
AC_DC_ADAPTER = "AC-DC (External/Adapter)"
ATX_PC = "ATX/PC"
DC_DC = "DC-DC Converter"

_WATTS = re.compile(r"\b(\d{2,4})\s?(?:W|WATTS?)\b")

# Seeded power_supplies.wattage numeric_range — the only range gate (record_spec
# performs no numeric_range check); pinned against the seeds by the drift guard.
_WATT_MIN, _WATT_MAX = 30, 5000

_CLASS_PATTERNS = (
    (
        SERVER_REDUNDANT,
        re.compile(r"HOT[- ]?SWAP|HOT[- ]?PLUG|REDUNDAN(?:T|CY)|\bN\+1\b|COMMON SLOT|SERVER POWER SUPPLY|SERVER PSU"),
    ),
    (AC_DC_ADAPTER, re.compile(r"\bAC ADAPTERS?\b|POWER ADAPTER|\bCHARGER\b")),
    (ATX_PC, re.compile(r"\bATX\b")),
    (DC_DC, re.compile(r"\bDC[-/ ]DC\b")),
)


def _wattage(text: str) -> int | None:
    """Distinct surviving wattage candidate in the seeded range, or None."""
    values = {int(m.group(1)) for m in _WATTS.finditer(text)}
    values = {v for v in values if _WATT_MIN <= v <= _WATT_MAX}
    return values.pop() if len(values) == 1 else None


def _psu_class(text: str) -> str | None:
    """Seeded psu_class member, or None (no token / conflicting members)."""
    members = {member for member, pattern in _CLASS_PATTERNS if pattern.search(text)}
    return members.pop() if len(members) == 1 else None


def extract_psu(text: str) -> SpecDict:
    """Extract power_supplies specs from an upper-cased, whitespace-collapsed
    description."""
    specs: SpecDict = {}
    wattage = _wattage(text)
    if wattage is not None:
        specs["wattage"] = wattage
    psu_class = _psu_class(text)
    if psu_class is not None:
        specs["psu_class"] = psu_class
    return specs
