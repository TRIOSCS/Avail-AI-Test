"""Salesperson Scorecard — 12 activity metrics for all active users (monthly + YTD).

Called by: routers/performance.py (on-demand)
Depends on: models, database
"""

from datetime import date, datetime, timezone

from sqlalchemy import and_, case
from sqlalchemy import func as sqlfunc
from sqlalchemy.orm import Session

from ..models import (
    ActivityLog,
    Company,
    Contact,
    CustomerSite,
    ProactiveOffer,
    Quote,
    Requisition,
    SiteContact,
    StockListHash,
    User,
)


def get_salesperson_scorecard(db: Session, month: date) -> dict:
    """Compute 12 activity metrics for all active users (monthly + YTD)."""
    month_start = month.replace(day=1)
    if month_start.month == 12:
        month_end = month_start.replace(year=month_start.year + 1, month=1)
    else:
        month_end = month_start.replace(month=month_start.month + 1)

    ytd_start = date(month_start.year, 1, 1)

    month_start_dt = datetime(month_start.year, month_start.month, month_start.day, tzinfo=timezone.utc)
    month_end_dt = datetime(month_end.year, month_end.month, month_end.day, tzinfo=timezone.utc)
    ytd_start_dt = datetime(ytd_start.year, ytd_start.month, ytd_start.day, tzinfo=timezone.utc)

    users = db.query(User).filter(User.is_active.is_(True)).all()

    user_ids = [u.id for u in users]

    # Batch compute all metrics in 2 rounds instead of N×2×12 queries
    monthly_batch = _salesperson_metrics_batch(db, user_ids, month_start_dt, month_end_dt)
    ytd_batch = _salesperson_metrics_batch(db, user_ids, ytd_start_dt, month_end_dt)

    entries = []
    for user in users:
        entries.append(
            {
                "user_id": user.id,
                "user_name": user.name or user.email,
                "monthly": monthly_batch.get(user.id, {}),
                "ytd": ytd_batch.get(user.id, {}),
            }
        )

    # Default sort: won_revenue descending
    entries.sort(key=lambda e: float(e["monthly"]["won_revenue"] or 0), reverse=True)

    return {
        "month": month_start.isoformat(),
        "year": month_start.year,
        "entries": entries,
    }


