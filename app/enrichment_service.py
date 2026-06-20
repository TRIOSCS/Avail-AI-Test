"""AVAIL v1.2.0 — Unified Enrichment Service

Shared enrichment workflow for both vendor cards and customer companies.
Synchronous providers: Lusha, Apollo, Explorium (Vibe Prospecting), Clearbit,
Gradient, and AI (Claude + web search). AI runs last to fill remaining gaps.
Clay enrichment is asynchronous (webhook → callback) and lives in
app.services.clay_service — it feeds results back via the EnrichmentQueue.
"""

import asyncio
import logging
import re
from datetime import datetime, timezone
from typing import Optional

from .connectors.resilience import resilient_call
from .http_client import http
from .services.ai_service import enrich_contacts_websearch
from .services.credential_service import get_credential_cached
from .utils.claude_client import claude_json, claude_text

log = logging.getLogger("avail.enrichment")


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
            log.debug("Typo fix skipped: %s", e)

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


# ── Provider: Lusha ──────────────────────────────────────────────────────
#
# Lusha is a synchronous provider (api_key header, v3 search → enrich).
# (Clay is intentionally NOT a synchronous provider here — it has no
# real-time API; it runs asynchronously via app.services.clay_service and
# feeds results back through the EnrichmentQueue callback.)


async def _lusha_find_company(domain: str, name: str = "") -> Optional[dict]:
    """Look up a company on Lusha by domain. Returns normalized company data."""
    if not get_credential_cached("lusha_enrichment", "LUSHA_API_KEY"):
        log.debug("Lusha API key not configured — skipping")
        return None
    try:
        from .connectors.lusha_client import enrich_company as lusha_enrich
        return await lusha_enrich(domain)
    except Exception as e:
        log.error("Lusha company lookup error: %s", e)
        return None


async def _lusha_find_contacts(domain: str, title_filter: str = "") -> list[dict]:
    """Find contacts at a company via Lusha. Returns list of contact dicts."""
    if not get_credential_cached("lusha_enrichment", "LUSHA_API_KEY"):
        return []
    try:
        from .connectors.lusha_client import search_contacts as lusha_search
        raw = await lusha_search(
            domain=domain,
            title_keywords=[title_filter] if title_filter else None,
            limit=5,
        )
        return [
            {
                "source": "lusha",
                "full_name": c.get("full_name"),
                "title": c.get("title"),
                "email": c.get("email"),
                "email_status": c.get("email_status"),
                "phone": c.get("phone"),
                "linkedin_url": c.get("linkedin_url"),
                "location": None,
                "company": domain,
            }
            for c in raw
            if c.get("full_name")
        ]
    except Exception as e:
        log.error("Lusha contacts lookup error: %s", e)
        return []


# ── Provider: Explorium (Vibe Prospecting) ──────────────────────────────
#
# Explorium uses an `api_key` header (NOT Bearer) and a match → enrich flow:
#   1. POST /v1/businesses/match            → business_id
#   2. POST /v1/businesses/firmographics/enrich (business_ids)   → firmographics
#   3. POST /v1/prospects                   (business_ids)       → prospect_ids
#   4. POST /v1/prospects/contacts_information/enrich (prospect_ids) → emails/phones
# Email-verified field on contacts is `professional_email_status` (valid/catch-all/invalid).

EXPLORIUM_BASE = "https://api.explorium.ai/v1"


def _explorium_headers() -> dict:
    """Auth headers for Explorium — key goes in the `api_key` header."""
    return {
        "api_key": get_credential_cached("explorium_enrichment", "EXPLORIUM_API_KEY"),
        "Content-Type": "application/json",
    }


def _explorium_records(data) -> list[dict]:
    """Pull the list of records out of an Explorium response (tolerant of shape)."""
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    if not isinstance(data, dict):
        return []
    for key in ("data", "results", "matched_businesses", "businesses", "prospects", "contacts"):
        val = data.get(key)
        if isinstance(val, list):
            return [r for r in val if isinstance(r, dict)]
    # Single record at top level
    return [data] if data else []


def _explorium_business_id(record: dict) -> Optional[str]:
    """Extract a business_id from an Explorium match record."""
    return (
        record.get("business_id")
        or record.get("businessId")
        or record.get("id")
    )


