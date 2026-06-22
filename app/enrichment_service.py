"""Unified Enrichment Service.

Shared enrichment workflow for both vendor cards and customer companies. Supports
Explorium (Vibe Prospecting), Apollo.io (company + contact enrichment), and AI (Claude +
web search) as enrichment providers. AI runs last to fill remaining gaps.
"""

import asyncio
import re
from datetime import datetime, timezone
from typing import Optional

import httpx
from loguru import logger

from .config import settings
from .connectors import lusha
from .http_client import http
from .services.ai_service import enrich_contacts_websearch
from .services.credential_service import get_credential_cached
from .services.enrichment_credit_guard import ProviderQuotaError, circuit_open, trip_circuit
from .utils.claude_client import claude_json, claude_text

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
    """Pure string cleanup for domain: strip, lowercase, remove protocol/www."""
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


# ── Provider: Explorium (Vibe Prospecting) ──────────────────────────────

EXPLORIUM_BASE = "https://api.explorium.ai/v1"


async def _explorium_find_company(domain: str, name: str = "") -> Optional[dict]:
    """Look up a company on Explorium by domain.

    Returns normalized company data.
    """
    api_key = get_credential_cached("explorium_enrichment", "EXPLORIUM_API_KEY")
    if not api_key:
        logger.debug("Explorium API key not configured — skipping")
        return None
    try:
        resp = await http.post(
            f"{EXPLORIUM_BASE}/match/business",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={"domain": domain, "name": name},
            timeout=15,
        )
        if resp.status_code in (402, 429):
            raise ProviderQuotaError(f"Explorium {resp.status_code}")
        if resp.status_code != 200:
            logger.warning("Explorium company lookup failed: {}", resp.status_code)
            return None
        data = resp.json()
        firmo = {k.replace("firmo_", ""): v for k, v in data.items() if k.startswith("firmo_")}
        return {
            "source": "explorium",
            "legal_name": firmo.get("name"),
            "domain": domain,
            "linkedin_url": firmo.get("linkedin_profile"),
            "industry": firmo.get("linkedin_industry_category"),
            "employee_size": firmo.get("number_of_employees_range"),
            "hq_city": firmo.get("city_name"),
            "hq_state": firmo.get("region_name"),
            "hq_country": firmo.get("country_name"),
            "website": firmo.get("website"),
            "ticker": firmo.get("ticker"),
            "naics": firmo.get("naics"),
            "revenue_range": firmo.get("yearly_revenue_range"),
        }
    except (httpx.HTTPError, KeyError, ValueError) as e:
        logger.error("Explorium company lookup error: {}", e)
        return None


async def _explorium_find_contacts(domain: str, title_filter: str = "") -> list[dict]:
    """Find contacts at a company via Explorium."""
    api_key = get_credential_cached("explorium_enrichment", "EXPLORIUM_API_KEY")
    if not api_key:
        return []
    try:
        payload = {"company_domain": domain}
        if title_filter:
            payload["job_title_keywords"] = [title_filter]
        resp = await http.post(
            f"{EXPLORIUM_BASE}/fetch/prospects",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=20,
        )
        if resp.status_code in (402, 429):
            raise ProviderQuotaError(f"Explorium {resp.status_code}")
        if resp.status_code != 200:
            logger.warning("Explorium contacts returned HTTP {} for {}", resp.status_code, domain)
            return []
        prospects = resp.json().get("prospects") or []
        return [
            {
                "source": "explorium",
                "full_name": p.get("full_name"),
                "title": p.get("job_title"),
                "email": p.get("email"),
                "phone": p.get("phone"),
                "linkedin_url": p.get("linkedin_url"),
                "location": p.get("location"),
                "company": p.get("company_name"),
            }
            for p in prospects
            if p.get("full_name")
        ]
    except (httpx.HTTPError, KeyError, ValueError) as e:
        logger.error("Explorium contacts lookup error: {}", e)
        return []


# ── Provider: AI (Claude + Web Search) ───────────────────────────────────