def _salesperson_metrics_batch(
    db: Session, user_ids: list[int], start_dt: datetime, end_dt: datetime
) -> dict[int, dict]:
    """Compute all 12 metrics for multiple users over a date range in batch.

    Returns {user_id: metrics_dict} — replaces N×12 individual queries with 6 batch
    queries.
    """
    # Initialize results for all users
    results = {
        uid: {
            "new_accounts": 0,
            "new_contacts": 0,
            "calls_made": 0,
            "emails_sent": 0,
            "requisitions_entered": 0,
            "quotes_sent": 0,
            "orders_won": 0,
            "won_revenue": 0.0,
            "proactive_sent": 0,
            "proactive_converted": 0,
            "proactive_revenue": 0.0,
            "boms_uploaded": 0,
        }
        for uid in user_ids
    }

    if not user_ids:
        return results

    # Batch 1: Company new accounts
    for uid, cnt in (
        db.query(Company.account_owner_id, sqlfunc.count(Company.id))
        .filter(Company.account_owner_id.in_(user_ids), Company.created_at >= start_dt, Company.created_at < end_dt)
        .group_by(Company.account_owner_id)
        .all()
    ):
        results[uid]["new_accounts"] = cnt

    # Batch 2: New contacts via CustomerSite
    for uid, cnt in (
        db.query(CustomerSite.owner_id, sqlfunc.count(SiteContact.id))
        .join(CustomerSite, CustomerSite.id == SiteContact.customer_site_id)
        .filter(
            CustomerSite.owner_id.in_(user_ids), SiteContact.created_at >= start_dt, SiteContact.created_at < end_dt
        )
        .group_by(CustomerSite.owner_id)
        .all()
    ):
        results[uid]["new_contacts"] = cnt

    # Batch 3: ActivityLog — calls
    for uid, cnt in (
        db.query(ActivityLog.user_id, sqlfunc.count(ActivityLog.id))
        .filter(
            ActivityLog.user_id.in_(user_ids),
            ActivityLog.activity_type == "call_outbound",
            ActivityLog.created_at >= start_dt,
            ActivityLog.created_at < end_dt,
        )
        .group_by(ActivityLog.user_id)
        .all()
    ):
        results[uid]["calls_made"] = cnt

    # Batch 4: Contacts — emails sent
    for uid, cnt in (
        db.query(Contact.user_id, sqlfunc.count(Contact.id))
        .filter(
            Contact.user_id.in_(user_ids),
            Contact.contact_type == "email",
            Contact.created_at >= start_dt,
            Contact.created_at < end_dt,
        )
        .group_by(Contact.user_id)
        .all()
    ):
        results[uid]["emails_sent"] = cnt

    # Batch 5: Requisitions entered
    for uid, cnt in (
        db.query(Requisition.created_by, sqlfunc.count(Requisition.id))
        .filter(
            Requisition.created_by.in_(user_ids), Requisition.created_at >= start_dt, Requisition.created_at < end_dt
        )
        .group_by(Requisition.created_by)
        .all()
    ):
        results[uid]["requisitions_entered"] = cnt

    # Batch 6: Quotes — sent, won, won_revenue (single query with conditional aggregation)
    for uid, q_sent, q_won, q_rev in (
        db.query(
            Quote.created_by_id,
            sqlfunc.count(
                case((and_(Quote.sent_at.isnot(None), Quote.sent_at >= start_dt, Quote.sent_at < end_dt), 1))
            ),
            sqlfunc.count(
                case((and_(Quote.result == "won", Quote.result_at >= start_dt, Quote.result_at < end_dt), 1))
            ),
            sqlfunc.coalesce(
                sqlfunc.sum(
                    case(
                        (
                            and_(Quote.result == "won", Quote.result_at >= start_dt, Quote.result_at < end_dt),
                            Quote.won_revenue,
                        )
                    )
                ),
                0,
            ),
        )
        .filter(Quote.created_by_id.in_(user_ids))
        .group_by(Quote.created_by_id)
        .all()
    ):
        results[uid]["quotes_sent"] = q_sent
        results[uid]["orders_won"] = q_won
        results[uid]["won_revenue"] = float(q_rev)

    # Batch 7: ProactiveOffer — sent, converted, revenue (single query)
    for uid, p_sent, p_conv, p_rev in (
        db.query(
            ProactiveOffer.salesperson_id,
            sqlfunc.count(case((and_(ProactiveOffer.sent_at >= start_dt, ProactiveOffer.sent_at < end_dt), 1))),
            sqlfunc.count(
                case(
                    (
                        and_(
                            ProactiveOffer.status == "converted",
                            ProactiveOffer.converted_at >= start_dt,
                            ProactiveOffer.converted_at < end_dt,
                        ),
                        1,
                    )
                )
            ),
            sqlfunc.coalesce(
                sqlfunc.sum(
                    case(
                        (
                            and_(
                                ProactiveOffer.status == "converted",
                                ProactiveOffer.converted_at >= start_dt,
                                ProactiveOffer.converted_at < end_dt,
                            ),
                            ProactiveOffer.total_sell,
                        )
                    )
                ),
                0,
            ),
        )
        .filter(ProactiveOffer.salesperson_id.in_(user_ids))
        .group_by(ProactiveOffer.salesperson_id)
        .all()
    ):
        results[uid]["proactive_sent"] = p_sent
        results[uid]["proactive_converted"] = p_conv
        results[uid]["proactive_revenue"] = float(p_rev)

    # Batch 8: BOMs uploaded
    for uid, cnt in (
        db.query(StockListHash.user_id, sqlfunc.count(StockListHash.id))
        .filter(
            StockListHash.user_id.in_(user_ids),
            StockListHash.first_seen_at >= start_dt,
            StockListHash.first_seen_at < end_dt,
        )
        .group_by(StockListHash.user_id)
        .all()
    ):
        results[uid]["boms_uploaded"] = cnt

    return results


def _salesperson_metrics(db: Session, user_id: int, start_dt: datetime, end_dt: datetime) -> dict:
    """Compute all 12 metrics for a single user over a date range.

    Delegates to batch function for consistency.
    """
    batch = _salesperson_metrics_batch(db, [user_id], start_dt, end_dt)
    return batch.get(
        user_id,
        {
            "new_accounts": 0,
            "new_contacts": 0,
            "calls_made": 0,
            "emails_sent": 0,
            "requisitions_entered": 0,
            "quotes_sent": 0,
            "orders_won": 0,
            "won_revenue": 0.0,
            "proactive_sent": 0,
            "proactive_converted": 0,
            "proactive_revenue": 0.0,
            "boms_uploaded": 0,
        },
    )
