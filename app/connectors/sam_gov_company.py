"""SAM.gov entity → firmographic adapter for the company enrichment chain.

Wraps the public SAM.gov entity-information API (the same source as
prospect_free_enrichment.enrich_from_sam_gov) but keyed by company name/domain and
returning the shared firmographic shape (authoritative legal_name / NAICS / HQ).

Called by: app/services/enrichment_router.py. Depends on: app/http_client.py.
"""

import httpx
from loguru import logger

from app.http_client import http
from app.services.credential_service import get_credential_cached

_URL = "https://api.sam.gov/entity-information/v3/entities"


async def enrich_company(domain: str, name: str) -> dict | None:
    """Look up a company on SAM.gov by legal name and return firmographic data.

    Returns None immediately if *name* is empty (no useful search key). Degrades to None
    on HTTP errors, non-200 responses, or empty result sets. Uses DEMO_KEY when no
    SAM_GOV_API_KEY credential is configured (free public tier, 10 req/min).

    402/429 → logs warning, returns None (not a ProviderQuotaError — SAM.gov is free).
    """
    if not name:
        return None
    key = get_credential_cached("sam_gov", "SAM_GOV_API_KEY") or "DEMO_KEY"
    try:
        resp = await http.get(
            _URL,
            params={"api_key": key, "legalBusinessName": name, "registrationStatus": "A"},
            timeout=15,
        )
        if resp.status_code != 200:
            logger.warning("SAM.gov company lookup failed: {}", resp.status_code)
            return None
        ents = resp.json().get("entityData") or []
        if not ents:
            return None
        reg = ents[0].get("entityRegistration") or {}
        core = ents[0].get("coreData") or {}
        addr = core.get("physicalAddress") or {}
        naics = core.get("assertions", {}).get("goodsAndServices", {}).get("primaryNaics")
        out = {
            "source": "sam_gov",
            "legal_name": reg.get("legalBusinessName"),
            "hq_city": addr.get("city"),
            "hq_state": addr.get("stateOrProvinceCode"),
            "hq_country": addr.get("countryCode"),
            "naics": naics,
        }
        # Return None if no informative field has a value.
        if not any(v for k, v in out.items() if k != "source"):
            return None
        return out
    except (httpx.HTTPError, KeyError, ValueError) as exc:
        logger.warning("SAM.gov company error: {}", exc)
        return None
