"""Shared helpers to normalize connector core attributes into MaterialCard vocab.

Core attributes: category, lifecycle_status, package_type, pin_count, rohs_status.
All helpers return None when the input is missing/unmappable — never a guess.
"""

from __future__ import annotations

from typing import Any

# DigiKey ProductStatus / generic distributor lifecycle text -> MaterialCard lifecycle_status
_LIFECYCLE_MAP = {
    "active": "active",
    "obsolete": "obsolete",
    "discontinued": "obsolete",
    "not for new designs": "nrfnd",
    "nrnd": "nrfnd",
    "last time buy": "ltb",
    "end of life": "eol",
    "eol": "eol",
}

_ROHS_MAP = {
    "rohs compliant": "compliant",
    "compliant": "compliant",
    "rohs3 compliant": "compliant",
    "non-compliant": "non-compliant",
    "not compliant": "non-compliant",
    "rohs exempt": "exempt",
    "exempt": "exempt",
}


def map_lifecycle(raw: Any) -> str | None:
    if not raw:
        return None
    return _LIFECYCLE_MAP.get(str(raw).strip().lower())


def map_rohs(raw: Any) -> str | None:
    if not raw:
        return None
    return _ROHS_MAP.get(str(raw).strip().lower())


def clean_str(raw: Any, *, maxlen: int) -> str | None:
    if raw is None:
        return None
    s = str(raw).strip()
    return s[:maxlen] if s else None


def safe_pin_count(raw: Any) -> int | None:
    try:
        v = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def _meaningful(value: Any) -> str | None:
    """Return the stripped value, or None if it is empty or the '-' placeholder."""
    val = str(value).strip()
    return val if val and val != "-" else None


def generic_attribute(attrs: Any, name_key: str, value_key: str, names: tuple[str, ...]) -> str | None:
    """Extract a value from a generic attribute list by label/name match.

    Works for DigiKey Parameters ({ParameterText, ValueText}), Mouser ProductAttributes
    ({AttributeName, AttributeValue}) and Element14 attributes ({attributeLabel,
    attributeValue}) by passing the appropriate key names.
    """
    if not isinstance(attrs, list):
        return None
    wanted = {n.lower() for n in names}
    for attr in attrs:
        if not isinstance(attr, dict):
            continue
        label = str(attr.get(name_key, "")).strip().lower()
        if label in wanted:
            val = _meaningful(attr.get(value_key, ""))
            if val is not None:
                return val
    return None


def digikey_parameter(params: Any, names: tuple[str, ...]) -> str | None:
    """Extract a ValueText from DigiKey Parameters[] by ParameterText match."""
    return generic_attribute(params, "ParameterText", "ValueText", names)
