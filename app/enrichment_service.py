"""AVAIL v1.2.0 — Unified Enrichment Service

Shared enrichment workflow for both vendor cards and customer companies.
Supports Clay, Explorium (Vibe Prospecting), and AI (Claude + web search)
as enrichment providers. AI runs last to fill any remaining gaps.
"""

import httpx
import logging
import re
from datetime import datetime, timezone
from typing import Optional

from .config import settings
from .utils.claude_client import claude_json, claude_text
from .services.ai_service import enrich_contacts_websearch

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
    if clean_name and _name_looks_suspicious(clean_name) and settings.anthropic_api_key:
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


# ── Provider: Clay ──────────────────────────────────────────────────────

CLAY_BASE = "https://api.clay.com/v3/sources"


async def _clay_find_company(domain: str) -> Optional[dict]:
    """Look up a company on Clay by domain. Returns normalized company data."""
    if not settings.clay_api_key:
        log.debug("Clay API key not configured — skipping")
        return None
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{CLAY_BASE}/enrich-company",
                headers={
                    "Authorization": f"Bearer {settings.clay_api_key}",
                    "Content-Type": "application/json",
                },
                json={"domain": domain},
            )
            if resp.status_code != 200:
                log.warning(
                    "Clay company lookup failed: %s %s",
                    resp.status_code,
                    resp.text[:200],
                )
                return None
            data = resp.json()
            return {
                "source": "clay",
                "legal_name": data.get("name"),
                "domain": domain,
                "linkedin_url": data.get("linkedin_url") or data.get("url"),
                "industry": data.get("industry"),
                "employee_size": data.get("size"),
                "hq_city": data.get("locality", "").split(",")[0].strip()
                if data.get("locality")
                else None,
                "hq_state": data.get("locality", "").split(",")[-1].strip()
                if data.get("locality") and "," in data.get("locality", "")
                else None,
                "hq_country": data.get("country"),
                "website": data.get("website"),
            }
    except Exception as e:
        log.error("Clay company lookup error: %s", e)
        return None


async def _clay_find_contacts(domain: str, title_filter: str = "") -> list[dict]:
    """Find contacts at a company via Clay. Returns list of contact dicts."""
    if not settings.clay_api_key:
        return []
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            payload = {"domain": domain}
            if title_filter:
                payload["title"] = title_filter
            resp = await client.post(
                f"{CLAY_BASE}/find-people",
                headers={
                    "Authorization": f"Bearer {settings.clay_api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            if resp.status_code != 200:
                log.warning("Clay contacts lookup failed: %s", resp.status_code)
                return []
            people = resp.json().get("people") or resp.json().get("contacts") or []
            return [
                {
                    "source": "clay",
                    "full_name": p.get("name") or p.get("full_name"),
                    "title": p.get("title") or p.get("latest_experience_title"),
                    "email": p.get("email"),
                    "phone": p.get("phone"),
                    "linkedin_url": p.get("linkedin_url") or p.get("url"),
                    "location": p.get("location_name") or p.get("location"),
                    "company": p.get("company") or p.get("latest_experience_company"),
                }
                for p in people
                if p.get("name") or p.get("full_name")
            ]
    except Exception as e:
        log.error("Clay contacts lookup error: %s", e)
        return []


# ── Provider: Explorium (Vibe Prospecting) ──────────────────────────────

EXPLORIUM_BASE = "https://api.explorium.ai/v1"


async def _explorium_find_company(domain: str, name: str = "") -> Optional[dict]:
    """Look up a company on Explorium by domain. Returns normalized company data."""
    if not settings.explorium_api_key:
        log.debug("Explorium API key not configured — skipping")
        return None
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{EXPLORIUM_BASE}/match/business",
                headers={
                    "Authorization": f"Bearer {settings.explorium_api_key}",
                    "Content-Type": "application/json",
                },
                json={"domain": domain, "name": name},
            )
            if resp.status_code != 200:
                log.warning("Explorium company lookup failed: %s", resp.status_code)
                return None
            data = resp.json()
            firmo = {
                k.replace("firmo_", ""): v
                for k, v in data.items()
                if k.startswith("firmo_")
            }
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
    except Exception as e:
        log.error("Explorium company lookup error: %s", e)
        return None


async def _explorium_find_contacts(domain: str, title_filter: str = "") -> list[dict]:
    """Find contacts at a company via Explorium."""
    if not settings.explorium_api_key:
        return []
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            payload = {"company_domain": domain}
            if title_filter:
                payload["job_title_keywords"] = [title_filter]
            resp = await client.post(
                f"{EXPLORIUM_BASE}/fetch/prospects",
                headers={
                    "Authorization": f"Bearer {settings.explorium_api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            if resp.status_code != 200:
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
    except Exception as e:
        log.error("Explorium contacts lookup error: %s", e)
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
    """Look up a company using Claude + web search. Returns normalized company data."""
    if not settings.anthropic_api_key:
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
    if not settings.anthropic_api_key:
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

    Tries Clay first, Explorium fills gaps, AI fills remaining gaps.
    Input is normalized before providers, output is normalized after.
    """
    # Layer 1: input cleanup
    name, domain = await normalize_company_input(name, domain)

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

    # Try Clay
    clay = await _clay_find_company(domain)
    if clay:
        result.update({k: v for k, v in clay.items() if v})
        result["source"] = "clay"

    # Try Explorium (fills gaps)
    exp = await _explorium_find_company(domain, name)
    if exp:
        for k, v in exp.items():
            if v and not result.get(k):
                result[k] = v
        if not result["source"]:
            result["source"] = "explorium"
        elif result["source"] == "clay":
            result["source"] = "clay+explorium"

    # Try AI (fills remaining gaps)
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
    return normalize_company_output(result)


async def find_suggested_contacts(
    domain: str, name: str = "", title_filter: str = ""
) -> list[dict]:
    """Find suggested contacts at a company from all configured providers.

    Returns deduplicated list sorted by relevance. Each contact has:
    full_name, title, email, phone, linkedin_url, location, source
    """
    all_contacts = []

    # Gather from all providers
    clay_contacts = await _clay_find_contacts(domain, title_filter)
    all_contacts.extend(clay_contacts)

    exp_contacts = await _explorium_find_contacts(domain, title_filter)
    all_contacts.extend(exp_contacts)

    ai_contacts = await _ai_find_contacts(domain, name, title_filter)
    all_contacts.extend(ai_contacts)

    # Deduplicate by email or linkedin_url or full_name
    seen = set()
    unique = []
    for c in all_contacts:
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
