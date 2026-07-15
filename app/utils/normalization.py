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
from urllib.parse import urlsplit

# ── Website domain normalization ──────────────────────────────────────


def parse_website_domain(website: str) -> str:
    """Extract a usable bare domain from user-typed website input (F12).

    urlsplit-based (scheme optional), lowercased host, strips ONE leading "www." —
    never a blanket str.replace that mangles hosts containing the substring. Returns
    "" when no plausible domain can be extracted (no ``.`` in the host, or characters
    outside ``[a-z0-9.-]``) — callers turn that into a visible error/None instead of
    silently saving a junk domain.

    Extracted from app.routers.sightings._parse_website_domain (originally the only
    validated extractor in the codebase — see that module's history) so
    app.services.company_import_service can share it instead of its own narrower
    regex-based ``_company_domain``. app.utils.vendor_helpers.scrape_website_contacts
    consolidated onto this helper too (cache-key site; unparseable input falls back
    to the raw string so junk keys stay distinct). app.enrichment_service._clean_domain
    deliberately stays on its own looser cleanup — measured divergence with persisted
    blast radius; see its docstring and tests/test_domain_extractor_consolidation.py.
    """
    raw = website.strip()
    try:
        parsed = urlsplit(raw if "://" in raw else f"//{raw}")
        host = (parsed.hostname or "").strip().lower()
    except ValueError:
        return ""
    if host.startswith("www."):
        host = host[4:]
    if "." not in host or not re.fullmatch(r"[a-z0-9.-]+", host):
        return ""
    return host


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

_CURRENCY_CODE_RE = re.compile(
    r"\b(?:" + "|".join(_CURRENCY_CODES) + r")\b",
    re.IGNORECASE,
)

# K/M shorthand multipliers, shared by price and quantity parsing.
_KM_MULTIPLIERS = {
    "k": 1_000,
    "K": 1_000,
    "m": 1_000_000,
    "M": 1_000_000,
}


def normalize_price(raw: Any) -> float | None:
    """Parse price string to float.

    Returns None if ambiguous.
    """
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
    s = _CURRENCY_CODE_RE.sub("", s).strip()

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
        return num * _KM_MULTIPLIERS.get(m.group(2), 1)

    try:
        val = float(s)
        return val if val > 0 else None
    except ValueError:
        return None


def detect_currency(raw: Any) -> str:
    """Detect currency from a price string or currency field.

    Default USD.
    """
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

_QTY_MULTIPLIERS = _KM_MULTIPLIERS


def normalize_quantity(raw: Any) -> int | None:
    """Parse quantity.

    Handles: 50000, "50,000", "50K", "50k". Returns None if ambiguous.
    """
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
    elif any(w in s for w in ("day", "dy", "d ", "aro", "business")) or re.search(r"\d\s*d\b", s):
        # Compact day shorthand ("30d", "5d", "14 d") has no trailing space, so
        # match a digit immediately followed by a standalone "d".
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
    """Parse MOQ.

    Handles: 10000, "10K", "10K minimum", "MOQ: 500".
    """
    if raw is None:
        return None
    s = str(raw).strip()
    # Strip common leading prefixes ("MOQ:", "Minimum", "Min")
    s = re.sub(r"^(?:moq|minimum|min)[:\s]*", "", s, flags=re.IGNORECASE).strip()
    # Strip trailing qualifier words ("10K minimum", "500 pcs", "250 each") before
    # quantity parsing — otherwise a trailing "minimum" makes the string end in "m"
    # and normalize_quantity mistakes it for the 1e6 multiplier suffix → None.
    s = re.sub(
        r"[\s:]*(?:minimum|min|pcs|pieces|piece|units|unit|each|ea|qty|quantity)\.?$",
        "",
        s,
        flags=re.IGNORECASE,
    ).strip()
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
    "bag": "bag",
    "box": "box",
    "loose": "bulk",
    "each": "each",
    "ea/": "each",
    "ea ": "each",
    "piece": "each",
    "pcs": "each",
    "strip": "strip",
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
    """Normalize MPN: uppercase, strip whitespace, remove common noise.

    Use for DISPLAY — keeps dashes (e.g. "LM2596S-5.0" → "LM2596S-5.0").
    """
    if not raw:
        return None
    s = str(raw).strip().upper()
    # Remove surrounding quotes
    s = s.strip("'\"")
    # Strip trailing punctuation (periods, commas) that are not part of the MPN
    s = s.rstrip(".,;:")
    # Collapse internal whitespace
    s = re.sub(r"\s+", "", s)

    if len(s) < 3:
        return None
    return s


_NONALNUM_RE = re.compile(r"[^a-z0-9]")


def normalize_mpn_key(raw: Any) -> str:
    """Canonical dedup key: strip ALL non-alphanumeric chars and lowercase.

    "LM2596S-5.0" → "lm2596s50"
    "LM2596S 5.0" → "lm2596s50"
    " lm-317t "   → "lm317t"
    """
    if not raw:
        return ""
    return _NONALNUM_RE.sub("", str(raw).strip().lower())


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

    # Strip to canonical key and compare
    a_stripped = normalize_mpn_key(a)
    b_stripped = normalize_mpn_key(b)
    if a_stripped == b_stripped:
        return True

    # One is prefix of the other (trailing revision). Compare the longer against
    # the shorter so the check is symmetric — the suffix is the longer key minus
    # the shared prefix, regardless of argument order. (The old str.replace()
    # form only worked when the longer MPN was passed first.)
    if a_stripped.startswith(b_stripped) or b_stripped.startswith(a_stripped):
        longer, shorter = sorted((a_stripped, b_stripped), key=len, reverse=True)
        suffix = longer[len(shorter) :]
        if 0 < len(suffix) <= 2:  # Short trailing suffix = likely revision
            return True

    return False


MAX_SUBSTITUTES = 20


def parse_substitute_mpns(
    subs: list[dict | str] | None, primary_mpn: str, *, limit: int = MAX_SUBSTITUTES
) -> list[dict]:
    """Parse structured substitute list, normalize MPNs, and deduplicate.

    Each sub is normally a dict with 'mpn' and 'manufacturer' keys, plus an
    optional 'source' provenance key (e.g. constants.FRU_ALIAS_SOURCE for
    system-derived FRU-crosswalk aliases) which is preserved when present.
    Legacy DB rows may hold plain MPN strings (["LM338T"]); those are accepted
    too. Returns a normalized, deduped list capped at limit.

    Called by: htmx_views.py (add/update/header-save endpoints)
    Depends on: normalize_mpn, normalize_mpn_key
    """
    result: list[dict] = []
    if not subs:
        return result
    seen_keys = {normalize_mpn_key(primary_mpn)}
    for sub in subs:
        # Legacy DB rows hold plain strings (e.g. ["LM338T"]); modern rows hold
        # dicts. Coerce both so an unguarded caller can't crash on legacy data.
        if isinstance(sub, str):
            sub = {"mpn": sub}
        elif not isinstance(sub, dict):
            continue
        raw_mpn = str(sub.get("mpn") or "").strip()
        if not raw_mpn:
            continue
        ns = normalize_mpn(raw_mpn) or raw_mpn
        key = normalize_mpn_key(ns)
        if key and key not in seen_keys:
            seen_keys.add(key)
            entry = {
                "mpn": ns,
                "manufacturer": str(sub.get("manufacturer") or "").strip(),
            }
            source = sub.get("source")
            if isinstance(source, str) and source.strip():
                entry["source"] = source.strip()
            result.append(entry)
    return result[:limit]
