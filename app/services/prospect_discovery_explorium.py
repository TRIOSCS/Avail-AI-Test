"""Explorium/Vibe Prospecting discovery service — primary discovery source.

Finds companies matching ICP segments with firmographics + intent/hiring/event
signals in a single API call. Normalizes results into ProspectAccountCreate schemas.
"""

import asyncio

from loguru import logger

from app.config import settings
from app.http_client import http
from app.schemas.prospect_account import ProspectAccountCreate
from app.services.prospect_scoring import (
    calculate_fit_score,
    calculate_readiness_score,
)

EXPLORIUM_BASE = getattr(settings, "explorium_api_base_url", "https://api.explorium.ai")

# ── Segment search definitions ───────────────────────────────────────

SEGMENT_SEARCH_PARAMS = {
    "aerospace_defense": {
        "linkedin_categories": [
            "Aviation and Aerospace Component Manufacturing",
            "Defense and Space Manufacturing",
        ],
        "naics_codes": ["336412", "336413"],
        "intent_keywords": ["mil-spec components", "ITAR compliance"],
    },
    "service_supply_chain": {
        "linkedin_categories": [
            "Medical Devices",
            "Industrial Machinery Manufacturing",
            "Measuring and Control Instrument Manufacturing",
        ],
        "naics_codes": ["334513", "333314", "334510"],
        "intent_keywords": ["spare parts procurement", "installed base management"],
    },
    "ems_electronics": {
        "linkedin_categories": [
            "Semiconductor Manufacturing",
            "Electronic Manufacturing Services",
            "Printed Circuit Board Manufacturing",
        ],
        "naics_codes": ["334418", "334417", "334112"],
        "intent_keywords": ["BOM sourcing", "component allocation"],
    },
    "automotive": {
        "linkedin_categories": [
            "Motor Vehicle Parts Manufacturing",
            "Automotive",
        ],
        "naics_codes": ["336310", "336360"],
        "intent_keywords": ["automotive electronics", "vehicle electrification"],
    },
}

REGIONS = {
    "US": ["US"],
    "EU": ["DE", "GB", "FR", "NL", "SE"],
    "Asia": ["CN", "JP", "KR", "TW", "SG", "IN"],
}

SIZE_RANGES = ["201-500", "501-1000", "1001-5000", "5001-10000", "10001+"]

SHARED_INTENT_TOPICS = [
    "electronic components",
    "integrated circuits",
    "semiconductors",
    "procurement solutions",
    "component sourcing",
]


def _get_api_key() -> str:
    return getattr(settings, "explorium_api_key", "")


# ── API calls ────────────────────────────────────────────────────────


async def discover_companies_with_signals(
    segment_key: str, region_key: str
) -> list[dict]:
    """Call Explorium API to find companies matching an ICP segment + region.

    Returns raw normalized dicts with company data + signal data combined.
    """
    api_key = _get_api_key()
    if not api_key:
        logger.warning("Explorium API key not configured — skipping discovery")
        return []

    seg = SEGMENT_SEARCH_PARAMS.get(segment_key)
    if not seg:
        logger.error("Unknown segment key: {}", segment_key)
        return []

    country_codes = REGIONS.get(region_key, ["US"])

    payload = {
        "linkedin_categories": seg["linkedin_categories"],
        "naics_codes": seg["naics_codes"],
        "company_size": SIZE_RANGES,
        "company_country_code": country_codes,
        "business_intent_topics": SHARED_INTENT_TOPICS + seg["intent_keywords"],
        "limit": 50,
    }

    try:
        resp = await http.post(
            f"{EXPLORIUM_BASE}/v1/businesses/search",
            json=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
            timeout=60,
        )

        if resp.status_code != 200:
            logger.warning(
                "Explorium search failed for {}/{}: {} {}",
                segment_key, region_key, resp.status_code, resp.text[:200],
            )
            return []

        data = resp.json()
        businesses = data.get("businesses", data.get("results", []))
        if not isinstance(businesses, list):
            businesses = []

        logger.info(
            "Explorium {}/{}: {} raw results",
            segment_key, region_key, len(businesses),
        )

        return [normalize_explorium_result(b, segment_key) for b in businesses]

    except Exception as e:
        logger.error("Explorium API error for {}/{}: {}", segment_key, region_key, e)
        return []


