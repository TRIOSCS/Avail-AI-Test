"""Unit normalization for structured specs.

What: Converts values between measurement units (uF->pF, kOhm->ohms, etc.).
Called by: spec_write_service.record_spec()
Depends on: nothing (pure functions)
"""

from loguru import logger

# Conversion table: (from_unit, to_unit) -> multiplier
# All keys are lowercase for case-insensitive matching.
_CONVERSIONS: dict[tuple[str, str], float] = {
    # Capacitance -> pF
    ("uf", "pf"): 1_000_000,
    ("nf", "pf"): 1_000,
    ("mf", "pf"): 1_000_000_000,
    # Resistance -> ohms
    ("kohm", "ohms"): 1_000,
    ("mohm", "ohms"): 1_000_000,
    # Inductance -> nH
    ("uh", "nh"): 1_000,
    ("mh", "nh"): 1_000_000,
    ("h", "nh"): 1_000_000_000,
    # Frequency -> MHz
    ("ghz", "mhz"): 1_000,
    ("khz", "mhz"): 0.001,
    ("hz", "mhz"): 0.000001,
    # Power -> W
    ("mw", "w"): 0.001,
    ("kw", "w"): 1_000,
    # Current -> A
    ("ma", "a"): 0.001,
    ("ua", "a"): 0.000001,
}


def normalize_value(
    value: float | int | str,
    from_unit: str | None,
    canonical_unit: str | None,
) -> float | int | str:
    """Normalize a value to its canonical unit.

    Returns the original value unchanged if:
    - value is a string (enum/text values)
    - units are the same
    - no conversion rule exists
    """
    if isinstance(value, str):
        return value

    if not from_unit or not canonical_unit:
        return value

    from_lower = from_unit.lower()
    canonical_lower = canonical_unit.lower()

    if from_lower == canonical_lower:
        return value

    multiplier = _CONVERSIONS.get((from_lower, canonical_lower))
    if multiplier is None:
        logger.warning(
            "No conversion rule: {} -> {}, returning original",
            from_unit,
            canonical_unit,
        )
        return value

    return value * multiplier
