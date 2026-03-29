"""Vendor name normalization, card enrichment, and fuzzy matching helpers."""

import re

# Generic email domains — not useful for vendor enrichment or domain matching.
# Shared by: app.routers.vendors, app.services.activity_service
GENERIC_EMAIL_DOMAINS: frozenset[str] = frozenset(
    {
        "gmail.com",
        "yahoo.com",
        "hotmail.com",
        "outlook.com",
        "aol.com",
        "icloud.com",
        "live.com",
        "msn.com",
        "protonmail.com",
        "mail.com",
        "yandex.com",
        "zoho.com",
        "gmx.com",
        "fastmail.com",
    }
)

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


def fuzzy_score_vendor(name_a: str, name_b: str) -> int:
    """Return rapidfuzz token_sort_ratio between two vendor names (normalized).

    Shared scoring function used by fuzzy_match_vendor and find_vendor_dedup_candidates
    to ensure consistent fuzzy matching across the codebase.

    Called by: fuzzy_match_vendor, find_vendor_dedup_candidates, utils/vendor_helpers.py
    Depends on: rapidfuzz, normalize_vendor_name
    """
    from rapidfuzz import fuzz

    a = normalize_vendor_name(name_a)
    b = normalize_vendor_name(name_b)
    if not a or not b:
        return 0
    return int(fuzz.token_sort_ratio(a, b))


def fuzzy_match_vendor(query: str, candidates: list[str], threshold: int = 80) -> list[dict]:
    """Fuzzy match a vendor name against a list of candidate names.

    Returns list of {"name": str, "score": int} sorted by score descending. Only returns
    matches at or above the threshold.
    """
    if not normalize_vendor_name(query):
        return []

    results = []
    for name in candidates:
        score = fuzzy_score_vendor(query, name)
        if score >= threshold:
            results.append({"name": name, "score": score})

    results.sort(key=lambda x: x["score"], reverse=True)  # type: ignore[arg-type,return-value]
    return results