def normalize_explorium_result(raw: dict, segment_key: str) -> dict:
    """Map Explorium response fields to our prospect schema.

    Extracts firmographics + signals into a unified dict ready for scoring.
    """
    domain = (raw.get("domain") or raw.get("website_domain") or "").strip().lower()
    if domain.startswith("www."):
        domain = domain[4:]

    # Firmographics
    result = {
        "name": raw.get("company_name") or raw.get("name") or "",
        "domain": domain,
        "website": raw.get("website") or raw.get("website_url"),
        "industry": raw.get("industry") or raw.get("linkedin_industry"),
        "naics_code": raw.get("naics_code") or raw.get("primary_naics_code"),
        "employee_count_range": _normalize_size(raw),
        "revenue_range": raw.get("annual_revenue") or raw.get("revenue_range"),
        "hq_location": _build_location(raw),
        "region": _detect_region(raw),
        "description": raw.get("description") or raw.get("short_description"),
        "parent_company_domain": raw.get("parent_domain"),
        "discovery_source": "explorium",
        "segment_key": segment_key,
    }

    # Intent signals
    intent_topics = raw.get("business_intent_topics") or raw.get("intent_topics") or []
    if isinstance(intent_topics, list) and intent_topics:
        component_topics = [
            t for t in intent_topics
            if any(kw in t.lower() for kw in [
                "electronic", "component", "semiconductor", "circuit",
                "procurement", "sourcing",
            ])
        ]
        if len(component_topics) >= 3:
            intent_strength = "strong"
        elif len(component_topics) >= 1:
            intent_strength = "moderate"
        else:
            intent_strength = "weak"
        result["intent"] = {
            "strength": intent_strength,
            "topics": intent_topics,
            "component_topics": component_topics,
        }
    else:
        result["intent"] = {}

    # Hiring signals
    workforce = raw.get("workforce_trends") or raw.get("department_growth") or {}
    if isinstance(workforce, dict):
        procurement_growth = workforce.get("procurement") or workforce.get("purchasing")
        engineering_growth = workforce.get("engineering") or workforce.get("r_and_d")

        if procurement_growth and (
            isinstance(procurement_growth, (int, float)) and procurement_growth > 0
            or isinstance(procurement_growth, str) and "growth" in procurement_growth.lower()
        ):
            result["hiring"] = {"type": "procurement", "detail": procurement_growth}
        elif engineering_growth and (
            isinstance(engineering_growth, (int, float)) and engineering_growth > 0
            or isinstance(engineering_growth, str) and "growth" in engineering_growth.lower()
        ):
            result["hiring"] = {"type": "engineering", "detail": engineering_growth}
        else:
            result["hiring"] = {}
    else:
        result["hiring"] = {}

    # Company events
    events_raw = raw.get("recent_events") or raw.get("events") or []
    result["events"] = []
    if isinstance(events_raw, list):
        for ev in events_raw:
            if isinstance(ev, dict):
                result["events"].append({
                    "type": ev.get("type") or ev.get("event_type", "unknown"),
                    "date": ev.get("date") or ev.get("event_date"),
                    "description": ev.get("description") or ev.get("title"),
                })
            elif isinstance(ev, str):
                result["events"].append({"type": ev, "date": None, "description": ev})

    # Raw data for enrichment_data JSONB
    result["enrichment_raw"] = raw

    return result


def _normalize_size(raw: dict) -> str | None:
    """Extract employee count range from various Explorium fields."""
    size = raw.get("company_size") or raw.get("employee_count") or raw.get("estimated_num_employees")
    if isinstance(size, str):
        return size
    if isinstance(size, (int, float)):
        n = int(size)
        if n <= 50:
            return "1-50"
        elif n <= 200:
            return "51-200"
        elif n <= 500:
            return "201-500"
        elif n <= 1000:
            return "501-1000"
        elif n <= 5000:
            return "1001-5000"
        elif n <= 10000:
            return "5001-10000"
        else:
            return "10001+"
    return None


