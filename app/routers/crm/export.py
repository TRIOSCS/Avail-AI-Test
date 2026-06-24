"""CRM CSV export endpoints.

Exports companies and contacts visible to the requesting user.
Visibility mirrors cdm_company_query: managers/admins see all; reps see own+site-owned.

Routes:
  GET /v2/customers/export.csv         — StreamingResponse, companies CSV
  GET /v2/customers/contacts/export.csv — StreamingResponse, contacts CSV

Called by: app/routers/crm/__init__.py (included into crm_router)
Depends on: app/services/crm_service.cdm_company_query,
            app/dependencies.is_manager_or_admin,
            app/models/crm.py (Company, CustomerSite, SiteContact)
"""

import csv
import io

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import is_manager_or_admin, require_user
from app.models import User
from app.models.crm import CustomerSite, SiteContact
from app.services.crm_service import cdm_company_query

router = APIRouter()


def _companies_generator(db: Session, user: User):
    """Yield CSV rows for the companies the user may see."""
    header = ["name", "domain", "phone", "industry", "account_type", "owner_name", "hq_city", "hq_state", "created_at"]
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(header)
    yield buf.getvalue()

    q = cdm_company_query(
        db,
        user,
        search="",
        staleness="",
        account_type="",
        my_only=not is_manager_or_admin(user),
        sort="name_asc",
    )
    for company in q.yield_per(200):
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(
            [
                company.name or "",
                company.domain or "",
                company.phone or "",
                company.industry or "",
                company.account_type or "",
                company.account_owner.name if company.account_owner else "",
                company.hq_city or "",
                company.hq_state or "",
                company.created_at.isoformat() if company.created_at else "",
            ]
        )
        yield buf.getvalue()


def _contacts_generator(db: Session, user: User):
    """Yield CSV rows for the contacts reachable via the user's visible companies."""
    header = ["full_name", "title", "email", "phone", "contact_role", "company_name", "site_name", "is_primary"]
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(header)
    yield buf.getvalue()

    company_q = cdm_company_query(
        db,
        user,
        search="",
        staleness="",
        account_type="",
        my_only=not is_manager_or_admin(user),
        sort="name_asc",
    )
    company_ids = [c.id for c in company_q.with_entities(__import__("app.models.crm", fromlist=["Company"]).Company.id)]

    rows = (
        db.query(SiteContact, CustomerSite)
        .join(CustomerSite, SiteContact.customer_site_id == CustomerSite.id)
        .filter(
            CustomerSite.company_id.in_(company_ids),
            CustomerSite.is_active.is_(True),
        )
        .order_by(SiteContact.full_name)
        .yield_per(200)
    )
    # Build a company-id → name lookup to avoid N+1
    company_name_map: dict[int, str] = {}

    for contact, site in rows:
        if site.company_id not in company_name_map:
            from app.models.crm import Company as _Company

            co = db.get(_Company, site.company_id)
            company_name_map[site.company_id] = co.name if co else ""
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(
            [
                contact.full_name or "",
                contact.title or "",
                contact.email or "",
                contact.phone or "",
                contact.contact_role or "",
                company_name_map.get(site.company_id, ""),
                site.site_name or "",
                "true" if contact.is_primary else "false",
            ]
        )
        yield buf.getvalue()


@router.get("/v2/customers/export.csv")
async def export_companies_csv(
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Export visible companies as a CSV download."""
    return StreamingResponse(
        _companies_generator(db, user),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=customers.csv"},
    )


@router.get("/v2/customers/contacts/export.csv")
async def export_contacts_csv(
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Export contacts for visible companies as a CSV download."""
    return StreamingResponse(
        _contacts_generator(db, user),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=contacts.csv"},
    )
