"""test_quote_send_dialog.py — Tests for Task 4 (B1): the quote send dialog.

Covers:
  - GET /v2/partials/quotes/{id}/send-dialog — renders the resolved recipient
    (site contact) prefilled + a CC input + preflight warnings.
  - POST /v2/partials/quotes/{id}/send reads override_email/override_name/cc form
    fields (all optional — absent form = legacy behavior unchanged) and forwards
    them to the canonical send_quote_email service.
  - send_quote_email's optional override_email/cc reach the Graph sendMail
    payload (toRecipients / ccRecipients) — same Graph mock seam as
    tests/test_quote_send.py (GraphClient.post_json, email_service._find_sent_message).
  - A successful send fires a showToast HX-Trigger naming quote number + recipient.

Depends on: tests/conftest.py fixtures (client, db_session, test_user,
test_requisition, test_customer_site), SQLite test engine.
"""

import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

from sqlalchemy.orm import Session

from app.constants import QuoteStatus
from app.models import Quote


def _draft_quote(db: Session, req, site, user, number="Q-2026-DLG") -> Quote:
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
        created_at=datetime.now(UTC),
    )
    db.add(q)
    db.commit()
    db.refresh(q)
    return q


# ── (a) GET dialog ───────────────────────────────────────────────────────────


def test_get_send_dialog_shows_prefilled_contact_and_cc_input(
    client, db_session, test_requisition, test_customer_site, test_user
):
    """The dialog prefills the site contact's email and offers an editable CC input."""
    quote = _draft_quote(db_session, test_requisition, test_customer_site, test_user)

    resp = client.get(f"/v2/partials/quotes/{quote.id}/send-dialog")

    assert resp.status_code == 200
    assert "jane@acme-electronics.com" in resp.text
    assert "name='override_email'" in resp.text
    assert "name='override_name'" in resp.text
    assert "name='cc'" in resp.text
    # The subject is built via the canonical build_quote_subject (Task 3) so the
    # dialog can never drift from what actually sends.
    assert quote.quote_number in resp.text
    assert "Trio Supply Chain Solutions" in resp.text


# ── (b) POST send with override_email → the route forwards it to the service,
#        which the mocked Graph send receives ────────────────────────────────


def test_post_send_forwards_override_and_cc_form_fields_to_service(
    client, db_session, test_requisition, test_customer_site, test_user
):
    """The route reads override_email/override_name/cc Form fields (all optional) and
    forwards them to send_quote_email — same wiring style as
    test_quote_send.py::test_htmx_send_triggers_real_send_email."""
    from app.routers.htmx import quotes
    from app.services.quote_send import SendQuoteResult

    quote = _draft_quote(db_session, test_requisition, test_customer_site, test_user, number="Q-2026-OVR")

    async def _fake(db, q, user, **kwargs):
        q.status = QuoteStatus.SENT
        q.sent_at = datetime.now(UTC)
        db.commit()
        return SendQuoteResult(
            sent_to=kwargs.get("override_email") or "jane@acme-electronics.com",
            status="sent",
            req_status=None,
            status_changed=False,
            graph_message_id=None,
        )

    with patch.object(quotes, "send_quote_email", new=AsyncMock(side_effect=_fake)) as mock_send:
        resp = client.post(
            f"/v2/partials/quotes/{quote.id}/send",
            data={"override_email": "override@example.com", "override_name": "Override Name", "cc": "cc@example.com"},
        )

    assert resp.status_code == 200
    assert mock_send.called
    kwargs = mock_send.call_args.kwargs
    assert kwargs["override_email"] == "override@example.com"
    assert kwargs["override_name"] == "Override Name"
    assert kwargs["cc"] == "cc@example.com"