async def _explorium_match_business(domain: str, name: str = "") -> Optional[str]:
    """Match a company to an Explorium business_id by domain. Returns the id or None."""
    if not get_credential_cached("explorium_enrichment", "EXPLORIUM_API_KEY"):
        return None
    business = {"domain": domain}
    if name:
        business["name"] = name
    resp = await resilient_call(
        "explorium",
        lambda: http.post(
            f"{EXPLORIUM_BASE}/businesses/match",
            headers=_explorium_headers(),
            json={"businesses_to_match": [business]},
            timeout=15,
        ),
    )
    if resp.status_code != 200:
        log.warning("Explorium match failed: %s %s", resp.status_code, resp.text[:200])
        return None
    records = _explorium_records(resp.json())
    if not records:
        return None
    return _explorium_business_id(records[0])


async def _explorium_find_company(domain: str, name: str = "") -> Optional[dict]:
    """Look up a company on Explorium by domain (match → firmographics enrich)."""
    if not get_credential_cached("explorium_enrichment", "EXPLORIUM_API_KEY"):
        log.debug("Explorium API key not configured — skipping")
        return None
    try:
        business_id = await _explorium_match_business(domain, name)
        if not business_id:
            return None

        resp = await resilient_call(
            "explorium",
            lambda: http.post(
                f"{EXPLORIUM_BASE}/businesses/firmographics/enrich",
                headers=_explorium_headers(),
                json={"business_ids": [business_id]},
                timeout=15,
            ),
        )
        if resp.status_code != 200:
            log.warning("Explorium firmographics failed: %s", resp.status_code)
            return None

        records = _explorium_records(resp.json())
        if not records:
            return None
        firmo = records[0]
        # Tolerate both flat and firmo_-prefixed field names.
        firmo = {k.replace("firmo_", ""): v for k, v in firmo.items()}

        return {
            "source": "explorium",
            "legal_name": firmo.get("name") or firmo.get("business_name"),
            "domain": domain,
            "linkedin_url": firmo.get("linkedin") or firmo.get("linkedin_profile"),
            "industry": firmo.get("linkedin_industry_category") or firmo.get("industry"),
            "employee_size": firmo.get("number_of_employees_range") or firmo.get("company_size"),
            "hq_city": firmo.get("city_name") or firmo.get("city"),
            "hq_state": firmo.get("region_name") or firmo.get("region"),
            "hq_country": firmo.get("country_name") or firmo.get("country"),
            "website": firmo.get("website"),
            "ticker": firmo.get("ticker"),
            "naics": firmo.get("naics"),
            "revenue_range": firmo.get("yearly_revenue_range") or firmo.get("revenue_range"),
        }
    except Exception as e:
        log.error("Explorium company lookup error: %s", e)
        return None


async def _explorium_find_contacts(domain: str, title_filter: str = "") -> list[dict]:
    """Find contacts at a company via Explorium (match → prospects → contact enrich)."""
    if not get_credential_cached("explorium_enrichment", "EXPLORIUM_API_KEY"):
        return []
    try:
        business_id = await _explorium_match_business(domain)
        if not business_id:
            return []

        payload = {"business_ids": [business_id], "size": 10}
        if title_filter:
            payload["filters"] = {"job_title": [title_filter]}
        resp = await resilient_call(
            "explorium",
            lambda: http.post(
                f"{EXPLORIUM_BASE}/prospects",
                headers=_explorium_headers(),
                json=payload,
                timeout=20,
            ),
        )
        if resp.status_code != 200:
            return []
        prospects = _explorium_records(resp.json())
        if not prospects:
            return []

        # Enrich the matched prospects to get emails + phones (+ verification status).
        prospect_ids = [
            p.get("prospect_id") or p.get("id") for p in prospects
            if p.get("prospect_id") or p.get("id")
        ]
        contact_info: dict[str, dict] = {}
        if prospect_ids:
            eresp = await resilient_call(
                "explorium",
                lambda: http.post(
                    f"{EXPLORIUM_BASE}/prospects/contacts_information/enrich",
                    headers=_explorium_headers(),
                    json={"prospect_ids": prospect_ids},
                    timeout=20,
                ),
            )
            if eresp.status_code == 200:
                for rec in _explorium_records(eresp.json()):
                    pid = rec.get("prospect_id") or rec.get("id")
                    if pid:
                        contact_info[pid] = rec

        out = []
        for p in prospects:
            pid = p.get("prospect_id") or p.get("id")
            ci = contact_info.get(pid, {})
            email = ci.get("professional_email") or ci.get("email") or p.get("email")
            out.append(
                {
                    "source": "explorium",
                    "full_name": p.get("full_name") or p.get("name"),
                    "title": p.get("job_title") or p.get("title"),
                    "email": email,
                    "email_status": ci.get("professional_email_status"),
                    "phone": ci.get("phone_number") or ci.get("mobile_phone") or p.get("phone"),
                    "linkedin_url": p.get("linkedin") or p.get("linkedin_url"),
                    "location": p.get("location"),
                    "company": p.get("company_name"),
                }
            )
        return [c for c in out if c["full_name"]]
    except Exception as e:
        log.error("Explorium contacts lookup error: %s", e)
        return []


