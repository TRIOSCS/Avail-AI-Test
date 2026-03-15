"""prospecting.py — Prospecting pool v1.3.0 routes.

Site-level ownership (pool, claim, release, my-sites, at-risk, assign owner),
accounts-first hierarchy, and capacity management.

Called by: v13_features package __init__.py
Depends on: services/ownership_service
"""

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from loguru import logger
from sqlalchemy.orm import Session

from ...database import get_db
from ...dependencies import is_admin as _is_admin
from ...dependencies import require_admin, require_user
from ...models import Company, CustomerSite, User

router = APIRouter(tags=["v13"])


# ═══════════════════════════════════════════════════════════════════════
#  PROSPECTING POOL — Site-Level Ownership
# ═══════════════════════════════════════════════════════════════════════


@router.get("/api/prospecting/pool")
async def prospecting_pool(user: User = Depends(require_user), db: Session = Depends(get_db)):
    """Get all unowned active sites available for claiming."""
    from app.services.ownership_service import get_open_pool_sites

    return get_open_pool_sites(db)


@router.post("/api/prospecting/claim/{site_id}")
async def prospecting_claim(site_id: int, user: User = Depends(require_user), db: Session = Depends(get_db)):
    """Claim an unowned site for the current user.

    Enforces 200-site cap — users at cap must release sites first.
    """
    from sqlalchemy import func

    if user.role not in ("sales", "trader"):
        raise HTTPException(403, "Only sales/trader users can claim sites")

    # Enforce 200-site cap
    current_count = (
        db.query(func.count(CustomerSite.id))
        .filter(
            CustomerSite.owner_id == user.id,
            CustomerSite.is_active.is_(True),
        )
        .scalar()
        or 0
    )
    if current_count >= SITE_CAP_PER_USER:
        raise HTTPException(
            409,
            f"You have {current_count} sites (cap is {SITE_CAP_PER_USER}). "
            "Release inactive sites before claiming new ones.",
        )

    site = db.get(CustomerSite, site_id)
    if not site:
        raise HTTPException(404, "Site not found")
    if site.owner_id is not None:
        owner = db.get(User, site.owner_id)
        owner_name = owner.name if owner else "Unknown"
        raise HTTPException(409, f"Site already owned by {owner_name}")

    from app.services.ownership_service import claim_site

    claimed = claim_site(site_id, user.id, db)
    if not claimed:
        raise HTTPException(409, "Site could not be claimed")

    db.commit()
    return {"status": "claimed", "site_id": site_id, "site_name": site.site_name}


@router.post("/api/prospecting/release/{site_id}")
async def prospecting_release(site_id: int, user: User = Depends(require_user), db: Session = Depends(get_db)):
    """Release ownership of a site back to the open pool."""
    site = db.get(CustomerSite, site_id)
    if not site:
        raise HTTPException(404, "Site not found")

    if site.owner_id != user.id and not _is_admin(user):
        raise HTTPException(403, "You can only release your own sites")

    site.owner_id = None
    site.ownership_cleared_at = datetime.now(timezone.utc)
    db.commit()

    logger.info("User {} released site {} ({})", user.name, site_id, site.site_name)
    return {"status": "released", "site_id": site_id, "site_name": site.site_name}


@router.get("/api/prospecting/my-sites")
async def prospecting_my_sites(user: User = Depends(require_user), db: Session = Depends(get_db)):
    """Get sites owned by the current user with health status."""
    from app.services.ownership_service import get_my_sites

    return get_my_sites(user.id, db)


@router.get("/api/prospecting/at-risk")
async def prospecting_at_risk(user: User = Depends(require_user), db: Session = Depends(get_db)):
    """Get owned sites approaching inactivity limit."""
    from app.services.ownership_service import get_sites_at_risk

    return get_sites_at_risk(db)


