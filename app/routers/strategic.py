"""Strategic vendor API endpoints — claim, drop, replace, status, open pool.

Called by: frontend HTMX, tests/test_strategic_vendors.py
Depends on: app/services/strategic_vendor_service.py, app/dependencies.py
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.dependencies import get_db, require_user
from app.models import User
from app.services import strategic_vendor_service as svc

router = APIRouter(prefix="/api/strategic-vendors", tags=["strategic-vendors"])


class ReplaceRequest(BaseModel):
    drop_vendor_card_id: int
    claim_vendor_card_id: int


def _serialize_record(record) -> dict:
    """Serialize a StrategicVendor record to JSON-safe dict."""
    return {
        "id": record.id,
        "user_id": record.user_id,
        "vendor_card_id": record.vendor_card_id,
        "claimed_at": record.claimed_at.isoformat() if record.claimed_at else None,
        "expires_at": record.expires_at.isoformat() if record.expires_at else None,
        "released_at": record.released_at.isoformat() if record.released_at else None,
    }


@router.get("/mine")
def get_my_vendors(user: User = Depends(require_user), db: Session = Depends(get_db)):
    """Return all active strategic vendors for the current user."""
    records = svc.get_my_strategic(db, user.id)
    return {
        "vendors": [_serialize_record(r) for r in records],
        "count": len(records),
        "cap": svc.MAX_STRATEGIC_VENDORS,
    }


@router.post("/claim/{vendor_card_id}")
def claim_vendor(vendor_card_id: int, user: User = Depends(require_user), db: Session = Depends(get_db)):
    """Claim a vendor as strategic."""
    record, err = svc.claim_vendor(db, user.id, vendor_card_id)
    if err:
        if "Already at" in err:
            raise HTTPException(409, detail=err)
        raise HTTPException(400, detail=err)
    return _serialize_record(record)


@router.delete("/drop/{vendor_card_id}")
def drop_vendor(vendor_card_id: int, user: User = Depends(require_user), db: Session = Depends(get_db)):
    """Drop a vendor from strategic list."""
    success, err = svc.drop_vendor(db, user.id, vendor_card_id)
    if not success:
        raise HTTPException(400, detail=err)
    return {"ok": True}


@router.post("/replace")
def replace_vendor(body: ReplaceRequest, user: User = Depends(require_user), db: Session = Depends(get_db)):
    """Atomic swap: drop one vendor, claim another."""
    record, err = svc.replace_vendor(db, user.id, body.drop_vendor_card_id, body.claim_vendor_card_id)
    if err:
        raise HTTPException(400, detail=err)
    return _serialize_record(record)


@router.get("/status/{vendor_card_id}")
def vendor_status(vendor_card_id: int, db: Session = Depends(get_db)):
    """Return ownership status for a vendor."""
    status = svc.get_vendor_status(db, vendor_card_id)
    if not status:
        raise HTTPException(404, detail="Not claimed")
    return status


@router.get("/open-pool")
def open_pool(
    search: str | None = None,
    limit: int = 50,
    offset: int = 0,
    db: Session = Depends(get_db),
):
    """Return vendors not claimed by any buyer."""
    vendors, total = svc.get_open_pool(db, limit=limit, offset=offset, search=search)
    return {
        "vendors": [
            {"id": v.id, "display_name": v.display_name, "normalized_name": v.normalized_name} for v in vendors
        ],
        "total": total,
        "limit": limit,
        "offset": offset,
    }
