"""F5 regression: 'Send All' must NOT wipe the follow-up queue.

The batch button targets #main-content. It used to return a bare
'<div>N marked as responded</div>' success fragment, which replaced the entire
main-content region — leaving the user on a one-line message with no list, no
heading, and no next action. The handler now re-renders the (now shorter/empty)
follow_ups/list.html so the surrounding page survives, and surfaces the count via
an HX-Trigger showToast the base layout renders.

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

    # The queue shell is re-rendered — after marking all stale contacts responded the
    # queue is empty, so the empty-state (still part of the list partial) shows. The key
    # regression assertion: the whole list template came back, not a lone success line.
    assert "No follow-ups needed!" in resp.text
    assert "needing follow-up" in resp.text
    # The count message moved OUT of the swapped body and into the toast trigger.
    assert "marked as responded" not in resp.text


def test_send_batch_emits_showtoast_trigger(client: TestClient, db_session, test_user):
    """The success count is surfaced via an HX-Trigger showToast, not the swapped
    body."""
    _stale_contact(db_session, test_user, "GammaVendor")
    _stale_contact(db_session, test_user, "DeltaVendor")

    resp = client.post("/v2/partials/follow-ups/send-batch")
    assert resp.status_code == 200

    trigger = json.loads(resp.headers["HX-Trigger"])
    assert "showToast" in trigger
    assert "marked as responded" in trigger["showToast"]["message"]
    assert trigger["showToast"]["type"] == "success"


def test_send_batch_actually_marks_contacts(client: TestClient, db_session, test_user):
    """Sanity: the batch still marks stale contacts responded (queue empties afterward)."""
    _stale_contact(db_session, test_user, "EpsilonVendor")
    _stale_contact(db_session, test_user, "ZetaVendor")

    # Before: queue lists both vendors.
    before = client.get("/v2/partials/follow-ups")
    assert "EpsilonVendor" in before.text
    assert "ZetaVendor" in before.text

    client.post("/v2/partials/follow-ups/send-batch")

    # After: queue is empty (both marked responded).
    after = client.get("/v2/partials/follow-ups")
    assert "EpsilonVendor" not in after.text
    assert "ZetaVendor" not in after.text
    assert "No follow-ups needed!" in after.text
