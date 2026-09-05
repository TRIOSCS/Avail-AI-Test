"""EBay Browse API result parser.

Turns a raw ``item_summary/search`` JSON payload into structured EbaySighting
dataclass instances, applying the two filters that keep eBay noise out of the
sightings table:

1. STRICT PART-NUMBER MATCH — eBay's ``q=`` is a full-text search, so a query
   for "0F8NV" happily returns "Dell PowerEdge server 32GB kit". An item is
   kept only when the alphanumeric-normalized MPN appears inside the
   alphanumeric-normalized title. Dell-style 5-character part numbers are
   also accepted without their leading zero (the label reads 0F8NV, sellers
   type F8NV), which is the only variant eBay listings actually use.
2. CONDITION / BUYING-OPTION EXCLUSIONS — ``conditionId`` 7000 ("For parts or
   not working") is never a sourceable offer, and auctions are dropped unless
   the worker is configured to include them (a live auction price is not a
   quotable number).

No item detail page is ever fetched: everything below comes from the search
response itself.

Called by: worker loop (after search_client)
Depends on: app.utils.normalization (normalize_condition), app.utils (safe_float/int)
"""

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime

from loguru import logger

from ...utils import safe_float, safe_int
from ...utils.normalization import normalize_condition

# eBay conditionId for "For parts or not working" — never a sourceable offer.
FOR_PARTS_CONDITION_ID = "7000"

# eBay condition LABELS mapped explicitly, because the generic
# normalization._CONDITION_MAP does not know them: "Open box" has no keyword
# it recognizes at all, and "Certified - Refurbished" style labels must land on
# refurb rather than falling through to None.
_EBAY_CONDITION_MAP = {
    "new": "new",
    "new other": "new",
    "open box": "new",
    "seller refurbished": "refurb",
    "certified - refurbished": "refurb",
    "excellent - refurbished": "refurb",
    "very good - refurbished": "refurb",
    "good - refurbished": "refurb",
    "used": "used",
}

_NONALNUM = re.compile(r"[^A-Z0-9]")
# eBay appends a clarifier to some condition labels ("New other (see details)").
_TRAILING_PAREN = re.compile(r"\s*\([^)]*\)\s*$")

# Seller feedback at or above this percentage earns the confidence bonus.
GOOD_FEEDBACK_PCT = 98.0


@dataclass
class EbaySighting:
    """A single eBay listing that passed the strict part-number filter.

    Field names match what search_worker_base.sighting_writer.save_sightings reads off a
    parsed row (part_number / manufacturer / quantity / date_code / vendor_name), so the
    shared save skeleton works unchanged.
    """

    part_number: str = ""  # the QUEUED MPN, not the seller's typing
    manufacturer: str = ""  # eBay Browse does not reliably provide one
    date_code: str = ""  # never present on eBay listings
    vendor_name: str = ""  # seller.username
    quantity: int | None = None
    quantity_estimated: bool = False  # True when eBay gave an availability
    unit_price: float | None = None
    currency: str = "USD"
    condition: str | None = None  # new / refurb / used / None
    confidence: float = 0.0
    item_id: str = ""
    title: str = ""
    raw_condition: str = ""
    condition_id: str = ""
    seller_feedback_pct: float | None = None
    seller_feedback_score: int | None = None
    item_location_country: str = ""
    buying_options: list[str] = field(default_factory=list)
    click_url: str = ""
    image_url: str = ""
    fetched_at: str = ""


def normalize_for_match(text: str | None) -> str:
    """Uppercase and strip everything outside [A-Z0-9].

    "Dell 0F8NV / H730-Mini" -> "DELL0F8NVH730MINI".
    """
    if not text:
        return ""
    return _NONALNUM.sub("", str(text).upper())


def mpn_match_variants(mpn: str | None) -> set[str]:
    """Accepted normalized forms of ``mpn`` for the strict title match.

    Always the normalized MPN itself. Dell-style 5-character part numbers that carry the
    leading zero also accept the zero-less form ("0F8NV" -> "F8NV"): Dell prints 0F8NV
    on the label while sellers routinely list it as F8NV. The reverse direction needs no
    rule — a title containing "0F8NV" already contains "F8NV" as a substring.
    """
    norm = normalize_for_match(mpn)
    if not norm:
        return set()
    variants = {norm}
    if len(norm) == 5 and norm.startswith("0"):
        variants.add(norm[1:])
    return variants


def title_matches_mpn(title: str | None, variants: set[str]) -> bool:
    """True when any accepted MPN form is a substring of the normalized title."""
    norm_title = normalize_for_match(title)
    if not norm_title:
        return False
    return any(v in norm_title for v in variants)


