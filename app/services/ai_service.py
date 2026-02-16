"""AI Service — All intelligence features live here.

Definitive Spec Features:
  1. Contact Enrichment (Apollo + Claude web search fallback)
  2. Vendor Reply → Structured Offers (delegates to response_parser)
  3. Company Intelligence Cards (Claude + web search, cached 7 days)
  4. Smart RFQ Email Drafts (personalized with vendor history)

Design rules:
  - Every function returns structured output or None (caller handles gracefully)
  - Caches where appropriate
  - Never auto-sends anything — always human confirmation
  - AVAIL still works if all AI fails
"""

import logging

from app.utils.claude_client import claude_text, claude_json
from app.cache.intel_cache import get_cached, set_cached

log = logging.getLogger("avail.ai_service")

# Model for intelligence features (needs quality)
SMART = "smart"
# Model for parsing (needs speed)
FAST = "fast"

# ── Feature 1: Contact Enrichment ─────────────────────────────────────

# Default title keywords for electronic component sales
DEFAULT_TITLE_KEYWORDS = [
    "procurement",
    "purchasing",
    "buyer",
    "supply chain",
    "component engineer",
    "commodity manager",
    "materials manager",
    "sourcing",
]

CONTACT_SEARCH_SCHEMA = {
    "type": "object",
    "properties": {
        "contacts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "full_name": {"type": "string"},
                    "title": {"type": "string"},
                    "email": {"type": "string"},
                    "phone": {"type": "string"},
                    "linkedin_url": {"type": "string"},
                },
                "required": ["full_name"],
            },
        },
    },
    "required": ["contacts"],
}


async def enrich_contacts_websearch(
    company_name: str,
    domain: str | None = None,
    title_keywords: list[str] | None = None,
    limit: int = 5,
) -> list[dict]:
    """Find contacts at a company using Claude + web search.

    Tier 2 fallback when Apollo has gaps. Lower confidence than Apollo.

    Returns list of {full_name, title, email, phone, linkedin_url, confidence, source}.
    """
    keywords = title_keywords or DEFAULT_TITLE_KEYWORDS
    keywords_str = ", ".join(keywords)

    prompt = (
        f"Find purchasing and procurement contacts at {company_name}"
        f"{f' ({domain})' if domain else ''}.\n\n"
        f"Target titles: {keywords_str}\n\n"
        f"I need: full name, job title, email address, phone number, LinkedIn profile URL.\n"
        f"Only return contacts you find real evidence for.\n"
        f"If you can't find a verified email, set it to null — don't guess.\n"
        f"Return up to {limit} contacts."
    )

    result = await claude_json(
        prompt,
        system="You find B2B contacts for electronic component sales outreach. "
        'Return JSON: {"contacts": [{"full_name", "title", "email", "phone", "linkedin_url"}]}. '
        "Only include contacts with real evidence. Null for unknown fields.",
        model_tier=SMART,
        max_tokens=1500,
        tools=[{"type": "web_search_20250305", "name": "web_search"}],
        timeout=60,
    )

    contacts = []
    raw_contacts = []

    if isinstance(result, dict):
        raw_contacts = result.get("contacts", [])
    elif isinstance(result, list):
        raw_contacts = result

    for c in raw_contacts[:limit]:
        if not isinstance(c, dict) or not c.get("full_name"):
            continue

        email = (c.get("email") or "").strip().lower() or None
        confidence = "low"

        # Confidence assessment
        if email and domain and domain in email:
            confidence = "medium"  # Email matches company domain
        elif email:
            confidence = "medium"
        elif c.get("linkedin_url"):
            confidence = "low"

        contacts.append(
            {
                "full_name": c["full_name"].strip(),
                "title": (c.get("title") or "").strip() or None,
                "email": email,
                "phone": (c.get("phone") or "").strip() or None,
                "linkedin_url": (c.get("linkedin_url") or "").strip() or None,
                "source": "web_search",
                "confidence": confidence,
            }
        )

    return contacts


# ── Feature 2: Vendor Reply Parsing ───────────────────────────────────
# Delegated to app.services.response_parser — see that module.


# ── Feature 3: Company Intelligence Cards ─────────────────────────────

INTEL_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string", "description": "1-2 sentence overview"},
        "revenue": {"type": "string", "description": "Annual revenue if known"},
        "employees": {"type": "string", "description": "Employee count if known"},
        "products": {"type": "string", "description": "What they manufacture/build"},
        "components_they_buy": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Types of electronic components they likely need",
        },
        "recent_news": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Recent news headlines",
        },
        "opportunity_signals": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Signals suggesting increased component buying",
        },
        "sources": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Sources of information",
        },
    },
    "required": ["summary"],
}


