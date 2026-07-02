"""Workflow-honesty P1s — the UI must not claim an action succeeded when it didn't, and
a self-refreshing action must not nest a second copy of its own surface.

Covers two audit findings (2026-07-02 production-polish review):
  * MAT-2 — the materials Enrich button returned the FULL detail partial into
    hx-target="this"/outerHTML, nesting a duplicate detail tree (and duplicate
    element IDs) inside the button. It must target #main-content like its sibling
    actions (conflict-accept, edit).
  * F2 — send_follow_up_htmx returned the green "Follow-up sent" card even when the
    Graph send raised (swallowed) or the contact had no email address, so email_sent
    stayed False. It must return an honest failure card in those cases.

Called by: pytest
Depends on: app.routers.htmx.offers, app.routers.htmx.materials, conftest fixtures
            (client = buyer test_user → unrestricted requisition access).
"""

from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.intelligence import MaterialCard
from app.models.offers import Contact as RfqContact
from app.models.sourcing import Requisition

# ── MAT-2: Enrich button targets #main-content, never self-nests ─────────────


def test_material_enrich_button_targets_main_content(client: TestClient, db_session: Session):
    """The Enrich button must swap into #main-content (its endpoint returns the whole
    detail partial), not hx-target='this'/outerHTML which nested a duplicate detail."""
    card = MaterialCard(normalized_mpn="lm317t", display_mpn="LM317T", manufacturer="TI", search_count=0)
    db_session.add(card)
    db_session.commit()
    db_session.refresh(card)

    html = client.get(f"/v2/partials/materials/{card.id}").text

    # Isolate the Enrich button's own attributes (from its hx-post to the class attr).
    marker = f'hx-post="/v2/partials/materials/{card.id}/enrich"'
    assert marker in html, "Enrich button missing from detail render"
    start = html.index(marker)
    button = html[start : start + 300]
    assert 'hx-target="#main-content"' in button, "Enrich must target #main-content"
    assert 'hx-target="this"' not in button, "Enrich must NOT self-target (nests a duplicate detail)"
    assert 'hx-swap="outerHTML"' not in button, "Enrich must not outerHTML-swap onto itself"


# ── F2: follow-up send tells the truth ───────────────────────────────────────


def _seed_contact(db: Session, user, *, vendor_contact: str | None) -> RfqContact:
    req = Requisition(name="FU-REQ", customer_name="FU Co", status="open")
    db.add(req)
    db.flush()
    contact = RfqContact(
        requisition_id=req.id,
        user_id=user.id,
        contact_type="email",
        vendor_name="V Co",
        vendor_contact=vendor_contact,
        status="sent",
    )
    db.add(contact)
    db.commit()
    db.refresh(contact)
    return contact


def test_follow_up_no_email_returns_failure_card(client, db_session, test_user, monkeypatch):
    """A contact with no email address on file: the Graph block is skipped, email_sent
    stays False — the endpoint must return the honest 'no email' card, not 'sent'."""
    # is_testing gates the success path open; turn it OFF so the real send/skip logic runs.
    monkeypatch.setenv("TESTING", "0")
    contact = _seed_contact(db_session, test_user, vendor_contact=None)

    resp = client.post(f"/v2/partials/follow-ups/{contact.id}/send")
    assert resp.status_code == 200
    assert "No email address on file" in resp.text
    assert "Follow-up sent" not in resp.text
    # Status must NOT have advanced to a sent-state on a non-send.
    db_session.refresh(contact)
    assert contact.status == "sent"  # unchanged from seed; never flipped to SENT by a real send


def test_follow_up_send_failure_returns_failure_card(client, db_session, test_user, monkeypatch):
    """When the Graph send raises (swallowed), email_sent is False — return the honest
    'couldn't send' card rather than the green success card."""
    monkeypatch.setenv("TESTING", "0")
    contact = _seed_contact(db_session, test_user, vendor_contact="v@example.com")

    class _BoomGraph:
        def __init__(self, *a, **k):
            pass

        async def post_json(self, *a, **k):
            raise RuntimeError("graph 500")

    with (
        patch("app.dependencies.require_fresh_token", new=AsyncMock(return_value="tok")),
        patch("app.utils.graph_client.GraphClient", _BoomGraph),
    ):
        resp = client.post(f"/v2/partials/follow-ups/{contact.id}/send")

    assert resp.status_code == 200
    assert "Couldn't send" in resp.text
    assert "Follow-up sent" not in resp.text


def test_follow_up_testing_mode_still_reports_sent(client, db_session, test_user):
    """Regression guard: in TESTING mode (is_testing True) the endpoint still returns the
    success card — the honesty gate must not swallow the normal test/success path."""
    contact = _seed_contact(db_session, test_user, vendor_contact="v@example.com")

    resp = client.post(f"/v2/partials/follow-ups/{contact.id}/send")
    assert resp.status_code == 200
    assert "Follow-up sent" in resp.text
