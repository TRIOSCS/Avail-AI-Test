"""Deterministic capacitor description→spec extraction (distributor MLCC grammar).

What: reads capacitance / voltage_rating / dielectric / tolerance / package out of
      distributor (Mouser/DigiKey) ceramic-capacitor descriptions like
      ``Multilayer Ceramic Capacitors MLCC - SMD/SMT 16V 0.1uF X7R 0402 10%`` or
      ``… 100nF+/-10% 16V X7R 0402`` — NO network, NO LLM. Distributor descriptions
      are dense and consistent, so the passive commodities (capacitors/resistors/…)
      get the same description-grammar treatment that storage/memory already have.
      Every emitted value maps to a seeded capacitors entry in
      app/data/commodity_seeds.json: capacitance is normalized to the seeded canonical
      unit (pF) HERE because the writer calls record_spec with no ``unit`` (it cannot
      run unit_normalizer for us); record_spec independently re-validates enum members
      and skips unseeded keys/values (so a non-seeded case code like ``0201`` is parsed
      and returned but dropped at write time), but performs NO numeric_range check —
      the voltage range is the only range gate and lives here.
Called by: app/services/desc_extractor/__init__.py (extract_desc routing).
Depends on: _common (SpecDict alias + unique_or_none helper) — pure functions.

CONSERVATIVE by design (a wrong facet value is worse than a missing one):
- capacitance requires an explicit farad unit token (pF/nF/µF/uF/mF). Each is converted
  to the seeded canonical pF (µF×1e6, nF×1e3, mF×1e9, pF×1) so 0.1µF and 100nF both land
  as 100000 pF. A resistor/drive description carries no farad token, so nothing is
  emitted (the negative-guard requirement). Conflicting capacitances ⇒ omit.
- voltage_rating from a bare ``<n>V`` token inside the seeded 1-10000 range; the unit
  must be a whole ``V`` (so ``X5R``/``16VDC`` extends fine, but ``5%`` / ``0402`` never
  match). Conflicting voltages ⇒ omit.
- dielectric / tolerance / package are seeded enums. tolerance accepts the distributor
  forms ``10%`` / ``+/-10%`` / ``±10%`` / ``5%`` and emits the seeded ``±N%`` member.
  package is the EIA case code verbatim (0402/0603/…); record_spec keeps only the
  seeded subset, so a parsed ``0201`` is returned here but never written.
- Multiple conflicting signals for any key ⇒ that key is omitted, the rest still extract.
"""

import re

from app.services.desc_extractor._common import SpecDict, unique_or_none

# Seeded canonical capacitance unit (app/data/commodity_seeds.json capacitors.capacitance).
_CANONICAL_CAP_UNIT = "pF"

# Farad unit → multiplier to the canonical pF (mirrors unit_normalizer._CONVERSIONS;
# the writer cannot normalize for us — record_spec is called without a unit — so the
# conversion is applied here, exactly as storage emits capacity already in GB).
_FARAD_TO_PF = {"PF": 1, "NF": 1_000, "UF": 1_000_000, "ΜF": 1_000_000, "µF": 1_000_000, "MF": 1_000_000_000}
_CAPACITANCE = re.compile(r"\b(\d+(?:\.\d+)?)\s?(PF|NF|UF|ΜF|µF|MF)\b")

# Seeded voltage_rating numeric_range — record_spec performs NO range check, so this is
# the only range gate; the drift guard in tests pins it against commodity_seeds.json.
_VOLT_MIN, _VOLT_MAX = 1, 10000
_VOLTAGE = re.compile(r"\b(\d+(?:\.\d+)?)\s?V(?![A-Z0-9])")

# Seeded enum vocabularies (capacitors.{dielectric,tolerance,package}).
_DIELECTRIC_VOCAB = {"X7R", "X5R", "C0G", "Y5V", "NP0"}
_DIELECTRIC = re.compile(r"\b(X7R|X5R|C0G|Y5V|NP0)\b")

_TOLERANCE_VOCAB = {"±1%", "±5%", "±10%", "±20%"}
# Distributor symmetric-tolerance forms: "10%", "+/-10%", "±10%" → seeded "±N%". The
# ``±``/``+/-`` prefix is accepted and discarded. A BARE "<n>%" matches only when it is
# NOT preceded by a lone +/- sign: a signed bound like the Y5V temperature coefficient
# "-20%+80%" is asymmetric, not a symmetric tolerance, so it must never read as ±20%.
# The trailing (?!\d) stops "10" matching inside "100%"-style tokens.
_TOLERANCE = re.compile(r"(?:\+/-\s?|±\s?|(?<![-+\d]))(1|5|10|20)\s?%(?!\d)")

_PACKAGE_VOCAB = {"0402", "0603", "0805", "1206", "1210"}
# The exact EIA imperial case-code series (no character classes — phantom codes like
# "0101"/"0302" would otherwise match a stray 4-digit token and conflict away the real
# package). 0201 is a real code but NOT seeded, so it is parsed and emitted yet dropped
# by record_spec; the seeded subset is _PACKAGE_VOCAB.
_PACKAGE = re.compile(r"\b(0201|0402|0603|0805|1206|1210|1812|2220)\b")


def _capacitance_pf(text: str) -> int | float | None:
    """Single capacitance in canonical pF, or None (no farad token / conflict)."""
    values: set[float] = set()
    for m in _CAPACITANCE.finditer(text):
        values.add(float(m.group(1)) * _FARAD_TO_PF[m.group(2)])
    value = unique_or_none(values)
    if value is None:
        return None
    return int(value) if value == int(value) else value


def _voltage_rating(text: str) -> int | float | None:
    """Single in-range voltage rating, or None (absent / conflict)."""
    values: set[float] = set()
    for m in _VOLTAGE.finditer(text):
        candidate = float(m.group(1))
        if _VOLT_MIN <= candidate <= _VOLT_MAX:
            values.add(candidate)
    value = unique_or_none(values)
    if value is None:
        return None
    return int(value) if value == int(value) else value


def _dielectric(text: str) -> str | None:
    return unique_or_none({m.group(1) for m in _DIELECTRIC.finditer(text)})


def _tolerance(text: str) -> str | None:
    return unique_or_none({f"±{m.group(1)}%" for m in _TOLERANCE.finditer(text)})


def _package(text: str) -> str | None:
    return unique_or_none({m.group(1) for m in _PACKAGE.finditer(text)})


def extract_capacitor(text: str) -> SpecDict:
    """Extract capacitors specs from an upper-cased, whitespace-collapsed
    description."""
    specs: SpecDict = {}
    capacitance = _capacitance_pf(text)
    if capacitance is not None:
        specs["capacitance"] = capacitance
    voltage = _voltage_rating(text)
    if voltage is not None:
        specs["voltage_rating"] = voltage
    dielectric = _dielectric(text)
    if dielectric is not None:
        specs["dielectric"] = dielectric
    tolerance = _tolerance(text)
    if tolerance is not None:
        specs["tolerance"] = tolerance
    package = _package(text)
    if package is not None:
        specs["package"] = package
    return specs