# ── Provider: Gradient AI (LLM knowledge, no web search) ─────────────────

GRADIENT_COMPANY_SYSTEM = (
    "You are a B2B company research assistant for an electronic component broker. "
    "Using your training knowledge, return firmographic data about the requested company as JSON. "
    "Return ONLY a JSON object with these keys: "
    '{"legal_name", "industry", "employee_size", "hq_city", "hq_state", "hq_country", '
    '"website", "linkedin_url"}. '
    "Use null for any field you are not confident about. Do not guess or fabricate data."
)


async def _gradient_find_company(domain: str, name: str = "") -> Optional[dict]:
    """Look up a company using Gradient AI (LLM knowledge). Returns normalized company data."""
    from .config import settings
    if not getattr(settings, "do_gradient_api_key", ""):
        return None
    try:
        from .services.gradient_service import gradient_json

        prompt = (
            f"What do you know about the company with domain '{domain}'"
            f"{f' (also known as {name})' if name else ''}?\n\n"
            f"Return their: legal name, industry, approximate employee count or range, "
            f"headquarters city/state/country, website URL, and LinkedIn company page URL.\n\n"
            f"Return ONLY valid JSON. Use null for unknown fields."
        )
        data = await gradient_json(
            prompt,
            system=GRADIENT_COMPANY_SYSTEM,
            model_tier="default",
            max_tokens=512,
            temperature=0.1,
            timeout=15,
        )
        if not data or not isinstance(data, dict):
            return None
        return {
            "source": "gradient",
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
    except Exception as e:
        log.debug("Gradient company lookup error: %s", e)
        return None


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
    """Look up a company using Claude + web search. Returns normalized company data."""
    if not get_credential_cached("anthropic_ai", "ANTHROPIC_API_KEY"):
        log.debug("Anthropic API key not configured — skipping AI enrichment")
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
            tools=[
                {"type": "web_search_20250305", "name": "web_search", "max_uses": 5}
            ],
            timeout=60,
        )
        if not data or not isinstance(data, dict):
            log.warning("AI company lookup returned no data for %s", domain)
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
    except Exception as e:
        log.error("AI company lookup error: %s", e)
        return None


