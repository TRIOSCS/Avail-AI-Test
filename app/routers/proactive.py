"""
proactive.py — Proactive Offers API

Endpoints for viewing matches, sending proactive offer emails,
converting wins, and viewing the scorecard.

Called by: main.py (router mount)
Depends on: models, dependencies, services/proactive_service
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..cache.decorators import cached_endpoint
from ..database import get_db
from ..dependencies import require_user
from ..models import ProactiveMatch, SiteContact, User
from ..scheduler import get_valid_token
from ..schemas.proactive import DismissMatches, SendProactive

router = APIRouter()


@router.get("/api/proactive/matches")
async def list_proactive_matches(
    status: str = "new",
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """List proactive matches for the current salesperson, grouped by customer."""
    from ..services.proactive_service import get_matches_for_user

    return get_matches_for_user(db, user.id, status=status)


@router.get("/api/proactive/count")
async def proactive_match_count(
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Count of new proactive matches for nav badge."""
    from ..services.proactive_service import get_match_count

    return {"count": get_match_count(db, user.id)}


@router.post("/api/proactive/dismiss")
async def dismiss_matches(
    body: DismissMatches,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Dismiss selected proactive matches."""
    match_ids = body.match_ids
    if not match_ids:
        raise HTTPException(400, "No match IDs provided")
    updated = (
        db.query(ProactiveMatch)
        .filter(
            ProactiveMatch.id.in_(match_ids),
            ProactiveMatch.salesperson_id == user.id,
            ProactiveMatch.status == "new",
        )
        .update({"status": "dismissed"}, synchronize_session=False)
    )
    db.commit()
    return {"dismissed": updated}


@router.post("/api/proactive/send")
async def send_proactive(
    body: SendProactive,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Send a proactive offer email to customer contacts."""
    match_ids = body.match_ids
    contact_ids = body.contact_ids
    sell_prices = body.sell_prices
    subject = body.subject
    notes = body.notes

    if not match_ids:
        raise HTTPException(400, "Select at least one match")
    if not contact_ids:
        raise HTTPException(400, "Select at least one contact")

    token = await get_valid_token(user, db)
    if not token:
        raise HTTPException(400, "M365 token not available — please reconnect")

    try:
        from ..services.proactive_service import send_proactive_offer

        result = await send_proactive_offer(
            db,
            user,
            token,
            match_ids,
            contact_ids,
            sell_prices,
            subject,
            notes,
        )
        return result
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.get("/api/proactive/offers")
async def list_sent_offers(
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """List sent proactive offers for the current salesperson."""
    from ..services.proactive_service import get_sent_offers

    return get_sent_offers(db, user.id)


@router.post("/api/proactive/convert/{offer_id}")
async def convert_to_win(
    offer_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Convert a proactive offer to a won requisition + quote + buy plan."""
    try:
        from ..services.proactive_service import convert_proactive_to_win

        result = convert_proactive_to_win(db, offer_id, user)
        return result
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.get("/api/proactive/scorecard")
async def proactive_scorecard(
    salesperson_id: int | None = None,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Proactive offer scorecard. Admins can see all, sales see own."""
    from ..dependencies import is_admin as _is_admin
    from ..services.proactive_service import get_scorecard

    is_admin = _is_admin(user)
    if salesperson_id and not is_admin:
        salesperson_id = user.id  # Non-admin can only see own
    if not is_admin and not salesperson_id:
        salesperson_id = user.id

    @cached_endpoint(prefix="proactive_scorecard", ttl_hours=1, key_params=["salesperson_id"])
    def _fetch(salesperson_id, db):
        return get_scorecard(db, salesperson_id)

    return _fetch(salesperson_id=salesperson_id, db=db)


@router.get("/api/proactive/contacts/{site_id}")
async def get_site_contacts(
    site_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Get SiteContacts for a customer site (for the send modal contact picker)."""
    contacts = (
        db.query(SiteContact)
        .filter(
            SiteContact.customer_site_id == site_id,
        )
        .order_by(SiteContact.is_primary.desc(), SiteContact.full_name)
        .all()
    )
    return [
        {
            "id": c.id,
            "full_name": c.full_name,
            "email": c.email,
            "title": c.title,
            "is_primary": c.is_primary,
        }
        for c in contacts
    ]
