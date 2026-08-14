"""tests/test_proactive_process.py — Matches-tab Process flow + manager scope.

Check send/ignore → Process stages ONE draft offer per customer (default
contact, sell prices seeded from the last quote, template body) and dismisses
the ignores; the review strip sends each draft with one click. Managers work
every rep's matches via scope=all; drafts never leak into the scorecard or
the Sent tab.

Called by: pytest autodiscovery
Depends on: conftest fixtures (db_session), app.main
"""

import os

os.environ["TESTING"] = "1"

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import update

from app.constants import ProactiveMatchStatus, ProactiveOfferStatus
from app.models import (
    Company,
    CustomerSite,
    Offer,
    ProactiveMatch,
    ProactiveOffer,
    ProactiveThrottle,
    Quote,
    QuoteLine,
    Requisition,
    SiteContact,
    User,
)
from app.services.proactive_service import (
    build_draft_offers,
    discard_draft_offer,
    get_scorecard,
    get_sent_offers,
    list_draft_offers,
    process_matches,
    send_draft_offer,
)
from tests.conftest import engine, requires_postgres  # noqa: F401

HX = {"HX-Request": "true"}


def _mk_user(db, name, email, role="sales"):
    u = User(email=email, name=name, role=role, azure_id=f"az-{email}")
    db.add(u)
    db.flush()
    return u


def _mk_customer(db, name, owner, *, contact_email=None):
    c = Company(name=name, is_active=True, account_owner_id=owner.id)
    db.add(c)
    db.flush()
    s = CustomerSite(company_id=c.id, site_name=f"{name} HQ", is_active=True)
    db.add(s)
    db.flush()
    if contact_email:
        db.add(SiteContact(customer_site_id=s.id, full_name=f"{name} Buyer", email=contact_email))
        db.flush()
    return c, s


def _mk_match(db, *, mpn, owner, company, site, price="8.00", qty=500, target=300):
    offer = Offer(vendor_name="Arrow", mpn=mpn, qty_available=qty, unit_price=Decimal(price), status="active")
    db.add(offer)
    db.flush()
    req = Requisition(name=f"req-{mpn}", customer_site_id=site.id if site else None, status="open", created_by=owner.id)
    db.add(req)
    db.flush()
    from app.models import Requirement

    requirement = Requirement(requisition_id=req.id, primary_mpn=mpn, target_qty=target)
    db.add(requirement)
    db.flush()
    m = ProactiveMatch(
        offer_id=offer.id,
        requirement_id=requirement.id,
        salesperson_id=owner.id,
        mpn=mpn,
        company_id=company.id if company else None,
        customer_site_id=site.id if site else None,
        status="new",
        match_score=70,
    )
    db.add(m)
    db.commit()
    return m


def _scenario(db):
    rep = _mk_user(db, "Sales Rep", "rep@trio.com")
    other = _mk_user(db, "Other Rep", "other@trio.com")
    manager = _mk_user(db, "The Manager", "mgr@trio.com", role="manager")
    beckhoff, beckhoff_site = _mk_customer(db, "Beckhoff", rep, contact_email="buyer@beckhoff.com")
    siemens, siemens_site = _mk_customer(db, "Siemens", rep)  # no contact on file
    m1 = _mk_match(db, mpn="LTSR15-NP", owner=rep, company=beckhoff, site=beckhoff_site, price="6.44")
    m2 = _mk_match(db, mpn="GSOT36C-E3-08", owner=rep, company=beckhoff, site=beckhoff_site, price="0.05")
    m3 = _mk_match(db, mpn="BSM300GA120DN2HOSA1", owner=rep, company=siemens, site=siemens_site, price="274")
    m4 = _mk_match(db, mpn="EE-SY1200", owner=rep, company=beckhoff, site=beckhoff_site, price="1.80")
    return {
        "rep": rep,
        "other": other,
        "manager": manager,
        "beckhoff": beckhoff,
        "beckhoff_site": beckhoff_site,
        "siemens": siemens,
        "m1": m1,
        "m2": m2,
        "m3": m3,
        "m4": m4,
    }


# ── Process: stage + dismiss ─────────────────────────────────────────────


