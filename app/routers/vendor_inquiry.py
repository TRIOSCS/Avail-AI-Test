"""routers/vendor_inquiry.py — Vendor stock inquiry endpoint.

Finds vendors who have (or had) specific parts, collects their emails from
all internal sources + external enrichment, and optionally blasts stock
inquiry emails to all of them.

Called by: main.py (router mount)
Depends on: services/vendor_email_lookup, email_service, dependencies
"""

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..constants import RequisitionStatus
from ..database import get_db
from ..dependencies import require_buyer, require_fresh_token, require_user
from ..models import User
from ..rate_limit import limiter
from ..services.vendor_email_lookup import (
    build_inquiry_groups,
    find_vendors_for_parts,
)

router = APIRouter(tags=["vendor-inquiry"])


class PartRequest(BaseModel):
    """A single part with quantity."""

    mpn: str
    qty: int = 0


class VendorLookupRequest(BaseModel):
    """Request to find vendor emails for parts."""

    parts: list[PartRequest] = Field(min_length=1, max_length=20)
    enrich_missing: bool = True


class VendorInquiryRequest(BaseModel):
    """Request to send stock inquiries to vendors."""

    parts: list[PartRequest] = Field(min_length=1, max_length=20)
    requisition_id: int | None = None
    enrich_missing: bool = True
    sender_name: str = "Purchasing Team"
    dry_run: bool = False


@router.post("/api/vendor-lookup")
async def vendor_email_lookup(
    payload: VendorLookupRequest,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Find all vendor emails for specific parts.

    Queries sightings, vendor cards, contacts, email intelligence, material history, and
    past RFQs. Optionally enriches vendors missing emails via Apollo, Hunter,
    RocketReach, and AI.
    """
    mpns = [p.mpn for p in payload.parts]
    results = await find_vendors_for_parts(
        mpns=mpns,
        db=db,
        enrich_missing=payload.enrich_missing,
    )

    # Build summary
    total_vendors = set()
    total_emails = set()
    for mpn, vendors in results.items():
        for v in vendors:
            total_vendors.add(v["vendor_name"])
            total_emails.update(v.get("emails", []))

    return {
        "results": results,
        "summary": {
            "parts_searched": len(mpns),
            "unique_vendors": len(total_vendors),
            "unique_emails": len(total_emails),
            "vendors_with_email": sum(
                1
                for v in total_vendors
                if any(
                    email
                    for vs in results.values()
                    for vv in vs
                    if vv["vendor_name"] == v
                    for email in vv.get("emails", [])
                )
            ),
        },
    }


@router.post("/api/vendor-inquiry")
@limiter.limit("3/minute")
async def send_vendor_inquiries(
    payload: VendorInquiryRequest,
    request: Request,
    user: User = Depends(require_buyer),
    db: Session = Depends(get_db),
):
    """Find vendors for parts and send stock inquiry emails to all of them.

    Flow:
    1. Query all internal sources for vendor emails
    2. Enrich vendors missing emails (optional)
    3. Build personalized inquiry emails
    4. Send via M365 Graph API (or dry_run to preview)
    """
    token = await require_fresh_token(request, db)

    mpns = [p.mpn for p in payload.parts]
    parts_with_qty = [{"mpn": p.mpn, "qty": p.qty} for p in payload.parts]

    # Step 1-2: Find vendor emails
    results = await find_vendors_for_parts(
        mpns=mpns,
        db=db,
        enrich_missing=payload.enrich_missing,
    )

    # Step 3: Build inquiry email groups
    groups = build_inquiry_groups(
        vendor_results=results,
        parts_with_qty=parts_with_qty,
        sender_name=payload.sender_name,
    )

    if not groups:
        return {
            "ok": False,
            "error": "No vendor emails found for these parts",
            "results": results,
            "groups": [],
        }

    # Step 4: Dry run — return preview without sending
    if payload.dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "groups": groups,
            "total_emails": len(groups),
            "results": results,
        }

    # Step 5: Send emails
    # Use requisition_id if provided, otherwise create a pseudo-ref
    req_id = payload.requisition_id
    if not req_id:
        # Find or create a requisition for this inquiry
        from ..models import Requisition

        req = Requisition(
            name=f"Stock Inquiry — {', '.join(mpns[:3])}",
            created_by=user.id,
            status=RequisitionStatus.SOURCING,
        )
        db.add(req)
        db.flush()
        req_id = req.id

    from ..email_service import send_batch_rfq

    send_results = await send_batch_rfq(
        token=token,
        db=db,
        user_id=user.id,
        requisition_id=req_id,
        vendor_groups=groups,
    )

    db.commit()

    sent = sum(1 for r in send_results if r.get("status") == "sent")
    failed = sum(1 for r in send_results if r.get("status") == "failed")

    return {
        "ok": True,
        "requisition_id": req_id,
        "total_emails": len(groups),
        "sent": sent,
        "failed": failed,
        "send_results": send_results,
        "results": results,
    }
