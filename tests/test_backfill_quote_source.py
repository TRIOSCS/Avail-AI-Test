"""test_backfill_quote_source.py — Tests for app/management/backfill_quote_source.py.

Covers:
- Quote linked via ProactiveOffer.converted_quote_id with NULL source → set to 'proactive'
- Re-run is a no-op (idempotent)
- Manual quote (not linked) stays NULL
"""

from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.management.backfill_quote_source import backfill
from app.models import (
    CustomerSite,
    ProactiveOffer,
    Quote,
    Requisition,
    User,
)


def _make_quote(db: Session, user: User, site: CustomerSite, req: Requisition, source=None) -> Quote:
    """Create a minimal Quote for testing."""
    # Unique quote number per call
    n = db.query(Quote).count() + 1
    q = Quote(
        requisition_id=req.id,
        customer_site_id=site.id,
        quote_number=f"Q-TEST-{n:04d}",
        line_items=[],
        subtotal=100,
        total_cost=80,
        total_margin_pct=20,
        created_by_id=user.id,
        status="won",
        result="won",
        result_at=datetime.now(UTC),
        won_revenue=100,
        source=source,
    )
    db.add(q)
    db.flush()
    return q


def _make_proactive_offer(db: Session, user: User, site: CustomerSite, converted_quote_id=None) -> ProactiveOffer:
    po = ProactiveOffer(
        customer_site_id=site.id,
        salesperson_id=user.id,
        line_items=[],
        recipient_emails=["test@test.com"],
        subject="Test",
        status="converted" if converted_quote_id else "sent",
        total_sell=100,
        total_cost=80,
        converted_quote_id=converted_quote_id,
    )
    db.add(po)
    db.flush()
    return po


class TestBackfillQuoteSource:
    def test_backfill_sets_proactive_source(self, db_session, test_user, test_customer_site, test_requisition):
        """Quote linked via ProactiveOffer.converted_quote_id with NULL source →
        'proactive'."""
        quote = _make_quote(db_session, test_user, test_customer_site, test_requisition, source=None)
        _make_proactive_offer(db_session, test_user, test_customer_site, converted_quote_id=quote.id)
        db_session.commit()

        count = backfill(db_session)

        db_session.refresh(quote)
        assert quote.source == "proactive"
        assert count == 1

    def test_backfill_is_idempotent(self, db_session, test_user, test_customer_site, test_requisition):
        """Re-running backfill is a no-op — already-set rows are not re-counted."""
        quote = _make_quote(db_session, test_user, test_customer_site, test_requisition, source=None)
        _make_proactive_offer(db_session, test_user, test_customer_site, converted_quote_id=quote.id)
        db_session.commit()

        backfill(db_session)
        db_session.refresh(quote)
        assert quote.source == "proactive"

        count2 = backfill(db_session)
        assert count2 == 0

    def test_backfill_leaves_manual_quote_unchanged(self, db_session, test_user, test_customer_site, test_requisition):
        """A quote not referenced by any ProactiveOffer stays NULL."""
        manual_quote = _make_quote(db_session, test_user, test_customer_site, test_requisition, source=None)
        # No ProactiveOffer references this quote
        db_session.commit()

        backfill(db_session)

        db_session.refresh(manual_quote)
        assert manual_quote.source is None
