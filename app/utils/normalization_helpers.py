"""Additional normalization helpers — phone, contact names, country/state codes.

Pure Python, no AI. Used by:
  - scripts/data_cleanup.py (one-time bulk cleanup)
  - schemas/*.py (write-path validation)
"""

import re

# ── Phone normalization ──────────────────────────────────────────────

# North American Numbering Plan: 10-digit numbers starting with area code
_NANP_PATTERN = re.compile(r"^1?(\d{10})$")


def normalize_phone_e164(raw: str | None) -> str | None:
    """Normalize phone number to E.164-ish format.

    - Strips non-digit characters (except leading +)
    - Detects country from prefix or assumes US/Canada for 10-digit
    - Returns +{country}{number} or None if too short

    Examples:
        "(555) 123-4567"     → "+15551234567"
        "+44 20 7946 0958"   → "+442079460958"
        "1-800-555-0100"     → "+18005550100"
        "ext 123"            → None
    """
    if not raw:
        return None

    s = str(raw).strip()
    if not s:
        return None

    # Strip extensions before processing
    s = re.split(r"(?i)\s*(?:ext\.?|x|extension)\s*:?\s*\d+", s)[0].strip()

    # Preserve leading +
    has_plus = s.startswith("+")

    # Strip to digits only
    digits = re.sub(r"\D", "", s)

    if len(digits) < 7:
        return None

    if has_plus:
        # Already has country code
        return f"+{digits}"

    # NANP: 10 digits (or 11 starting with 1)
    m = _NANP_PATTERN.match(digits)
    if m:
        return f"+1{m.group(1)}"

    # 11+ digits — assume country code is included
    if len(digits) >= 11:
        return f"+{digits}"

    # 7-10 digits — assume US domestic (missing area code or partial)
    # Return as-is with +1 prefix for consistency
    return f"+1{digits}"


# ── Country normalization ────────────────────────────────────────────

_COUNTRY_MAP = {
    # Full names → ISO 3166-1 alpha-2
    "united states": "US",
    "united states of america": "US",
    "usa": "US",
    "u.s.a.": "US",
    "u.s.": "US",
    "us": "US",
    "america": "US",
    "canada": "CA",
    "can": "CA",
    "ca": "CA",
    "united kingdom": "GB",
    "uk": "GB",
    "england": "GB",
    "great britain": "GB",
    "germany": "DE",
    "deutschland": "DE",
    "de": "DE",
    "france": "FR",
    "fr": "FR",
    "italy": "IT",
    "italia": "IT",
    "it": "IT",
    "spain": "ES",
    "espana": "ES",
    "españa": "ES",
    "es": "ES",
    "japan": "JP",
    "jp": "JP",
    "china": "CN",
    "cn": "CN",
    "prc": "CN",
    "south korea": "KR",
    "korea": "KR",
    "kr": "KR",
    "taiwan": "TW",
    "tw": "TW",
    "india": "IN",
    "in": "IN",
    "australia": "AU",
    "au": "AU",
    "brazil": "BR",
    "br": "BR",
    "mexico": "MX",
    "mx": "MX",
    "netherlands": "NL",
    "holland": "NL",
    "nl": "NL",
    "switzerland": "CH",
    "ch": "CH",
    "sweden": "SE",
    "se": "SE",
    "norway": "NO",
    "no": "NO",
    "denmark": "DK",
    "dk": "DK",
    "finland": "FI",
    "fi": "FI",
    "ireland": "IE",
    "ie": "IE",
    "belgium": "BE",
    "be": "BE",
    "austria": "AT",
    "at": "AT",
    "poland": "PL",
    "pl": "PL",
    "portugal": "PT",
    "pt": "PT",
    "czech republic": "CZ",
    "czechia": "CZ",
    "cz": "CZ",
    "hungary": "HU",
    "hu": "HU",
    "singapore": "SG",
    "sg": "SG",
    "hong kong": "HK",
    "hk": "HK",
    "malaysia": "MY",
    "my": "MY",
    "thailand": "TH",
    "th": "TH",
    "philippines": "PH",
    "ph": "PH",
    "vietnam": "VN",
    "vn": "VN",
    "indonesia": "ID",
    "id": "ID",
    "israel": "IL",
    "il": "IL",
    "turkey": "TR",
    "türkiye": "TR",
    "tr": "TR",
    "south africa": "ZA",
    "za": "ZA",
    "new zealand": "NZ",
    "nz": "NZ",
    "russia": "RU",
    "ru": "RU",
    "uae": "AE",
    "united arab emirates": "AE",
    "ae": "AE",
    "saudi arabia": "SA",
    "sa": "SA",
    "argentina": "AR",
    "ar": "AR",
    "colombia": "CO",
    "co": "CO",
    "chile": "CL",
    "cl": "CL",
    "romania": "RO",
    "ro": "RO",
    "greece": "GR",
    "gr": "GR",
    "ukraine": "UA",
    "ua": "UA",
    "slovakia": "SK",
    "sk": "SK",
    "slovenia": "SI",
    "si": "SI",
    "croatia": "HR",
    "hr": "HR",
    "serbia": "RS",
    "rs": "RS",
    "bulgaria": "BG",
    "bg": "BG",
    "lithuania": "LT",
    "lt": "LT",
    "latvia": "LV",
    "lv": "LV",
    "estonia": "EE",
    "ee": "EE",
    "luxembourg": "LU",
    "lu": "LU",
    "iceland": "IS",
    "pakistan": "PK",
    "pk": "PK",
    "bangladesh": "BD",
    "bd": "BD",
    "egypt": "EG",
    "eg": "EG",
    "nigeria": "NG",
    "ng": "NG",
    "kenya": "KE",
    "ke": "KE",
    "peru": "PE",
    "pe": "PE",
    "costa rica": "CR",
    "puerto rico": "PR",
}

