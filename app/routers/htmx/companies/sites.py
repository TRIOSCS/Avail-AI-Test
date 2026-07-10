"""routers/htmx/companies/sites.py — CustomerSite + site-scoped SiteContact CRUD (P4.3
split, "Sites & Site Contacts CRUD (Phase 4)").

Create/soft-delete/mark-DNC a site, plus the legacy site-scoped contact CRUD
(create/delete/set-primary — the canonical add/edit path is now the Contacts tab in
``.contacts``, but these routes remain live). ``edit_site`` re-renders the Sites tab
via ``company_tab`` (owned by ``.detail``) — resolved off the PACKAGE attribute at
call time so a test that monkeypatches
``app.routers.htmx.companies.company_tab`` still takes effect.

Called by: app.routers.htmx.companies (package __init__ re-export, route registration)
Depends on: app.models, app.dependencies, .contacts (_render_contacts_list)
"""

from datetime import UTC, datetime

from fastapi import Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from loguru import logger
from sqlalchemy import func as sqlfunc
from sqlalchemy.orm import Session

import app.routers.htmx.companies as _pkg

from ....database import get_db
from ....dependencies import can_manage_account, require_user
from ....models import Company, CustomerSite, SiteContact, User
from ....template_env import template_response
from .._shared import _base_ctx
from . import router
from .contacts import _render_contacts_list


