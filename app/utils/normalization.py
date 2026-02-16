"""Deterministic normalization — pure Python, no AI.

Normalizes vendor data values extracted from emails and attachments:
  - Prices: "$1,234.56" → 1234.56
  - Quantities: "50K" → 50000
  - Lead times: "4-6 weeks" → 35 (days, midpoint)
  - Conditions: "factory new" → "new"
  - Currencies: "¥" → "JPY"
  - Date codes: "2024+" → "2024+"
  - MOQ: "10K minimum" → 10000

Design: Prefer less data if it means better data. Return None for ambiguous values.
"""

import re
from typing import Any

# ── Price normalization ───────────────────────────────────────────────

_CURRENCY_SYMBOLS = {
    "$": "USD",
    "€": "EUR",
    "£": "GBP",
    "¥": "JPY",
    "₩": "KRW",
    "₹": "INR",
    "元": "CNY",
    "A$": "AUD",
    "C$": "CAD",
    "S$": "SGD",
    "HK$": "HKD",
}

_CURRENCY_CODES = {
    "USD",
    "EUR",
    "GBP",
    "JPY",
    "CNY",
    "KRW",
    "INR",
    "AUD",
    "CAD",
    "SGD",
    "HKD",
    "TWD",
    "THB",
    "MYR",
}


def normalize_price(raw: Any) -> float | None:
    """Parse price string to float. Returns None if ambiguous."""
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw) if raw > 0 else None

    s = str(raw).strip()
    if not s:
        return None

    # Remove currency symbols and whitespace
    for sym in _CURRENCY_SYMBOLS:
        s = s.replace(sym, "")
    # Remove currency codes (USD, EUR, GBP, etc.)
    for code in _CURRENCY_CODES:
        s = re.sub(rf"\b{code}\b", "", s, flags=re.IGNORECASE)
    s = s.strip()

    # Remove commas (thousand separators)
    s = s.replace(",", "")

    # Handle ranges — take the lower bound: "0.38-0.42" → 0.38
    if "-" in s and not s.startswith("-"):
        parts = s.split("-")
        try:
            return float(parts[0].strip())
        except ValueError:
            pass

    # Handle "each", "ea", "/ea", "/pc", "/unit"
    s = re.sub(
        r"[/\s]*(ea|each|pc|pcs|unit|units|piece|pieces)\.?\s*$",
        "",
        s,
        flags=re.IGNORECASE,
    )

    # Handle K/M shorthand: "1.5k" → 1500, "2M" → 2000000
    m = re.match(r"^([\d.]+)\s*([kKmM])$", s)
    if m:
        num = float(m.group(1))
        mult = {"k": 1_000, "K": 1_000, "m": 1_000_000, "M": 1_000_000}
        return num * mult.get(m.group(2), 1)

    try:
        val = float(s)
        return val if val > 0 else None
    except ValueError:
        return None


def detect_currency(raw: Any) -> str:
    """Detect currency from a price string or currency field. Default USD."""
    if not raw:
        return "USD"
    s = str(raw).strip().upper()

    # Direct code match
    if s in _CURRENCY_CODES:
        return s

    # Symbol match
    raw_str = str(raw).strip()
    for sym, code in _CURRENCY_SYMBOLS.items():
        if sym in raw_str:
            return code

    return "USD"


# ── Quantity normalization ────────────────────────────────────────────

_QTY_MULTIPLIERS = {
    "k": 1_000,
    "K": 1_000,
    "m": 1_000_000,
    "M": 1_000_000,
}


def normalize_quantity(raw: Any) -> int | None:
    """Parse quantity. Handles: 50000, "50,000", "50K", "50k". Returns None if ambiguous."""
    if raw is None:
        return None
    if isinstance(raw, int):
        return raw if raw > 0 else None
    if isinstance(raw, float):
        return int(raw) if raw > 0 else None

    s = str(raw).strip().replace(",", "").replace(" ", "")
    if not s:
        return None

    # Handle multiplier suffix: "50K" → 50000
    for suffix, mult in _QTY_MULTIPLIERS.items():
        if s.endswith(suffix):
            try:
                return int(float(s[:-1]) * mult)
            except ValueError:
                pass

    # Handle "+" suffix: "50000+" → 50000
    s = s.rstrip("+")

    try:
        return int(float(s))
    except ValueError:
        return None


# ── Lead time normalization ───────────────────────────────────────────


def normalize_lead_time(raw: Any) -> int | None:
    """Parse lead time to days. Returns midpoint for ranges. None if ambiguous.

    Handles: "4-6 weeks", "30 days", "2-3 wks", "stock", "ARO"
    """
    if raw is None:
        return None
    s = str(raw).strip().lower()
    if not s:
        return None

    # Stock / immediate
    if s in ("stock", "in stock", "immediate", "from stock", "0"):
        return 0

    # Extract numbers
    numbers = re.findall(r"(\d+(?:\.\d+)?)", s)
    if not numbers:
        return None

    nums = [float(n) for n in numbers]

    # Determine unit
    if any(w in s for w in ("week", "wk")):
        multiplier = 7
    elif any(w in s for w in ("month", "mo")):
        multiplier = 30
    elif any(w in s for w in ("day", "dy", "d ", "aro", "business")):
        multiplier = 1
    else:
        # Ambiguous — assume weeks if >0 and <52, days otherwise
        if len(nums) == 1 and nums[0] <= 52:
            multiplier = 7  # Probably weeks
        else:
            multiplier = 1

    # Range: take midpoint
    if len(nums) >= 2:
        midpoint = (nums[0] + nums[1]) / 2
    else:
        midpoint = nums[0]

    return int(midpoint * multiplier)


