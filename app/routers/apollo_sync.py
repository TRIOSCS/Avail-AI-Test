"""Apollo sync router -- contact discovery, enrichment, sync, and sequences.

Provides /api/apollo/* endpoints for bidirectional Apollo.io integration.
Discovery returns masked emails; enrichment reveals full contact data.

Called by: app/main.py
Depends on: app/services/apollo_sync_service.py, app/dependencies.py
"""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from ..database import get_db
from ..dependencies import require_user
from ..schemas.apollo import (
    ApolloCreditsResponse,
    ApolloEnrichRequest,
    ApolloEnrichResponse,
    ApolloSyncResponse,
)
from ..services.apollo_sync_service import (
    discover_contacts,
    enrich_selected_contacts,
    get_credits,
    sync_contacts_to_apollo,
)

router = APIRouter(prefix="/api/apollo", tags=["apollo"])


@router.get("/discover/{domain}")
async def discover(
    domain: str,
    max_results: int = Query(default=10, ge=1, le=25),
    user=Depends(require_user),
):
    """Search Apollo for procurement contacts at a domain. Returns masked preview."""
    return await discover_contacts(domain, max_results=max_results)


@router.post("/enrich", response_model=ApolloEnrichResponse)
async def enrich(
    req: ApolloEnrichRequest,
    user=Depends(require_user),
    db: Session = Depends(get_db),
):
    """Enrich selected contacts via Apollo people/match. Costs 1 lead credit each."""
    return await enrich_selected_contacts(
        apollo_ids=req.apollo_ids,
        vendor_card_id=req.vendor_card_id,
        db=db,
    )


@router.get("/credits", response_model=ApolloCreditsResponse)
async def credits(user=Depends(require_user)):
    """Get current Apollo credit usage."""
    return await get_credits()


@router.post("/sync-contacts", response_model=ApolloSyncResponse)
async def sync(
    user=Depends(require_user),
    db: Session = Depends(get_db),
):
    """Push AvailAI vendor contacts to Apollo (dedup enabled)."""
    return await sync_contacts_to_apollo(db=db)
