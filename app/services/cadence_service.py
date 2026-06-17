"""CRM cadence clocks: derive last_outbound_at / last_reply_at from ActivityLog.

The ActivityLog event table is the source of truth; the clock columns on
Company/CustomerSite/SiteContact (and the vendor mirrors) are a materialized
cache kept fresh by bump_clocks_from_activity() (real-time) and these
functions (nightly self-healing backstop).
"""

from sqlalchemy import func
from sqlalchemy.orm import Session

from ..constants import Direction
from ..models.crm import Company, CustomerSite, SiteContact
from ..models.intelligence import ActivityLog


def _outbound_max(db: Session, col):
    return db.query(func.max(ActivityLog.created_at)).filter(col, ActivityLog.direction == Direction.OUTBOUND)


def _reply_max(db: Session, col):
    return db.query(func.max(ActivityLog.created_at)).filter(
        col, ActivityLog.direction == Direction.INBOUND, ActivityLog.is_meaningful.is_(True)
    )


def materialize_company_clocks(db: Session, company_id: int) -> None:
    """Recompute both clocks for a company and each of its sites + contacts."""
    db.query(Company).filter(Company.id == company_id).update(
        {
            "last_outbound_at": _outbound_max(db, ActivityLog.company_id == company_id).scalar_subquery(),
            "last_reply_at": _reply_max(db, ActivityLog.company_id == company_id).scalar_subquery(),
        },
        synchronize_session=False,
    )
    for site in db.query(CustomerSite).filter(CustomerSite.company_id == company_id).all():
        db.query(CustomerSite).filter(CustomerSite.id == site.id).update(
            {
                "last_outbound_at": _outbound_max(db, ActivityLog.customer_site_id == site.id).scalar_subquery(),
                "last_reply_at": _reply_max(db, ActivityLog.customer_site_id == site.id).scalar_subquery(),
            },
            synchronize_session=False,
        )
        for contact in db.query(SiteContact).filter(SiteContact.customer_site_id == site.id).all():
            db.query(SiteContact).filter(SiteContact.id == contact.id).update(
                {
                    "last_outbound_at": _outbound_max(db, ActivityLog.site_contact_id == contact.id).scalar_subquery(),
                    "last_reply_at": _reply_max(db, ActivityLog.site_contact_id == contact.id).scalar_subquery(),
                },
                synchronize_session=False,
            )


def materialize_all_clocks(db: Session) -> int:
    """Recompute clocks for every company.

    Returns number of companies processed.
    """
    ids = [row[0] for row in db.query(Company.id).all()]
    for cid in ids:
        materialize_company_clocks(db, cid)
    return len(ids)