@router.put("/api/prospecting/sites/{site_id}/owner")
async def prospecting_assign_owner(
    site_id: int,
    request: Request,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Admin-only: assign/reassign a site to a specific user."""
    site = db.get(CustomerSite, site_id)
    if not site:
        raise HTTPException(404, "Site not found")

    body = await request.json()
    new_owner_id = body.get("owner_id")

    if new_owner_id is not None:
        target = db.get(User, new_owner_id)
        if not target:
            raise HTTPException(404, "User not found")

    # Enforce site cap unless admin explicitly overrides
    cap_warning = None
    if new_owner_id is not None:
        from sqlalchemy import func

        current_count = (
            db.query(func.count(CustomerSite.id))
            .filter(
                CustomerSite.owner_id == new_owner_id,
                CustomerSite.is_active.is_(True),
            )
            .scalar()
            or 0
        )
        force = body.get("force", False)
        if current_count >= SITE_CAP_PER_USER and not force:
            raise HTTPException(
                409,
                f"User already owns {current_count} sites (cap is {SITE_CAP_PER_USER}). Pass force=true to override.",
            )
        if current_count >= SITE_CAP_PER_USER:
            cap_warning = f"User now has {current_count + 1} sites (cap is {SITE_CAP_PER_USER})"

    site.owner_id = new_owner_id
    if new_owner_id is None:
        site.ownership_cleared_at = datetime.now(timezone.utc)
    else:
        site.ownership_cleared_at = None
    db.commit()

    result = {
        "ok": True,
        "site_id": site_id,
        "owner_id": new_owner_id,
    }
    if cap_warning:
        result["warning"] = cap_warning
    return result


# ═══════════════════════════════════════════════════════════════════════
#  PROSPECTING: Accounts-First Hierarchy + 200-Site Cap
# ═══════════════════════════════════════════════════════════════════════

SITE_CAP_PER_USER = 200


@router.get("/api/prospecting/my-accounts")
async def prospecting_my_accounts(user: User = Depends(require_user), db: Session = Depends(get_db)):
    """Get accounts grouped from the user's owned sites.

    Returns accounts (companies) with site counts and health status.
    This is the accounts-first view: accounts -> drill into sites.
    """
    from sqlalchemy import case, func

    now = datetime.now(timezone.utc)
    thirty_days = now - timedelta(days=30)

    # Get all companies that have at least one site owned by this user
    rows = (
        db.query(
            Company.id,
            Company.name,
            Company.domain,
            Company.industry,
            Company.hq_city,
            Company.hq_state,
            Company.employee_size,
            Company.is_strategic,
            func.count(CustomerSite.id).label("site_count"),
            func.count(
                case(
                    (CustomerSite.last_activity_at >= thirty_days, CustomerSite.id),
                    else_=None,
                )
            ).label("active_sites"),
            func.count(
                case(
                    (
                        (CustomerSite.last_activity_at < thirty_days) | (CustomerSite.last_activity_at.is_(None)),
                        CustomerSite.id,
                    ),
                    else_=None,
                )
            ).label("inactive_sites"),
            func.max(CustomerSite.last_activity_at).label("last_activity"),
        )
        .join(CustomerSite, CustomerSite.company_id == Company.id)
        .filter(
            CustomerSite.owner_id == user.id,
            CustomerSite.is_active.is_(True),
        )
        .group_by(Company.id)
        .order_by(Company.name)
        .all()
    )

    accounts = []
    for r in rows:
        # Health: green=all active, yellow=some inactive, red=all inactive
        if r.site_count == 0:  # pragma: no cover
            health = "grey"
        elif r.active_sites == r.site_count:
            health = "green"
        elif r.active_sites > 0:
            health = "yellow"
        else:
            health = "red"

        accounts.append(
            {
                "company_id": r.id,
                "name": r.name,
                "domain": r.domain,
                "industry": r.industry,
                "location": ", ".join(filter(None, [r.hq_city, r.hq_state])) or None,
                "employee_size": r.employee_size,
                "is_strategic": r.is_strategic or False,
                "site_count": r.site_count,
                "active_sites": r.active_sites,
                "inactive_sites": r.inactive_sites,
                "health": health,
                "last_activity": r.last_activity.isoformat() if r.last_activity else None,
            }
        )

    return accounts


@router.get("/api/prospecting/accounts/{company_id}/sites")
async def prospecting_account_sites(
    company_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Get all sites for a specific account owned by the current user.

    Returns site list with contact info, status, and last activity.
    """
    now = datetime.now(timezone.utc)
    thirty_days = now - timedelta(days=30)

    company = db.get(Company, company_id)
    if not company:
        raise HTTPException(404, "Company not found")

    sites = (
        db.query(CustomerSite)
        .filter(
            CustomerSite.company_id == company_id,
            CustomerSite.owner_id == user.id,
            CustomerSite.is_active.is_(True),
        )
        .order_by(CustomerSite.site_name)
        .all()
    )

    result = []
    for s in sites:
        last = s.last_activity_at
        if last and last >= thirty_days:
            status = "green"
        elif last:
            status = "yellow" if (now - last).days <= 60 else "red"
        else:
            status = "grey"

        result.append(
            {
                "site_id": s.id,
                "site_name": s.site_name,
                "site_type": s.site_type,
                "contact_name": s.contact_name,
                "contact_email": s.contact_email,
                "city": s.city,
                "state": s.state,
                "status": status,
                "last_activity_at": last.isoformat() if last else None,
                "days_inactive": (now - last).days if last else None,
            }
        )

    return {
        "company": {
            "id": company.id,
            "name": company.name,
            "domain": company.domain,
            "industry": company.industry,
        },
        "sites": result,
    }


@router.get("/api/prospecting/capacity")
async def prospecting_capacity(
    user_id: int | None = None,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Get site capacity for a user (current user by default).

    Returns used/cap counts and list of stalest accounts for nudges.
    """
    from sqlalchemy import func

    target_id = user_id or user.id

    # Count active sites owned by user
    used = (
        db.query(func.count(CustomerSite.id))
        .filter(
            CustomerSite.owner_id == target_id,
            CustomerSite.is_active.is_(True),
        )
        .scalar()
        or 0
    )

    # Find stalest accounts (no activity in 90+ days) for nudge
    ninety_days = datetime.now(timezone.utc) - timedelta(days=90)
    stale_sites = (
        db.query(
            CustomerSite.id,
            CustomerSite.site_name,
            Company.name.label("company_name"),
            CustomerSite.last_activity_at,
        )
        .join(Company, Company.id == CustomerSite.company_id)
        .filter(
            CustomerSite.owner_id == target_id,
            CustomerSite.is_active.is_(True),
            (CustomerSite.last_activity_at < ninety_days) | (CustomerSite.last_activity_at.is_(None)),
        )
        .order_by(CustomerSite.last_activity_at.asc().nullsfirst())
        .limit(10)
        .all()
    )

    stale = [
        {
            "site_id": s.id,
            "site_name": s.site_name,
            "company_name": s.company_name,
            "last_activity_at": s.last_activity_at.isoformat() if s.last_activity_at else None,
        }
        for s in stale_sites
    ]

    return {
        "used": used,
        "cap": SITE_CAP_PER_USER,
        "remaining": max(0, SITE_CAP_PER_USER - used),
        "at_cap": used >= SITE_CAP_PER_USER,
        "stale_accounts": stale,
    }