COMPANY_SEARCH_SYSTEM = (
    "You are a B2B company research assistant for an electronic component broker. "
    "Look up the requested company by domain and return firmographic data as JSON. "
    "Return ONLY a JSON object with these keys: "
    '{"legal_name", "industry", "employee_size", "hq_city", "hq_state", "hq_country", '
    '"website", "linkedin_url"}. '
    "Use null for any field you cannot verify. Do not guess or fabricate data."
)


async def _ai_find_company(domain: str, name: str = "") -> Optional[dict]:
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


async def _ai_find_contacts(domain: str, name: str = "", title_filter: str = "") -> list[dict]:
    """Find contacts at a company using Claude + web search.

    Delegates to ai_service.enrich_contacts_websearch() and normalizes the output to
    match the enrichment service contact shape.
    """
    if not get_credential_cached("anthropic_ai", "ANTHROPIC_API_KEY"):
        return []
    try:
        title_keywords = [title_filter] if title_filter else None
        raw = await enrich_contacts_websearch(
            company_name=name or domain,
            domain=domain,
            title_keywords=title_keywords,
            limit=5,
        )
        return [
            {
                "source": "ai",
                "full_name": c.get("full_name"),
                "title": c.get("title"),
                "email": c.get("email"),
                "phone": c.get("phone"),
                "linkedin_url": c.get("linkedin_url"),
                "location": None,
                "company": name or domain,
            }
            for c in raw
            if c.get("full_name")
        ]
    except (httpx.HTTPError, KeyError, ValueError, TypeError) as e:
        logger.error("AI contacts lookup error: {}", e)
        return []


# ── Unified Enrichment ──────────────────────────────────────────────────


def _lusha_enabled() -> bool:
    """True when Lusha is feature-gated on AND a key is resolvable (DB or env)."""
    return settings.lusha_enrichment_enabled and bool(get_credential_cached("lusha_enrichment", "LUSHA_API_KEY"))


def _explorium_enabled() -> bool:
    """True when Explorium is feature-gated on AND a key is resolvable.

    Opt-in: Explorium only runs when the operator has confirmed it works and set
    EXPLORIUM_ENRICHMENT_ENABLED=true (default off).
    """
    return settings.explorium_enrichment_enabled and bool(
        get_credential_cached("explorium_enrichment", "EXPLORIUM_API_KEY")
    )


async def enrich_entity(domain: str, name: str = "") -> dict:
    """Enrich a business entity (vendor or customer) by domain.

    Phase 1: Explorium lookup.
    Phase 1b: Apollo enrichment (fills gaps from Phase 1).
    Phase 2: AI + web search fills remaining gaps (conditional).
    Merge priority: Explorium > Apollo > AI.
    Results cached in IntelCache with 14-day TTL keyed by domain.
    """
    from .cache.intel_cache import get_cached, set_cached

    # Layer 1: input cleanup
    name, domain = await normalize_company_input(name, domain)

    # Check cache first (14-day TTL)
    cache_key = f"enrich:{domain}"
    cached = get_cached(cache_key)
    if cached is not None:
        return cached

    result = {
        "legal_name": None,
        "domain": domain,
        "linkedin_url": None,
        "industry": None,
        "employee_size": None,
        "hq_city": None,
        "hq_state": None,
        "hq_country": None,
        "website": None,
        "source": None,
    }

    _enrichable = [
        "legal_name",
        "industry",
        "employee_size",
        "hq_city",
        "hq_state",
        "hq_country",
        "website",
        "linkedin_url",
    ]

    def _merge(provider_data: dict | None, source_label: str) -> None:
        """Merge provider results into result, tracking sources."""
        if not provider_data:
            return
        for k, v in provider_data.items():
            if k != "source" and v and not result.get(k):
                result[k] = v
        current = result.get("source") or ""
        if source_label not in current:
            result["source"] = f"{current}+{source_label}" if current else source_label

    def _gaps_remain() -> bool:
        return any(not result.get(f) for f in _enrichable)

    # ── Phase 1: Explorium (opt-in, circuit-guarded) ──
    if _explorium_enabled() and not circuit_open("explorium"):
        try:
            _merge(await _explorium_find_company(domain, name), "explorium")
        except ProviderQuotaError:
            logger.warning("Explorium quota/rate-limit on company {} — tripping circuit", domain)
            trip_circuit("explorium", settings.explorium_cooldown_minutes)

    # ── Phase 1a: Lusha (verified contacts/firmographics) — gap-gated, circuit-guarded ──
    if _lusha_enabled() and not circuit_open("lusha") and _gaps_remain():
        try:
            _merge(
                await lusha.enrich_company(domain, get_credential_cached("lusha_enrichment", "LUSHA_API_KEY") or ""),
                "lusha",
            )
        except ProviderQuotaError:
            logger.warning("Lusha quota/rate-limit on company {} — tripping circuit", domain)
            trip_circuit("lusha", settings.lusha_cooldown_minutes)

    # ── Phase 1b: Apollo enrichment (fills gaps; gap-gated → spares credits) ──
    if settings.apollo_api_key and _gaps_remain():
        from .connectors.apollo import search_company as apollo_search

        _merge(await apollo_search(domain, settings.apollo_api_key), "apollo")

    # ── Phase 2: AI fills remaining gaps (conditional) ──
    if _gaps_remain():
        _merge(await _ai_find_company(domain, name), "ai")

    # Layer 2: output normalization
    normalized = normalize_company_output(result)

    # Cache enrichment result for 14 days
    if any(v for k, v in normalized.items() if k != "domain"):
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