@router.post("/v2/partials/customers/{company_id}/sites", response_class=HTMLResponse)
async def create_site(
    request: Request,
    company_id: int,
    site_name: str = Form(""),
    site_type: str = Form(""),
    city: str = Form(""),
    country: str = Form(""),
    owner_id: str = Form(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Create a new site for a company, return the site card partial."""
    company = db.query(Company).filter(Company.id == company_id).first()
    if not company:
        raise HTTPException(404, "Company not found")
    if not can_manage_account(user, company, db):
        raise HTTPException(403, "Not authorized to manage this account")

    if not site_name.strip():
        return HTMLResponse('<div class="p-2 text-xs text-rose-600">Site name is required.</div>')

    try:
        parsed_owner_id = int(owner_id) if owner_id else None
    except (ValueError, TypeError):
        parsed_owner_id = None

    site = CustomerSite(
        company_id=company_id,
        site_name=site_name.strip(),
        site_type=site_type or None,
        city=city or None,
        country=country or None,
        owner_id=parsed_owner_id,
        is_active=True,
    )
    db.add(site)
    db.commit()
    db.refresh(site)

    # Eager load owner for template
    if site.owner_id:
        _ = site.owner

    # Avoid lazy-load during render: new site has zero contacts by definition.
    site.site_contacts = []

    ctx = _base_ctx(request, user, "customers")
    ctx["company"] = company
    ctx["s"] = site
    return template_response("htmx/partials/customers/tabs/site_card.html", ctx)


@router.delete("/v2/partials/customers/{company_id}/sites/{site_id}", response_class=HTMLResponse)
async def delete_site(
    request: Request,
    company_id: int,
    site_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Soft-delete a site (set is_active=False).

    Gate: can_manage_account — mirrors edit_site.  Any authenticated user can hit
    this route, so we must check ownership before mutating.
    """
    company = db.get(Company, company_id)
    if company is None or not can_manage_account(user, company, db):
        raise HTTPException(403, "Not authorized to manage this account")
    site = db.query(CustomerSite).filter(CustomerSite.id == site_id, CustomerSite.company_id == company_id).first()
    if not site:
        raise HTTPException(404, "Site not found")
    site.is_active = False
    db.commit()
    return HTMLResponse("")


@router.post(
    "/v2/partials/customers/{company_id}/sites/{site_id}/mark-dnc",
    response_class=HTMLResponse,
)
async def mark_site_dnc(
    request: Request,
    company_id: int,
    site_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Toggle do_not_contact on a CustomerSite.

    Gate: can_manage_account — account owner, collaborator, or manager/admin.
    A DNC site is excluded from call-list surfaces but NOT deleted.
    Returns an updated site_card partial.

    Called by: "Mark DNC" / "Clear DNC" toggle in site_card.html.
    """
    company = db.get(Company, company_id)
    if not company:
        raise HTTPException(404, "Company not found")
    if not can_manage_account(user, company, db):
        raise HTTPException(403, "Not authorized to manage this account")
    site = db.get(CustomerSite, site_id)
    if not site or site.company_id != company_id:
        raise HTTPException(404, "Site not found")
    site.do_not_contact = not site.do_not_contact
    db.commit()
    db.refresh(site)
    logger.info(
        "Site {} do_not_contact={} by {}",
        site_id,
        site.do_not_contact,
        user.email,
    )
    return template_response(
        "htmx/partials/customers/tabs/site_card.html",
        {"request": request, "s": site, "company": company, "user": user},
    )


@router.get(
    "/v2/partials/customers/{company_id}/sites/{site_id}/contacts",
    response_class=HTMLResponse,
)
async def site_contacts_list(
    request: Request,
    company_id: int,
    site_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Redirect site-scoped contact list to the canonical grouped contacts surface.

    site_contacts.html has been retired — all contact management now lives on the
    canonical #contacts-tab-list surface (contacts_tab.html /
    _contacts_grouped_list.html).
    """
    site = db.query(CustomerSite).filter(CustomerSite.id == site_id, CustomerSite.company_id == company_id).first()
    if not site:
        raise HTTPException(404, "Site not found")
    company = db.query(Company).filter(Company.id == company_id).first()
    if not company:
        raise HTTPException(404, "Company not found")
    return _render_contacts_list(request, user, company, db)


@router.post(
    "/v2/partials/customers/{company_id}/sites/{site_id}/contacts",
    response_class=HTMLResponse,
)
async def create_site_contact(
    request: Request,
    company_id: int,
    site_id: int,
    full_name: str = Form(""),
    email: str = Form(""),
    title: str = Form(""),
    phone: str = Form(""),
    wechat_id: str = Form(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Create a site contact and return refreshed contacts list."""
    site = db.query(CustomerSite).filter(CustomerSite.id == site_id, CustomerSite.company_id == company_id).first()
    if not site:
        raise HTTPException(404, "Site not found")
    company = db.get(Company, company_id)
    if company is None or not can_manage_account(user, company, db):
        raise HTTPException(403, "Not authorized to manage this account")

    if not full_name.strip():
        return HTMLResponse('<div class="p-2 text-xs text-rose-600">Name is required.</div>')

    # SiteContact.wechat_id is String(100) — reject over-length input here (the
    # in-memory SQLite test engine ignores VARCHAR lengths, but Postgres 500s).
    if len(wechat_id.strip()) > 100:
        return HTMLResponse('<div class="p-2 text-xs text-rose-600">WeChat ID must be 100 characters or fewer.</div>')

    # Dedup by email
    if email:
        existing = (
            db.query(SiteContact)
            .filter(
                SiteContact.customer_site_id == site_id,
                sqlfunc.lower(SiteContact.email) == email.strip().lower(),
            )
            .first()
        )
        if existing:
            # Deprecated route: dedup silently returns the list; legacy path kept for
            # backwards-compat only (contacts_tab_create is the canonical add endpoint).
            logger.info(
                "Dedup [legacy create_site_contact]: email {} already exists at site {} (company {})",
                email,
                site_id,
                company_id,
            )
        else:
            contact = SiteContact(
                customer_site_id=site_id,
                full_name=full_name.strip(),
                email=email.strip() or None,
                title=title.strip() or None,
                phone=phone.strip() or None,
                wechat_id=wechat_id.strip() or None,
            )
            db.add(contact)
            db.commit()
    else:
        contact = SiteContact(
            customer_site_id=site_id,
            full_name=full_name.strip(),
            title=title.strip() or None,
            phone=phone.strip() or None,
            wechat_id=wechat_id.strip() or None,
        )
        db.add(contact)
        db.commit()

    # Return canonical grouped contacts list (site_contacts.html is retired).
    company = db.query(Company).filter(Company.id == company_id).first()
    return _render_contacts_list(request, user, company, db)


@router.delete(
    "/v2/partials/customers/{company_id}/sites/{site_id}/contacts/{contact_id}",
    response_class=HTMLResponse,
)
async def delete_site_contact(
    request: Request,
    company_id: int,
    site_id: int,
    contact_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Delete a site contact and re-render the Contacts tab grouped list.

    The Sites tab no longer carries a contact editor — the only render target is
    #contacts-tab-list (the canonical Contacts tab surface).
    """
    # Validate site belongs to company BEFORE mutating — closes IDOR gap and
    # guarantees company is available for the contacts-tab-list render path.
    site = db.query(CustomerSite).filter(CustomerSite.id == site_id, CustomerSite.company_id == company_id).first()
    if not site:
        raise HTTPException(404, "Site not found")
    company = db.query(Company).filter(Company.id == company_id).first()
    if not company:
        raise HTTPException(404, "Company not found")
    if not can_manage_account(user, company, db):
        raise HTTPException(403, "Not authorized")

    contact = (
        db.query(SiteContact).filter(SiteContact.id == contact_id, SiteContact.customer_site_id == site_id).first()
    )
    if not contact:
        raise HTTPException(404, "Contact not found")
    db.delete(contact)
    db.commit()
    logger.info("Contact {} deleted by {}", contact_id, user.email)

    return _render_contacts_list(request, user, company, db)


@router.post(
    "/v2/partials/customers/{company_id}/sites/{site_id}/contacts/{contact_id}/primary",
    response_class=HTMLResponse,
)
async def set_primary_contact(
    request: Request,
    company_id: int,
    site_id: int,
    contact_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Set a contact as primary for the site (unsets others)."""
    # Validate the site belongs to the company BEFORE mutating — a mismatched
    # URL must not flip the primary flag and then 500 on render.
    site = db.query(CustomerSite).filter(CustomerSite.id == site_id, CustomerSite.company_id == company_id).first()
    if not site:
        raise HTTPException(404, "Site not found")
    company = db.query(Company).filter(Company.id == company_id).first()
    if not company:
        raise HTTPException(404, "Company not found")
    if not can_manage_account(user, company, db):
        raise HTTPException(403, "Not authorized")

    contact = (
        db.query(SiteContact).filter(SiteContact.id == contact_id, SiteContact.customer_site_id == site_id).first()
    )
    if not contact:
        raise HTTPException(404, "Contact not found")

    # Unset all other primary contacts on this site
    db.query(SiteContact).filter(
        SiteContact.customer_site_id == site_id,
        SiteContact.is_primary.is_(True),
        SiteContact.id != contact_id,
    ).update({"is_primary": False})
    contact.is_primary = True
    db.commit()

    # The Sites tab no longer carries a contact editor — always render the
    # canonical Contacts tab surface (#contacts-tab-list).
    return _render_contacts_list(request, user, company, db)


@router.get("/v2/partials/customers/{company_id}/sites/{site_id}/edit-form", response_class=HTMLResponse)
async def site_edit_form(
    request: Request,
    company_id: int,
    site_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return modal edit form for a customer site."""
    site = db.query(CustomerSite).filter(CustomerSite.id == site_id, CustomerSite.company_id == company_id).first()
    if not site:
        raise HTTPException(404, "Site not found")
    users = db.query(User).order_by(User.name).all()
    return template_response(
        "htmx/partials/customers/tabs/site_edit_modal.html",
        {"request": request, "site": site, "company_id": company_id, "users": users},
    )


@router.post("/v2/partials/customers/{company_id}/sites/{site_id}/edit", response_class=HTMLResponse)
async def edit_site(
    request: Request,
    company_id: int,
    site_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Update site fields and return refreshed sites tab."""
    site = db.query(CustomerSite).filter(CustomerSite.id == site_id, CustomerSite.company_id == company_id).first()
    if not site:
        raise HTTPException(404, "Site not found")
    company = db.get(Company, company_id)
    if company is None or not can_manage_account(user, company, db):
        raise HTTPException(403, "Not authorized to manage this account")

    form = await request.form()
    site_name = form.get("site_name", "").strip()
    if not site_name:
        raise HTTPException(400, "site_name is required")
    site.site_name = site_name
    site.address_line1 = form.get("address_line1", "").strip() or None
    site.address_line2 = form.get("address_line2", "").strip() or None
    site.city = form.get("city", "").strip() or None
    site.state = form.get("state", "").strip() or None
    site.zip = form.get("zip", "").strip() or None
    site.country = form.get("country", "").strip() or None
    site.site_type = form.get("site_type", "").strip() or None
    site.payment_terms = form.get("payment_terms", "").strip() or None
    site.shipping_terms = form.get("shipping_terms", "").strip() or None
    site.notes = form.get("notes", "").strip() or None
    owner_id = form.get("owner_id", "")
    if owner_id and str(owner_id).isdigit():
        site.owner_id = int(owner_id)
    site.updated_at = datetime.now(UTC)
    db.commit()
    logger.info("Site {} edited by {}", site_id, user.email)

    # Resolved off the PACKAGE attribute (not a static import of .detail.company_tab) so a
    # test that monkeypatches app.routers.htmx.companies.company_tab replaces what actually
    # runs here — see the module docstring.
    return await _pkg.company_tab(request=request, company_id=company_id, tab="sites", user=user, db=db)
