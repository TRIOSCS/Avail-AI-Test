"""Phone normalization sweep — site_contacts + vendor_contacts.

Fixes 2 phone-in-site_name records missed by 055, then normalizes
international phone numbers across site_contacts and vendor_contacts.

Revision ID: 056
Revises: 055
Create Date: 2026-03-07
"""

import re

from sqlalchemy import text

from alembic import op

revision = "056"
down_revision = "055"
branch_labels = None
depends_on = None


# Known country code prefixes (longest match first within each length)
# 3-digit codes checked before 2-digit to avoid false matches
_CC3 = {
    "852",
    "853",
    "855",
    "856",
    "880",
    "886",  # HK, Macau, Cambodia, Laos, BD, TW
    "353",
    "354",
    "355",
    "356",
    "357",
    "358",
    "359",  # IE, IS, AL, MT, CY, FI, BG
    "370",
    "371",
    "372",
    "373",
    "374",
    "375",
    "376",  # LT, LV, EE, MD, AM, BY, AD
    "380",
    "381",
    "382",
    "383",
    "385",
    "386",
    "387",  # UA, RS, ME, XK, HR, SI, BA
    "420",
    "421",  # CZ, SK
    "960",
    "961",
    "962",
    "963",
    "964",
    "965",
    "966",
    "967",
    "968",
    "970",
    "971",
    "972",
    "973",
    "974",
    # MV, LB, JO, SY, IQ, KW, SA, YE, OM, PS, UAE, IL, BH, QA
}
_CC2 = {
    "20",
    "27",  # EG, ZA
    "30",
    "31",
    "32",
    "33",
    "34",
    "36",
    "39",  # GR, NL, BE, FR, ES, HU, IT
    "40",
    "41",
    "43",
    "44",
    "45",
    "46",
    "47",
    "48",
    "49",  # RO, CH, AT, UK, DK, SE, NO, PL, DE
    "51",
    "52",
    "53",
    "54",
    "55",
    "56",
    "57",
    "58",  # PE, MX, CU, AR, BR, CL, CO, VE
    "60",
    "61",
    "62",
    "63",
    "64",
    "65",
    "66",  # MY, AU, ID, PH, NZ, SG, TH
    "70",
    "71",
    "72",
    "73",
    "74",
    "75",
    "76",
    "77",
    "78",
    "79",  # RU/KZ block
    "81",
    "82",
    "84",
    "86",  # JP, KR, VN, CN
    "90",
    "91",
    "92",
    "93",
    "94",
    "95",
    "98",  # TR, IN, PK, AF, LK, MM, IR
}
_CC1 = {"1"}  # US/CA


def _parse_scientific(raw: str) -> str | None:
    """Convert Excel scientific notation like '4.40175E+13' back to digits."""
    m = re.match(r"^(\d+\.?\d*)[Ee]\+?(\d+)$", raw.strip())
    if m:
        return str(int(float(raw.strip())))
    return None


