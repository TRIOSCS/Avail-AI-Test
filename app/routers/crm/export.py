"""CRM CSV export endpoints.

Exports the CURRENT FILTERED customers/contacts view visible to the requesting
user. Visibility mirrors the list routes: managers/admins see all; reps see
own + site-owned (effective_my_only). The same filter query params the list
routes accept are honored here so "Export CSV" matches what is on screen.

Routes:
  GET /v2/customers/export.csv         — StreamingResponse, companies CSV
  GET /v2/customers/contacts/export.csv — StreamingResponse, contacts CSV

Called by: app/routers/crm/__init__.py (included into crm_router)
Depends on: app/services/crm_service (cdm_company_query, customer_contacts_query,
            contact_cadence_predicate, CONTACT_CADENCE_DOTS),
            app/dependencies.is_manager_or_admin,
            app/models/crm.py (Company, CustomerSite, SiteContact)
"""

import csv
import io
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.constants import AccessKey
from app.database import get_db
from app.dependencies import is_manager_or_admin, require_access
from app.models import User
from app.services.crm_service import (
    CONTACT_CADENCE_DOTS,
    cdm_company_query,
    contact_cadence_predicate,
    customer_contacts_query,
)

_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _safe_cell(v: object) -> str:
    """Prefix formula-injection chars with a single quote so spreadsheets treat the
    value as text."""
    s = "" if v is None else str(v)
    return "'" + s if s[:1] in _FORMULA_PREFIXES else s


router = APIRouter()


def _companies_generator(
    db: Session,
    user: User,
    *,
    search: str,
    staleness: str,
    account_type: str,
    segment: int,
    disposition: str,
    has_open_reqs: bool,
    my_only: bool,
    sort: str,
):
    """Yield CSV rows for the filtered companies the user may see."""
    header = ["name", "domain", "phone", "industry", "account_type", "owner_name", "hq_city", "hq_state", "created_at"]
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(header)
    yield buf.getvalue()

    # Reps are always scoped to their own visible set; managers can additionally
    # opt into "My accounts" via the my_only filter.
    effective_my_only = my_only or not is_manager_or_admin(user)
    q = cdm_company_query(
        db,
        user,
        search=search,
        staleness=staleness,
        account_type=account_type,
        my_only=effective_my_only,
        sort=sort or "name_asc",
        segment=segment,
        disposition=disposition or None,
        has_open_reqs=has_open_reqs,
    )
    for company in q.yield_per(200):
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(
            [
                _safe_cell(company.name),
                _safe_cell(company.domain),
                _safe_cell(company.phone),
                _safe_cell(company.industry),
                _safe_cell(company.account_type),
                _safe_cell(company.account_owner.name if company.account_owner else ""),
                _safe_cell(company.hq_city),
                _safe_cell(company.hq_state),
                company.created_at.isoformat() if company.created_at else "",
            ]
        )
        yield buf.getvalue()


def _contacts_generator(
    db: Session,
    user: User,
    *,
    search: str,
    company_id: int,
    contact_role: str,
    cadence_state: str,
):
    """Yield CSV rows for the filtered contacts reachable in the user's scope."""
    header = ["full_name", "title", "email", "phone", "contact_role", "company_name", "site_name", "is_primary"]
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(header)
    yield buf.getvalue()

    # customer_contacts_query is role-scoped (reps see only manageable accounts)
    # and applies search/company_id/contact_role. cadence_state is derived, but
    # contact_cadence_predicate expresses it as a SQL cutoff (mirrors
    # customer_contacts_list_ctx / cadence_state_of EXACTLY) so the filter runs in the
    # database and the whole set streams via yield_per instead of materializing. (PERF-10)
    base = customer_contacts_query(db, user, search=search, company_id=company_id, contact_role=contact_role)
    if cadence_state in CONTACT_CADENCE_DOTS:
        now = datetime.now(timezone.utc)
        base = base.filter(contact_cadence_predicate(cadence_state, now))
    rows = base.yield_per(200)

    for contact in rows:
        site = contact.customer_site
        company = site.company if site else None
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(
            [
                _safe_cell(contact.full_name),
                _safe_cell(contact.title),
                _safe_cell(contact.email),
                _safe_cell(contact.phone),
                _safe_cell(contact.contact_role),
                _safe_cell(company.name if company else ""),
                _safe_cell(site.site_name if site else ""),
                "true" if contact.is_primary else "false",
            ]
        )
        yield buf.getvalue()


@router.get("/v2/customers/export.csv")
async def export_companies_csv(
    search: str = "",
    staleness: str = "",
    account_type: str = "",
    segment: int = Query(0, ge=0),
    disposition: str = "",
    has_open_reqs: bool = False,
    my_only: bool = False,
    sort: str = "name_asc",
    user: User = Depends(require_access(AccessKey.EXPORT_DATA)),
    db: Session = Depends(get_db),
):
    """Export the current filtered companies view as a CSV download."""
    return StreamingResponse(
        _companies_generator(
            db,
            user,
            search=search,
            staleness=staleness,
            account_type=account_type,
            segment=segment,
            disposition=disposition,
            has_open_reqs=has_open_reqs,
            my_only=my_only,
            sort=sort,
        ),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=customers.csv"},
    )


@router.get("/v2/customers/contacts/export.csv")
async def export_contacts_csv(
    search: str = "",
    company_id: int = Query(0, ge=0),
    contact_role: str = "",
    cadence_state: str = "",
    user: User = Depends(require_access(AccessKey.EXPORT_DATA)),
    db: Session = Depends(get_db),
):
    """Export the current filtered contacts view as a CSV download."""
    return StreamingResponse(
        _contacts_generator(
            db,
            user,
            search=search,
            company_id=company_id,
            contact_role=contact_role,
            cadence_state=cadence_state,
        ),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=contacts.csv"},
    )
