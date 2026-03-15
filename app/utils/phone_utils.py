"""Phone number formatting utilities — E.164 normalization and display formatting.

Provides two core functions:
- format_phone_e164(raw) → canonical "+1XXXXXXXXXX" for US or "+CC..." for intl
- format_phone_display(raw) → human-readable "(415) 555-1234" or "+852 9876 5432"

Called by: app/routers/activity.py, app/static/app.js (mirrored in JS)
Depends on: nothing (regex only, no external packages)
"""

import re


def format_phone_e164(raw: str) -> str | None:
    """Normalize a phone string to E.164 format.

    Returns None if unparseable (too short, letters, empty). Assumes US (+1) for
    10-digit numbers.
    """
    if not raw:
        return None

    # Strip everything except digits and leading +
    cleaned = raw.strip()
    # Remove extension markers and everything after
    cleaned = re.split(r"(?i)\s*(?:ext|x|#)\s*\.?\s*\d*$", cleaned)[0]
    # Check for alpha characters (e.g. "CALL JOHN") — reject
    if re.search(r"[a-zA-Z]", cleaned):
        return None

    # Extract digits and possible leading +
    has_plus = cleaned.startswith("+")
    digits = re.sub(r"\D", "", cleaned)

    if not digits or len(digits) < 7:
        return None

    # US: 10 digits → +1XXXXXXXXXX
    if len(digits) == 10:
        return f"+1{digits}"

    # US: 11 digits starting with 1 → +1XXXXXXXXXX
    if len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"

    # International: had a + prefix, keep as-is
    if has_plus and len(digits) >= 7:
        return f"+{digits}"

    # 7-9 digits with no country context — ambiguous but usable
    if 7 <= len(digits) <= 9:
        return None

    # 12+ digits with no + — likely international
    if len(digits) >= 12:
        return f"+{digits}"

    return None


def format_phone_display(raw: str) -> str:
    """Format a phone number for human display.

    US numbers: (415) 555-1234
    International: +852 9876 5432
    Unparseable: returns raw input unchanged.
    """
    if not raw:
        return raw or ""

    e164 = format_phone_e164(raw)
    if not e164:
        return raw.strip()

    digits = e164.lstrip("+")

    # US number: +1XXXXXXXXXX → (XXX) XXX-XXXX
    if len(digits) == 11 and digits.startswith("1"):
        local = digits[1:]
        return f"({local[:3]}) {local[3:6]}-{local[6:]}"

    # International: group in chunks of 4 from the right
    # e.g. +852 9876 5432
    cc_len = _guess_country_code_len(digits)
    cc = digits[:cc_len]
    rest = digits[cc_len:]
    # Split rest into groups of 4
    groups = []
    while rest:
        groups.append(rest[:4])
        rest = rest[4:]
    return f"+{cc} {' '.join(groups)}"


def _guess_country_code_len(digits: str) -> int:
    """Guess the country code length from a digit string (no +)."""
    # Common 1-digit: 1 (US/CA)
    if digits.startswith("1"):
        return 1
    # Common 2-digit: 44 (UK), 61 (AU), 49 (DE), 33 (FR), 86 (CN), 91 (IN), 81 (JP)
    two = digits[:2]
    if two in ("44", "61", "49", "33", "86", "91", "81", "82", "34", "39", "55", "52", "65"):
        return 2
    # Common 3-digit: 852 (HK), 353 (IE), 972 (IL), 971 (UAE), 966 (SA)
    three = digits[:3]
    if three in ("852", "353", "972", "971", "966", "886", "855", "856"):
        return 3
    # Default: assume 2-digit country code
    return 2