async def find_suggested_contacts(domain: str, name: str = "", title_filter: str = "", limit: int = 10) -> list[dict]:
    """Find suggested contacts at a company from all configured providers.

    Lusha (verified) runs first; if it returns >= limit verified contacts the existing
    concurrent Explorium+Hunter+AI gather is skipped, else they run and results are
    merged. Returns a deduplicated, relevance-filtered list. Each contact has:
    full_name, title, email, phone, linkedin_url, location, source (and verified for
    Lusha rows).
    """
    all_contacts: list[dict] = []

    # ── Lusha first (verified source) — circuit-guarded ──
    if _lusha_enabled() and not circuit_open("lusha"):
        try:
            all_contacts = await lusha.search_contacts(
                domain, get_credential_cached("lusha_enrichment", "LUSHA_API_KEY") or "", limit
            )
        except ProviderQuotaError:
            logger.warning("Lusha quota/rate-limit on contacts {} — tripping circuit", domain)
            trip_circuit("lusha", settings.lusha_cooldown_minutes)

    # Fall through to the existing concurrent providers unless Lusha already satisfied the need.
    if not (len(all_contacts) >= limit and any(c.get("verified") for c in all_contacts)):
        providers = []
        explorium_on = _explorium_enabled() and not circuit_open("explorium")
        if explorium_on:
            providers.append(_explorium_find_contacts(domain, title_filter))
        providers.append(_hunter_find_contacts(domain))
        providers.append(_ai_find_contacts(domain, name, title_filter))
        results = await asyncio.gather(*providers, return_exceptions=True)
        for r in results:
            if isinstance(r, ProviderQuotaError):
                logger.warning("Explorium quota/rate-limit on contacts {} — tripping circuit", domain)
                trip_circuit("explorium", settings.explorium_cooldown_minutes)
                continue
            if isinstance(r, Exception):
                logger.warning("Contact provider failed: {}", r)
                continue
            all_contacts.extend(r)

    # Deduplicate by email or linkedin_url or full_name
    seen = set()
    unique = []
    for c in all_contacts:
        key = (c.get("email") or "").lower() or c.get("linkedin_url") or (c.get("full_name") or "").lower()
        if not key or key in seen:
            continue
        seen.add(key)
        unique.append(c)

    # Filter to relevant B2B titles — avoid wasting credits on irrelevant contacts
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

    filtered = [c for c in unique if _is_relevant(c)]
    # If filter removed everything, return unfiltered (don't lose all results)
    return filtered if filtered else unique


