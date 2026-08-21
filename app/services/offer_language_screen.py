"""app/services/offer_language_screen.py — deterministic red-flag screen of an offer's
vendor communication.

Reliable phrase/structure checks (no LLM: no cost, no hallucination, fully testable) for
the two "communication" red flags on the qualification checklist — vague/hedging vendor
language ("New & Original") and the claims-in-stock-but-quotes-a-lead-time contradiction.
Returns advisory flags with the matched evidence; nothing gates. A fuzzier LLM tone pass
can layer on later without changing callers.

Called by: app/services/buyplan_builder.py (generate_ai_flags, per line) and the sightings
offer Pre-check (app/routers/sightings.py).
"""

from __future__ import annotations

import re

from ..utils.normalization import normalize_lead_time

# Counterfeit-adjacent hedging phrases that read as reassurance but carry no verifiable
# meaning — the "vague communication" red flag (David's checklist names "New & Original").
_VAGUE_PHRASES: tuple[str, ...] = (
    "new & original",
    "new and original",
    "100% original",
    "genuine original",
    "as-is",
    "as is",
    "no returns",
    "no coo",
    "no c of o",
    "no cofo",
    "no test",
    "no testing",
    "sold as-is",
    "unverified",
)

# Match phrases on WORD BOUNDARIES so "as is" does not fire inside "was issued", etc.
_VAGUE_RE = re.compile(r"\b(?:" + "|".join(re.escape(p) for p in _VAGUE_PHRASES) + r")\b")


def _offer_text(offer) -> str:
    parts = [offer.notes or ""]
    q = offer.qualification or {}
    for k in ("provenance_story", "terms", "lead_time_reason"):
        if q.get(k):
            parts.append(str(q[k]))
    return " ".join(parts).lower()


def screen_offer_language(offer) -> list[dict]:
    """Advisory communication red flags for one offer. Each item: ``{code, note}``.

    - ``vague_language``: the vendor's wording contains counterfeit-adjacent hedges.
    - ``stock_leadtime_conflict``: claims stock on hand yet quotes a real lead time.
    """
    flags: list[dict] = []

    hits = sorted(set(_VAGUE_RE.findall(_offer_text(offer))))
    if hits:
        flags.append({"code": "vague_language", "note": "Vague vendor wording: " + ", ".join(hits)})

    # Only a POSITIVE parsed lead time contradicts an in-stock claim. Reuse the canonical
    # normalizer: "in stock"/"from stock"/"immediate" → 0 (no conflict), ambiguous
    # ("ex-stock", "available", "ARO") → None (don't guess), "2-3 weeks" → a positive.
    days = normalize_lead_time(offer.lead_time)
    if offer.qty_available and offer.qty_available > 0 and days is not None and days > 0:
        flags.append(
            {
                "code": "stock_leadtime_conflict",
                "note": f"Claims {offer.qty_available} in stock but quotes lead time '{offer.lead_time}'",
            }
        )
    return flags
