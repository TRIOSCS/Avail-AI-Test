"""test_v13_activity_ownership.py — Activity Log & Ownership Endpoint Tests.

Covers: the surviving vendor activity timeline endpoint
(GET /api/vendors/{id}/activities).

(The company/user timeline, POST /api/activities/call, and company
activity-status routes were removed in the Wave-2 orphan-route cleanup.)

Called by: pytest
Depends on: conftest.py fixtures, app.routers.v13_features
"""

from datetime import UTC, datetime

from app.models import ActivityLog

# ═══════════════════════════════════════════════════════════════════════
#  ACTIVITY LOG ENDPOINTS
# ═══════════════════════════════════════════════════════════════════════


def _seed_activity(db, user_id: int, company_id: int, **overrides) -> ActivityLog:
    """Helper: create an ActivityLog row with sensible defaults."""
    defaults = {
        "user_id": user_id,
        "activity_type": "email_sent",
        "channel": "email",
        "company_id": company_id,
        "contact_email": "vendor@example.com",
        "subject": "RFQ for LM317T",
        "created_at": datetime.now(UTC),
    }
    defaults.update(overrides)
    a = ActivityLog(**defaults)
    db.add(a)
    db.commit()
    db.refresh(a)
    return a


def test_get_vendor_activities_empty(client, test_vendor_card):
    resp = client.get(f"/api/vendors/{test_vendor_card.id}/activities")
    assert resp.status_code == 200
    data = resp.json()
    assert data["items"] == []
    assert data["total"] == 0


def test_get_vendor_activities_returns_records(client, db_session, test_user, test_vendor_card):
    _seed_activity(
        db_session,
        test_user.id,
        company_id=None,
        vendor_card_id=test_vendor_card.id,
        subject="Vendor RFQ",
    )
    resp = client.get(f"/api/vendors/{test_vendor_card.id}/activities")
    assert resp.status_code == 200
    assert resp.json()["total"] == 1
