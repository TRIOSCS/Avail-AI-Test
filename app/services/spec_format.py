"""spec_format.py — Human-readable spec chips for material cards.

Numeric spec values are stored as CANONICAL-UNIT magnitudes (spec_write_service /
unit_normalizer canonicalise on write: capacitance in pF, inductance in nH,
resistance in ohms, frequency in MHz ...), so raw rendering shows values like
``4700000000`` for a 4.7 mF capacitor. This module turns ``(value, data_type,
unit)`` into the human string the material cards show — SI-prefix promotion for
scalable electrical units, plain "value unit" for non-scalable ones (GB, cores,
pins ...), Yes/No for booleans, enum strings verbatim — and assembles the
per-card chip lists (build_card_specs) the materials list renders.

Used by: app/routers/htmx/materials.py (material card spec chips).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

# Canonical units that scale by SI prefix → (base-unit display symbol, factor to base).
# Everything NOT listed here renders as "value unit" verbatim (GB, cores, mAh, dBm ...).
# The canonical-unit vocabulary is OWNED by the write path (unit_normalizer +
# app/data/commodity_seeds.json); tests/test_spec_format.py pins every canonical unit
# in the seeds to either this table or the deliberate-verbatim list, so a new
# canonical unit can't silently render as a raw magnitude.
_SCALABLE_UNITS: dict[str, tuple[str, float]] = {
    "pF": ("F", 1e-12),
    "nH": ("H", 1e-9),
    "mOhm": ("Ω", 1e-3),
    "ohms": ("Ω", 1.0),
    "kHz": ("Hz", 1e3),
    "MHz": ("Hz", 1e6),
    "GHz": ("Hz", 1e9),
    "mV": ("V", 1e-3),
    "V": ("V", 1.0),
    "mA": ("A", 1e-3),
    "A": ("A", 1.0),
    "W": ("W", 1.0),
}

# Largest SI factor each base unit may promote UP to — conventions differ per unit:
# a 1200 W power supply stays "1,200 W" (never kW); voltages/currents stop at kilo
# ("4 kV" isolation is standard, "0.01 MV" is not); F/H/Ω/Hz promote freely.
_MAX_FACTOR: dict[str, float] = {"W": 1.0, "V": 1e3, "A": 1e3}

# (factor, prefix) from largest to smallest; the first factor <= |value| wins.
_SI_STEPS: tuple[tuple[float, str], ...] = (
    (1e9, "G"),
    (1e6, "M"),
    (1e3, "k"),
    (1.0, ""),
    (1e-3, "m"),
    (1e-6, "µ"),
    (1e-9, "n"),
    (1e-12, "p"),
)

# Units that read awkwardly appended ("3 ratio", "2 count") — value renders bare.
_SILENT_UNITS = frozenset({"ratio", "count"})


def _fmt_num(value: float) -> str:
    """Trim a magnitude for display: integers with thousands separators, else <=2 decimals."""
    if abs(value - round(value)) < 1e-9 and abs(value) < 1e15:
        return f"{int(round(value)):,}"
    return f"{value:.2f}".rstrip("0").rstrip(".")


def _promote(value: float, base_symbol: str, to_base: float) -> str:
    """Scale a canonical-unit magnitude to the best SI prefix of its base unit."""
    base_val = value * to_base
    if base_val == 0:
        return f"0 {base_symbol}"
    max_factor = _MAX_FACTOR.get(base_symbol, 1e9)
    for factor, prefix in _SI_STEPS:
        if factor <= max_factor and abs(base_val) >= factor:
            return f"{_fmt_num(base_val / factor)} {prefix}{base_symbol}"
    factor, prefix = _SI_STEPS[-1]
    return f"{_fmt_num(base_val / factor)} {prefix}{base_symbol}"


def format_spec_value(value, data_type: str | None = None, unit: str | None = None) -> str:
    """Format one structured-spec scalar for card display.

    ``unit`` must be the CANONICAL unit the value is stored in
    (CommoditySpecSchema.canonical_unit, falling back to .unit).
    """
    if value is None:
        return ""
    if data_type == "boolean" or isinstance(value, bool):
        if isinstance(value, str):
            return {"true": "Yes", "false": "No"}.get(value.strip().lower(), value)
        return "Yes" if value else "No"
    if data_type == "numeric":
        try:
            num = float(value)
        except (TypeError, ValueError):
            return str(value)
        if unit in _SCALABLE_UNITS:
            base_symbol, to_base = _SCALABLE_UNITS[unit]
            return _promote(num, base_symbol, to_base)
        if not unit or unit in _SILENT_UNITS:
            return _fmt_num(num)
        if unit == "%":
            return f"{_fmt_num(num)}%"
        return f"{_fmt_num(num)} {unit}"
    if isinstance(value, (int, float)):
        return _fmt_num(float(value))
    return str(value)


# Card chips are capped; overflow renders as "+N more" (m._specs_more).
_CARD_SPECS_MAX = 6
# Unscoped schema-less fallback keeps the pre-hybrid density of 3 chips.
_FALLBACK_SPECS_MAX = 3


def _spec_scalar(raw: Any) -> str | int | float | bool | None:
    """Scalar payload of one specs_structured entry (dict-wrapped or raw), else None."""
    val = raw.get("value") if isinstance(raw, dict) else raw
    return val if isinstance(val, (str, int, float, bool)) else None


def build_card_specs(db: Session, materials: list, commodity: str | None = None) -> None:
    """Attach display spec chips to material cards: m._card_specs, m._specs_more.

    Commodity-scoped: ALL of the commodity's filterable fields the card has values
    for — primary fields first, then the filter sidebar's order — formatted
    human-readable, capped at _CARD_SPECS_MAX with the overflow count in
    _specs_more. Without a commodity: each card's OWN category's primary keys (one
    batched query — no N+1); whenever that yields no chips (schema-less category OR
    a card lacking values for every primary key) fall back to the first
    _FALLBACK_SPECS_MAX scalar specs_structured entries labelled by their
    prettified spec key.
    """
    from ..models.faceted_search import CommoditySpecSchema

    schema_by_cat: dict[str, list[CommoditySpecSchema]] = {}
    if commodity:
        schema_by_cat[commodity.lower().strip()] = (
            db.query(CommoditySpecSchema)
            .filter(CommoditySpecSchema.commodity == commodity, CommoditySpecSchema.is_filterable.is_(True))
            .order_by(CommoditySpecSchema.is_primary.desc(), CommoditySpecSchema.sort_order)
            .all()
        )
    else:
        card_cats = {(m.category or "").lower().strip() for m in materials if m.category}
        if card_cats:
            schema_rows = (
                db.query(CommoditySpecSchema)
                .filter(CommoditySpecSchema.commodity.in_(card_cats), CommoditySpecSchema.is_primary.is_(True))
                .order_by(CommoditySpecSchema.sort_order)
                .all()
            )
            for s in schema_rows:
                schema_by_cat.setdefault(s.commodity, []).append(s)

    for m in materials:
        specs = m.specs_structured or {}
        card_cat = commodity.lower().strip() if commodity else (m.category or "").lower().strip()
        chips: list[dict[str, str]] = []
        populated = 0
        for s in schema_by_cat.get(card_cat, []):
            val = _spec_scalar(specs.get(s.spec_key))
            if val is None:
                continue
            populated += 1
            if len(chips) < _CARD_SPECS_MAX:  # format only what actually renders
                chips.append(
                    # str(): the legacy Column-style model types attributes as Column[str]
                    {
                        "label": str(s.display_name),
                        "value": format_spec_value(val, s.data_type, s.canonical_unit or s.unit),
                    }
                )
        if not commodity and not chips:
            for k, raw in specs.items():
                val = _spec_scalar(raw)
                if val is None:
                    continue
                chips.append({"label": k.replace("_", " "), "value": format_spec_value(val)})
                if len(chips) >= _FALLBACK_SPECS_MAX:
                    break
            populated = len(chips)
        m._card_specs = chips
        m._specs_more = max(0, populated - _CARD_SPECS_MAX)
