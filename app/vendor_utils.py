"""Vendor name normalization and card enrichment helpers."""
import re


# Legal entity suffixes only — conservative to avoid stripping name parts
_SUFFIXES = [
    "incorporated", "corporation", "limited", "company",
    "inc.", "inc", "llc.", "llc", "ltd.", "ltd", "corp.", "corp",
    "co.", "l.l.c.", "l.l.c", "p.l.c.", "plc",
    "gmbh", "s.a.", "sa", "ag", "b.v.", "bv", "n.v.", "nv",
    "pty", "pvt",
]

# Compile patterns
_SUFFIX_PATTERN = re.compile(
    r'\b(?:' + '|'.join(re.escape(s) for s in _SUFFIXES) + r')\.?\s*$',
    re.IGNORECASE
)


def normalize_vendor_name(name: str) -> str:
    """Normalize a vendor name for matching.

    - Lowercase
    - Strip common suffixes (Inc., LLC, Ltd., Corp., etc.)
    - Remove trailing punctuation and extra whitespace
    - Strip leading 'the'

    Examples:
        "Mouser Electronics, Inc." → "mouser electronics"
        "Arrow Electronics"        → "arrow electronics"
        "Digi-Key Corp."          → "digi-key"
        "The Phoenix Company LLC" → "phoenix"
    """
    if not name:
        return ""
    n = name.strip().lower()
    # Remove trailing comma before suffix
    n = re.sub(r',\s*$', '', n)
    # Strip suffixes (may need multiple passes)
    for _ in range(3):
        prev = n
        n = re.sub(r',\s*$', '', n)  # trailing comma
        n = _SUFFIX_PATTERN.sub('', n).strip()
        n = re.sub(r'[,.\-]+$', '', n).strip()  # trailing punctuation
        if n == prev:
            break
    # Strip leading "the "
    n = re.sub(r'^the\s+', '', n)
    # Collapse whitespace
    n = re.sub(r'\s+', ' ', n).strip()
    return n


def merge_emails_into_card(card, new_emails: list[str]) -> int:
    """Merge new emails into a VendorCard. Returns count of emails added.

    Deduplicates case-insensitively. Preserves existing order.
    """
    if not new_emails:
        return 0
    existing = set(e.lower() for e in (card.emails or []))
    added = 0
    merged = list(card.emails or [])
    for email in new_emails:
        email = email.strip().lower() if email else ""
        if not email or "@" not in email or email in existing:
            continue
        merged.append(email)
        existing.add(email)
        added += 1
    if added:
        card.emails = merged
    return added


def merge_phones_into_card(card, new_phones: list[str]) -> int:
    """Merge new phones into a VendorCard. Returns count of phones added.

    Deduplicates by digit content.
    """
    if not new_phones:
        return 0
    existing_digits = {re.sub(r'\D', '', p) for p in (card.phones or [])}
    added = 0
    merged = list(card.phones or [])
    for phone in new_phones:
        phone = phone.strip() if phone else ""
        digits = re.sub(r'\D', '', phone)
        if not phone or len(digits) < 7 or digits in existing_digits:
            continue
        merged.append(phone)
        existing_digits.add(digits)
        added += 1
    if added:
        card.phones = merged
    return added
