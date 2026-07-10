"""CRM cadence clocks: derive last_outbound_at / last_reply_at from ActivityLog.

The ActivityLog event table is the source of truth; the clock columns on
Company/CustomerSite/SiteContact (and the vendor mirrors) are a materialized
cache kept fresh by bump_clocks_from_activity() (real-time) and these
functions (nightly self-healing backstop).
"""

from datetime import UTC, datetime

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from ..constants import Direction
from ..models.crm import Company, CustomerSite, SiteContact
from ..models.intelligence import ActivityLog
from ..models.vendors import VendorCard, VendorContact

_CLOCK_TARGETS = (
    (Company, "company_id"),
    (CustomerSite, "customer_site_id"),
    (SiteContact, "site_contact_id"),
    (VendorCard, "vendor_card_id"),
    (VendorContact, "vendor_contact_id"),
)


def _advance(db: Session, model, entity_id, field: str, when: datetime) -> None:
    if not entity_id:
        return
    col = getattr(model, field)
    db.query(model).filter(model.id == entity_id, or_(col.is_(None), col < when)).update(
        {field: when}, synchronize_session=False
    )


def bump_clocks_from_activity(db: Session, activity: ActivityLog) -> None:
    """Forward-only clock update from a freshly-written ActivityLog row.

    Outbound advances last_outbound_at; meaningful inbound advances last_reply_at. Non-
    meaningful inbound and NULL direction are ignored (timeline-only noise).
    """
    if activity.direction == Direction.OUTBOUND:
        field = "last_outbound_at"
    elif activity.direction == Direction.INBOUND and activity.is_meaningful:
        field = "last_reply_at"
    else:
        return
    when = activity.occurred_at or activity.created_at or datetime.now(UTC)
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    for model, fk in _CLOCK_TARGETS:
        _advance(db, model, getattr(activity, fk), field, when)


def _outbound_max(db: Session, col):
    return db.query(func.max(func.coalesce(ActivityLog.occurred_at, ActivityLog.created_at))).filter(
        col, ActivityLog.direction == Direction.OUTBOUND
    )


def _reply_max(db: Session, col):
    return db.query(func.max(func.coalesce(ActivityLog.occurred_at, ActivityLog.created_at))).filter(
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
