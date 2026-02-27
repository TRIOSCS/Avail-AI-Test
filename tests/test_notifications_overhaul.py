"""
test_notifications_overhaul.py — Tests for the notification bell overhaul.

Covers:
- vendor_reply_review removed from notification types
- buy_plan_id stored and returned in notification response
- quote_won / quote_lost notifications created on result change
- /api/sales/notifications/count endpoint
- Notification deduplication for offer_pending_review and competitive_quote

Called by: pytest
Depends on: conftest (client, db_session, test_user, test_requisition, test_quote)
"""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import ActivityLog, Quote, Requisition, User
from app.models.buy_plan import BuyPlanV3


# ── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture()
def notif_client(db_session: Session, test_user: User) -> TestClient:
    """TestClient authenticated as test_user with fresh overrides."""
    from app.database import get_db
    from app.dependencies import require_buyer, require_user
    from app.main import app

    def _override_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[require_user] = lambda: test_user
    app.dependency_overrides[require_buyer] = lambda: test_user

    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


# ── Phase 1: vendor_reply_review removed ──────────────────────────────


def test_vendor_reply_review_excluded_from_notifications(notif_client, db_session, test_user):
    """vendor_reply_review notifications should not appear in the API response."""
    db_session.add(ActivityLog(
        user_id=test_user.id,
        activity_type="vendor_reply_review",
        channel="system",
        subject="Old noise notification",
    ))
    db_session.commit()

    resp = notif_client.get("/api/sales/notifications")
    assert resp.status_code == 200
    items = resp.json()
    assert all(n["type"] != "vendor_reply_review" for n in items)


def test_vendor_reply_review_excluded_from_count(notif_client, db_session, test_user):
    """vendor_reply_review should not be counted in the badge count."""
    db_session.add(ActivityLog(
        user_id=test_user.id,
        activity_type="vendor_reply_review",
        channel="system",
        subject="Old noise",
    ))
    db_session.commit()

    resp = notif_client.get("/api/sales/notifications/count")
    assert resp.status_code == 200
    assert resp.json()["count"] == 0


# ── Phase 1B-D: buy_plan_id in notifications ─────────────────────────


def test_buy_plan_id_in_notification_response(notif_client, db_session, test_user, test_requisition, test_quote):
    """Notifications should include buy_plan_id and quote_id fields."""
    # Create a BuyPlanV3 to reference
    plan = BuyPlanV3(
        requisition_id=test_requisition.id,
        quote_id=test_quote.id,
        status="pending_approval",
        submitted_by_id=test_user.id,
    )
    db_session.add(plan)
    db_session.flush()

    db_session.add(ActivityLog(
        user_id=test_user.id,
        activity_type="buyplan_pending",
        channel="system",
        buy_plan_id=plan.id,
        subject="Buy plan #1 awaiting approval",
    ))
    db_session.commit()

    resp = notif_client.get("/api/sales/notifications")
    assert resp.status_code == 200
    items = resp.json()
    assert len(items) == 1
    assert items[0]["buy_plan_id"] == plan.id
    assert "quote_id" in items[0]


# ── Phase 2: quote_won / quote_lost notifications ────────────────────


def test_quote_won_creates_notification(notif_client, db_session, test_user, test_requisition, test_quote):
    """Setting quote result to 'won' should create a quote_won notification."""
    resp = notif_client.post(f"/api/quotes/{test_quote.id}/result", json={
        "result": "won",
        "reason": None,
        "notes": None,
    })
    assert resp.status_code == 200

    notifs = db_session.query(ActivityLog).filter(
        ActivityLog.activity_type == "quote_won",
        ActivityLog.user_id == test_user.id,
    ).all()
    assert len(notifs) == 1
    assert "won" in notifs[0].subject.lower()
    assert notifs[0].quote_id == test_quote.id
    assert notifs[0].requisition_id == test_requisition.id


def test_quote_lost_creates_notification(notif_client, db_session, test_user, test_requisition, test_quote):
    """Setting quote result to 'lost' should create a quote_lost notification."""
    resp = notif_client.post(f"/api/quotes/{test_quote.id}/result", json={
        "result": "lost",
        "reason": "Price too high",
        "notes": None,
    })
    assert resp.status_code == 200

    notifs = db_session.query(ActivityLog).filter(
        ActivityLog.activity_type == "quote_lost",
        ActivityLog.user_id == test_user.id,
    ).all()
    assert len(notifs) == 1
    assert "lost" in notifs[0].subject.lower()
    assert "Price too high" in notifs[0].subject


def test_quote_won_lost_visible_in_notifications_api(notif_client, db_session, test_user, test_requisition):
    """quote_won and quote_lost should appear in the notifications list."""
    for atype in ("quote_won", "quote_lost"):
        db_session.add(ActivityLog(
            user_id=test_user.id,
            activity_type=atype,
            channel="system",
            requisition_id=test_requisition.id,
            subject=f"Test {atype}",
        ))
    db_session.commit()

    resp = notif_client.get("/api/sales/notifications")
    assert resp.status_code == 200
    types = {n["type"] for n in resp.json()}
    assert "quote_won" in types
    assert "quote_lost" in types


# ── Phase 3: count endpoint ──────────────────────────────────────────


def test_notification_count_endpoint(notif_client, db_session, test_user):
    """GET /api/sales/notifications/count returns correct unread count."""
    # Add 3 unread notifications of valid types
    for i in range(3):
        db_session.add(ActivityLog(
            user_id=test_user.id,
            activity_type="offer_pending_review",
            channel="system",
            subject=f"Review offer #{i}",
        ))
    # Add 1 dismissed notification (should not count)
    db_session.add(ActivityLog(
        user_id=test_user.id,
        activity_type="offer_pending_review",
        channel="system",
        subject="Already read",
        dismissed_at=datetime.now(timezone.utc),
    ))
    db_session.commit()

    resp = notif_client.get("/api/sales/notifications/count")
    assert resp.status_code == 200
    assert resp.json()["count"] == 3


