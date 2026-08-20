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

# lead_time strings that actually mean "in stock, no wait" — not a contradiction.
_IN_STOCK_LEAD = {"", "in stock", "stock", "0", "0 days", "immediate", "same day", "ready"}


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

    hits = sorted({p for p in _VAGUE_PHRASES if p in _offer_text(offer)})
    if hits:
        flags.append({"code": "vague_language", "note": "Vague vendor wording: " + ", ".join(hits)})

    lead = (offer.lead_time or "").strip().lower()
    if offer.qty_available and offer.qty_available > 0 and lead not in _IN_STOCK_LEAD:
        flags.append(
            {
                "code": "stock_leadtime_conflict",
                "note": f"Claims {offer.qty_available} in stock but quotes lead time '{offer.lead_time}'",
            }
        )
    return flags
