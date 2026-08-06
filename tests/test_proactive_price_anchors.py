"""Tests for the per-part price anchors (2026-08-06 spec, D5) and their score wiring.

last_quote_for_part / last_win_for_part read the structured quote_lines table: price +
date + customer + rep, any customer, any rep, bounded by
proactive_price_lookback_months. Lines are omitted entirely when no record exists — the
lookups return None, never a placeholder.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.models import Company, CustomerSite, Quote, QuoteLine, Requisition, User
from app.services.pricing_history import last_quote_for_part, last_win_for_part
from app.services.proactive_matching import find_matches_for_offer
from tests.conftest import engine  # noqa: F401

MPN = "LTSR15-NP"


def _seed_quote(
    db,
    *,
    quote_number: str,
    customer: str,
    rep_name: str,
    sell_price: str,
    status: str = "sent",
    result: str | None = None,
    sent_days_ago: int = 30,
    result_days_ago: int | None = None,
    mpn: str = MPN,
):
    rep = db.query(User).filter(User.name == rep_name).first()
    if not rep:
        rep = User(
            email=f"{rep_name.replace(' ', '.').lower()}@trioscs.com",
            name=rep_name,
            role="sales",
            azure_id=f"az-{rep_name.replace(' ', '')}",
        )
        db.add(rep)
        db.flush()
    company = db.query(Company).filter(Company.name == customer).first()
    if not company:
        company = Company(name=customer, is_active=True, account_owner_id=rep.id)
        db.add(company)
        db.flush()
    site = db.query(CustomerSite).filter(CustomerSite.company_id == company.id).first()
    if not site:
        site = CustomerSite(company_id=company.id, site_name=f"{customer} HQ", is_active=True)
        db.add(site)
        db.flush()
    req = Requisition(name=f"req-{quote_number}", customer_site_id=site.id, status="quoted", created_by=rep.id)
    db.add(req)
    db.flush()
    quote = Quote(
        requisition_id=req.id,
        customer_site_id=site.id,
        quote_number=quote_number,
        status=status,
        result=result,
        sent_at=datetime.now(UTC) - timedelta(days=sent_days_ago),
        result_at=(datetime.now(UTC) - timedelta(days=result_days_ago)) if result_days_ago is not None else None,
        created_by_id=rep.id,
    )
    db.add(quote)
    db.flush()
    db.add(QuoteLine(quote_id=quote.id, mpn=mpn, sell_price=Decimal(sell_price), qty=100))
    db.commit()
    return {"company": company, "rep": rep, "quote": quote}


def test_no_history_returns_none(db_session):
    assert last_quote_for_part(db_session, part=MPN) is None
    assert last_win_for_part(db_session, part=MPN) is None


def test_last_quote_newest_wins_and_carries_customer_and_rep(db_session):
    _seed_quote(
        db_session,
        quote_number="Q-1",
        customer="Beckhoff",
        rep_name="Martina Tewes",
        sell_price="8.10",
        sent_days_ago=90,
    )
    _seed_quote(
        db_session,
        quote_number="Q-2",
        customer="Siemens",
        rep_name="Marcus Moawad",
        sell_price="8.75",
        sent_days_ago=10,
    )
    a = last_quote_for_part(db_session, part=MPN)
    assert a is not None
    assert a["price"] == 8.75
    assert a["company"] == "Siemens"
    assert a["rep"] == "Marcus Moawad"


def test_draft_quotes_do_not_anchor(db_session):
    _seed_quote(
        db_session, quote_number="Q-3", customer="Beckhoff", rep_name="Martina Tewes", sell_price="9.99", status="draft"
    )
    assert last_quote_for_part(db_session, part=MPN) is None


def test_lookback_cutoff(db_session):
    _seed_quote(
        db_session,
        quote_number="Q-4",
        customer="Beckhoff",
        rep_name="Martina Tewes",
        sell_price="7.00",
        sent_days_ago=800,
    )
    assert last_quote_for_part(db_session, part=MPN) is None


def test_last_win_only_won_quotes_ordered_by_result_date(db_session):
    _seed_quote(
        db_session,
        quote_number="Q-5",
        customer="Beckhoff",
        rep_name="Martina Tewes",
        sell_price="8.00",
        status="lost",
        result="lost",
        result_days_ago=5,
    )
    _seed_quote(
        db_session,
        quote_number="Q-6",
        customer="Siemens",
        rep_name="Marcus Moawad",
        sell_price="8.40",
        status="won",
        result="won",
        sent_days_ago=200,
        result_days_ago=180,
    )
    w = last_win_for_part(db_session, part=MPN)
    assert w is not None
    assert w["price"] == 8.4
    assert w["company"] == "Siemens"
    assert w["rep"] == "Marcus Moawad"
    assert w["at"].date() == (datetime.now(UTC) - timedelta(days=180)).date()


def test_anchor_matching_is_part_exact(db_session):
    """A quote on the space variant never anchors the exact part."""
    _seed_quote(
        db_session,
        quote_number="Q-7",
        customer="Beckhoff",
        rep_name="Martina Tewes",
        sell_price="9.00",
        mpn="LTSR 15-NP",
    )
    assert last_quote_for_part(db_session, part=MPN) is None
    assert last_quote_for_part(db_session, part="LTSR 15-NP") is not None


def test_same_customer_win_outranks_other_customer_win(db_session):
    """Engine wiring: a recent win lifts the score, same-customer most of all."""
    from app.models import Offer, ProactiveMatch, Requirement

    seeded = _seed_quote(
        db_session,
        quote_number="Q-8",
        customer="Beckhoff",
        rep_name="Martina Tewes",
        sell_price="12.00",
        status="won",
        result="won",
        result_days_ago=30,
    )
    # Beckhoff also asked for the part recently → requirement-seeded match.
    req = Requisition(
        name="ask",
        customer_site_id=db_session.query(CustomerSite)
        .filter(CustomerSite.company_id == seeded["company"].id)
        .first()
        .id,
        status="open",
        created_by=seeded["rep"].id,
    )
    db_session.add(req)
    db_session.flush()
    db_session.add(Requirement(requisition_id=req.id, primary_mpn=MPN, target_qty=1000))
    offer = Offer(vendor_name="Arrow", mpn=MPN, qty_available=2000, unit_price=Decimal("6.44"), status="active")
    db_session.add(offer)
    db_session.commit()

    matches = find_matches_for_offer(offer.id, db_session)
    db_session.commit()
    assert len(matches) == 1
    with_win = matches[0].match_score

    # Same scenario minus the win record scores lower.
    db_session.query(ProactiveMatch).delete()
    db_session.query(QuoteLine).delete()
    db_session.commit()
    rematches = find_matches_for_offer(offer.id, db_session)
    assert len(rematches) == 1
    assert with_win > rematches[0].match_score
