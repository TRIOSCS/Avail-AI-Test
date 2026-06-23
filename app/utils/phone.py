"""E.164 phone number normalization — parse-and-normalize using Google libphonenumber.

Provides normalize_e164() used by:
- @validates hooks on Company, CustomerSite, SiteContact, VendorContact, VendorCard
  to keep normalized_phone / normalized_phones columns in sync on every write.
- Migration 130 backfill.
- WS2b phone matcher (to be built).

Depends on: phonenumbers (Google libphonenumber Python port, pure-Python).
"""

from __future__ import annotations

import phonenumbers


def normalize_e164(raw: object, default_region: str = "US") -> str | None:
    """Parse *raw* and return the E.164 representation, or None.

    TOTAL: never raises for any input type. Returns None for blank/garbage/
    too-short input, None values, and non-string inputs that cannot be coerced
    to a valid phone string — so @validates hooks and migration backfills can
    call this without try/except.

    Args:
        raw: Any value — phone strings like "(415) 555-1234", "4155551234",
             "+441234567890", integers, None, empty strings, garbage.
             Non-string values are coerced via str() before parsing.
        default_region: ISO 3166-1 alpha-2 region assumed when no country code is
            present.  Defaults to "US".

    Returns:
        E.164 string like "+14155551234", or None if unparseable / invalid.
    """
    if raw is None:
        return None
    if not isinstance(raw, str):
        raw = str(raw)
    raw = raw.strip()
    if not raw:
        return None
    try:
        parsed = phonenumbers.parse(raw, default_region)
        if not phonenumbers.is_valid_number(parsed):
            return None
        return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
    except Exception:
        return None