async def _ai_find_contacts(
    domain: str, name: str = "", title_filter: str = ""
) -> list[dict]:
    """Find contacts at a company using Claude + web search.

    Delegates to ai_service.enrich_contacts_websearch() and normalizes
    the output to match the enrichment service contact shape.
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
    except Exception as e:
        log.error("AI contacts lookup error: %s", e)
        return []


# ── Unified Enrichment ──────────────────────────────────────────────────


async def enrich_entity(domain: str, name: str = "") -> dict:
    """Enrich a business entity (vendor or customer) by domain.

    Phase 1: Lusha, Apollo, Explorium, Clearbit, Gradient run concurrently.
    Phase 2: AI + web search fills remaining gaps (conditional).
    Merge priority: Lusha > Apollo > Explorium > Clearbit > Gradient > AI.
    Results cached in IntelCache with 14-day TTL keyed by domain.

    Note: Clay is async (webhook → callback) and is not part of this
    synchronous waterfall — see app.services.clay_service.
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
        "legal_name", "industry", "employee_size",
        "hq_city", "hq_state", "hq_country", "website", "linkedin_url",
    ]

    # ── Phase 1: Lusha + Apollo + Explorium + Clearbit concurrently ──
    async def _safe_apollo_company(domain: str):
        try:
            from .connectors.apollo_client import enrich_company as apollo_enrich
            return await apollo_enrich(domain)
        except Exception as e:
            log.debug("Apollo company enrichment skipped: %s", e)
            return None

    async def _safe_clearbit(domain: str):
        try:
            from .connectors.clearbit_client import enrich_company as clearbit_enrich
            return await clearbit_enrich(domain)
        except Exception as e:
            log.debug("Clearbit enrichment skipped: %s", e)
            return None

    lusha_result, apollo_result, exp_result, cb_result, grad_result = await asyncio.gather(
        _lusha_find_company(domain, name),
        _safe_apollo_company(domain),
        _explorium_find_company(domain, name),
        _safe_clearbit(domain),
        _gradient_find_company(domain, name),
        return_exceptions=True,
    )

    # Merge in priority order: Lusha > Apollo > Explorium > Clearbit > Gradient
    sources = []
    for provider_data, provider_name in [
        (lusha_result, "lusha"),
        (apollo_result, "apollo"),
        (exp_result, "explorium"),
        (cb_result, "clearbit"),
        (grad_result, "gradient"),
    ]:
        if isinstance(provider_data, Exception) or not provider_data:
            continue
        for k, v in provider_data.items():
            if v and not result.get(k):
                result[k] = v
        sources.append(provider_name)

    if sources:
        result["source"] = "+".join(sources)

    # ── Phase 2: AI fills remaining gaps (conditional) ──
    if any(not result.get(f) for f in _enrichable):
        ai = await _ai_find_company(domain, name)
        if ai:
            for k, v in ai.items():
                if v and not result.get(k):
                    result[k] = v
            if not result["source"]:
                result["source"] = "ai"
            elif "ai" not in result["source"]:
                result["source"] = result["source"] + "+ai"

    # Layer 2: output normalization
    normalized = normalize_company_output(result)

    # Cache 14 days when we found data; negative-cache misses for 1 day so a
    # domain with no provider coverage doesn't re-bill every lookup.
    has_data = any(v for k, v in normalized.items() if k not in ("domain", "source"))
    set_cached(cache_key, normalized, ttl_days=14 if has_data else 1)

    return normalized


async def find_suggested_contacts(
    domain: str, name: str = "", title_filter: str = ""
) -> list[dict]:
    """Find suggested contacts at a company from configured providers.

    Cheaper/paid providers (Lusha, Explorium, Hunter, RocketReach, Apollo) run
    concurrently first; the expensive AI web-search lookup only runs if those are
    sparse. Results are cached 7 days per domain+title to avoid re-billing.
    Returns a deduplicated, relevance-filtered list. Each contact has:
    full_name, title, email, phone, linkedin_url, location, source
    """
    from .cache.intel_cache import get_cached, set_cached

    cache_key = f"contacts:{_clean_domain(domain)}:{(title_filter or '').lower().strip()}"
    cached = get_cached(cache_key)
    if cached is not None:
        return cached

    async def _safe_hunter(domain: str) -> list[dict]:
        try:
            from .connectors.hunter_client import find_domain_emails
            raw = await find_domain_emails(domain, limit=10)
            return [
                {
                    "source": "hunter",
                    "full_name": hc["full_name"],
                    "title": hc.get("position"),
                    "email": hc.get("email"),
                    "phone": hc.get("phone_number"),
                    "linkedin_url": hc.get("linkedin_url"),
                    "location": None,
                    "company": name or domain,
                }
                for hc in raw
                if hc.get("full_name")
            ]
        except Exception as e:
            log.debug("Hunter contacts skipped: %s", e)
            return []

    async def _safe_rocketreach(domain: str) -> list[dict]:
        try:
            from .connectors.rocketreach_client import search_company_contacts
            raw = await search_company_contacts(
                company=name or domain, domain=domain,
                title_filter=title_filter, limit=5,
            )
            return [
                {
                    "source": "rocketreach",
                    "full_name": rc["full_name"],
                    "title": rc.get("title"),
                    "email": rc.get("email"),
                    "phone": rc.get("phone"),
                    "linkedin_url": rc.get("linkedin_url"),
                    "location": None,
                    "company": rc.get("company_name") or name or domain,
                }
                for rc in raw
                if rc.get("full_name")
            ]
        except Exception as e:
            log.debug("RocketReach contacts skipped: %s", e)
            return []

    async def _safe_apollo_contacts(domain: str) -> list[dict]:
        try:
            from .connectors.apollo_client import search_contacts as apollo_search
            raw = await apollo_search(
                company_name=name or domain,
                domain=domain,
                title_keywords=[title_filter] if title_filter else None,
                limit=5,
            )
            return [
                {
                    "source": "apollo",
                    "full_name": ac.get("full_name"),
                    "title": ac.get("title"),
                    "email": ac.get("email"),
                    "phone": ac.get("phone"),
                    "linkedin_url": ac.get("linkedin_url"),
                    "location": ac.get("city"),
                    "company": name or domain,
                }
                for ac in raw
                if ac.get("full_name")
            ]
        except Exception as e:
            log.debug("Apollo contacts skipped: %s", e)
            return []

    # Filter to relevant B2B titles — avoid wasting credits on irrelevant contacts
    _RELEVANT_KEYWORDS = {
        "procurement", "purchasing", "buyer", "sourcing", "supply chain",
        "component", "commodity", "materials", "vendor", "supplier",
        "sales", "account", "business development", "director",
        "president", "vp", "manager", "engineer", "operations",
        "logistics", "inventory", "planning", "quality",
    }

    def _is_relevant(contact: dict) -> bool:
        title = (contact.get("title") or "").lower()
        if not title:
            return bool(contact.get("email"))  # Keep if has email but no title
        return any(kw in title for kw in _RELEVANT_KEYWORDS)

    def _dedupe(contacts: list[dict]) -> list[dict]:
        seen: set = set()
        unique: list[dict] = []
        for c in contacts:
            key = (
                (c.get("email") or "").lower()
                or c.get("linkedin_url")
                or (c.get("full_name") or "").lower()
            )
            if not key or key in seen:
                continue
            seen.add(key)
            unique.append(c)
        return unique

    def _collect(results) -> list[dict]:
        out: list[dict] = []
        for r in results:
            if isinstance(r, Exception):
                log.debug("Contact provider failed: %s", r)
                continue
            out.extend(r)
        return out

    # Tier 1: the paid/cheaper providers run concurrently.
    tier1 = await asyncio.gather(
        _lusha_find_contacts(domain, title_filter),
        _explorium_find_contacts(domain, title_filter),
        _safe_hunter(domain),
        _safe_rocketreach(domain),
        _safe_apollo_contacts(domain),
        return_exceptions=True,
    )
    unique = _dedupe(_collect(tier1))

    # Tier 2: only spend on the AI web-search lookup if results are still sparse.
    AI_CONTACT_THRESHOLD = 3
    if sum(1 for c in unique if _is_relevant(c)) < AI_CONTACT_THRESHOLD:
        ai = await _ai_find_contacts(domain, name, title_filter)
        if isinstance(ai, list):
            unique = _dedupe(unique + ai)

    filtered = [c for c in unique if _is_relevant(c)]
    # If filter removed everything, return unfiltered (don't lose all results)
    result = filtered if filtered else unique

    if result:
        set_cached(cache_key, result, ttl_days=7)
    return result