def _normalize_intl(raw: str) -> str | None:
    """Normalize an international phone number to E.164.

    Handles numbers without + prefix by matching known country code prefixes. Returns
    None if unparseable.
    """
    if not raw or not raw.strip():
        return None

    cleaned = raw.strip()

    # Already E.164
    if cleaned.startswith("+"):
        return None  # Already normalized — caller skips these

    # Handle scientific notation from Excel
    sci = _parse_scientific(cleaned)
    if sci:
        cleaned = sci

    # Take first number before "or" / "|" / ";" separators
    cleaned = re.split(r"\s+or\s+|\|{1,2}|;", cleaned, flags=re.IGNORECASE)[0]

    # Strip trailing parenthesized labels like (D), (HQ), (cell), (M)
    cleaned = re.sub(r"\s*\([A-Za-z]+\s*(?:ofc|office)?\)\s*$", "", cleaned)

    # Strip trailing text labels: "exec ofc", "wrong no. person", "Direct", etc.
    cleaned = re.sub(
        r"\s+(?:exec\s+ofc|wrong\s+no\.?\s*\w*|direct|cell|mobile|fax)\s*$", "", cleaned, flags=re.IGNORECASE
    )

    # Strip extension markers (Ext:, EX:, ext., ext,, xt, x, #) and everything after
    cleaned = re.split(r"(?i)\s*[-,]?\s*(?:ext\.?|xt\.?|EX)\s*[:.,]?\s*\d*.*$", cleaned)[0]
    # Simpler "x" or "#" extension at end (only if followed by digits)
    cleaned = re.split(r"\s+[x#]\s*\d+\s*$", cleaned, flags=re.IGNORECASE)[0]

    # Strip trailing " - " with text
    cleaned = re.sub(r"\s*-\s*$", "", cleaned)

    # Reject strings with letters (job titles, names stored in phone field)
    if re.search(r"[a-zA-Z]", cleaned):
        return None

    # Remove trailing non-digit junk like ) or special chars
    cleaned = re.sub(r"[^\d]+$", "", cleaned)

    # Handle country code in parens like (65) 6843 9237 → 65 6843 9237
    cleaned = re.sub(r"^\((\d{2,3})\)\s*", r"\1", cleaned)

    digits = re.sub(r"\D", "", cleaned)

    if not digits or len(digits) < 7:
        return None

    # US/CA: 10 digits → +1XXXXXXXXXX
    if len(digits) == 10 and digits[0] in "23456789":
        return f"+1{digits}"

    # US/CA: 11 digits starting with 1
    if len(digits) == 11 and digits[0] == "1" and digits[1] in "23456789":
        return f"+{digits}"

    # Try matching known country code prefixes (3-digit first, then 2, then 1)
    for cc_len, cc_set in [(3, _CC3), (2, _CC2), (1, _CC1)]:
        prefix = digits[:cc_len]
        if prefix in cc_set:
            subscriber = digits[cc_len:]
            # Sanity: subscriber should be 5-13 digits
            if 5 <= len(subscriber) <= 13:
                return f"+{digits}"

    # UK local format: starts with 0, 10-11 digits total → +44 + rest
    if digits.startswith("0") and 10 <= len(digits) <= 11:
        return f"+44{digits[1:]}"

    # Long number (12+ digits) that didn't match a CC — likely international
    if len(digits) >= 12:
        return f"+{digits}"

    return None


def _fix_phone_in_site_names(conn):
    """Fix 2 specific phone-in-site_name records missed by 055."""
    conn.execute(
        text("""
        UPDATE customer_sites
        SET site_name = 'Natus Medical', contact_phone = '+19252236700'
        WHERE id = 521
    """)
    )
    conn.execute(
        text("""
        UPDATE customer_sites
        SET site_name = 'Verifone - Israel', contact_phone = '+97239029740'
        WHERE id = 583
    """)
    )


def _normalize_table_phones(conn, table: str, col: str):
    """Normalize all un-prefixed phone numbers in a table column."""
    rows = conn.execute(
        text(f"""
        SELECT id, {col} FROM {table}
        WHERE {col} IS NOT NULL AND {col} <> ''
          AND {col} NOT LIKE '+%%'
    """)
    ).fetchall()
    updated = 0
    for row in rows:
        raw = getattr(row, col)
        normalized = _normalize_intl(raw)
        if normalized:
            conn.execute(text(f"UPDATE {table} SET {col} = :val WHERE id = :id"), {"val": normalized, "id": row.id})
            updated += 1
    return updated


def upgrade():
    conn = op.get_bind()
    _fix_phone_in_site_names(conn)
    _normalize_table_phones(conn, "site_contacts", "phone")
    _normalize_table_phones(conn, "vendor_contacts", "phone")
    _normalize_table_phones(conn, "vendor_contacts", "phone_mobile")


def downgrade():
    pass  # Data-only, no rollback