# Valid ISO 3166-1 alpha-2 codes (for pass-through)
_VALID_ISO2 = {v for v in _COUNTRY_MAP.values()}


def normalize_country(raw: str | None) -> str | None:
    """Normalize country name/code to ISO 3166-1 alpha-2.

    Examples:
        "United States" → "US"
        "USA"           → "US"
        "DE"            → "DE"
        "Deutschland"   → "DE"
        None            → None
    """
    if not raw:
        return None

    s = str(raw).strip()
    if not s:
        return None

    # Already a valid 2-letter code?
    upper = s.upper()
    if upper in _VALID_ISO2:
        return upper

    # Lookup by lowercase full name
    result = _COUNTRY_MAP.get(s.lower())
    if result:
        return result

    # Return original if we can't normalize (don't lose data)
    return s


# ── US State normalization ───────────────────────────────────────────

_US_STATE_MAP = {
    "alabama": "AL",
    "alaska": "AK",
    "arizona": "AZ",
    "arkansas": "AR",
    "california": "CA",
    "colorado": "CO",
    "connecticut": "CT",
    "delaware": "DE",
    "florida": "FL",
    "georgia": "GA",
    "hawaii": "HI",
    "idaho": "ID",
    "illinois": "IL",
    "indiana": "IN",
    "iowa": "IA",
    "kansas": "KS",
    "kentucky": "KY",
    "louisiana": "LA",
    "maine": "ME",
    "maryland": "MD",
    "massachusetts": "MA",
    "michigan": "MI",
    "minnesota": "MN",
    "mississippi": "MS",
    "missouri": "MO",
    "montana": "MT",
    "nebraska": "NE",
    "nevada": "NV",
    "new hampshire": "NH",
    "new jersey": "NJ",
    "new mexico": "NM",
    "new york": "NY",
    "north carolina": "NC",
    "north dakota": "ND",
    "ohio": "OH",
    "oklahoma": "OK",
    "oregon": "OR",
    "pennsylvania": "PA",
    "rhode island": "RI",
    "south carolina": "SC",
    "south dakota": "SD",
    "tennessee": "TN",
    "texas": "TX",
    "utah": "UT",
    "vermont": "VT",
    "virginia": "VA",
    "washington": "WA",
    "west virginia": "WV",
    "wisconsin": "WI",
    "wyoming": "WY",
    "district of columbia": "DC",
    "d.c.": "DC",
    "dc": "DC",
    # Territories
    "puerto rico": "PR",
    "guam": "GU",
    "virgin islands": "VI",
    "american samoa": "AS",
}

_VALID_STATE_CODES = {v for v in _US_STATE_MAP.values()}


def normalize_us_state(raw: str | None) -> str | None:
    """Normalize US state name to 2-letter abbreviation.

    Examples:
        "California" → "CA"
        "ca"         → "CA"
        "New York"   → "NY"
        "TX"         → "TX"
        None         → None
    """
    if not raw:
        return None

    s = str(raw).strip()
    if not s:
        return None

    # Already a valid 2-letter code?
    upper = s.upper()
    if upper in _VALID_STATE_CODES:
        return upper

    # Lookup by lowercase full name
    result = _US_STATE_MAP.get(s.lower())
    if result:
        return result

    # Return original if we can't normalize
    return s


# ── Encoding fix ─────────────────────────────────────────────────────

# Common mojibake patterns from UTF-8 → Latin-1 → UTF-8 round-trips
_ENCODING_FIXES = {
    "\u00e2\u0080\u0099": "'",
    "\u00e2\u0080\u0098": "'",
    "\u00e2\u0080\u009c": '"',
    "\u00e2\u0080\u009d": '"',
    "\u00e2\u0080\u0094": "\u2014",  # em-dash
    "\u00e2\u0080\u0093": "\u2013",  # en-dash
    "\u00c3\u00a9": "\u00e9",  # e-acute
    "\u00c3\u00a8": "\u00e8",  # e-grave
    "\u00c3\u00bc": "\u00fc",  # u-umlaut
    "\u00c3\u00b6": "\u00f6",  # o-umlaut
    "\u00c3\u00a4": "\u00e4",  # a-umlaut
    "\u00c3\u00b1": "\u00f1",  # n-tilde
    "int?l": "Int'l",
    "INT?L": "Int'l",
    "Int?l": "Int'l",
}


def fix_encoding(text: str | None) -> str | None:
    """Fix common encoding corruption in vendor names.

    Examples:
        "int?l"  → "Int'l"
        "Müller" → "Müller" (no change if clean)
    """
    if not text:
        return text

    result = text
    for bad, good in _ENCODING_FIXES.items():
        if bad in result:
            result = result.replace(bad, good)

    return result
