"""Deterministic resistor description→spec extraction (distributor thick/thin-film
grammar).

What: reads resistance / power_rating / tolerance / package / mounting out of
      distributor (Mouser/DigiKey) resistor descriptions like
      ``Thick Film Resistors - SMD 0402 Zero ohms 5% Tol AEC-Q200`` or
      ``Thick Film Resistors - SMD 10K ohms 1% 0603`` — NO network, NO LLM.
      Distributor descriptions are dense and consistent, so the passive commodities
      (capacitors/resistors/…) get the same description-grammar treatment that
      storage/memory already have. Every emitted value maps to a seeded resistors
      entry in app/data/commodity_seeds.json: resistance is the seeded canonical unit
      (ohms) and power_rating the seeded canonical unit (W), normalized HERE because
      the writer calls record_spec with no ``unit`` (it cannot run unit_normalizer for
      us — a numeric value with no unit is taken as already-canonical); record_spec
      independently re-validates enum members and skips unseeded keys/values, but
      performs NO numeric_range check (resistors carries no numeric_range, so the
      numerics are emitted as parsed).
Called by: app/services/desc_extractor/__init__.py (extract_desc routing).
Depends on: _common (SpecDict alias + unique_or_none helper) — pure functions.

CONSERVATIVE by design (a wrong facet value is worse than a missing one):
- resistance requires an explicit ohm signal: an ``OHM(S)``/``Ω`` token (possibly with
  a K/M multiplier — ``10K OHMS`` = 10000, ``2.2M OHMS`` = 2.2e6), the literal
  ``ZERO OHMS``/``0 OHMS`` (= 0), or a BS-1852/RKM code whose embedded letter is both
  the decimal point and the multiplier (``4M7`` = 4.7e6, ``4K7`` = 4700, ``4R7`` = 4.7,
  ``100R`` = 100, ``10K`` = 10000). A bare number with no ohm context is NEVER read as a
  resistance — and a ferrite-bead/inductor impedance is excluded at the ROUTING layer
  (a bare ``OHM`` token does not route to resistors; only RESISTOR(S)/THICK/THIN FILM
  do — see __init__._BODY_TOKENS), so this module only ever sees true resistor rows.
  A capacitor/drive description carries no ohm/RKM value, so nothing is emitted.
  Conflicting resistances ⇒ omit.
- power_rating is a plain numeric in canonical watts: distributor fractions
  (``1/16 W`` → 0.0625, ``1/10 W`` → 0.1, ``1/4W`` → 0.25), decimals (``0.5W`` → 0.5)
  and integers (``1 W`` → 1). The unit token (W/WATT/WATTS) is required, so a bare
  number is never a wattage. Conflicting wattages ⇒ omit.
- tolerance / package / mounting are seeded enums. The resistors tolerance enum is BARE
  (``0.1%``/``1%``/``5%`` — NOT the ±-prefixed capacitor form); the distributor forms
  ``5%`` / ``5 %`` / ``±5%`` / ``+/-5%`` / ``5% Tol`` all collapse to the seeded ``5%``.
  package is the EIA imperial case code (resistors seed has 0402/0603/0805/1206 +
  through-hole — note NO 1210, unlike capacitors). mounting is SMD/through-hole/
  press-fit from SMD/SMT and through-hole/THT/press-fit tokens.
- Multiple conflicting signals for any key ⇒ that key is omitted, the rest still extract.
"""

import re

from app.services.desc_extractor._common import SpecDict, unique_or_none

# Seeded canonical units (app/data/commodity_seeds.json resistors.{resistance,power_rating}).
_CANONICAL_OHM_UNIT = "ohms"
_CANONICAL_POWER_UNIT = "W"

# RKM / ohm multipliers → ohms. "R"=×1, "K"=×1e3, "M"=×1e6 (the BS-1852/RKM letter is
# also the decimal point: "4K7" = 4700, "4M7" = 4.7e6).
_OHM_MULT = {"R": 1, "K": 1_000, "M": 1_000_000}

# Explicit "<n><mult?> OHM(S)/Ω" form: an optional K/M multiplier hugging the number,
# then a (possibly spaced) OHM word or Ω sign. "10K OHMS" / "4.7K OHM" / "2.2M OHMS" /
# "100 OHMS" / "100R" handled by _RKM below; the bare-Ω sign and the spelled "OHM(S)"
# both anchor here.
_OHM_EXPLICIT = re.compile(r"\b(\d+(?:\.\d+)?)\s?([KM])?\s?(?:OHMS?|Ω)\b")
# "ZERO OHMS"/"ZERO OHM" — the distributor spelling of a 0Ω jumper.
_ZERO_OHM = re.compile(r"\bZERO\s?(?:OHMS?|Ω)\b")
# BS-1852/RKM code: digits, an R/K/M letter as decimal point, optional trailing digits
# ("4M7", "4K7", "4R7", "100R", "10K"). The letter must be flanked so a stray "K"/"M"
# elsewhere never matches: leading digits required, and a trailing word char (other than
# the optional fractional digits) is rejected so "10KRPM"/"4MHZ"/"100KB" never read as a
# resistance. A pure-integer with a multiplier ("10K", "100R") is the multiplier-only
# form. The leading (?<!\.) rejects the fractional tail of a decimal kilohm ("4.7K OHMS"
# → the "7K" must NOT read as 7000; that value is captured whole by _OHM_EXPLICIT).
_RKM = re.compile(r"(?<!\.)\b(\d+)([RKM])(\d*)\b(?![A-Z])")

