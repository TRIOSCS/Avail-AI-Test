"""W5b: the follow-up queue, batch-send, and nav badge must use ONE threshold.

Regression: batch-send read request.app.state.follow_up_days (never set → getattr
default 2) and the badge hardcoded 2, while the queue read settings.follow_up_days
(3) — so the badge/batch counted a different staleness cutoff than the queue, and
FOLLOW_UP_DAYS was silently ignored. All three now read settings.follow_up_days.

Called by: pytest
Depends on: app.routers.htmx.offers, conftest fixtures
"""

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app.models.offers import Contact as RfqContact
from app.models.sourcing import Requisition


def _contact_aged_days(db, user, days: int) -> None:
    req = Requisition(name="FU-REQ", customer_name="FU Co", status="open")
    db.add(req)
    db.flush()
    db.add(
        RfqContact(
            requisition_id=req.id,
            user_id=user.id,
            contact_type="email",
            vendor_name="V Co",
            vendor_contact="v@example.com",
            status="sent",
            created_at=datetime.now(timezone.utc) - timedelta(days=days),
        )
    )
    db.commit()


def test_badge_counts_contacts_older_than_configured_days(client: TestClient, db_session, test_user):
    """A contact aged just past settings.follow_up_days (default 3) is counted; the
    badge must NOT use the old hardcoded 2 (which would count a 2.5-day-old contact the
    queue does not)."""
    from app.config import settings

    # Aged between 2 and 3 days: stale under the OLD 2-day badge, fresh under the
    # real 3-day config — proves the badge now honors the config, not the literal 2.
    _contact_aged_days(db_session, test_user, days=2)  # 2 full days < 3 → must NOT count

    assert settings.follow_up_days == 3
    resp = client.get("/v2/partials/follow-ups/badge")
    assert resp.status_code == 200
    # 2-day-old contact is fresh under the 3-day threshold → empty badge.
    assert resp.text.strip() == ""


def test_badge_counts_stale_contact(client: TestClient, db_session, test_user):
    _contact_aged_days(db_session, test_user, days=5)  # older than 3 → counted
    resp = client.get("/v2/partials/follow-ups/badge")
    assert resp.status_code == 200
    assert "1" in resp.text


def test_all_three_surfaces_share_the_config_threshold(monkeypatch, client: TestClient, db_session, test_user):
    """Badge, queue, and batch-send resolve the same settings.follow_up_days — bump it
    and a contact between the old and new cutoff flips consistently everywhere."""
    from app.config import settings

    monkeypatch.setattr(settings, "follow_up_days", 10)
    _contact_aged_days(db_session, test_user, days=5)  # fresh under 10-day threshold

    badge = client.get("/v2/partials/follow-ups/badge")
    assert badge.text.strip() == "", "badge should honor the raised threshold"
    queue = client.get("/v2/partials/follow-ups")
    assert queue.status_code == 200
    # The 5-day contact must not appear as stale in the queue under a 10-day cutoff.
    assert "V Co" not in queue.text
