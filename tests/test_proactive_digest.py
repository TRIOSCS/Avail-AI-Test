"""Tests for the per-salesperson proactive digest (Steps 6-8 of the 2026-08-06 brief).

Brief-exact formatting (thousands separators, M/D/YYYY, whole-vs-decimal prices),
customer grouping with Trio Back Order last, anchor-line phrasing and omission,
duplicate-materials section, draft replacement, human-triggered send, and the weekly
outreach summary.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest

from app.constants import ProactiveDigestStatus
from app.models import (
    Company,
    CustomerSite,
    Offer,
    ProactiveDigest,
    ProactiveMatch,
    ProactiveOutreachLine,
    Quote,
    QuoteLine,
    Requisition,
    User,
)
from app.services.proactive_digest import (
    find_duplicate_material_groups,
    fmt_date,
    fmt_price,
    fmt_qty,
    generate_digests,
    send_digest,
    weekly_outreach_summary,
)
from tests.conftest import engine  # noqa: F401


def _mk_user(db, name, email, role="sales"):
    u = User(email=email, name=name, role=role, azure_id=f"az-{email}")
    db.add(u)
    db.flush()
    return u


def _mk_customer(db, name, owner):
    c = Company(name=name, is_active=True, account_owner_id=owner.id)
    db.add(c)
    db.flush()
    s = CustomerSite(company_id=c.id, site_name=f"{name} HQ", is_active=True)
    db.add(s)
    db.flush()
    return c, s


def _mk_match(db, *, mpn, owner, company=None, asked_days_ago=7, asked_qty=3000, req_count=1):
    m = ProactiveMatch(
        offer_id=db.query(Offer).filter(Offer.mpn == mpn).first().id,
        salesperson_id=owner.id,
        mpn=mpn,
        company_id=company.id if company else None,
        status="new",
        requirement_count=req_count,
        last_asked_at=datetime.now(UTC) - timedelta(days=asked_days_ago),
        last_asked_qty=asked_qty,
    )
    db.add(m)
    db.commit()
    return m


def _mk_offer(db, *, mpn, qty, price, vendor="Arrow"):
    o = Offer(vendor_name=vendor, mpn=mpn, qty_available=qty, unit_price=Decimal(str(price)), status="active")
    db.add(o)
    db.commit()
    return o


# ── Formatting ───────────────────────────────────────────────────────────


def test_fmt_qty_thousands():
    assert fmt_qty(1_805_521) == "1,805,521"
    assert fmt_qty(96) == "96"


def test_fmt_price_whole_vs_decimals():
    assert fmt_price(274.0) == "274"
    assert fmt_price(6.44) == "6.44"
    assert fmt_price(0.05) == "0.05"
    assert fmt_price(28000) == "28000"


def test_fmt_date_no_leading_zeros():
    assert fmt_date(datetime(2026, 8, 3, tzinfo=UTC)) == "8/3/2026"
    assert fmt_date(datetime(2026, 11, 30, tzinfo=UTC)) == "11/30/2026"


# ── Generation + body format ─────────────────────────────────────────────


def test_digest_line_format_exact(db_session):
    owner = _mk_user(db_session, "Martina Tewes", "mt@trioscs.com")
    beckhoff, _ = _mk_customer(db_session, "Beckhoff Automation", owner)
    _mk_offer(db_session, mpn="LTSR15-NP", qty=1000, price="7.50")
    _mk_offer(db_session, mpn="LTSR15-NP", qty=850, price="6.44", vendor="Sierra")
    _mk_offer(db_session, mpn="LTSR15-NP", qty=3000, price="9.00", vendor="Shenzhen")
    asked = datetime(2026, 8, 3, tzinfo=UTC)
    m = _mk_match(db_session, mpn="LTSR15-NP", owner=owner, company=beckhoff, asked_qty=3000, req_count=2)
    m.last_asked_at = asked
    db_session.commit()

    stats = generate_digests(db_session)
    db_session.commit()
    assert stats["digests_generated"] == 1

    digest = db_session.query(ProactiveDigest).one()
    assert digest.subject == "Availability match on your requirements, please check with customers"
    body = digest.body_html
    assert "Hi Martina," in body
    assert "Beckhoff Automation" in body
    assert (
        "- LTSR15-NP | last asked 8/3/2026 for 3,000 pcs (2 requests on file) | available 4,850 pcs, low cost $6.44"
        in body
    )
    assert "Internal reference — not for forwarding." in body
    # Frozen snapshot line
    line = db_session.query(ProactiveOutreachLine).one()
    assert line.available_qty == 4850
    assert float(line.low_cost) == 6.44
    assert line.requirement_count == 2


def test_single_request_omits_count_suffix(db_session):
    owner = _mk_user(db_session, "Sales Rep", "sr@trioscs.com")
    customer, _ = _mk_customer(db_session, "IBM Corporation", owner)
    _mk_offer(db_session, mpn="02PX530", qty=214, price="275")
    _mk_match(db_session, mpn="02PX530", owner=owner, company=customer, asked_qty=80, req_count=1)
    generate_digests(db_session)
    db_session.commit()
    body = db_session.query(ProactiveDigest).one().body_html
    assert "requests on file" not in body


def test_anchor_phrasing_same_and_other_customer(db_session):
    owner = _mk_user(db_session, "Sales Rep", "sr@trioscs.com")
    beckhoff, beckhoff_site = _mk_customer(db_session, "Beckhoff", owner)
    siemens, siemens_site = _mk_customer(db_session, "Siemens", owner)
    rep2 = _mk_user(db_session, "Marcus Moawad", "mm@trioscs.com")

    def quote(customer_site, number, price, *, result=None, result_at=None, mpn="PARTA"):
        req = Requisition(name=f"r-{number}", customer_site_id=customer_site.id, status="quoted", created_by=rep2.id)
        db_session.add(req)
        db_session.flush()
        q = Quote(
            requisition_id=req.id,
            customer_site_id=customer_site.id,
            quote_number=number,
            status="won" if result else "sent",
            result=result,
            result_at=result_at,
            sent_at=datetime.now(UTC) - timedelta(days=40),
            created_by_id=rep2.id,
        )
        db_session.add(q)
        db_session.flush()
        db_session.add(QuoteLine(quote_id=q.id, mpn=mpn, sell_price=Decimal(price)))
        db_session.commit()

    # PARTA: quoted to Siemens (other customer), won at Siemens by Marcus.
    quote(siemens_site, "Q-A", "12.00", result="won", result_at=datetime(2026, 7, 1, tzinfo=UTC))
    _mk_offer(db_session, mpn="PARTA", qty=500, price="8.00")
    _mk_match(db_session, mpn="PARTA", owner=owner, company=beckhoff, asked_qty=400)

    generate_digests(db_session)
    db_session.commit()
    body = db_session.query(ProactiveDigest).one().body_html
    assert "we quoted this part $12 on" in body
    assert "to Siemens, Marcus Moawad" in body
    assert "won $12 on 7/1/2026, Marcus Moawad at Siemens" in body


def test_no_anchor_lines_when_no_history(db_session):
    owner = _mk_user(db_session, "Sales Rep", "sr@trioscs.com")
    customer, _ = _mk_customer(db_session, "IBM", owner)
    _mk_offer(db_session, mpn="02PX530", qty=214, price="275")
    _mk_match(db_session, mpn="02PX530", owner=owner, company=customer)
    generate_digests(db_session)
    db_session.commit()
    body = db_session.query(ProactiveDigest).one().body_html
    assert "we quoted" not in body
    assert "won $" not in body
    assert "N/A" not in body


def test_backorder_group_last(db_session):
    owner = _mk_user(db_session, "Trader One", "t1@trioscs.com", role="trader")
    customer, _ = _mk_customer(db_session, "Zeta Corp", owner)
    _mk_offer(db_session, mpn="PARTB", qty=100, price="5")
    _mk_offer(db_session, mpn="PARTC", qty=50, price="9")
    _mk_match(db_session, mpn="PARTB", owner=owner, company=customer)
    _mk_match(db_session, mpn="PARTC", owner=owner, company=None)  # no customer account
    generate_digests(db_session)
    db_session.commit()
    body = db_session.query(ProactiveDigest).one().body_html
    assert "Trio Back Order" in body
    assert body.index("Zeta Corp") < body.index("Trio Back Order")


def test_duplicate_materials_section(db_session):
    owner = _mk_user(db_session, "Sales Rep", "sr@trioscs.com")
    customer, _ = _mk_customer(db_session, "Beckhoff", owner)
    _mk_offer(db_session, mpn="LTSR15-NP", qty=4850, price="6.44")
    _mk_offer(db_session, mpn="LTSR 15-NP", qty=1000, price="10.00")
    _mk_match(db_session, mpn="LTSR15-NP", owner=owner, company=customer)
    groups = find_duplicate_material_groups(db_session)
    assert ["LTSR 15-NP", "LTSR15-NP"] in groups
    generate_digests(db_session)
    db_session.commit()
    body = db_session.query(ProactiveDigest).one().body_html
    assert "Suspected duplicate materials" in body
    assert "LTSR 15-NP / LTSR15-NP" in body


def test_regenerate_clears_stale_draft_when_matches_moved(db_session):
    """A rep whose matches were reassigned (or evaporated) must not keep a stale draft.

    Found live 2026-08-07: the seed's actor held a 32-line draft that survived
    regeneration after every match moved to the real reps.
    """
    old_rep = _mk_user(db_session, "Old Rep", "old@trioscs.com")
    new_rep = _mk_user(db_session, "New Rep", "new@trioscs.com")
    customer, _ = _mk_customer(db_session, "IBM", old_rep)
    _mk_offer(db_session, mpn="02PX530", qty=214, price="275")
    m = _mk_match(db_session, mpn="02PX530", owner=old_rep, company=customer)
    generate_digests(db_session)
    db_session.commit()
    assert db_session.query(ProactiveDigest).one().salesperson_id == old_rep.id

    m.salesperson_id = new_rep.id
    db_session.commit()
    generate_digests(db_session)
    db_session.commit()
    drafts = db_session.query(ProactiveDigest).all()
    assert len(drafts) == 1
    assert drafts[0].salesperson_id == new_rep.id  # old rep's stale draft is gone


def test_regenerate_replaces_draft_not_sent(db_session):
    owner = _mk_user(db_session, "Sales Rep", "sr@trioscs.com")
    customer, _ = _mk_customer(db_session, "IBM", owner)
    _mk_offer(db_session, mpn="02PX530", qty=214, price="275")
    _mk_match(db_session, mpn="02PX530", owner=owner, company=customer)
    generate_digests(db_session)
    db_session.commit()
    first = db_session.query(ProactiveDigest).one()
    first.status = ProactiveDigestStatus.SENT
    db_session.commit()

    generate_digests(db_session)
    db_session.commit()
    assert db_session.query(ProactiveDigest).count() == 2  # sent kept + fresh draft

    generate_digests(db_session)
    db_session.commit()
    assert db_session.query(ProactiveDigest).count() == 2  # draft replaced, not stacked


def test_stale_match_with_no_live_supply_is_skipped(db_session):
    owner = _mk_user(db_session, "Sales Rep", "sr@trioscs.com")
    customer, _ = _mk_customer(db_session, "IBM", owner)
    offer = _mk_offer(db_session, mpn="OLDPART", qty=100, price="5")
    _mk_match(db_session, mpn="OLDPART", owner=owner, company=customer)
    offer.created_at = datetime.now(UTC) - timedelta(days=30)  # fell out of the 7-day window
    db_session.commit()
    stats = generate_digests(db_session)
    assert stats["digests_generated"] == 0


# ── Send ─────────────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_send_digest_marks_sent_and_stamps_lines(db_session):
    owner = _mk_user(db_session, "Sales Rep", "sr@trioscs.com")
    manager = _mk_user(db_session, "The Manager", "mgr@trioscs.com", role="manager")
    customer, _ = _mk_customer(db_session, "IBM", owner)
    _mk_offer(db_session, mpn="02PX530", qty=214, price="275")
    _mk_match(db_session, mpn="02PX530", owner=owner, company=customer)
    generate_digests(db_session)
    db_session.commit()
    digest = db_session.query(ProactiveDigest).one()

    with patch("app.utils.graph_client.GraphClient.post_json", new_callable=AsyncMock) as mock_send:
        await send_digest(db_session, digest.id, manager, "tok")
        db_session.commit()

    payload = mock_send.call_args.args[1]
    assert payload["message"]["toRecipients"][0]["emailAddress"]["address"] == "sr@trioscs.com"
    db_session.refresh(digest)
    assert digest.status == ProactiveDigestStatus.SENT
    assert digest.sent_by_id == manager.id
    line = db_session.query(ProactiveOutreachLine).one()
    assert line.sent_at is not None


@pytest.mark.anyio
async def test_send_digest_rejects_non_draft(db_session):
    manager = _mk_user(db_session, "The Manager", "mgr@trioscs.com", role="manager")
    with pytest.raises(ValueError):
        await send_digest(db_session, 9999, manager, "tok")


# ── Weekly summary ───────────────────────────────────────────────────────


def test_weekly_outreach_summary(db_session):
    owner = _mk_user(db_session, "Sales Rep", "sr@trioscs.com")
    owner2 = _mk_user(db_session, "Other Rep", "or@trioscs.com")
    digest = ProactiveDigest(salesperson_id=owner.id, status=ProactiveDigestStatus.SENT)
    db_session.add(digest)
    db_session.flush()
    now = datetime.now(UTC)
    db_session.add_all(
        [
            ProactiveOutreachLine(
                digest_id=digest.id,
                mpn="A",
                salesperson_id=owner.id,
                sent_at=now,
                contacted=True,
                produced_requisition_id=None,
                sales_order_number="SO-1001",
            ),
            ProactiveOutreachLine(digest_id=digest.id, mpn="B", salesperson_id=owner.id, sent_at=now),
            ProactiveOutreachLine(digest_id=digest.id, mpn="C", salesperson_id=owner2.id, sent_at=now),
            # outside the window — ignored
            ProactiveOutreachLine(
                digest_id=digest.id, mpn="D", salesperson_id=owner.id, sent_at=now - timedelta(days=30)
            ),
        ]
    )
    db_session.commit()

    s = weekly_outreach_summary(db_session)
    assert s["totals"]["lines_sent"] == 3
    assert s["totals"]["lines_contacted"] == 1
    assert s["totals"]["orders_won"] == 1
    assert s["by_salesperson"]["Sales Rep"]["sent"] == 2
    assert s["by_salesperson"]["Sales Rep"]["contact_rate"] == 50.0
    assert s["by_salesperson"]["Other Rep"]["contact_rate"] == 0.0
