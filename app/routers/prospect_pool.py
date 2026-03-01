"""Prospect pool router — browse, claim, and dismiss unowned companies."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..database import get_db
from ..dependencies import require_user
from ..models import Company, User
from ..schemas.prospect_pool import PoolDismissRequest, PoolFilters
from ..services.prospect_pool_service import (
    claim_pool_account,
    dismiss_pool_account,
    get_pool_accounts,
    get_pool_stats,
)

router = APIRouter()


@router.get("/api/prospects/pool")
async def list_pool_accounts(
    search: str = "",
    import_priority: str = "",
    industry: str = "",
    sort_by: str = "priority",
    page: int = 1,
    per_page: int = 20,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """List unowned companies available in the prospect pool."""
    filters = PoolFilters(
        search=search or None,
        import_priority=import_priority or None,
        industry=industry or None,
        sort_by=sort_by,
        page=max(1, page),
        per_page=min(max(1, per_page), 100),
    )
    return get_pool_accounts(filters, db)


@router.get("/api/prospects/pool/stats")
async def pool_stats(
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Pool aggregate statistics for the stats bar."""
    return get_pool_stats(db)


@router.get("/api/prospects/pool/{company_id}")
async def get_pool_account(
    company_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Single pool account detail."""
    company = db.get(Company, company_id)
    if not company:
        raise HTTPException(404, "Company not found")
    if company.account_owner_id is not None:
        raise HTTPException(404, "Company is not in the pool")
    return {
        "id": company.id,
        "name": company.name,
        "domain": company.domain,
        "website": company.website,
        "industry": company.industry,
        "phone": company.phone,
        "hq_city": company.hq_city,
        "hq_state": company.hq_state,
        "hq_country": company.hq_country,
        "import_priority": company.import_priority,
        "sf_account_id": company.sf_account_id,
        "notes": company.notes,
    }


@router.post("/api/prospects/pool/{company_id}/claim")
async def claim_account(
    company_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Claim a pool account — assigns to current user."""
    result = claim_pool_account(company_id, user.id, user.name, db)
    if result.get("error"):
        raise HTTPException(result["status"], result["error"])
    return result


@router.post("/api/prospects/pool/{company_id}/dismiss")
async def dismiss_account(
    company_id: int,
    payload: PoolDismissRequest,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Dismiss a pool account with a reason."""
    result = dismiss_pool_account(company_id, user.id, user.name, payload.reason, db)
    if result.get("error"):
        raise HTTPException(result["status"], result["error"])
    return result
