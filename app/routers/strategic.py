"""Strategic Vendors API — claim, drop, replace, and query strategic vendors.

Each buyer can claim up to 10 strategic vendors with a 39-day TTL.
Offers reset the clock; expired vendors return to the open pool.

Called by: frontend (crm.js)
Depends on: services/strategic_vendor_service.py, dependencies.py
"""

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import require_buyer, require_user
from app.services import strategic_vendor_service as svc

router = APIRouter(prefix="/api/strategic-vendors", tags=["strategic-vendors"])


class ReplaceRequest(BaseModel):
    drop_vendor_card_id: int
    claim_vendor_card_id: int


def _vendor_to_dict(record):
    """Convert a StrategicVendor record to API response dict."""
    from datetime import datetime, timezone
    from app.services.strategic_vendor_service import _ensure_utc

    now = datetime.now(timezone.utc)
    expires = _ensure_utc(record.expires_at)
    days_left = max(0, (expires - now).days)
    return {
        "id": record.id,
        "vendor_card_id": record.vendor_card_id,
        "vendor_name": record.vendor_card.display_name if record.vendor_card else None,
        "vendor_score": record.vendor_card.vendor_score if record.vendor_card else None,
        "claimed_at": record.claimed_at.isoformat(),
        "last_offer_at": record.last_offer_at.isoformat() if record.last_offer_at else None,
        "expires_at": record.expires_at.isoformat(),
        "days_remaining": days_left,
    }


@router.get("/mine")
def get_mine(
    db: Session = Depends(get_db),
    user=Depends(require_user),
):
    """Get current user's strategic vendors."""
    vendors = svc.get_my_strategic(db, user.id)
    count = len(vendors)
    return {
        "vendors": [_vendor_to_dict(v) for v in vendors],
        "count": count,
        "max": svc.MAX_STRATEGIC_VENDORS,
        "slots_remaining": svc.MAX_STRATEGIC_VENDORS - count,
    }


@router.post("/claim/{vendor_card_id}")
def claim_vendor(
    vendor_card_id: int,
    db: Session = Depends(get_db),
    user=Depends(require_buyer),
):
    """Claim a vendor as strategic."""
    record, err = svc.claim_vendor(db, user.id, vendor_card_id)
    if not record:
        return JSONResponse(status_code=409, content={"error": err, "status_code": 409})
    return _vendor_to_dict(record)


@router.delete("/drop/{vendor_card_id}")
def drop_vendor(
    vendor_card_id: int,
    db: Session = Depends(get_db),
    user=Depends(require_buyer),
):
    """Drop a strategic vendor back to the open pool."""
    ok, err = svc.drop_vendor(db, user.id, vendor_card_id)
    if not ok:
        return JSONResponse(status_code=404, content={"error": err, "status_code": 404})
    return {"ok": True}


@router.post("/replace")
def replace_vendor(
    body: ReplaceRequest,
    db: Session = Depends(get_db),
    user=Depends(require_buyer),
):
    """Atomic swap: drop one vendor, claim another."""
    record, err = svc.replace_vendor(
        db, user.id, body.drop_vendor_card_id, body.claim_vendor_card_id
    )
    if not record:
        return JSONResponse(status_code=409, content={"error": err, "status_code": 409})
    return _vendor_to_dict(record)


@router.get("/status/{vendor_card_id}")
def vendor_status(
    vendor_card_id: int,
    db: Session = Depends(get_db),
    user=Depends(require_user),
):
    """Check if a vendor is claimed and by whom."""
    status = svc.get_vendor_status(db, vendor_card_id)
    if not status:
        return {"vendor_card_id": vendor_card_id, "status": "open_pool"}
    return status


@router.get("/open-pool")
def open_pool(
    search: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    user=Depends(require_user),
):
    """List vendors not claimed by anyone."""
    vendors, total = svc.get_open_pool(db, limit=limit, offset=offset, search=search)
    return {
        "vendors": [
            {"id": v.id, "display_name": v.display_name, "vendor_score": v.vendor_score}
            for v in vendors
        ],
        "total": total,
        "limit": limit,
        "offset": offset,
    }