async def find_suggested_contacts_with_errors(
    domain: str, name: str = "", title_filter: str = "", limit: int = 10
) -> tuple[list[dict], list[str]]:
    """Like find_suggested_contacts but also returns which providers errored.

    Returns (contacts, errored_provider_names) so the UI can distinguish:
      - zero results + empty errors  → neutral "No contacts found"
      - zero results + errors        → amber "Couldn't reach <provider>"
      - contacts present             → render the list with Add buttons

    Calls the same provider waterfall as find_suggested_contacts.
    """
    all_contacts: list[dict] = []
    errored: list[str] = []

    # ── Lusha first (verified source) — circuit-guarded ──
    if _lusha_enabled() and not circuit_open("lusha"):
        try:
            all_contacts = await lusha.search_contacts(
                domain, get_credential_cached("lusha_enrichment", "LUSHA_API_KEY") or "", limit
            )
        except ProviderQuotaError:
            logger.warning("Lusha quota/rate-limit on contacts {} — tripping circuit", domain)
            trip_circuit("lusha", settings.lusha_cooldown_minutes)
            errored.append("lusha")
        except Exception as exc:
            logger.warning("Lusha contact provider failed: {}", exc)
            errored.append("lusha")

    if not (len(all_contacts) >= limit and any(c.get("verified") for c in all_contacts)):
        providers: list = []
        provider_names: list[str] = []
        explorium_on = _explorium_enabled() and not circuit_open("explorium")
        if explorium_on:
            providers.append(_explorium_find_contacts(domain, title_filter))
            provider_names.append("explorium")
        providers.append(_hunter_find_contacts(domain))
        provider_names.append("hunter")
        providers.append(_ai_find_contacts(domain, name, title_filter))
        provider_names.append("ai")

        results = await asyncio.gather(*providers, return_exceptions=True)
        for pname, r in zip(provider_names, results):
            if isinstance(r, ProviderQuotaError):
                logger.warning("{} quota/rate-limit on contacts {} — tripping circuit", pname, domain)
                if pname == "explorium":
                    trip_circuit("explorium", settings.explorium_cooldown_minutes)
                errored.append(pname)
                continue
            if isinstance(r, Exception):
                logger.warning("Contact provider {} failed: {}", pname, r)
                errored.append(pname)
                continue
            all_contacts.extend(r)

    # Deduplicate by email or linkedin_url or full_name
    seen: set[str] = set()
    unique: list[dict] = []
    for c in all_contacts:
        key = (c.get("email") or "").lower() or c.get("linkedin_url") or (c.get("full_name") or "").lower()
        if not key or key in seen:
            continue
        seen.add(key)
        unique.append(c)

    # Filter to relevant B2B titles
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
            return bool(contact.get("email"))
        return any(kw in title for kw in _RELEVANT_KEYWORDS)

    filtered = [c for c in unique if _is_relevant(c)]
    contacts = filtered if filtered else unique
    return contacts, errored


def apply_enrichment_to_company(company, data: dict) -> list[str]:
    """Apply enrichment data dict to a Company model.

    Returns list of fields updated.
    """
    updated = []
    # Each field is set only when present in `data` and currently empty on the model.
    fields = (
        "domain",
        "linkedin_url",
        "legal_name",
        "industry",
        "employee_size",
        "hq_city",
        "hq_state",
        "hq_country",
        "website",
    )
    for field in fields:
        val = data.get(field)
        if val and not getattr(company, field, None):
            setattr(company, field, val)
            updated.append(field)
    if updated:
        company.last_enriched_at = datetime.now(timezone.utc)
        company.enrichment_source = data.get("source", "unknown")
    return updated


def apply_enrichment_to_vendor(card, data: dict) -> list[str]:
    """Apply enrichment data dict to a VendorCard model.

    Returns list of fields updated.
    """
    updated = []
    # Each field is set only when present in `data` and currently empty on the model.
    fields = (
        "linkedin_url",
        "legal_name",
        "industry",
        "employee_size",
        "hq_city",
        "hq_state",
        "hq_country",
        "domain",
        "website",
    )
    for field in fields:
        val = data.get(field)
        if val and not getattr(card, field, None):
            setattr(card, field, val)
            updated.append(field)
    if updated:
        card.last_enriched_at = datetime.now(timezone.utc)
        card.enrichment_source = data.get("source", "unknown")
    return updated
