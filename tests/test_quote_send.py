"""test_quote_send.py — Tests for the canonical quote-send service and its two callers.

Covers app/services/quote_send.py (send_quote_email) plus the two routes that wrap it:
  - htmx route  POST /v2/partials/quotes/{id}/send  (app/routers/htmx_views.py)
  - JSON  route POST /api/quotes/{id}/send          (app/routers/crm/quotes.py)

Proves the S1 regression fix (htmx route now actually emails), DNC hard-block on the
quote path, Graph message-id capture, and the OUTBOUND ActivityLog write. No network:
GraphClient.post_json and app.email_service._find_sent_message are monkeypatched.

Depends on: tests/conftest.py fixtures (client, db_session, test_user, test_company,
test_requisition, test_customer_site), SQLite test engine.
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from sqlalchemy.orm import Session

from app.constants import Direction, QuoteStatus, RequisitionStatus
from app.models import ActivityLog, CustomerSite, Quote, SiteContact


def _draft_quote(db: Session, req, site, user, number="Q-2026-SEND") -> Quote:
    """Build and persist a DRAFT quote tied to req/site/user."""
    q = Quote(
        requisition_id=req.id,
        customer_site_id=site.id,
        quote_number=number,
        status="draft",
        line_items=[{"mpn": "LM317T", "qty": 100, "sell_price": 5.00}],
        subtotal=500.0,
        total_cost=300.0,
        total_margin_pct=40.0,
        created_by_id=user.id,
        created_at=datetime.now(timezone.utc),
    )
    db.add(q)
    db.commit()
    db.refresh(q)
    return q


# ── S1 regression: htmx route now actually emails ──────────────────────────────


def test_htmx_send_triggers_real_send_email(client, db_session, test_requisition, test_customer_site, test_user):
    """The htmx send route must invoke the canonical service (which sends) — proving S1
    is fixed.

    In TESTING mode the service skips the real Graph POST but still marks sent, so we
    assert send_quote_email itself was called for our quote.
    """
    from app.routers import htmx_views

    quote = _draft_quote(db_session, test_requisition, test_customer_site, test_user)

    async def _fake(db, q, user, **kwargs):
        from app.services.quote_send import SendQuoteResult

        q.status = QuoteStatus.SENT
        q.sent_at = datetime.now(timezone.utc)
        db.commit()
        return SendQuoteResult(
            sent_to="jane@acme-electronics.com",
            status="sent",
            req_status=RequisitionStatus.QUOTED,
            status_changed=True,
            graph_message_id=None,
        )

    with patch.object(htmx_views, "send_quote_email", new=AsyncMock(side_effect=_fake)) as mock_send:
        resp = client.post(f"/v2/partials/quotes/{quote.id}/send")

    assert resp.status_code == 200
    assert mock_send.called
    # The route passes the resolved Quote ORM object as the second positional arg.
    called_quote = mock_send.call_args.args[1]
    assert called_quote.id == quote.id


def test_htmx_send_marks_sent_via_real_service(client, db_session, test_requisition, test_customer_site, test_user):
    """End-to-end through the REAL service under TESTING=1: status→sent, req→quoted."""
    quote = _draft_quote(db_session, test_requisition, test_customer_site, test_user, number="Q-2026-E2E")
    resp = client.post(f"/v2/partials/quotes/{quote.id}/send")
    assert resp.status_code == 200
    db_session.refresh(quote)
    assert quote.status == QuoteStatus.SENT
    assert quote.sent_at is not None
    db_session.refresh(test_requisition)
    assert test_requisition.status == RequisitionStatus.QUOTED


# ── DNC hard-block on the quote path ───────────────────────────────────────────


def test_htmx_send_blocked_when_site_dnc(client, db_session, test_requisition, test_customer_site, test_user):
    """Site-level do_not_contact blocks the send: status unchanged, rose partial back."""
    test_customer_site.do_not_contact = True
    db_session.commit()
    quote = _draft_quote(db_session, test_requisition, test_customer_site, test_user, number="Q-2026-DNCS")

    resp = client.post(f"/v2/partials/quotes/{quote.id}/send")
    assert resp.status_code == 200
    assert "do-not-contact" in resp.text
    db_session.refresh(quote)
    assert quote.status == "draft"


def test_htmx_send_blocked_when_contact_dnc(client, db_session, test_requisition, test_customer_site, test_user):
    """A SiteContact matching the recipient email with do_not_contact blocks the
    send."""
    db_session.add(
        SiteContact(
            customer_site_id=test_customer_site.id,
            full_name="Jane Doe",
            email="JANE@acme-electronics.com",  # case-insensitive match
            do_not_contact=True,
        )
    )
    db_session.commit()
    quote = _draft_quote(db_session, test_requisition, test_customer_site, test_user, number="Q-2026-DNCC")

    resp = client.post(f"/v2/partials/quotes/{quote.id}/send")
    assert resp.status_code == 200
    assert "do-not-contact" in resp.text
    db_session.refresh(quote)
    assert quote.status == "draft"


@patch("app.dependencies.require_fresh_token", new_callable=AsyncMock)
def test_json_send_dnc_returns_409(mock_token, client, db_session, test_requisition, test_customer_site, test_user):
    """The JSON route surfaces a DNC block as HTTP 409."""
    mock_token.return_value = "fake-token"
    test_customer_site.do_not_contact = True
    db_session.commit()
    quote = _draft_quote(db_session, test_requisition, test_customer_site, test_user, number="Q-2026-409")

    resp = client.post(f"/api/quotes/{quote.id}/send")
    assert resp.status_code == 409
    db_session.refresh(quote)
    assert quote.status == "draft"


# ── Service-level: happy path, activity log, graph-id capture ──────────────────


async def test_service_happy_path_sets_status_and_req(db_session, test_requisition, test_customer_site, test_user):
    """Direct service call in TESTING mode: status→sent, sent_at set, req→quoted."""
    from app.services.quote_send import send_quote_email

    quote = _draft_quote(db_session, test_requisition, test_customer_site, test_user, number="Q-2026-SVC1")
    result = await send_quote_email(db_session, quote, test_user, token="t", testing=True)

    assert result.status == "sent"
    assert result.sent_to == "jane@acme-electronics.com"
    assert result.req_status == RequisitionStatus.QUOTED
    assert result.status_changed is True
    db_session.refresh(quote)
    assert quote.status == QuoteStatus.SENT
    assert quote.sent_at is not None
    db_session.refresh(test_requisition)
    assert test_requisition.status == RequisitionStatus.QUOTED


async def test_service_writes_outbound_activity_log(db_session, test_requisition, test_customer_site, test_user):
    """An OUTBOUND email ActivityLog row is written for the customer recipient."""
    from app.services.quote_send import send_quote_email

    quote = _draft_quote(db_session, test_requisition, test_customer_site, test_user, number="Q-2026-LOG")
    await send_quote_email(db_session, quote, test_user, token="t", testing=True)

    logs = db_session.query(ActivityLog).filter(ActivityLog.contact_email == "jane@acme-electronics.com").all()
    assert len(logs) >= 1
    log = logs[-1]
    assert log.direction == Direction.OUTBOUND
    assert "Q-2026-LOG" in (log.subject or "")


async def test_service_captures_graph_ids_when_message_found(
    db_session, test_requisition, test_customer_site, test_user
):
    """Non-testing path: graph_message_id/conversation_id captured from _find_sent_message."""
    from app.services.quote_send import send_quote_email

    quote = _draft_quote(db_session, test_requisition, test_customer_site, test_user, number="Q-2026-GID")

    with (
        patch("app.utils.graph_client.GraphClient.post_json", new_callable=AsyncMock) as mock_post,
        patch("app.email_service._find_sent_message", new_callable=AsyncMock) as mock_find,
    ):
        mock_post.return_value = {}
        mock_find.return_value = {"id": "MSG123", "conversationId": "CONV456"}
        result = await send_quote_email(db_session, quote, test_user, token="t", testing=False)

    assert mock_post.called
    assert result.graph_message_id == "MSG123"
    db_session.refresh(quote)
    assert quote.graph_message_id == "MSG123"
    assert quote.graph_conversation_id == "CONV456"


async def test_service_graph_ids_none_safe_when_no_message(db_session, test_requisition, test_customer_site, test_user):
    """None-safe: when _find_sent_message returns None the graph ids stay NULL."""
    from app.services.quote_send import send_quote_email

    quote = _draft_quote(db_session, test_requisition, test_customer_site, test_user, number="Q-2026-GNON")

    with (
        patch("app.utils.graph_client.GraphClient.post_json", new_callable=AsyncMock) as mock_post,
        patch("app.email_service._find_sent_message", new_callable=AsyncMock) as mock_find,
    ):
        mock_post.return_value = {}
        mock_find.return_value = None
        result = await send_quote_email(db_session, quote, test_user, token="t", testing=False)

    assert result.graph_message_id is None
    db_session.refresh(quote)
    assert quote.graph_message_id is None
    assert quote.graph_conversation_id is None


async def test_service_raises_on_graph_error(db_session, test_requisition, test_customer_site, test_user):
    """A Graph error response raises QuoteSendError and does NOT mark sent."""
    from app.services.quote_send import QuoteSendError, send_quote_email

    quote = _draft_quote(db_session, test_requisition, test_customer_site, test_user, number="Q-2026-GERR")

    with patch("app.utils.graph_client.GraphClient.post_json", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = {"error": "SendFailed", "detail": "Auth error"}
        raised = False
        try:
            await send_quote_email(db_session, quote, test_user, token="t", testing=False)
        except QuoteSendError:
            raised = True
    assert raised
    db_session.refresh(quote)
    assert quote.status == "draft"


async def test_service_raises_dnc_blocked(db_session, test_requisition, test_customer_site, test_user):
    """DNC site raises QuoteSendDNCBlocked carrying the recipient; no send, no status
    change."""
    from app.services.quote_send import QuoteSendDNCBlocked, send_quote_email

    test_customer_site.do_not_contact = True
    db_session.commit()
    quote = _draft_quote(db_session, test_requisition, test_customer_site, test_user, number="Q-2026-SDNC")

    recipient = None
    try:
        await send_quote_email(db_session, quote, test_user, token="t", testing=True)
    except QuoteSendDNCBlocked as exc:
        recipient = exc.recipient
    assert recipient == "jane@acme-electronics.com"
    db_session.refresh(quote)
    assert quote.status == "draft"


async def test_service_raises_on_missing_email(db_session, test_requisition, test_company, test_user):
    """No recipient email raises QuoteSendError (400-equivalent at the route)."""
    from app.services.quote_send import QuoteSendError, send_quote_email

    site = CustomerSite(company_id=test_company.id, site_name="No Email Site", contact_email=None)
    db_session.add(site)
    db_session.flush()
    quote = _draft_quote(db_session, test_requisition, site, test_user, number="Q-2026-NOEM2")

    raised = False
    try:
        await send_quote_email(db_session, quote, test_user, token="t", testing=True)
    except QuoteSendError:
        raised = True
    assert raised


# ── JSON route preserves its documented response shape ─────────────────────────


@patch("app.dependencies.require_fresh_token", new_callable=AsyncMock)
def test_json_send_returns_documented_shape(
    mock_token, client, db_session, test_requisition, test_customer_site, test_user
):
    """crm/quotes.py send_quote keeps its {ok,status,sent_to,req_status,status_changed}
    JSON."""
    mock_token.return_value = "fake-token"
    quote = _draft_quote(db_session, test_requisition, test_customer_site, test_user, number="Q-2026-JSON")

    with (
        patch("app.utils.graph_client.GraphClient.post_json", new_callable=AsyncMock) as mock_post,
        patch("app.email_service._find_sent_message", new_callable=AsyncMock) as mock_find,
    ):
        mock_post.return_value = {}
        mock_find.return_value = {"id": "M1", "conversationId": "C1"}
        resp = client.post(f"/api/quotes/{quote.id}/send")

    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["status"] == "sent"
    assert data["sent_to"] == "jane@acme-electronics.com"
    assert data["req_status"] == RequisitionStatus.QUOTED
    assert "status_changed" in data
    assert mock_post.called