def test_process_stages_one_draft_per_customer_and_dismisses_ignores(db_session):
    s = _scenario(db_session)
    stats = process_matches(
        db_session,
        s["rep"],
        send_ids=[s["m1"].id, s["m2"].id, s["m3"].id],
        ignore_ids=[s["m4"].id],
    )
    db_session.commit()

    assert stats["ignored"] == 1
    assert stats["drafts_created"] == 2  # Beckhoff (m1+m2), Siemens (m3)
    assert stats["needs_contact"] == 1  # Siemens has no contact
    db_session.refresh(s["m4"])
    assert s["m4"].status == ProactiveMatchStatus.DISMISSED
    assert s["m4"].dismiss_reason == "processed_ignore"
    db_session.refresh(s["m1"])
    assert s["m1"].status == ProactiveMatchStatus.NEW  # staged, NOT sent

    drafts = list_draft_offers(db_session, s["rep"])
    assert len(drafts) == 2
    beckhoff_draft = next(d for d in drafts if d["company_name"] == "Beckhoff")
    assert beckhoff_draft["line_count"] == 2
    assert beckhoff_draft["recipient_emails"] == ["buyer@beckhoff.com"]
    siemens_draft = next(d for d in drafts if d["company_name"] == "Siemens")
    assert siemens_draft["recipient_emails"] == []


def test_process_seeds_sell_price_from_last_quote(db_session):
    s = _scenario(db_session)
    req = Requisition(name="q-req", customer_site_id=s["beckhoff_site"].id, status="quoted", created_by=s["rep"].id)
    db_session.add(req)
    db_session.flush()
    quote = Quote(
        requisition_id=req.id,
        customer_site_id=s["beckhoff_site"].id,
        quote_number="Q-77",
        status="sent",
        sent_at=datetime.now(UTC) - timedelta(days=10),
        created_by_id=s["rep"].id,
    )
    db_session.add(quote)
    db_session.flush()
    db_session.add(QuoteLine(quote_id=quote.id, mpn="LTSR15-NP", sell_price=Decimal("8.10")))
    db_session.commit()

    build_draft_offers(db_session, s["rep"], [s["m1"].id])
    db_session.commit()
    draft = db_session.query(ProactiveOffer).filter(ProactiveOffer.status == ProactiveOfferStatus.DRAFT).one()
    line = draft.line_items[0]
    assert line["sell_price"] == 8.10  # last quote, not cost x 1.3
    assert "$8.1000" in draft.email_body_html


def test_process_double_queue_guard(db_session):
    s = _scenario(db_session)
    build_draft_offers(db_session, s["rep"], [s["m1"].id])
    db_session.commit()
    stats = build_draft_offers(db_session, s["rep"], [s["m1"].id, s["m2"].id])
    db_session.commit()
    assert stats["skipped_already_queued"] == 1
    assert stats["drafts_created"] == 1  # m2 only


def test_backorder_matches_are_skipped_with_note(db_session):
    rep = _mk_user(db_session, "Trader", "t@trio.com", role="trader")
    m = _mk_match(db_session, mpn="T521X477M016ATE020", owner=rep, company=None, site=None)
    stats = build_draft_offers(db_session, rep, [m.id])
    assert stats["skipped_backorder"] == 1
    assert stats["drafts_created"] == 0


# ── Send / discard drafts ────────────────────────────────────────────────


@pytest.mark.anyio
async def test_send_draft_marks_everything_and_throttles(db_session):
    s = _scenario(db_session)
    build_draft_offers(db_session, s["rep"], [s["m1"].id, s["m2"].id])
    db_session.commit()
    draft = db_session.query(ProactiveOffer).one()

    with patch("app.utils.graph_client.GraphClient.post_json", new_callable=AsyncMock) as mock_send:
        result = await send_draft_offer(db_session, s["rep"], "tok", draft.id)

    assert mock_send.call_args.args[1]["message"]["toRecipients"][0]["emailAddress"]["address"] == "buyer@beckhoff.com"
    # QC 2026-08-10 P0-1: internal tracking tag no longer leaks into the customer subject.
    assert "[AVAIL-PROACTIVE-" not in mock_send.call_args.args[1]["message"]["subject"]
    assert result["status"] == ProactiveOfferStatus.SENT
    db_session.refresh(s["m1"])
    db_session.refresh(s["m2"])
    assert s["m1"].status == ProactiveMatchStatus.SENT
    assert s["m2"].status == ProactiveMatchStatus.SENT
    throttles = db_session.query(ProactiveThrottle).all()
    assert {t.mpn for t in throttles} == {"LTSR15-NP", "GSOT36C-E3-08"}


