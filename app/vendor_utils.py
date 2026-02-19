"""Vendor name normalization, card enrichment, and fuzzy matching helpers."""

import re
from typing import Optional


# Legal entity suffixes only — conservative to avoid stripping name parts
# Ordered longest-first to avoid partial matches (e.g. "s.a.s." before "s.a.")
_SUFFIXES = [
    "incorporated",
    "corporation",
    "limited",
    "company",
    "inc.",
    "inc",
    "llc.",
    "llc",
    "ltd.",
    "ltd",
    "corp.",
    "corp",
    "co.",
    "l.l.c.",
    "l.l.c",
    "p.l.c.",
    "plc",
    "gmbh",
    "s.a.s.",
    "s.a.s",
    "s.r.l.",
    "s.r.l",
    "s.p.a.",
    "s.p.a",
    "s.a.",
    "sa",
    "ag",
    "b.v.",
    "bv",
    "n.v.",
    "nv",
    "k.k.",
    "k.k",
    "a.s.",
    "a.s",
    "a/s",
    "pty",
    "pvt",
    "sp.z o.o.",
    "sp. z o.o.",
    "sp.z o.o",
    "sp. z o.o",
    "e.k.",
    "e.k",
    "ohg",
    "kg",
    "ab",
    "oy",
    "oyj",
    "aps",
]

# Compile pattern — use word boundary OR punctuation/space before suffix
# to avoid stripping fragments like "technologyco"
_SUFFIX_PATTERN = re.compile(
    r"(?:^|\s|,\s*)(?:" + "|".join(re.escape(s) for s in _SUFFIXES) + r")\.?\s*$",
    re.IGNORECASE,
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
    n = re.sub(r",\s*$", "", n)
    # Strip suffixes (may need multiple passes)
    for _ in range(3):
        prev = n
        n = re.sub(r",\s*$", "", n)  # trailing comma
        n = _SUFFIX_PATTERN.sub("", n).strip()
        n = re.sub(r"[,.\-]+$", "", n).strip()  # trailing punctuation
        if n == prev:
            break
    # Strip leading "the "
    n = re.sub(r"^the\s+", "", n)
    # Collapse whitespace
    n = re.sub(r"\s+", " ", n).strip()
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
    existing_digits = {re.sub(r"\D", "", p) for p in (card.phones or [])}
    added = 0
    merged = list(card.phones or [])
    for phone in new_phones:
        phone = phone.strip() if phone else ""
        digits = re.sub(r"\D", "", phone)
        if not phone or len(digits) < 7 or digits in existing_digits:
            continue
        merged.append(phone)
        existing_digits.add(digits)
        added += 1
    if added:
        card.phones = merged
    return added


def fuzzy_match_vendor(query: str, candidates: list[str], threshold: int = 80) -> list[dict]:
    """Fuzzy match a vendor name against a list of candidate names.

    Returns list of {"name": str, "score": int} sorted by score descending.
    Only returns matches at or above the threshold.
    """
    from thefuzz import fuzz

    query_norm = normalize_vendor_name(query)
    if not query_norm:
        return []

    results = []
    for name in candidates:
        name_norm = normalize_vendor_name(name)
        if not name_norm:
            continue
        score = fuzz.token_sort_ratio(query_norm, name_norm)
        if score >= threshold:
            results.append({"name": name, "score": score})

    results.sort(key=lambda x: x["score"], reverse=True)
    return results


def find_vendor_dedup_candidates(db, threshold: int = 85, limit: int = 50) -> list[dict]:
    """Find potential duplicate vendor cards using fuzzy matching.

    Returns groups of vendors that may be duplicates, sorted by match score.
    """
    from thefuzz import fuzz
    from .models import VendorCard

    cards = (
        db.query(VendorCard.id, VendorCard.display_name, VendorCard.normalized_name, VendorCard.sighting_count)
        .order_by(VendorCard.sighting_count.desc().nullslast())
        .limit(500)
        .all()
    )

    seen_pairs: set[tuple] = set()
    candidates = []

    for i, card_a in enumerate(cards):
        for card_b in cards[i + 1:]:
            pair_key = (min(card_a.id, card_b.id), max(card_a.id, card_b.id))
            if pair_key in seen_pairs:
                continue

            score = fuzz.token_sort_ratio(card_a.normalized_name, card_b.normalized_name)
            if score >= threshold:
                seen_pairs.add(pair_key)
                candidates.append({
                    "vendor_a": {"id": card_a.id, "name": card_a.display_name, "sightings": card_a.sighting_count or 0},
                    "vendor_b": {"id": card_b.id, "name": card_b.display_name, "sightings": card_b.sighting_count or 0},
                    "score": score,
                })

            if len(candidates) >= limit:
                break
        if len(candidates) >= limit:
            break

    candidates.sort(key=lambda x: x["score"], reverse=True)
    return candidates