async def company_intel(company_name: str, domain: str | None = None) -> dict | None:
    """Brief intelligence report for a salesperson. Cached 7 days.

    Returns: {summary, revenue, employees, products, components_they_buy[],
              recent_news[], opportunity_signals[], sources[]}
    """
    cache_key = f"intel:{company_name.lower().strip()}"
    cached = get_cached(cache_key)
    if cached:
        return cached

    prompt = (
        f"Company intelligence brief on {company_name}"
        f"{f' ({domain})' if domain else ''} "
        f"for an electronic component broker/distributor salesperson.\n\n"
        f"Include: company overview, size, revenue, what they manufacture, "
        f"what electronic components they likely buy, recent news (last 6 months), "
        f"and any signals that suggest increased component procurement.\n\n"
        f"Only include what you actually find. Leave unknown fields empty."
    )

    intel = await claude_json(
        prompt,
        system="You provide concise company intelligence for electronic component salespeople. "
        "Return JSON with: summary, revenue, employees, products, components_they_buy, "
        "recent_news, opportunity_signals, sources. Null for unknown fields.",
        model_tier=SMART,
        max_tokens=1500,
        tools=[{"type": "web_search_20250305", "name": "web_search"}],
        timeout=60,
    )

    if intel and isinstance(intel, dict):
        set_cached(cache_key, intel, ttl_days=7)
        return intel

    return None


# ── Feature 4: Smart RFQ Email Drafts ─────────────────────────────────


async def draft_rfq(
    vendor_name: str,
    parts: list[dict],
    vendor_history: dict | None = None,
    user_name: str = "",
    user_signature: str = "",
) -> str | None:
    """Generate personalized RFQ email body.

    Args:
        vendor_name: Target vendor
        parts: List of {mpn, qty, target_price} dicts
        vendor_history: From AVAIL DB — past offers, response rate, last interaction
        user_name: Salesperson name
        user_signature: Email signature to append

    Returns:
        Email body string, or None on failure
    """
    history_context = ""
    if vendor_history:
        history_context = (
            f"\nPast relationship context:\n"
            f"- RFQs sent to this vendor: {vendor_history.get('total_rfqs', 0)}\n"
            f"- Offers received: {vendor_history.get('total_offers', 0)}\n"
            f"- Last interaction: {vendor_history.get('last_contact_date', 'unknown')}\n"
            f"- Avg response time: {vendor_history.get('avg_response_hours', 'unknown')} hours\n"
            f"- Best past price for this part: {vendor_history.get('best_price', 'unknown')}\n"
        )

    parts_str = "\n".join(
        f"- {p.get('mpn', '?')}: {p.get('qty', '?')} pcs"
        + (f" (target: ${p['target_price']})" if p.get("target_price") else "")
        for p in parts[:20]
    )

    prompt = (
        f"Vendor: {vendor_name}\n"
        f"Sender: {user_name or 'the buyer'}\n\n"
        f"Parts needed:\n{parts_str}\n"
        f"{history_context}\n"
        f"Draft a short, professional RFQ email. 3-5 sentences max.\n"
        f"Reference past business if history is provided.\n"
        f"Include the parts in a simple list or table.\n"
        f"No greeting line (it'll be added separately).\n"
        f"No signature (it'll be added separately)."
    )

    body = await claude_text(
        prompt,
        system="You write concise, professional RFQ emails for an electronic component broker. "
        "Keep it short — 3-5 sentences plus a parts table. Reference past business "
        "if history is provided. No fluff, no over-politeness. Business-direct tone.",
        model_tier=FAST,
        max_tokens=500,
    )

    return body


async def rephrase_rfq(body: str) -> str | None:
    """Rephrase an RFQ email body so each send sounds unique.

    Keeps all part numbers, quantities, conditions, and requirements intact
    but varies the wording, sentence structure, and tone slightly.

    Returns rephrased body or None on failure.
    """
    prompt = (
        f"Rephrase this RFQ email so it reads naturally and differently from the original. "
        f"Keep ALL part numbers, quantities, and requirements exactly as-is — only change "
        f"the surrounding wording, sentence structure, and phrasing. Keep it concise and "
        f"professional. Do not add new requirements or remove existing ones. "
        f"Return ONLY the rephrased email text, nothing else.\n\n"
        f"Original:\n{body}"
    )

    return await claude_text(
        prompt,
        system="You rephrase procurement emails for an electronic component broker. "
        "Vary the greeting, intro, closing, and transitions while keeping all part numbers, "
        "conditions, and requirements exactly intact. Keep it short and professional. "
        "Never add placeholder text or commentary — return only the final email.",
        model_tier=FAST,
        max_tokens=800,
    )
