"""tests/test_proactive_digest_routes.py — Digests tab, generate/send, tracking, picks,
drill-down.

Route-level coverage for the 2026-08-06 augmentation endpoints in
app/routers/htmx/proactive.py: manager-gated generate/send (nothing sends
without a human click), per-line tracking authz, the 24h-cached picks strip,
and the rolled-up line's offers drill-down.

Called by: pytest autodiscovery
Depends on: conftest fixtures (db_session), app.main
"""

import os

os.environ["TESTING"] = "1"

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.constants import ProactiveDigestStatus
from app.models import (
    Company,
    CustomerSite,
    Offer,
    ProactiveDigest,
    ProactiveMatch,
    ProactiveOutreachLine,
    User,
)
from tests.conftest import engine  # noqa: F401

HX = {"HX-Request": "true"}


def _make_client(db: Session, user: User) -> TestClient:
    """TestClient authenticated as *user* (same pattern as the router-gap tests)."""
    from app.database import get_db
    from app.dependencies import require_fresh_token, require_user
    from app.main import app

    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[require_user] = lambda: user

    async def _tok():
        return "token"

    app.dependency_overrides[require_fresh_token] = _tok
    return TestClient(app, raise_server_exceptions=False)


def _clear_overrides():
    from app.main import app

    app.dependency_overrides.clear()


def _scenario(db: Session):
    sales = User(email="rep@trio.com", name="Sales Rep", role="sales", azure_id="az-rep")
    other = User(email="other@trio.com", name="Other Rep", role="sales", azure_id="az-other")
    manager = User(email="mgr@trio.com", name="The Manager", role="manager", azure_id="az-mgr")
    db.add_all([sales, other, manager])
    db.flush()
    company = Company(name="Beckhoff", is_active=True, account_owner_id=sales.id)
    db.add(company)
    db.flush()
    site = CustomerSite(company_id=company.id, site_name="HQ", is_active=True)
    db.add(site)
    db.flush()
    offer_a = Offer(
        vendor_name="Arrow", mpn="LTSR15-NP", qty_available=850, unit_price=Decimal("6.44"), status="active"
    )
    offer_b = Offer(
        vendor_name="Sierra", mpn="LTSR15-NP", qty_available=4000, unit_price=Decimal("7.10"), status="active"
    )
    db.add_all([offer_a, offer_b])
    db.flush()
    match = ProactiveMatch(
        offer_id=offer_a.id,
        salesperson_id=sales.id,
        mpn="LTSR15-NP",
        company_id=company.id,
        customer_site_id=site.id,
        status="new",
        match_score=80,
        requirement_count=2,
        last_asked_at=datetime.now(UTC) - timedelta(days=3),
        last_asked_qty=3000,
    )
    db.add(match)
    db.commit()
    return {"sales": sales, "other": other, "manager": manager, "company": company, "match": match}


def test_digests_tab_renders(db_session):
    s = _scenario(db_session)
    try:
        client = _make_client(db_session, s["manager"])
        r = client.get("/v2/partials/proactive?tab=digests", headers=HX)
        assert r.status_code == 200
        assert "Generate digests" in r.text
        assert "Last 7 days" in r.text
    finally:
        _clear_overrides()


def test_generate_requires_manager(db_session):
    s = _scenario(db_session)
    try:
        client = _make_client(db_session, s["sales"])
        r = client.post("/v2/partials/proactive/digests/generate", headers=HX)
        assert r.status_code == 403
    finally:
        _clear_overrides()


def test_generate_and_send_flow(db_session):
    s = _scenario(db_session)
    try:
        client = _make_client(db_session, s["manager"])
        r = client.post("/v2/partials/proactive/digests/generate", headers=HX)
        assert r.status_code == 200
        digest = db_session.query(ProactiveDigest).one()
        assert digest.status == ProactiveDigestStatus.DRAFT
        assert "LTSR15-NP" in digest.body_html
        assert "(2 requests on file)" in digest.body_html
        assert "4,850 pcs" in digest.body_html  # 850 + 4,000 rolled up

        with (
            patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="tok"),
            patch("app.utils.graph_client.GraphClient.post_json", new_callable=AsyncMock) as mock_send,
        ):
            r = client.post(f"/v2/partials/proactive/digests/{digest.id}/send", headers=HX)
        assert r.status_code == 200
        assert mock_send.call_args.args[1]["message"]["toRecipients"][0]["emailAddress"]["address"] == "rep@trio.com"
        db_session.refresh(digest)
        assert digest.status == ProactiveDigestStatus.SENT
        assert digest.sent_by_id == s["manager"].id
    finally:
        _clear_overrides()


def test_send_requires_manager(db_session):
    s = _scenario(db_session)
    digest = ProactiveDigest(salesperson_id=s["sales"].id, status=ProactiveDigestStatus.DRAFT)
    db_session.add(digest)
    db_session.commit()
    try:
        client = _make_client(db_session, s["sales"])
        r = client.post(f"/v2/partials/proactive/digests/{digest.id}/send", headers=HX)
        assert r.status_code == 403
    finally:
        _clear_overrides()


def _sent_line(db, s):
    digest = ProactiveDigest(salesperson_id=s["sales"].id, status=ProactiveDigestStatus.SENT, sent_at=datetime.now(UTC))
    db.add(digest)
    db.flush()
    line = ProactiveOutreachLine(
        digest_id=digest.id,
        mpn="LTSR15-NP",
        company_id=s["company"].id,
        salesperson_id=s["sales"].id,
        sent_at=datetime.now(UTC),
    )
    db.add(line)
    db.commit()
    return line


def test_line_tracking_updates_and_authz(db_session):
    s = _scenario(db_session)
    line = _sent_line(db_session, s)
    try:
        client = _make_client(db_session, s["sales"])
        r = client.post(
            f"/v2/partials/proactive/lines/{line.id}/tracking",
            data={"outcome": "still_looking"},
            headers=HX,
        )
        assert r.status_code == 200
        db_session.refresh(line)
        assert line.outcome == "still_looking"
        assert line.contacted is True  # a real answer implies contact

        r = client.post(
            f"/v2/partials/proactive/lines/{line.id}/tracking",
            data={"sales_order_number": "SO-1001"},
            headers=HX,
        )
        assert r.status_code == 200
        db_session.refresh(line)
        assert line.sales_order_number == "SO-1001"
    finally:
        _clear_overrides()

    try:
        client = _make_client(db_session, s["other"])
        r = client.post(
            f"/v2/partials/proactive/lines/{line.id}/tracking",
            data={"contacted": "true"},
            headers=HX,
        )
        assert r.status_code == 403
    finally:
        _clear_overrides()


def test_picks_strip(db_session):
    s = _scenario(db_session)
    try:
        client = _make_client(db_session, s["sales"])
        r = client.get("/v2/partials/proactive/picks", headers=HX)
        assert r.status_code == 200
        assert "LTSR15-NP" in r.text
        assert "Top picks" in r.text
    finally:
        _clear_overrides()


def test_offers_drilldown_owner_only(db_session):
    s = _scenario(db_session)
    try:
        client = _make_client(db_session, s["sales"])
        r = client.get(f"/v2/partials/proactive/{s['match'].id}/offers", headers=HX)
        assert r.status_code == 200
        assert "Arrow" in r.text
        assert "Sierra" in r.text
    finally:
        _clear_overrides()
    try:
        client = _make_client(db_session, s["other"])
        r = client.get(f"/v2/partials/proactive/{s['match'].id}/offers", headers=HX)
        assert r.status_code == 403
    finally:
        _clear_overrides()