async def test_service_override_email_reaches_graph_to_payload(
    db_session, test_requisition, test_customer_site, test_user
):
    """send_quote_email's override_email reaches the Graph sendMail toRecipients —
    same mock seam as test_quote_send.py::test_service_captures_graph_ids_when_message_found."""
    from app.services.quote_send import send_quote_email

    quote = _draft_quote(db_session, test_requisition, test_customer_site, test_user, number="Q-2026-OVRSVC")

    with (
        patch("app.utils.graph_client.GraphClient.post_json", new_callable=AsyncMock) as mock_post,
        patch("app.email_service._find_sent_message", new_callable=AsyncMock) as mock_find,
    ):
        mock_post.return_value = {}
        mock_find.return_value = {"id": "MSG1", "conversationId": "CONV1"}
        result = await send_quote_email(
            db_session,
            quote,
            test_user,
            token="t",
            testing=False,
            override_email="override@example.com",
            override_name="Override Name",
        )

    assert mock_post.called
    payload = mock_post.call_args.args[1]
    assert payload["message"]["toRecipients"] == [
        {"emailAddress": {"address": "override@example.com", "name": "Override Name"}}
    ]
    assert result.sent_to == "override@example.com"


# ── (c) success response carries the HX-Trigger showToast naming sent_to ────


def test_post_send_success_toast_names_recipient(client, db_session, test_requisition, test_customer_site, test_user):
    """A successful send fires a showToast HX-Trigger naming the quote number +
    recipient."""
    quote = _draft_quote(db_session, test_requisition, test_customer_site, test_user, number="Q-2026-TOAST")

    resp = client.post(f"/v2/partials/quotes/{quote.id}/send")

    assert resp.status_code == 200
    trigger = json.loads(resp.headers["HX-Trigger"])
    assert quote.quote_number in trigger["showToast"]["message"]
    assert "jane@acme-electronics.com" in trigger["showToast"]["message"]
    assert trigger["showToast"]["type"] == "success"


# ── (d) POST send with no form fields still works (legacy path) ─────────────


def test_post_send_with_no_form_fields_is_legacy_unchanged(
    client, db_session, test_requisition, test_customer_site, test_user
):
    """Absent override/cc form fields must not change existing (legacy) send
    behavior."""
    quote = _draft_quote(db_session, test_requisition, test_customer_site, test_user, number="Q-2026-LEGACY")

    resp = client.post(f"/v2/partials/quotes/{quote.id}/send")

    assert resp.status_code == 200
    db_session.refresh(quote)
    assert quote.status == QuoteStatus.SENT
    assert quote.sent_at is not None


# ── (e) cc value reaches the Graph payload ───────────────────────────────────


async def test_service_cc_reaches_graph_payload(db_session, test_requisition, test_customer_site, test_user):
    """send_quote_email's optional cc param threads into the Graph sendMail payload as
    ccRecipients."""
    from app.services.quote_send import send_quote_email

    quote = _draft_quote(db_session, test_requisition, test_customer_site, test_user, number="Q-2026-CC")

    with (
        patch("app.utils.graph_client.GraphClient.post_json", new_callable=AsyncMock) as mock_post,
        patch("app.email_service._find_sent_message", new_callable=AsyncMock) as mock_find,
    ):
        mock_post.return_value = {}
        mock_find.return_value = {"id": "MSG2", "conversationId": "CONV2"}
        await send_quote_email(db_session, quote, test_user, token="t", testing=False, cc="cc@example.com")

    assert mock_post.called
    payload = mock_post.call_args.args[1]
    assert payload["message"]["ccRecipients"] == [{"emailAddress": {"address": "cc@example.com"}}]


async def test_service_empty_cc_is_none_no_cc_recipients_key(
    db_session, test_requisition, test_customer_site, test_user
):
    """An empty/whitespace cc string is treated as no CC — key omitted entirely."""
    from app.services.quote_send import send_quote_email

    quote = _draft_quote(db_session, test_requisition, test_customer_site, test_user, number="Q-2026-CCEMPTY")

    with (
        patch("app.utils.graph_client.GraphClient.post_json", new_callable=AsyncMock) as mock_post,
        patch("app.email_service._find_sent_message", new_callable=AsyncMock) as mock_find,
    ):
        mock_post.return_value = {}
        mock_find.return_value = None
        await send_quote_email(db_session, quote, test_user, token="t", testing=False, cc="   ")

    assert mock_post.called
    payload = mock_post.call_args.args[1]
    assert "ccRecipients" not in payload["message"]