def apply_enrichment_to_company(company, data: dict) -> list[str]:
    """Apply enrichment data dict to a Company model. Returns list of fields updated."""
    updated = []
    field_map = {
        "domain": "domain",
        "linkedin_url": "linkedin_url",
        "legal_name": "legal_name",
        "industry": "industry",
        "employee_size": "employee_size",
        "hq_city": "hq_city",
        "hq_state": "hq_state",
        "hq_country": "hq_country",
    }
    for data_key, model_field in field_map.items():
        val = data.get(data_key)
        if val and not getattr(company, model_field, None):
            setattr(company, model_field, val)
            updated.append(model_field)
    # Website: only set if empty
    if data.get("website") and not company.website:
        company.website = data["website"]
        updated.append("website")
    if updated:
        company.last_enriched_at = datetime.now(timezone.utc)
        company.enrichment_source = data.get("source", "unknown")
    return updated


def apply_enrichment_to_vendor(card, data: dict) -> list[str]:
    """Apply enrichment data dict to a VendorCard model. Returns list of fields updated."""
    updated = []
    field_map = {
        "linkedin_url": "linkedin_url",
        "legal_name": "legal_name",
        "industry": "industry",
        "employee_size": "employee_size",
        "hq_city": "hq_city",
        "hq_state": "hq_state",
        "hq_country": "hq_country",
    }
    for data_key, model_field in field_map.items():
        val = data.get(data_key)
        if val and not getattr(card, model_field, None):
            setattr(card, model_field, val)
            updated.append(model_field)
    # Domain: only set if empty
    if data.get("domain") and not card.domain:
        card.domain = data["domain"]
        updated.append("domain")
    if data.get("website") and not card.website:
        card.website = data["website"]
        updated.append("website")
    if updated:
        card.last_enriched_at = datetime.now(timezone.utc)
        card.enrichment_source = data.get("source", "unknown")
    return updated
