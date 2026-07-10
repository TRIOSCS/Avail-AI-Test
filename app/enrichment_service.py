"""Unified Enrichment Service.

Shared enrichment workflow for both vendor cards and customer companies. enrich_entity
and find_suggested_contacts delegate provider orchestration to
app.services.enrichment_router and per-field arbitration (by tier) to
app.services.firmo_tiers. apply_enrichment_to_* functions are provenance-aware.
"""

import re
from datetime import UTC, datetime

import httpx
from loguru import logger

from .services.credential_service import get_credential_cached
from .utils.claude_client import claude_json, claude_text

# ── Contact relevance ─────────────────────────────────────────────────────

# B2B title keywords used to filter suggested contacts to procurement-relevant roles.
# Referenced by find_suggested_contacts and find_suggested_contacts_with_errors — single source.
_RELEVANT_CONTACT_KEYWORDS: frozenset[str] = frozenset(
    {
        "procurement",
        "purchasing",
        "buyer",
        "sourcing",
        "supply chain",
        "component",
        "commodity",
        "materials",
        "vendor",
        "supplier",
        "sales",
        "account",
        "business development",
        "director",
        "president",
        "vp",
        "manager",
        "engineer",
        "operations",
        "logistics",
        "inventory",
        "planning",
        "quality",
    }
)

# ── Normalization ────────────────────────────────────────────────────────

# Acronyms to preserve in Title Case conversions
_KNOWN_ACRONYMS = {
    "IBM",
    "AMD",
    "TI",
    "NXP",
    "STM",
    "TDK",
    "AVX",
    "TE",
    "3M",
    "ON",
    "IXYS",
    "QFN",
    "BGA",
    "SOP",
    "IC",
    "LED",
    "PCB",
    "USB",
    "FPGA",
    "CPU",
    "GPU",
    "RAM",
    "LLC",
    "INC",
    "LTD",
    "CO",
    "CORP",
    "GmbH",
    "AG",
    "SA",
    "PLC",
    "LP",
    "NA",
    "USA",
    "UK",
    "EU",
    "HK",
}

_US_STATES = {
    "AL",
    "AK",
    "AZ",
    "AR",
    "CA",
    "CO",
    "CT",
    "DE",
    "FL",
    "GA",
    "HI",
    "ID",
    "IL",
    "IN",
    "IA",
    "KS",
    "KY",
    "LA",
    "ME",
    "MD",
    "MA",
    "MI",
    "MN",
    "MS",
    "MO",
    "MT",
    "NE",
    "NV",
    "NH",
    "NJ",
    "NM",
    "NY",
    "NC",
    "ND",
    "OH",
    "OK",
    "OR",
    "PA",
    "RI",
    "SC",
    "SD",
    "TN",
    "TX",
    "UT",
    "VT",
    "VA",
    "WA",
    "WV",
    "WI",
    "WY",
    "DC",
    "PR",
    "VI",
    "GU",
}

_COUNTRY_MAP = {
    "US": "United States",
    "USA": "United States",
    "UK": "United Kingdom",
    "GB": "United Kingdom",
    "DE": "Germany",
    "FR": "France",
    "JP": "Japan",
    "CN": "China",
    "KR": "South Korea",
    "TW": "Taiwan",
    "HK": "Hong Kong",
    "SG": "Singapore",
    "IN": "India",
    "CA": "Canada",
    "AU": "Australia",
    "NL": "Netherlands",
    "CH": "Switzerland",
    "SE": "Sweden",
    "IL": "Israel",
    "IT": "Italy",
    "MX": "Mexico",
    "BR": "Brazil",
    "MY": "Malaysia",
    "TH": "Thailand",
    "PH": "Philippines",
    "VN": "Vietnam",
}


def _clean_domain(domain: str) -> str:
    """Pure string cleanup for domain: strip, lowercase, remove protocol/www.

    TODO: not yet migrated onto the shared, validated
    app.utils.normalization.parse_website_domain (urlsplit-based; rejects junk like
    "user@host:8080" instead of naively regexing it into a bogus domain) — this
    function's callers accept a value that's returned by AI enrichment /
    normalize_company_input's own logic, not raw user-typed website input, so
    swapping the extractor here needs its own behavior verification (a stricter
    validator could start rejecting domains this pipeline currently accepts). Out of
    scope for the company_import_service / sightings consolidation this note
    accompanies.
    """
    d = domain.strip().rstrip(".").rstrip("/")
    d = re.sub(r"^https?://", "", d, flags=re.IGNORECASE)
    d = re.sub(r"^www\.", "", d, flags=re.IGNORECASE)
    return d.lower().split("/")[0]


