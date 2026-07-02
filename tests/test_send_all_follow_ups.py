"""Send-All follow-ups must ACTUALLY send (not silently mark everyone RESPONDED).

User decision (2026-07-02): the "Send All" batch button's label promises to send
follow-up emails, so it must — via the same shared _deliver_follow_up path as the single
send (DNC hard-block + Graph send + honest SENT-marking) — and report an HONEST summary of
what happened (N sent, M skipped, K failed), never a blanket "marked as responded".

In TESTING mode the shared helper marks a contact SENT without a real Graph call (contacts
with an address), or returns no_email (no address), so the batch's tally + honest summary
are exercised without mocking Graph.

Called by: pytest
Depends on: app.routers.htmx.offers send_batch_follow_up + _deliver_follow_up.
"""

import json
from datetime import datetime, timedelta, timezone

from app.constants import ContactStatus
from app.models.offers import Contact as RfqContact
from app.models.sourcing import Requisition


def _stale_contact(db, user, *, vendor_contact, days=5) -> RfqContact:
    req = Requisition(name="SA-REQ", customer_name="SA Co", status="open", created_by=user.id)
    db.add(req)
    db.flush()
    c = RfqContact(
        requisition_id=req.id,
        user_id=user.id,
        contact_type="email",
        vendor_name="V Co",
        vendor_contact=vendor_contact,
        subject="RFQ",
        status="sent",
        created_at=datetime.now(timezone.utc) - timedelta(days=days),
    )
    db.add(c)
    db.commit()
    db.refresh(c)
    return c


def _toast(resp) -> dict:
    return json.loads(resp.headers.get("HX-Trigger", "{}")).get("showToast", {})


def test_send_all_actually_sends_and_marks_sent(client, db_session, test_user):
    """Both stale emailable contacts are SENT (not RESPONDED); toast says '2 sent'."""
    c1 = _stale_contact(db_session, test_user, vendor_contact="a@vendor.com")
    c2 = _stale_contact(db_session, test_user, vendor_contact="b@vendor.com")

    resp = client.post("/v2/partials/follow-ups/send-batch")
    assert resp.status_code == 200
    assert "2 sent" in _toast(resp).get("message", "")

    db_session.refresh(c1)
    db_session.refresh(c2)
    # The old bug marked contacts RESPONDED without sending — must not happen.
    assert c1.status == ContactStatus.SENT
    assert c2.status == ContactStatus.SENT
    # status_updated_at is stamped only on an actual send (seed leaves it None) — proof
    # the batch really sent, not just left the pre-existing 'sent' seed status.
    assert c1.status_updated_at is not None
    assert c2.status_updated_at is not None


def test_send_all_reports_skipped_no_address(client, db_session, test_user):
    """A stale contact with no email address is skipped (not sent); summary is
    honest."""
    sent_c = _stale_contact(db_session, test_user, vendor_contact="c@vendor.com")
    noaddr = _stale_contact(db_session, test_user, vendor_contact=None)

    resp = client.post("/v2/partials/follow-ups/send-batch")
    assert resp.status_code == 200
    msg = _toast(resp).get("message", "")
    assert "1 sent" in msg
    assert "skipped" in msg

    db_session.refresh(sent_c)
    db_session.refresh(noaddr)
    # status_updated_at is the send signal (seed leaves it None): the emailable contact was
    # sent (stamped), the no-address one was skipped (never stamped).
    assert sent_c.status_updated_at is not None
    assert noaddr.status_updated_at is None


def test_send_all_empty_queue_is_honest(client, db_session, test_user):
    """No stale contacts → '0 sent', 200, no crash."""
    resp = client.post("/v2/partials/follow-ups/send-batch")
    assert resp.status_code == 200
    assert "0 sent" in _toast(resp).get("message", "")
