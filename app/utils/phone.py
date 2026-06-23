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
from phonenumbers import NumberParseException


def normalize_e164(raw: str | None, default_region: str = "US") -> str | None:
    """Parse *raw* and return the E.164 representation, or None.

    LENIENT: never raises. Returns None for blank/garbage/too-short input so
    @validates hooks and migration backfills can call this without try/except.

    Args:
        raw: Any phone string — "(415) 555-1234", "4155551234", "+441234567890",
             "ext 123", "", None.
        default_region: ISO 3166-1 alpha-2 region assumed when no country code is
            present.  Defaults to "US".

    Returns:
        E.164 string like "+14155551234", or None if unparseable / invalid.
    """
    if not raw or not raw.strip():
        return None
    try:
        parsed = phonenumbers.parse(raw, default_region)
    except NumberParseException:
        return None
    if not phonenumbers.is_valid_number(parsed):
        return None
    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