def _name_looks_suspicious(name: str) -> bool:
    """Heuristic: name might have typos if it has no vowels or weird patterns."""
    words = [w for w in name.split() if len(w) > 2 and w.upper() not in _KNOWN_ACRONYMS]
    if not words:
        return False
    for w in words:
        if not re.search(r"[aeiouAEIOU]", w):
            return True
    return False


async def normalize_company_input(name: str, domain: str = "") -> tuple[str, str]:
    """Layer 1: clean up name and domain before any provider call.

    Returns (cleaned_name, cleaned_domain).
    """
    clean_name = (name or "").strip()
    clean_domain = _clean_domain(domain) if domain else ""

    # AI typo fix only when name looks suspicious and we have an API key
    if clean_name and _name_looks_suspicious(clean_name) and get_credential_cached("anthropic_ai", "ANTHROPIC_API_KEY"):
        try:
            fixed = await claude_text(
                f'Fix any typos in this company name. Return ONLY the corrected name, nothing else: "{clean_name}"',
                system="You fix typos in company names. Return only the corrected name. If no typos, return the original exactly.",
                model_tier="fast",
                max_tokens=60,
                timeout=5,
            )
            if fixed and fixed.strip().strip('"'):
                clean_name = fixed.strip().strip('"')
        except Exception as e:
            logger.warning("Typo fix skipped: {}", e)

    return clean_name, clean_domain


def _title_case_preserve_acronyms(s: str) -> str:
    """Title Case but preserve known acronyms."""
    if not s:
        return s
    words = s.split()
    result = []
    for w in words:
        if w.upper() in _KNOWN_ACRONYMS:
            result.append(w.upper())
        else:
            result.append(w.title())
    return " ".join(result)


def normalize_company_output(data: dict) -> dict:
    """Layer 2: normalize enrichment output fields to consistent format."""
    out = dict(data)

    if out.get("legal_name"):
        out["legal_name"] = _title_case_preserve_acronyms(out["legal_name"])

    if out.get("domain"):
        out["domain"] = _clean_domain(out["domain"])

    if out.get("industry"):
        out["industry"] = out["industry"].strip().title()

    if out.get("employee_size"):
        s = str(out["employee_size"]).strip().replace(",", "").replace(" ", "")
        s = re.sub(r"employees?", "", s, flags=re.IGNORECASE).strip()
        if s.isdigit() and int(s) >= 1000:
            out["employee_size"] = f"{int(s):,}+"
        elif not re.match(r"^\d+[-–]\d+$|^\d+[,\d]*\+?$", s):
            out["employee_size"] = s
        else:
            out["employee_size"] = s.replace("–", "-")

    if out.get("hq_city"):
        out["hq_city"] = out["hq_city"].strip().title()

    if out.get("hq_state"):
        st = out["hq_state"].strip()
        if st.upper() in _US_STATES:
            out["hq_state"] = st.upper()
        else:
            out["hq_state"] = st.title()

    if out.get("hq_country"):
        c = out["hq_country"].strip()
        out["hq_country"] = _COUNTRY_MAP.get(c.upper(), c.title())

    if out.get("website"):
        w = out["website"].strip().lower()
        if not w.startswith("http"):
            w = "https://" + w
        out["website"] = w

    if out.get("linkedin_url"):
        li = out["linkedin_url"].strip().lower()
        if not li.startswith("http"):
            li = "https://" + li
        out["linkedin_url"] = li

    return out


# ── Provider: AI (Claude + Web Search) ───────────────────────────────────

COMPANY_SEARCH_SYSTEM = (
    "You are a B2B company research assistant for an electronic component broker. "
    "Look up the requested company by domain and return firmographic data as JSON. "
    "Return ONLY a JSON object with these keys: "
    '{"legal_name", "industry", "employee_size", "hq_city", "hq_state", "hq_country", '
    '"website", "linkedin_url"}. '
    "Use null for any field you cannot verify. Do not guess or fabricate data."
)