def _build_location(raw: dict) -> str | None:
    """Build location string from city/state/country fields."""
    parts = [
        raw.get("city") or raw.get("hq_city"),
        raw.get("state") or raw.get("hq_state"),
        raw.get("country") or raw.get("hq_country") or raw.get("country_code"),
    ]
    location = ", ".join(filter(None, parts))
    return location or None


def _detect_region(raw: dict) -> str | None:
    """Detect region from country code."""
    cc = (raw.get("country_code") or raw.get("hq_country") or "").upper()
    if cc in ("US", "USA", "UNITED STATES"):
        return "US"
    if cc in ("DE", "GB", "FR", "NL", "SE", "IT", "ES", "CH", "AT", "BE"):
        return "EU"
    if cc in ("CN", "JP", "KR", "TW", "SG", "IN", "TH", "VN", "MY"):
        return "Asia"
    if cc:
        return cc
    return None


# ── Batch orchestration ──────────────────────────────────────────────


async def run_explorium_discovery_batch(
    batch_id: str, existing_domains: set[str] | None = None
) -> list[ProspectAccountCreate]:
    """Run discovery across all ICP segments x regions.

    Args:
        batch_id: human-readable batch identifier
        existing_domains: domains already in prospect_accounts + owned companies (for dedup)

    Returns list of ProspectAccountCreate schemas ready for DB insert.
    """
    if not _get_api_key():
        logger.warning("Explorium API key not configured — batch {} skipped", batch_id)
        return []

    known_domains = existing_domains or set()
    seen_domains: set[str] = set()
    prospects: list[ProspectAccountCreate] = []
    total_raw = 0
    credits_est = 0

    for seg_key in SEGMENT_SEARCH_PARAMS:
        for region_key in REGIONS:
            results = await discover_companies_with_signals(seg_key, region_key)
            total_raw += len(results)
            credits_est += len(results)  # ~1 credit per result

            for r in results:
                domain = r.get("domain", "")
                if not domain:
                    continue

                # Dedup
                if domain in seen_domains:
                    continue
                if domain in known_domains:
                    logger.debug("Dedup skip: {} already known", domain)
                    continue

                seen_domains.add(domain)

                # Score immediately with signal data
                fit_data = {
                    "name": r.get("name"),
                    "industry": r.get("industry"),
                    "naics_code": r.get("naics_code"),
                    "employee_count_range": r.get("employee_count_range"),
                    "region": r.get("region"),
                    "has_procurement_staff": None,  # filled by Apollo in Phase 3B
                    "uses_brokers": None,
                }
                fit_score, fit_reasoning = calculate_fit_score(fit_data)

                signals = {
                    "intent": r.get("intent", {}),
                    "events": r.get("events", []),
                    "hiring": r.get("hiring", {}),
                }
                readiness_score, readiness_breakdown = calculate_readiness_score(
                    fit_data, signals
                )

                prospect = ProspectAccountCreate(
                    name=r["name"],
                    domain=domain,
                    website=r.get("website"),
                    industry=r.get("industry"),
                    naics_code=r.get("naics_code"),
                    employee_count_range=r.get("employee_count_range"),
                    revenue_range=r.get("revenue_range"),
                    hq_location=r.get("hq_location"),
                    region=r.get("region"),
                    description=r.get("description"),
                    parent_company_domain=r.get("parent_company_domain"),
                    discovery_source="explorium",
                    enrichment_data={
                        "explorium": r.get("enrichment_raw", {}),
                        "signals": {
                            "intent": r.get("intent", {}),
                            "events": r.get("events", []),
                            "hiring": r.get("hiring", {}),
                        },
                    },
                )
                prospects.append(prospect)

            # Small delay between searches to be polite to the API
            await asyncio.sleep(0.5)

    logger.info(
        "Explorium batch {}: {} raw results, {} unique after dedup, ~{} credits",
        batch_id, total_raw, len(prospects), credits_est,
    )

    return prospects
