"""Per-commodity vendor-attribute → seeded-spec-key alias map (Element14 parametrics).

What: ``VENDOR_SPEC_MAP`` maps, per commodity, each seeded spec_key (the canonical
      ``app/data/commodity_seeds.json`` keys the F1 ladder + materials facets use) to the
      tuple of vendor attribute LABELS a distributor may carry for it. Element14's catalog
      response returns structured parametrics as an ``attributes`` list of
      ``{attributeLabel, attributeValue}`` dicts (see ``element14.py:_parse``); the labels
      vary per commodity, so the mapping is data-driven and harvested from real responses,
      never guessed. ``extract_vendor_specs`` looks up the commodity's aliases, pulls the
      first matching attribute value via ``_core_attrs.generic_attribute``, light-normalizes
      a few enum value FORMATS (Element14's "± 10%" / "0402 [1005 Metric]" shapes) to the
      seed enum spelling, and returns a normalized ``specs`` dict keyed by seeded spec_keys
      plus a ``dropped`` dict of attributes that matched no seeded key (mirrors the
      desc-extractor's observable-drop discipline).

      Values are otherwise left raw — the WRITER's ``record_spec`` (spec_write_service)
      runs the authoritative enum/numeric+unit gate and is the single point that rejects a
      value that does not match the seed schema. The format normalizer here only closes the
      cosmetic gaps (whitespace inside ``± 10%``, the metric suffix on ``0402 [1005
      Metric]``) that would otherwise make a real, correct value miss its enum.

Called by: app/connectors/element14.py:_parse (emits ``specs``/``dropped`` per result).
Depends on: app/connectors/_core_attrs.generic_attribute (pure label-match extraction).
      Commodity keys + spec_keys are the seeded ones (commodity_seeds.json); registering a
      seeded key here that the seed schema lacks is a no-op (record_spec drops it).
"""

from __future__ import annotations

import re
from typing import Any

from ._core_attrs import generic_attribute

# commodity -> seeded spec_key -> (vendor attribute label aliases, case-insensitive).
# Harvested from real Element14 (Farnell/Newark) catalog responses for the top-demand
# passive commodities (the credentialed, structured-parametric source per the design
# spec Revision 1). Element14 rate-limits hard, so this is a bounded top-demand supplement
# to the Mouser-description backbone; only commodities with a seeded schema AND observed
# structured attributes are mapped. Unmapped attributes land in the result's ``dropped``.
VENDOR_SPEC_MAP: dict[str, dict[str, tuple[str, ...]]] = {
    "capacitors": {
        "capacitance": ("Capacitance",),
        "voltage_rating": ("Voltage Rating DC", "Voltage Rating", "Voltage Rating AC", "DC Voltage Rating"),
        "dielectric": ("Dielectric", "Dielectric Characteristic", "Temperature Coefficient", "Dielectric Type"),
        "tolerance": ("Capacitance Tolerance", "Tolerance"),
        "package": ("Case Style", "Case Code - Imperial", "Package / Case", "Capacitor Case Style"),
        "mounting": ("Mounting Type", "Termination Style"),
    },
    "resistors": {
        "resistance": ("Resistance",),
        "power_rating": ("Power Rating", "Power Rating - Max", "Power Dissipation"),
        "tolerance": ("Resistance Tolerance", "Tolerance"),
        "package": ("Case Style", "Case Code - Imperial", "Package / Case", "Resistor Case Style"),
        "mounting": ("Mounting Type", "Termination Style"),
    },
}

# ── Enum value-format normalizers ────────────────────────────────────────────────────
# Element14 returns real, correct enum values in a slightly different SPELLING than the
# seed enums (whitespace + sign style on the tolerance, the metric suffix on the imperial
# case code). These close only the cosmetic gap so a correct value reaches its enum;
# anything that still does not match the seed enum is dropped by record_spec's enum gate
# (the authoritative validator), never coerced.

# Imperial case code with a trailing metric/letter annotation:
#   "0402 [1005 Metric]" / "0402 (1005 Metric)" / "0402M" / "0402 Metric" → "0402".
# Anchored on the LEADING 3-4 digit imperial code followed by a NON-DIGIT (so a 5-digit
# code like "01005" is left intact rather than truncated to its first 4 digits).
_CASE_CODE_RE = re.compile(r"^\s*(\d{3,4})(?=\D|$)")
# Tolerance value: optional sign + number + "%", e.g. "± 10%" / "+/- 10 %" / "10 %".
_TOL_RE = re.compile(r"^\s*(?:±|\+/-|\+/−)?\s*([\d.]+)\s*%")