@pytest.mark.anyio
async def test_send_draft_requires_recipients_and_ownership(db_session):
    s = _scenario(db_session)
    build_draft_offers(db_session, s["rep"], [s["m3"].id])  # Siemens — no contact
    db_session.commit()
    draft = db_session.query(ProactiveOffer).one()

    with pytest.raises(ValueError, match="No contact on file"):
        await send_draft_offer(db_session, s["rep"], "tok", draft.id)
    with pytest.raises(ValueError, match="Not your draft"):
        await send_draft_offer(db_session, s["other"], "tok", draft.id)
    # Manager may act with allow_all — passes ownership, still blocked on contact.
    with pytest.raises(ValueError, match="No contact on file"):
        await send_draft_offer(db_session, s["manager"], "tok", draft.id, allow_all=True)


def test_discard_draft_returns_matches_to_list(db_session):
    s = _scenario(db_session)
    build_draft_offers(db_session, s["rep"], [s["m1"].id])
    db_session.commit()
    draft = db_session.query(ProactiveOffer).one()
    discard_draft_offer(db_session, s["rep"], draft.id)
    db_session.commit()
    assert db_session.query(ProactiveOffer).count() == 0
    db_session.refresh(s["m1"])
    assert s["m1"].status == ProactiveMatchStatus.NEW


# ── Drafts never masquerade as outreach ──────────────────────────────────


def test_drafts_excluded_from_scorecard_and_sent_tab(db_session):
    s = _scenario(db_session)
    build_draft_offers(db_session, s["rep"], [s["m1"].id])
    db_session.commit()
    assert get_scorecard(db_session)["total_sent"] == 0
    assert get_sent_offers(db_session, s["rep"].id) == []


# ── Routes + manager scope ───────────────────────────────────────────────


def _make_client(db, user):
    from fastapi.testclient import TestClient

    from app.database import get_db
    from app.dependencies import require_user
    from app.main import app

    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[require_user] = lambda: user
    return TestClient(app, raise_server_exceptions=False)


def _clear_overrides():
    from app.main import app

    app.dependency_overrides.clear()


def test_process_endpoint_stages_and_reports(db_session):
    s = _scenario(db_session)
    try:
        client = _make_client(db_session, s["rep"])
        r = client.post(
            "/v2/partials/proactive/process",
            data={"send_ids": [str(s["m1"].id), str(s["m3"].id)], "ignore_ids": [str(s["m4"].id)]},
            headers=HX,
        )
        assert r.status_code == 200
        assert "2 offer(s) prepared for review" in r.text
        assert "1 ignored" in r.text
        assert "Prepared offers" in r.text
    finally:
        _clear_overrides()


def test_prepared_send_endpoint_authz(db_session):
    s = _scenario(db_session)
    build_draft_offers(db_session, s["rep"], [s["m1"].id])
    db_session.commit()
    draft = db_session.query(ProactiveOffer).one()
    try:
        client = _make_client(db_session, s["other"])
        r = client.post(f"/v2/partials/proactive/prepared/{draft.id}/send", headers=HX)
        assert r.status_code in (400, 403)  # blocked before any send attempt
        db_session.refresh(draft)
        assert draft.status == ProactiveOfferStatus.DRAFT
    finally:
        _clear_overrides()


def test_manager_scope_all_sees_rep_matches(db_session):
    s = _scenario(db_session)
    try:
        client = _make_client(db_session, s["manager"])
        r = client.get("/v2/partials/proactive?tab=matches&scope=all", headers=HX)
        assert r.status_code == 200
        assert "LTSR15-NP" in r.text
        assert "Sales Rep" in r.text  # rep attribution chip
        r = client.get("/v2/partials/proactive?tab=matches", headers=HX)
        assert "LTSR15-NP" not in r.text  # manager's own list is empty
    finally:
        _clear_overrides()