async def _ai_find_company(domain: str, name: str = "") -> dict | None:
    """Look up a company using Claude + web search.

    Returns normalized company data.
    """
    if not get_credential_cached("anthropic_ai", "ANTHROPIC_API_KEY"):
        logger.debug("Anthropic API key not configured — skipping AI enrichment")
        return None
    try:
        prompt = (
            f"Look up the company with domain '{domain}'"
            f"{f' (also known as {name})' if name else ''}.\n\n"
            f"Find:\n"
            f"- Official legal/registered name\n"
            f"- Industry or sector\n"
            f"- Approximate employee count or range (e.g. '51-200')\n"
            f"- Headquarters city, state/region, and country\n"
            f"- Main website URL\n"
            f"- LinkedIn company page URL\n\n"
            f"Return ONLY valid JSON. Use null for unknown fields."
        )
        data = await claude_json(
            prompt,
            system=COMPANY_SEARCH_SYSTEM,
            model_tier="smart",
            max_tokens=1024,
            tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 5}],
            timeout=60,
        )
        if not data or not isinstance(data, dict):
            logger.warning("AI company lookup returned no data for {}", domain)
            return None
        return {
            "source": "ai",
            "legal_name": data.get("legal_name") or data.get("name"),
            "domain": domain,
            "linkedin_url": data.get("linkedin_url"),
            "industry": data.get("industry"),
            "employee_size": data.get("employee_size") or data.get("employees"),
            "hq_city": data.get("hq_city") or data.get("city"),
            "hq_state": data.get("hq_state") or data.get("state"),
            "hq_country": data.get("hq_country") or data.get("country"),
            "website": data.get("website"),
        }
    except (httpx.HTTPError, KeyError, ValueError, TypeError) as e:
        logger.error("AI company lookup error: {}", e)
        return None


# ── Unified Enrichment ──────────────────────────────────────────────────


async def enrich_entity(domain: str, name: str = "") -> dict:
    """Enrich a business entity (vendor or customer) by domain.

    Delegates provider orchestration to enrichment_router.gather_company and field-level
    arbitration to firmo_tiers.blend_company. Results cached in IntelCache with 14-day
    TTL keyed by domain.
    """
    from .cache.intel_cache import get_cached, set_cached
    from .services import enrichment_router
    from .services.firmo_tiers import blend_company

    # Layer 1: input cleanup
    name, domain = await normalize_company_input(name, domain)

    # Check cache first (14-day TTL)
    cache_key = f"enrich:{domain}"
    cached = get_cached(cache_key)
    if cached is not None:
        return cached

    results = await enrichment_router.gather_company(domain, name)
    blended = blend_company(results)
    blended.setdefault("domain", domain)

    # Layer 2: output normalization
    normalized = normalize_company_output(blended)

    # Carry provenance through normalization so apply functions and callers can use it
    if blended.get("_provenance"):
        normalized["_provenance"] = blended["_provenance"]

    # Cache only when there is substantive data beyond the bare domain
    if any(v for k, v in normalized.items() if k not in ("domain", "_provenance")):
        set_cached(cache_key, normalized, ttl_days=14)

    return normalized


async def _hunter_find_contacts(domain: str) -> list[dict]:
    """Hunter.io domain search → normalised contact list for find_suggested_contacts."""
    api_key = get_credential_cached("hunter_enrichment", "HUNTER_API_KEY")
    if not api_key:
        return []
    from .connectors.hunter import HunterConnector

    contacts = await HunterConnector(api_key).domain_search(domain, limit=10)
    results = []
    for c in contacts:
        email = c.get("email", "")
        first = c.get("first_name", "")
        last = c.get("last_name", "")
        full_name = f"{first} {last}".strip() or email.split("@")[0]
        results.append(
            {
                "full_name": full_name,
                "title": c.get("position", ""),
                "email": email,
                "phone": c.get("phone_number", ""),
                "linkedin_url": c.get("linkedin_url", ""),
                "location": "",
                "source": "hunter",
            }
        )
    return results


_RELEVANT_KEYWORDS = {
    "procurement",
    "purchasing",
    "buyer",
    "sourcing",
    "supply chain",
    "component",
    "commodity",
    "materials",
    "vendor",
    "supplier",
    "sales",
    "account",
    "business development",
    "director",
    "president",
    "vp",
    "manager",
    "engineer",
    "operations",
    "logistics",
    "inventory",
    "planning",
    "quality",
}


def _is_relevant(contact: dict) -> bool:
    title = (contact.get("title") or "").lower()
    if not title:
        return bool(contact.get("email"))  # Keep if has email but no title
    return any(kw in title for kw in _RELEVANT_KEYWORDS)