# ── Condition normalization ───────────────────────────────────────────

_CONDITION_MAP = {
    "new": "new",
    "factory new": "new",
    "brand new": "new",
    "original": "new",
    "oem": "new",
    "genuine": "new",
    "refurbished": "refurb",
    "refurb": "refurb",
    "reconditioned": "refurb",
    "reclaimed": "refurb",
    "used": "used",
    "pulls": "used",
    "pulled": "used",
    "surplus": "used",
    "excess": "used",
}


def normalize_condition(raw: Any) -> str | None:
    """Normalize condition to: new, refurb, used, or None."""
    if not raw:
        return None
    s = str(raw).strip().lower()
    for pattern, normalized in _CONDITION_MAP.items():
        if pattern in s:
            return normalized
    return None


# ── Date code normalization ───────────────────────────────────────────


def normalize_date_code(raw: Any) -> str | None:
    """Normalize date codes. Passes through common formats, strips noise.

    Handles: "2024+", "DC 23/45", "2023", "N/A" → None
    """
    if not raw:
        return None
    s = str(raw).strip()

    # Skip non-values
    if s.lower() in ("n/a", "na", "tbd", "unknown", "various", "-", ""):
        return None

    # Strip "DC" prefix
    s = re.sub(r"^(?:dc|date\s*code)[:\s]*", "", s, flags=re.IGNORECASE).strip()

    # Keep if it contains at least 2 digits (year info)
    if sum(c.isdigit() for c in s) >= 2:
        return s

    return None


# ── MOQ normalization ─────────────────────────────────────────────────


def normalize_moq(raw: Any) -> int | None:
    """Parse MOQ. Handles: 10000, "10K", "10K minimum", "MOQ: 500"."""
    if raw is None:
        return None
    s = str(raw).strip()
    # Strip common prefixes
    s = re.sub(r"^(?:moq|minimum|min)[:\s]*", "", s, flags=re.IGNORECASE).strip()
    return normalize_quantity(s)


# ── Packaging normalization ───────────────────────────────────────────

_PACKAGING_MAP = {
    # Longest/most-specific patterns first to avoid substring collisions
    "tape and reel": "reel",
    "tape & reel": "reel",
    "cut tape": "cut_tape",
    "t&r": "reel",
    "tray": "tray",  # MUST come before "tr" — "tr" is a substring of "tray"
    "reel": "reel",
    "tube": "tube",
    "bulk": "bulk",
    "bag": "bulk",
    "loose": "bulk",
    "ct": "cut_tape",
    "dip": "tube",
    "smd": "reel",
    "tr": "reel",  # "T/R" shorthand — checked AFTER "tray"
}


def normalize_packaging(raw: Any) -> str | None:
    """Normalize packaging to: reel, tube, tray, bulk, cut_tape, or None."""
    if not raw:
        return None
    s = str(raw).strip().lower()
    for pattern, normalized in _PACKAGING_MAP.items():
        if pattern in s:
            return normalized
    return None


# ── MPN normalization ─────────────────────────────────────────────────


def normalize_mpn(raw: Any) -> str | None:
    """Normalize MPN: uppercase, strip whitespace, remove common noise."""
    if not raw:
        return None
    s = str(raw).strip().upper()
    # Remove surrounding quotes
    s = s.strip("'\"")
    # Collapse internal whitespace
    s = re.sub(r"\s+", "", s)

    if len(s) < 3:
        return None
    return s


def fuzzy_mpn_match(mpn_a: str | None, mpn_b: str | None) -> bool:
    """Check if two MPNs are likely the same part.

    Handles: trailing revision letters, dashes vs no dashes, case differences.
    """
    if not mpn_a or not mpn_b:
        return False
    a = normalize_mpn(mpn_a) or ""
    b = normalize_mpn(mpn_b) or ""
    if not a or not b:
        return False

    # Exact match
    if a == b:
        return True

    # Strip dashes and compare
    a_stripped = a.replace("-", "").replace("/", "")
    b_stripped = b.replace("-", "").replace("/", "")
    if a_stripped == b_stripped:
        return True

    # One is prefix of the other (trailing revision)
    if a_stripped.startswith(b_stripped) or b_stripped.startswith(a_stripped):
        suffix = a_stripped.replace(b_stripped, "") or b_stripped.replace(
            a_stripped, ""
        )
        if len(suffix) <= 2:  # Short suffix = likely revision
            return True

    return False