def test_sales_cannot_use_scope_all(db_session):
    s = _scenario(db_session)
    other_customer, other_site = _mk_customer(db_session, "Zeta", s["other"], contact_email="z@z.com")
    _mk_match(db_session, mpn="ZETA-1", owner=s["other"], company=other_customer, site=other_site)
    try:
        client = _make_client(db_session, s["rep"])
        r = client.get("/v2/partials/proactive?tab=matches&scope=all", headers=HX)
        assert r.status_code == 200
        assert "ZETA-1" not in r.text  # silently pinned to their own matches
        assert "LTSR15-NP" in r.text
    finally:
        _clear_overrides()


# ── QC 2026-08-08: Graph failures never fake success ─────────────────────


@pytest.mark.anyio
async def test_failed_draft_send_leaves_everything_untouched(db_session):
    from app.utils.graph_client import GraphAPIError

    s = _scenario(db_session)
    build_draft_offers(db_session, s["rep"], [s["m1"].id])
    db_session.commit()
    draft = db_session.query(ProactiveOffer).one()

    with patch(
        "app.utils.graph_client.GraphClient.post_json",
        new_callable=AsyncMock,
        side_effect=GraphAPIError(401, "token expired"),
    ):
        with pytest.raises(ValueError, match="draft is untouched"):
            await send_draft_offer(db_session, s["rep"], "tok", draft.id)

    db_session.rollback()
    db_session.refresh(draft)
    assert draft.status == ProactiveOfferStatus.DRAFT
    db_session.refresh(s["m1"])
    assert s["m1"].status == ProactiveMatchStatus.NEW
    assert db_session.query(ProactiveThrottle).count() == 0


@pytest.mark.anyio
async def test_failed_oneshot_send_marks_offer_failed_not_sent(db_session):
    from app.services.proactive_service import get_scorecard, send_proactive_offer
    from app.utils.graph_client import GraphAPIError

    s = _scenario(db_session)
    from app.models import SiteContact

    contact = db_session.query(SiteContact).filter(SiteContact.email == "buyer@beckhoff.com").one()

    with patch(
        "app.utils.graph_client.GraphClient.post_json",
        new_callable=AsyncMock,
        side_effect=GraphAPIError(503, "graph down"),
    ):
        await send_proactive_offer(
            db=db_session,
            user=s["rep"],
            token="tok",
            match_ids=[s["m1"].id],
            contact_ids=[contact.id],
            sell_prices={},
        )

    po = db_session.query(ProactiveOffer).one()
    assert po.status == ProactiveOfferStatus.FAILED  # never a phantom 'sent'
    assert po.sent_at is None
    db_session.refresh(s["m1"])
    assert s["m1"].status == ProactiveMatchStatus.FAILED
    assert db_session.query(ProactiveThrottle).count() == 0  # retry not suppressed
    assert get_scorecard(db_session)["total_sent"] == 0  # never counted as outreach


@requires_postgres
def test_proactive_offer_claim_is_exclusive(pg_session, pg_engine):
    """The DRAFT→SENT claim send_draft_offer runs before the Graph send can match a row
    only ONCE — this is the DB-level guarantee that two racing sends never double-email
    a customer.

    First claimant gets rowcount 1; after it commits, the second sees 0 and bails.
    """
    from sqlalchemy.orm import sessionmaker

    po = ProactiveOffer(status=ProactiveOfferStatus.DRAFT, line_items=[])
    pg_session.add(po)
    pg_session.commit()

    def _claim(session):
        return session.execute(
            update(ProactiveOffer)
            .where(ProactiveOffer.id == po.id, ProactiveOffer.status == ProactiveOfferStatus.DRAFT)
            .values(status=ProactiveOfferStatus.SENT)
        ).rowcount

    second_session = sessionmaker(bind=pg_engine)()
    try:
        first = _claim(pg_session)
        pg_session.commit()
        second = _claim(second_session)
        second_session.commit()
    finally:
        second_session.close()

    assert first == 1  # this caller won the claim and sends
    assert second == 0  # the racer finds no DRAFT row and must not send
