"""company_detail_service.py — Company detail tab data fetching.

Extracts data-loading logic from routers/htmx_views.py company_tab() to keep
the router thin (HTTP + templates only).

Called by: routers/htmx_views.py
Depends on: models (Company, CustomerSite, Requisition)
"""

from sqlalchemy.orm import Session

from ..models import CustomerSite, Requisition


def get_company_sites(db: Session, company_id: int) -> list:
    """Fetch active customer sites for the sites tab."""
    return (
        db.query(CustomerSite)
        .filter(CustomerSite.company_id == company_id, CustomerSite.is_active.is_(True))
        .all()
    )


def get_company_contacts(db: Session, company_id: int) -> list[dict]:
    """Fetch contacts from company sites for the contacts tab.

    Returns list of dicts with contact info + site name.
    """
    sites = db.query(CustomerSite).filter(CustomerSite.company_id == company_id).all()
    contacts = []
    for s in sites:
        if s.contact_name or s.contact_email:
            contacts.append({
                "contact_name": s.contact_name,
                "site_name": s.site_name,
                "contact_email": s.contact_email,
                "contact_phone": getattr(s, "contact_phone", None),
            })
    return contacts


def get_company_requisitions(db: Session, company_name: str, limit: int = 50) -> list:
    """Fetch requisitions for the requisitions tab."""
    return (
        db.query(Requisition)
        .filter(Requisition.customer_name == company_name)
        .order_by(Requisition.created_at.desc().nullslast())
        .limit(limit)
        .all()
    )