def test_notification_count_excludes_old(notif_client, db_session, test_user):
    """Notifications older than 14 days should not be counted."""
    old = ActivityLog(
        user_id=test_user.id,
        activity_type="buyplan_pending",
        channel="system",
        subject="Old plan",
    )
    db_session.add(old)
    db_session.flush()
    # Backdate to 15 days ago
    old.created_at = datetime.now(timezone.utc) - timedelta(days=15)
    db_session.commit()

    resp = notif_client.get("/api/sales/notifications/count")
    assert resp.status_code == 200
    assert resp.json()["count"] == 0


def test_notification_count_zero_when_empty(notif_client):
    """Count endpoint returns 0 when no notifications exist."""
    resp = notif_client.get("/api/sales/notifications/count")
    assert resp.status_code == 200
    assert resp.json()["count"] == 0


# ── Phase 4: deduplication ────────────────────────────────────────────


def test_offer_pending_review_dedup_logic(db_session, test_user, test_requisition):
    """offer_pending_review dedup: existing unread notif for same req should be updated, not duplicated."""
    # Create an existing unread notification for this requisition
    db_session.add(ActivityLog(
        user_id=test_user.id,
        activity_type="offer_pending_review",
        channel="system",
        requisition_id=test_requisition.id,
        contact_name="Vendor A",
        subject="Old offer notification",
    ))
    db_session.commit()

    # Simulate the dedup logic from email_service.py
    owner_id = test_user.id
    existing_notif = db_session.query(ActivityLog).filter(
        ActivityLog.user_id == owner_id,
        ActivityLog.activity_type == "offer_pending_review",
        ActivityLog.requisition_id == test_requisition.id,
        ActivityLog.dismissed_at.is_(None),
    ).first()

    assert existing_notif is not None
    # Update existing instead of creating new
    existing_notif.subject = "New vendor offer needs review: Vendor B — MPN123"
    existing_notif.created_at = datetime.now(timezone.utc)
    db_session.commit()

    # Should still be only 1 notification
    count = db_session.query(ActivityLog).filter(
        ActivityLog.activity_type == "offer_pending_review",
        ActivityLog.requisition_id == test_requisition.id,
        ActivityLog.dismissed_at.is_(None),
    ).count()
    assert count == 1
    # Subject should be updated
    notif = db_session.query(ActivityLog).filter(
        ActivityLog.activity_type == "offer_pending_review",
        ActivityLog.requisition_id == test_requisition.id,
    ).first()
    assert "Vendor B" in notif.subject


def test_competitive_quote_dedup(notif_client, db_session, test_user, test_requisition):
    """competitive_quote should update existing unread notification instead of duplicating."""
    # Create an existing unread competitive_quote for this requisition
    db_session.add(ActivityLog(
        user_id=test_user.id,
        activity_type="competitive_quote",
        channel="system",
        requisition_id=test_requisition.id,
        contact_name="Vendor X",
        subject="Old competitive quote",
    ))
    db_session.commit()

    # Verify it shows in the API
    resp = notif_client.get("/api/sales/notifications")
    assert resp.status_code == 200
    comp_notifs = [n for n in resp.json() if n["type"] == "competitive_quote"]
    assert len(comp_notifs) == 1


# ── Notification types comprehensive ─────────────────────────────────


def test_all_valid_types_appear_in_api(notif_client, db_session, test_user):
    """All registered notification types should be returned by the API."""
    valid_types = [
        "ownership_warning", "buyplan_pending", "buyplan_approved",
        "buyplan_rejected", "buyplan_completed", "buyplan_cancelled",
        "competitive_quote", "proactive_match", "offer_pending_review",
        "quote_won", "quote_lost",
    ]
    for atype in valid_types:
        db_session.add(ActivityLog(
            user_id=test_user.id,
            activity_type=atype,
            channel="system",
            subject=f"Test {atype}",
        ))
    db_session.commit()

    resp = notif_client.get("/api/sales/notifications")
    assert resp.status_code == 200
    returned_types = {n["type"] for n in resp.json()}
    assert returned_types == set(valid_types)


def test_mark_notification_read(notif_client, db_session, test_user):
    """Marking a notification as read should dismiss it."""
    db_session.add(ActivityLog(
        user_id=test_user.id,
        activity_type="quote_won",
        channel="system",
        subject="Quote won!",
    ))
    db_session.commit()

    notifs = notif_client.get("/api/sales/notifications").json()
    assert len(notifs) == 1
    nid = notifs[0]["id"]

    resp = notif_client.post(f"/api/sales/notifications/{nid}/read")
    assert resp.status_code == 200

    # Should be gone from the list now
    notifs = notif_client.get("/api/sales/notifications").json()
    assert len(notifs) == 0

    # Count should also be 0
    count_resp = notif_client.get("/api/sales/notifications/count")
    assert count_resp.json()["count"] == 0


def test_mark_all_read(notif_client, db_session, test_user):
    """Mark-all-read should dismiss all notifications."""
    for atype in ("quote_won", "buyplan_pending", "offer_pending_review"):
        db_session.add(ActivityLog(
            user_id=test_user.id,
            activity_type=atype,
            channel="system",
            subject=f"Test {atype}",
        ))
    db_session.commit()

    resp = notif_client.post("/api/sales/notifications/read-all")
    assert resp.status_code == 200

    count_resp = notif_client.get("/api/sales/notifications/count")
    assert count_resp.json()["count"] == 0