def _is_whole_token(title: str | None, variants: set[str]) -> bool:
    """True when an accepted MPN form is a standalone word of the title.

    "... Controller 0F8NV Genuine" is a whole-token hit; "...0F8NVX..." is not.
    A whole-token hit is much stronger evidence than a substring hit, so it
    earns a confidence bonus.
    """
    tokens = {normalize_for_match(tok) for tok in re.split(r"\s+", title or "") if tok}
    return bool(tokens & variants)


def normalize_ebay_condition(raw: str | None) -> str | None:
    """Map an eBay condition label to new / refurb / used (or None).

    The explicit eBay label map wins; anything unknown falls through to the
    shared ``normalize_condition`` keyword matcher.
    """
    if not raw:
        return None
    cleaned = _TRAILING_PAREN.sub("", str(raw).strip().lower())
    cleaned = re.sub(r"\s+", " ", cleaned)
    mapped = _EBAY_CONDITION_MAP.get(cleaned)
    if mapped:
        return mapped
    return normalize_condition(raw)


def compute_confidence(*, quantity_estimated: bool, feedback_pct: float | None, whole_token: bool) -> float:
    """Score one listing on the 0.0-1.0 scale the Sighting CHECK constraint demands.

    0.55 base (a strict-matched eBay listing is real supply, but from an
    unvetted seller), +0.15 when eBay reported an actual available quantity,
    +0.15 for a seller at >= 98% feedback, +0.10 when the MPN is a whole title
    token rather than a substring. Capped at 0.95 — a marketplace listing is
    never certainty.
    """
    score = 0.55
    if quantity_estimated:
        score += 0.15
    if feedback_pct is not None and feedback_pct >= GOOD_FEEDBACK_PCT:
        score += 0.15
    if whole_token:
        score += 0.10
    return round(min(score, 0.95), 2)


def _estimated_quantity(item: dict) -> int | None:
    """First ``estimatedAvailabilities[].estimatedAvailableQuantity``, or None."""
    for avail in item.get("estimatedAvailabilities") or []:
        qty = safe_int(avail.get("estimatedAvailableQuantity"))
        if qty is not None:
            return qty
    return None


def parse_item_summaries(payload: dict | None, mpn: str, *, include_auctions: bool = False) -> list[EbaySighting]:
    """Parse a Browse API ``item_summary/search`` payload into EbaySighting rows.

    ``mpn`` is the QUEUED part number — it becomes each row's ``part_number``
    so the sighting records what we searched for, never the seller's spelling.
    Items are dropped when: the seller has no username, the title fails the
    strict part-number match, the condition is "For parts or not working", or
    the listing is auction-only and ``include_auctions`` is False.
    """
    if not payload:
        return []
    items = payload.get("itemSummaries") or []
    variants = mpn_match_variants(mpn)
    if not variants:
        logger.debug("EBAY parser: blank MPN — nothing to match against")
        return []

    now_iso = datetime.now(UTC).isoformat()
    rows: list[EbaySighting] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        seller = item.get("seller") or {}
        seller_name = (seller.get("username") or "").strip()
        if not seller_name:
            continue

        title = item.get("title") or ""
        if not title_matches_mpn(title, variants):
            continue

        condition_id = str(item.get("conditionId") or "").strip()
        if condition_id == FOR_PARTS_CONDITION_ID:
            continue

        buying_options = [str(b) for b in (item.get("buyingOptions") or [])]
        if not include_auctions and buying_options and all(b.upper() == "AUCTION" for b in buying_options):
            continue

        price_info = item.get("price") or {}
        est_qty = _estimated_quantity(item)
        feedback_pct = safe_float(seller.get("feedbackPercentage"))

        rows.append(
            EbaySighting(
                part_number=mpn,
                vendor_name=seller_name,
                quantity=est_qty if est_qty is not None else 1,
                quantity_estimated=est_qty is not None,
                unit_price=safe_float(price_info.get("value")),
                currency=price_info.get("currency") or "USD",
                condition=normalize_ebay_condition(item.get("condition")),
                confidence=compute_confidence(
                    quantity_estimated=est_qty is not None,
                    feedback_pct=feedback_pct,
                    whole_token=_is_whole_token(title, variants),
                ),
                item_id=str(item.get("itemId") or ""),
                title=title,
                raw_condition=str(item.get("condition") or ""),
                condition_id=condition_id,
                seller_feedback_pct=feedback_pct,
                seller_feedback_score=safe_int(seller.get("feedbackScore")),
                item_location_country=str((item.get("itemLocation") or {}).get("country") or ""),
                buying_options=buying_options,
                click_url=item.get("itemWebUrl") or "",
                image_url=(item.get("image") or {}).get("imageUrl") or "",
                fetched_at=now_iso,
            )
        )

    logger.info("EBAY parser: {} -> {} of {} items kept", mpn, len(rows), len(items))
    return rows