async def find_suggested_contacts(domain: str, name: str = "", title_filter: str = "", limit: int = 10) -> list[dict]:
    """Find suggested contacts at a company from all configured providers.

    Delegates provider orchestration to enrichment_router.gather_contacts and field-
    level deduplication / arbitration to firmo_tiers.blend_contacts. Returns a
    relevance-filtered list capped at *limit*. Each contact has: full_name, title,
    email, phone, linkedin_url, location, source (and verified for rows from providers
    that surface it).
    """
    from .services import enrichment_router
    from .services.firmo_tiers import blend_contacts

    raw = await enrichment_router.gather_contacts(domain, name, title_filter, limit)
    unique = blend_contacts(raw)
    filtered = [c for c in unique if _is_relevant(c)]
    # If filter removed everything, return unfiltered (don't lose all results)
    return (filtered if filtered else unique)[:limit]


_ENRICH_FIELDS = (
    "domain",
    "linkedin_url",
    "legal_name",
    "industry",
    "employee_size",
    "hq_city",
    "hq_state",
    "hq_country",
    "website",
    "ticker",
    "naics",
    "revenue_range",
)


def _apply_enrichment(obj, data: dict) -> list[str]:
    """Shared provenance-aware enrichment writer.

    Rules:
      1. Empty field → always write (whether or not provenance is present).
      2. Field has existing value but no stored provenance → manual/legacy; protect it.
      3. Field has existing value with stored provenance → overwrite only when incoming
         tier (then confidence) strictly beats the stored tier (then confidence).
    """
    updated: list[str] = []
    prov_in = data.get("_provenance") or {}
    store = dict(getattr(obj, "enrichment_provenance", None) or {})
    for field in _ENRICH_FIELDS:
        val = data.get(field)
        if not val:
            continue
        incoming = prov_in.get(field)
        current = getattr(obj, field, None)
        if current:
            # Existing value: check whether we may overwrite.
            if incoming is None:
                # No provenance for the incoming value → never clobber.
                continue
            existing = store.get(field)
            if existing is None:
                # Stored value lacks provenance (manual/legacy) → protect it.
                continue
            inc_key = (incoming.get("tier", 0), incoming.get("confidence", 0.0))
            cur_key = (existing.get("tier", 0), existing.get("confidence", 0.0))
            if inc_key <= cur_key:
                continue
        setattr(obj, field, val)
        if incoming:
            store[field] = {
                "source": incoming.get("source"),
                "tier": incoming.get("tier", 0),
                "confidence": incoming.get("confidence", 1.0),
            }
        updated.append(field)
    if updated:
        obj.enrichment_provenance = store
        obj.last_enriched_at = datetime.now(UTC)
        obj.enrichment_source = data.get("source", "unknown")
    return updated


async def find_suggested_contacts_with_errors(
    domain: str, name: str = "", title_filter: str = "", limit: int = 10
) -> tuple[list[dict], list[str]]:
    """Like find_suggested_contacts but also returns which providers errored.

    Returns (contacts, errored_provider_names) so the UI can distinguish:
      - zero results + empty errors  → neutral "No contacts found"
      - zero results + errors        → amber "Couldn't reach <provider>"
      - contacts present             → render the list with Add buttons

    Delegates to find_suggested_contacts (which calls enrichment_router.gather_contacts).
    Derives errored_providers by snapshotting circuit state before/after: any metered
    contact provider whose circuit transitions from closed to open during the call has
    tripped due to a quota/rate-limit error.
    """
    from .services import enrichment_router as er

    # Metered contact providers that gather_contacts may trip on ProviderQuotaError.
    _metered: tuple[str, ...] = ("clay", "lusha", "explorium")

    # Snapshot closed circuits before the gather so we can detect newly-tripped ones.
    before: frozenset[str] = frozenset(p for p in _metered if not er.circuit_open(p))

    contacts = await find_suggested_contacts(domain, name, title_filter, limit)

    # Any provider that was closed before but is now open has newly tripped.
    errored: list[str] = [p for p in before if er.circuit_open(p)]

    return contacts, errored


def apply_enrichment_to_company(company, data: dict) -> list[str]:
    """Apply blended enrichment to a Company (provenance-aware; protects manual values).

    Returns list of fields updated.
    """
    return _apply_enrichment(company, data)


def apply_enrichment_to_vendor(card, data: dict) -> list[str]:
    """Apply blended enrichment to a VendorCard (provenance-aware; protects manual
    values).

    Returns list of fields updated.
    """
    return _apply_enrichment(card, data)