# Seeded resistors power_rating is numeric watts. Distributor wattages are commonly
# fractions ("1/16 W", "1/10 W", "1/4W"), decimals ("0.5W") or integers ("1 W"). The
# W/WATT(S) unit token is required (a bare number is never a wattage).
_POWER_FRACTION = re.compile(r"\b(\d+)\s?/\s?(\d+)\s?(?:W\b|WATTS?\b)")
_POWER_DECIMAL = re.compile(r"\b(\d+(?:\.\d+)?)\s?(?:W\b|WATTS?\b)")

# Seeded enum vocabularies (resistors.{tolerance,package,mounting}). tolerance is BARE
# here (no ± prefix), unlike capacitors.
_TOLERANCE_VOCAB = {"0.1%", "1%", "5%"}
# Accept the distributor symmetric forms "5%", "5 %", "±5%", "+/-5%" and emit the seeded
# bare member. A leading lone +/- (an asymmetric signed bound) is tolerated only as the
# explicit ±/+- symmetric prefix; the trailing (?!\d) stops "1" matching inside "100%".
_TOLERANCE = re.compile(r"(?:\+/-\s?|±\s?)?\b(0\.1|1|5)\s?%(?!\d)")

_PACKAGE_VOCAB = {"0402", "0603", "0805", "1206", "through-hole"}
# The exact EIA imperial case-code series the resistor corpus uses (no character classes
# — a phantom code like "0302" would otherwise match a stray 4-digit token and conflict
# away the real package). 0201/1210 are real EIA codes but NOT seeded for resistors, so
# they are parsed and emitted here yet dropped by record_spec; the seeded subset is
# _PACKAGE_VOCAB. through-hole / THT collapses to the seeded "through-hole" member.
_PACKAGE_EIA = re.compile(r"\b(0201|0402|0603|0805|1206|1210)\b")
_THROUGH_HOLE = re.compile(r"\bTHROUGH[- ]?HOLE\b|\bTHT\b")

_MOUNTING_VOCAB = {"SMD", "through-hole", "press-fit"}
_MOUNT_SMD = re.compile(r"\bSMD\b|\bSMT\b")
_MOUNT_PRESS_FIT = re.compile(r"\bPRESS[- ]?FIT\b")


def _resistance_ohms(text: str) -> int | float | None:
    """Single resistance in canonical ohms, or None (no ohm signal / conflict)."""
    values: set[float] = set()
    if _ZERO_OHM.search(text):
        values.add(0.0)
    for m in _OHM_EXPLICIT.finditer(text):
        mult = _OHM_MULT[m.group(2)] if m.group(2) else 1
        values.add(float(m.group(1)) * mult)
    for m in _RKM.finditer(text):
        whole, letter, frac = m.group(1), m.group(2), m.group(3)
        mult = _OHM_MULT[letter]
        # "4M7" → 4.7 × 1e6; "100R" / "10K" (no fractional digits) → 100 × 1, 10 × 1e3.
        value = float(f"{whole}.{frac}") if frac else float(whole)
        values.add(value * mult)
    value = unique_or_none(values)
    if value is None:
        return None
    return int(value) if value == int(value) else value


def _power_rating(text: str) -> int | float | None:
    """Single power rating in canonical watts, or None (no W token / conflict)."""
    values: set[float] = set()
    for m in _POWER_FRACTION.finditer(text):
        denom = int(m.group(2))
        if denom:
            values.add(int(m.group(1)) / denom)
    for m in _POWER_DECIMAL.finditer(text):
        # The fraction "1/16 W" also matches "16 W" here — skip a candidate that is the
        # denominator of a fraction at the same position by requiring no preceding "/".
        if text[max(0, m.start() - 2) : m.start()].rstrip().endswith("/"):
            continue
        values.add(float(m.group(1)))
    value = unique_or_none(values)
    if value is None:
        return None
    return int(value) if value == int(value) else value


def _tolerance(text: str) -> str | None:
    return unique_or_none({f"{m.group(1)}%" for m in _TOLERANCE.finditer(text)})


def _package(text: str) -> str | None:
    packages = {m.group(1) for m in _PACKAGE_EIA.finditer(text)}
    if _THROUGH_HOLE.search(text):
        packages.add("through-hole")
    return unique_or_none(packages)


def _mounting(text: str) -> str | None:
    mounts: set[str] = set()
    if _MOUNT_SMD.search(text):
        mounts.add("SMD")
    if _THROUGH_HOLE.search(text):
        mounts.add("through-hole")
    if _MOUNT_PRESS_FIT.search(text):
        mounts.add("press-fit")
    return unique_or_none(mounts)


def extract_resistor(text: str) -> SpecDict:
    """Extract resistors specs from an upper-cased, whitespace-collapsed description."""
    specs: SpecDict = {}
    resistance = _resistance_ohms(text)
    if resistance is not None:
        specs["resistance"] = resistance
    power = _power_rating(text)
    if power is not None:
        specs["power_rating"] = power
    tolerance = _tolerance(text)
    if tolerance is not None:
        specs["tolerance"] = tolerance
    package = _package(text)
    if package is not None:
        specs["package"] = package
    mounting = _mounting(text)
    if mounting is not None:
        specs["mounting"] = mounting
    return specs
