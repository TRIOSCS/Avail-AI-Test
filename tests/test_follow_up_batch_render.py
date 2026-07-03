"""'Send All' must actually SEND follow-ups AND not wipe the surrounding page.

Two invariants this file guards:
1. The batch button targets #main-content, so the handler must re-render the whole
   follow_ups/list.html (page survives) — never a bare '<div>N ...</div>' success
   fragment that replaces the entire main-content region with a one-line message.
2. The batch ACTUALLY sends (via the shared _deliver_follow_up path), it does NOT
   silently mark everyone RESPONDED. Because the queue keys on the LAST outbound
   contact (status_updated_at, stamped on every send) being older than the follow-up
   window, a just-sent contact correctly drops OFF the queue — so the re-render is
   (usually) the empty-state, and re-clicking Send All won't re-spam the same vendors.
The count is surfaced via an HX-Trigger showToast the base layout renders.

Called by: pytest
Depends on: app.routers.htmx.offers, conftest fixtures
"""

import json
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app.models.offers import Contact as RfqContact
from app.models.sourcing import Requisition


def _stale_contact(db, user, name: str, days: int = 5) -> None:
    req = Requisition(name=f"FU-{name}", customer_name="FU Co", status="open")
    db.add(req)
    db.flush()
    db.add(
        RfqContact(
            requisition_id=req.id,
            user_id=user.id,
            contact_type="email",
            vendor_name=name,
            vendor_contact=f"{name.lower()}@example.com",
            status="sent",
            created_at=datetime.now(timezone.utc) - timedelta(days=days),
        )
    )
    db.commit()


def test_send_batch_rerenders_list_not_bare_div(client: TestClient, db_session, test_user):
    """Batch send returns the re-rendered queue (page survives), not a bare success
    div."""
    _stale_contact(db_session, test_user, "AlphaVendor")
    _stale_contact(db_session, test_user, "BetaVendor")

    resp = client.post("/v2/partials/follow-ups/send-batch")
    assert resp.status_code == 200

    # After sending, each contact's status_updated_at is stamped to now, so both leave the
    # follow-up window and the queue empties — the empty-state (still part of the list
    # partial) shows. The key regression assertion: the whole list template came back
    # (header + empty-state), not a lone success line.
    assert "No follow-ups needed!" in resp.text
    assert "needing follow-up" in resp.text  # the header count line always renders
    # The count message lives in the toast trigger, and the old "marked as responded" lie
    # must be gone entirely.
    assert "marked as responded" not in resp.text


def test_send_batch_emits_showtoast_trigger(client: TestClient, db_session, test_user):
    """The honest sent-count is surfaced via an HX-Trigger showToast, not the swapped
    body."""
    _stale_contact(db_session, test_user, "GammaVendor")
    _stale_contact(db_session, test_user, "DeltaVendor")

    resp = client.post("/v2/partials/follow-ups/send-batch")
    assert resp.status_code == 200

    trigger = json.loads(resp.headers["HX-Trigger"])
    assert "showToast" in trigger
    # Both emailable contacts are sent (TESTING mode marks SENT without a real Graph call);
    # the toast reports the honest count, never a blanket "marked as responded".
    assert "2 sent" in trigger["showToast"]["message"]
    assert "marked as responded" not in trigger["showToast"]["message"]
    assert trigger["showToast"]["type"] == "success"


def test_send_batch_actually_sends_and_clears_queue(client: TestClient, db_session, test_user):
    """The batch sends each stale contact, which drops them off the queue (last-contact
    recency), so the queue empties afterward — without ever marking them 'responded'."""
    _stale_contact(db_session, test_user, "EpsilonVendor")
    _stale_contact(db_session, test_user, "ZetaVendor")

    # Before: queue lists both vendors.
    before = client.get("/v2/partials/follow-ups")
    assert "EpsilonVendor" in before.text
    assert "ZetaVendor" in before.text

    client.post("/v2/partials/follow-ups/send-batch")

    # After: both were just contacted (status_updated_at=now) so they fall outside the
    # follow-up window — the queue is empty. They are SENT, not 'responded': status stays
    # 'sent' and status_updated_at is stamped.
    after = client.get("/v2/partials/follow-ups")
    assert "EpsilonVendor" not in after.text
    assert "ZetaVendor" not in after.text
    assert "No follow-ups needed!" in after.text
    for c in db_session.query(RfqContact).filter(RfqContact.vendor_name.in_(["EpsilonVendor", "ZetaVendor"])):
        assert c.status == "sent"  # sent, NOT "responded"
        assert c.status_updated_at is not None  # proof it was actually contacted