def _enrich_with_vendor_cards(results: dict, db) -> None:
    """Add vendor card rating info to search results.

    Enriches each sighting dict with vendor_card summary data (avg_rating,
    review_count, vendor_score, etc.).  Filters blacklisted vendors and
    garbage vendor names.  Also auto-creates VendorCard rows for new vendors
    and merges harvested contact info back into the card.

    Called by: app.routers.requisitions.requirements (search endpoints)
    Depends on: models.VendorCard, models.VendorReview, vendor_utils helpers
    """
    from loguru import logger

    from .models import VendorCard, VendorReview

    all_vendor_names: set[str] = set()
    for group in results.values():
        for s in group.get("sightings", []):
            if s.get("vendor_name"):
                all_vendor_names.add(s["vendor_name"])
    if not all_vendor_names:
        return

    # Build normalized name map
    norm_map: dict[str, list[str]] = {}
    for name in all_vendor_names:
        norm = normalize_vendor_name(name)
        norm_map.setdefault(norm, []).append(name)

    cards = db.query(VendorCard).filter(VendorCard.normalized_name.in_(norm_map.keys())).all()
    card_by_norm = {c.normalized_name: c for c in cards}

    # Auto-create cards for vendors we haven't seen before
    new_cards_added = False
    for norm, names in norm_map.items():
        if norm not in card_by_norm and norm:
            card = VendorCard(
                normalized_name=norm,
                display_name=names[0],
                emails=[],
                phones=[],
                sighting_count=0,
            )
            db.add(card)
            cards.append(card)
            card_by_norm[norm] = card
            new_cards_added = True
    if new_cards_added:
        db.flush()  # Assign IDs to new cards

    # Batch fetch reviews
    card_ids = [c.id for c in cards]
    all_reviews = db.query(VendorReview).filter(VendorReview.vendor_card_id.in_(card_ids)).all() if card_ids else []
    reviews_by_card: dict[int, list] = {}
    for r in all_reviews:
        reviews_by_card.setdefault(r.vendor_card_id, []).append(r)

    # Build summary cache
    summary_cache: dict[str, dict] = {}
    for norm, card in card_by_norm.items():
        revs = reviews_by_card.get(card.id, [])
        avg = round(sum(r.rating for r in revs) / len(revs), 1) if revs else None
        summary_cache[norm] = {
            "card_id": card.id,
            "avg_rating": avg,
            "review_count": len(revs),
            "vendor_score": round(card.vendor_score, 1) if card.vendor_score is not None else None,
            "is_new_vendor": card.is_new_vendor if card.is_new_vendor is not None else True,
            "engagement_score": round(card.vendor_score, 1) if card.vendor_score is not None else None,
            "has_emails": bool(card.emails),
            "email_count": len(card.emails or []),
            "is_blacklisted": card.is_blacklisted or False,
        }

    # Count distinct MPNs per vendor and harvest contact info
    mpns_by_norm: dict[str, set] = {}
    emails_by_norm: dict[str, set] = {}
    phones_by_norm: dict[str, set] = {}
    websites_by_norm: dict[str, str] = {}
    for group in results.values():
        for s in group.get("sightings", []):
            if not s.get("is_historical") and not s.get("is_material_history") and s.get("vendor_name"):
                n = normalize_vendor_name(s["vendor_name"])
                mpns_by_norm.setdefault(n, set()).add((s.get("mpn_matched") or "").lower())
                if s.get("vendor_email"):
                    emails_by_norm.setdefault(n, set()).add(s["vendor_email"].strip().lower())
                if s.get("vendor_phone"):
                    phones_by_norm.setdefault(n, set()).add(s["vendor_phone"].strip())
                if s.get("vendor_url"):
                    websites_by_norm.setdefault(n, s["vendor_url"])

    cards_dirty = False
    for card in cards:
        mpn_set = mpns_by_norm.get(card.normalized_name, set())
        count = len(mpn_set - {""})
        if count > 0:
            card.sighting_count = (card.sighting_count or 0) + count
            cards_dirty = True

        new_emails = list(emails_by_norm.get(card.normalized_name, set()))
        if merge_emails_into_card(card, new_emails) > 0:
            cards_dirty = True

        new_phones = list(phones_by_norm.get(card.normalized_name, set()))
        if merge_phones_into_card(card, new_phones) > 0:
            cards_dirty = True

        if not card.website and card.normalized_name in websites_by_norm:
            card.website = websites_by_norm[card.normalized_name]
            cards_dirty = True

    if cards_dirty:
        try:
            db.commit()
        except Exception:
            logger.error("Failed to commit vendor card updates during search enrichment", exc_info=True)
            db.rollback()
        # Refresh summary cache with updated email counts
        for norm, card in card_by_norm.items():
            if norm in summary_cache:
                summary_cache[norm]["has_emails"] = bool(card.emails)
                summary_cache[norm]["email_count"] = len(card.emails or [])

    # Enrich each sighting + filter blacklisted + garbage vendors
    _GARBAGE_VENDORS = {"no seller listed", "no seller", "n/a", "unknown", ""}
    empty_summary = {
        "card_id": None,
        "avg_rating": None,
        "review_count": 0,
        "vendor_score": None,
        "is_new_vendor": True,
        "engagement_score": None,
        "has_emails": False,
        "email_count": 0,
        "is_blacklisted": False,
    }
    for group in results.values():
        enriched = []
        blacklisted_count = 0
        for s in group.get("sightings", []):
            vname = (s.get("vendor_name") or "").strip()
            if vname.lower() in _GARBAGE_VENDORS:
                continue
            norm = normalize_vendor_name(vname)
            summary = summary_cache.get(norm, empty_summary)
            if summary.get("is_blacklisted"):
                blacklisted_count += 1
                continue
            s["vendor_card"] = summary
            enriched.append(s)
        group["sightings"] = enriched
        group["blacklisted_count"] = blacklisted_count


def find_vendor_dedup_candidates(db, threshold: int = 85, limit: int = 50) -> list[dict]:
    """Find potential duplicate vendor cards using fuzzy matching.

    Returns groups of vendors that may be duplicates, sorted by match score.
    """
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
        for card_b in cards[i + 1 :]:
            pair_key = (min(card_a.id, card_b.id), max(card_a.id, card_b.id))
            if pair_key in seen_pairs:
                continue

            score = fuzzy_score_vendor(card_a.normalized_name, card_b.normalized_name)
            if score >= threshold:
                seen_pairs.add(pair_key)
                candidates.append(
                    {
                        "vendor_a": {
                            "id": card_a.id,
                            "name": card_a.display_name,
                            "sightings": card_a.sighting_count or 0,
                        },
                        "vendor_b": {
                            "id": card_b.id,
                            "name": card_b.display_name,
                            "sightings": card_b.sighting_count or 0,
                        },
                        "score": score,
                    }
                )

            if len(candidates) >= limit:
                break
        if len(candidates) >= limit:
            break

    candidates.sort(key=lambda x: x["score"], reverse=True)  # type: ignore[arg-type,return-value]
    return candidates
