"""tests/test_remediation_p2.py — QC 2026-08-10 P2 (dead-end states) regressions.

- D5: marking a quote WON closes every contributing requisition AND records
  won_revenue (the HTMX workspace path used to set only the quote status, so the
  owner's win metrics never recorded the win). Both quote-result routes now
  share app.services.quote_requisitions.apply_quote_result.

Called by: pytest autodiscovery
Depends on: conftest fixtures (db_session, test_user).
"""

from decimal import Decimal

from tests.conftest import engine  # noqa: F401


def _won_quote_setup(db, test_user, *, combined=False):
    from app.models import CustomerSite, Quote, Requisition
    from app.models.crm import Company

    company = Company(name="WonCo", is_active=True)
    db.add(company)
    db.flush()
    site = CustomerSite(company_id=company.id, site_name="HQ")
    db.add(site)
    db.flush()
    req = Requisition(
        name="REQ-WON", customer_name="WonCo", status="quoted", created_by=test_user.id, customer_site_id=site.id
    )
    db.add(req)
    db.flush()
    quote = Quote(
        requisition_id=req.id,
        customer_site_id=site.id,
        quote_number="Q-WON-1",
        status="sent",
        line_items=[],
        subtotal=Decimal("52500.00"),
    )
    db.add(quote)
    db.flush()
    extra = None
    if combined:
        from app.services.quote_requisitions import link_quote_to_requisitions

        extra = Requisition(name="REQ-WON-2", customer_name="WonCo", status="quoted", created_by=test_user.id)
        db.add(extra)
        db.flush()
        link_quote_to_requisitions(db, quote.id, [req.id, extra.id])
    return quote, req, extra


def test_won_quote_closes_requisition_and_records_revenue(db_session, test_user):
    from app.constants import QuoteStatus, RequisitionStatus
    from app.services.quote_requisitions import apply_quote_result

    quote, req, _ = _won_quote_setup(db_session, test_user)
    apply_quote_result(db_session, quote, result="won", reason=None)
    db_session.commit()

    db_session.refresh(quote)
    db_session.refresh(req)
    assert quote.status == QuoteStatus.WON
    assert quote.won_revenue == Decimal("52500.00")  # win metric now records
    assert req.status == RequisitionStatus.WON  # requisition closed, not stranded in QUOTED


def test_won_quote_closes_every_contributing_requisition(db_session, test_user):
    from app.constants import RequisitionStatus
    from app.services.quote_requisitions import apply_quote_result

    quote, req, extra = _won_quote_setup(db_session, test_user, combined=True)
    apply_quote_result(db_session, quote, result="won", reason=None)
    db_session.commit()

    db_session.refresh(req)
    db_session.refresh(extra)
    assert req.status == RequisitionStatus.WON
    assert extra.status == RequisitionStatus.WON  # combined quote closes BOTH


def test_lost_quote_records_reason_and_closes_requisition(db_session, test_user):
    from app.constants import QuoteStatus, RequisitionStatus
    from app.models import ActivityLog
    from app.services.quote_requisitions import apply_quote_result

    quote, req, _ = _won_quote_setup(db_session, test_user)
    apply_quote_result(db_session, quote, result="lost", reason="price too high")
    db_session.commit()

    db_session.refresh(quote)
    db_session.refresh(req)
    assert quote.status == QuoteStatus.LOST
    assert quote.result_reason == "price too high"
    assert quote.won_revenue is None  # lost never books revenue
    assert req.status == RequisitionStatus.LOST
    act = db_session.query(ActivityLog).filter(ActivityLog.quote_id == quote.id).one()
    assert act.activity_type == "quote_lost"
