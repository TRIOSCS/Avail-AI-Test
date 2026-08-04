"""test_quote_result_draft.py — W1.18: draft quotes offer Won/Lost, not just sent ones.

The kernel walk found a keys-off door: the server-side status machine has always
allowed draft→won / draft→lost (QUOTE_TRANSITIONS in app/services/status_machine.py),
but the quote detail partial only rendered the Mark Won / Mark Lost buttons for
status == 'sent' — so an out-of-band result (the customer decided off a quote never
formally sent from AVAIL) could only be logged by hand-posting the endpoint.

Covers:
  - template render: a DRAFT quote's detail partial shows Mark Won + Mark Lost
    (and still shows them on 'sent'; still hides them on closed states);
  - endpoint: POST /v2/partials/quotes/{id}/result flips a draft quote to won/lost.

Depends on: tests/conftest.py (client, db_session, test_quote), app/routers/htmx/quotes.py,
app/templates/htmx/partials/quotes/detail.html.
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.constants import QuoteStatus
from app.models import Quote


def _make_draft(db_session: Session, quote: Quote) -> Quote:
    """Flip the seeded sent quote back to a plain draft."""
    quote.status = QuoteStatus.DRAFT
    db_session.commit()
    db_session.refresh(quote)
    return quote


# ── Template render ──────────────────────────────────────────────────


def test_draft_quote_renders_won_lost_buttons(client: TestClient, db_session: Session, test_quote: Quote):
    """A DRAFT quote's detail partial offers Mark Won AND Mark Lost (W1.18)."""
    quote = _make_draft(db_session, test_quote)
    resp = client.get(f"/v2/partials/quotes/{quote.id}")
    assert resp.status_code == 200
    assert "Mark Won" in resp.text
    assert "Mark Lost" in resp.text
    # Both buttons post the existing result endpoint — no new UI elements.
    assert resp.text.count(f"/v2/partials/quotes/{quote.id}/result") == 2


def test_sent_quote_still_renders_won_lost_buttons(client: TestClient, test_quote: Quote):
    """The widened condition keeps the buttons on 'sent' (the original state)."""
    resp = client.get(f"/v2/partials/quotes/{test_quote.id}")
    assert resp.status_code == 200
    assert "Mark Won" in resp.text
    assert "Mark Lost" in resp.text


def test_won_quote_hides_result_buttons(client: TestClient, db_session: Session, test_quote: Quote):
    """Closed states still hide the result buttons — the widening stops at
    draft+sent."""
    test_quote.status = QuoteStatus.WON
    db_session.commit()
    resp = client.get(f"/v2/partials/quotes/{test_quote.id}")
    assert resp.status_code == 200
    assert "Mark Won" not in resp.text
    assert "Mark Lost" not in resp.text


# ── Endpoint: draft → won / lost ─────────────────────────────────────


def test_draft_quote_mark_won(client: TestClient, db_session: Session, test_quote: Quote):
    """Draft→won is a valid server transition — the endpoint accepts it directly."""
    quote = _make_draft(db_session, test_quote)
    resp = client.post(
        f"/v2/partials/quotes/{quote.id}/result",
        data={"result": "won", "result_reason": "Customer confirmed off-channel"},
    )
    assert resp.status_code == 200
    db_session.refresh(quote)
    assert quote.status == QuoteStatus.WON
    assert quote.result == "won"
    assert quote.result_at is not None


def test_draft_quote_mark_lost(client: TestClient, db_session: Session, test_quote: Quote):
    """Draft→lost is equally valid — the lost leg needs no server change."""
    quote = _make_draft(db_session, test_quote)
    resp = client.post(
        f"/v2/partials/quotes/{quote.id}/result",
        data={"result": "lost", "result_reason": "Went with another broker"},
    )
    assert resp.status_code == 200
    db_session.refresh(quote)
    assert quote.status == QuoteStatus.LOST
    assert quote.result == "lost"
