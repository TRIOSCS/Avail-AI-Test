"""Explorium/Vibe discovery router -- ICP segment browsing, company discovery, status.

Provides /api/explorium/* endpoints for discovering companies via Explorium's
firmographic + intent/hiring/event signal API. Results are normalized into
prospect-compatible schemas for downstream scoring and import.

Called by: app/main.py
Depends on: app/services/prospect_discovery_explorium.py, app/dependencies.py
"""

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from loguru import logger

from ..dependencies import require_user
from ..schemas.explorium import (
    DiscoveredCompany,
    DiscoverRequest,
    DiscoverResponse,
    ExploriumSegment,
    ExploriumStatus,
    SegmentsResponse,
)
from ..services.prospect_discovery_explorium import (
    REGIONS,
    SEGMENT_SEARCH_PARAMS,
    _get_api_key,
    discover_companies_with_signals,
)

router = APIRouter(prefix="/api/explorium", tags=["explorium"])


def _segment_key_to_name(key: str) -> str:
    """Convert a segment key like 'aerospace_defense' to 'Aerospace Defense'."""
    return key.replace("_", " ").title()


@router.get("/segments", response_model=SegmentsResponse)
async def list_segments(user=Depends(require_user)):
    """List all available ICP segments and regions for Explorium discovery."""
    segments = []
    for key, params in SEGMENT_SEARCH_PARAMS.items():
        segments.append(
            ExploriumSegment(
                key=key,
                name=_segment_key_to_name(key),
                linkedin_categories=params.get("linkedin_categories", []),
                naics_codes=params.get("naics_codes", []),
                intent_keywords=params.get("intent_keywords", []),
            )
        )
    return SegmentsResponse(segments=segments, regions=REGIONS)


@router.post("/discover", response_model=DiscoverResponse)
async def discover(req: DiscoverRequest, user=Depends(require_user)):
    """Discover companies matching an ICP segment and region via Explorium API."""
    if req.segment not in SEGMENT_SEARCH_PARAMS:
        return JSONResponse(
            status_code=400,
            content={"error": f"Unknown segment: {req.segment}", "status_code": 400},
        )

    if req.region not in REGIONS:
        return JSONResponse(
            status_code=400,
            content={"error": f"Unknown region: {req.region}", "status_code": 400},
        )

    logger.info(
        "Explorium discover: segment={}, region={}, user={}",
        req.segment,
        req.region,
        getattr(user, "email", "unknown"),
    )

    results = await discover_companies_with_signals(req.segment, req.region)

    companies = [DiscoveredCompany(**r) for r in results]

    return DiscoverResponse(
        segment=req.segment,
        region=req.region,
        companies=companies,
        total=len(companies),
    )


@router.get("/status", response_model=ExploriumStatus)
async def status(user=Depends(require_user)):
    """Check Explorium API connectivity and configuration status."""
    api_key = _get_api_key()
    if not api_key:
        return ExploriumStatus(
            configured=False,
            reachable=False,
            message="Explorium API key not configured",
        )

    # Try a minimal API call to verify connectivity
    try:
        results = await discover_companies_with_signals("ems_electronics", "US")
        reachable = isinstance(results, list)
        return ExploriumStatus(
            configured=True,
            reachable=reachable,
            message=f"OK — {len(results)} test results" if reachable else "API returned unexpected response",
        )
    except Exception as e:
        logger.warning("Explorium status check failed: {}", e)
        return ExploriumStatus(
            configured=True,
            reachable=False,
            message=f"API error: {e}",
        )