# Per-commodity tolerance sign convention in commodity_seeds.json: capacitors carry the ±
# ("±10%"), resistors are bare ("5%"). Element14 reports tolerance WITH the sign for both
# ("± 10%", "± 1%"), so the normalizer must EMIT the seed's convention per commodity — not
# merely preserve the vendor's — or a real value misses its enum and is silently dropped.
# (docs/MATERIALS_FILTER_TREE_SPEC_MATRIX.md flagged the resistor seed as left bare.)
_TOLERANCE_SIGN_BY_COMMODITY = {"capacitors": "±", "resistors": ""}


def _normalize_case_code(value: str, _commodity: str) -> str:
    """Reduce an imperial case code with a metric/letter annotation to the bare imperial
    code ("0402 [1005 Metric]" / "0402M" → "0402")."""
    m = _CASE_CODE_RE.match(value)
    return m.group(1) if m else value


def _normalize_tolerance(value: str, commodity: str) -> str:
    """Re-spell a ``± N%`` tolerance to the SEED's sign convention for *commodity*.

    Capacitors keep the ± ("± 10%" → "±10%"); resistors drop it ("± 1%" → "1%"),
    matching the bare resistor seed enum. The seed convention wins over the vendor's so
    a correct Element14 value lands in its enum instead of being dropped by the enum
    gate.
    """
    m = _TOL_RE.match(value)
    if not m:
        return value
    sign = _TOLERANCE_SIGN_BY_COMMODITY.get(commodity, "")
    return f"{sign}{m.group(1)}%"


# seeded spec_key -> commodity-aware value normalizer (only the enum keys whose vendor
# SPELLING differs from the seed; every normalizer takes (value, commodity)).
_VALUE_NORMALIZERS = {
    "package": _normalize_case_code,
    "tolerance": _normalize_tolerance,
}


def extract_vendor_specs(
    attrs: Any, commodity: str | None, *, name_key: str, value_key: str
) -> tuple[dict[str, str], dict[str, str]]:
    """Map a vendor attribute list to ``(specs, dropped)`` for *commodity*.

    *attrs* is the distributor's attribute list (Element14: ``[{attributeLabel,
    attributeValue}, …]``); *name_key*/*value_key* name its label/value fields.
    For each seeded spec_key in ``VENDOR_SPEC_MAP[commodity]`` the first matching attribute
    value (``generic_attribute``) is taken, light-normalized for the known enum-format gaps,
    and collected into ``specs``. Every attribute whose label matched no seeded alias — OR
    matched an alias but lost (a later same-key alias already supplied the value) — is
    recorded in ``dropped`` (observable, mirroring the desc-extractor) so coverage gaps and
    second-value losses are visible. Returns ``({}, {})`` when *commodity* is unmapped or
    *attrs* is not a list.
    """
    aliases = VENDOR_SPEC_MAP.get(commodity or "")
    if not aliases or not isinstance(attrs, list):
        return {}, {}

    specs: dict[str, str] = {}
    used_labels: set[str] = set()  # the SINGLE label per spec_key that supplied the value
    for spec_key, names in aliases.items():
        value = generic_attribute(attrs, name_key, value_key, names)
        if value is None:
            continue
        normalizer = _VALUE_NORMALIZERS.get(spec_key)
        specs[spec_key] = normalizer(value, commodity or "") if normalizer else value
        # Record ONLY the alias that actually matched (the first present one), so a
        # second, distinct attribute sharing this key's alias list still surfaces in
        # `dropped` rather than being silently suppressed.
        used_labels.add(_first_present_label(attrs, name_key, value_key, names))

    dropped: dict[str, str] = {}
    for attr in attrs:
        if not isinstance(attr, dict):
            continue
        label = str(attr.get(name_key, "")).strip()
        if label and label.lower() not in used_labels:
            value = str(attr.get(value_key, "")).strip()
            if value and value != "-":
                dropped[label] = value
    return specs, dropped


def _first_present_label(attrs: list, name_key: str, value_key: str, names: tuple[str, ...]) -> str:
    """The lower-cased label of the FIRST attribute (in *attrs* order) whose name is in
    *names* and carries a meaningful value — i.e. the one ``generic_attribute``
    returned.

    Mirrors ``generic_attribute``'s match rule so ``extract_vendor_specs`` marks exactly
    the alias it consumed as used (not every alias of the key), leaving other distinct
    attributes free to land in ``dropped``.
    """
    wanted = {n.lower() for n in names}
    for attr in attrs:
        if not isinstance(attr, dict):
            continue
        label = str(attr.get(name_key, "")).strip().lower()
        if label in wanted and str(attr.get(value_key, "")).strip() not in ("", "-"):
            return label
    return ""
