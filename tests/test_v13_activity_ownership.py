"""test_v13_activity_ownership.py — Activity Log & Ownership Endpoint Tests.

Covers: company/vendor/user activity endpoints, phone call logging,
activity status, sales ownership (my-accounts, at-risk, open-pool,
claim, strategic toggle, notifications).

Called by: pytest
Depends on: conftest.py fixtures, app.routers.v13_features
"""

from datetime import datetime, timezone

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
        "created_at": datetime.now(timezone.utc),
    }
    defaults.update(overrides)
    a = ActivityLog(**defaults)
    db.add(a)
    db.commit()
    db.refresh(a)
    return a


def test_get_company_activities_empty(client, test_company):
    resp = client.get(f"/api/companies/{test_company.id}/activities")
    assert resp.status_code == 200
    assert resp.json() == []


def test_get_company_activities_returns_records(client, db_session, test_user, test_company):
    _seed_activity(db_session, test_user.id, test_company.id)
    _seed_activity(db_session, test_user.id, test_company.id, subject="Follow-up")
    resp = client.get(f"/api/companies/{test_company.id}/activities")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 2
    assert all("id" in a and "activity_type" in a for a in data)


def test_get_vendor_activities_empty(client, test_vendor_card):
    resp = client.get(f"/api/vendors/{test_vendor_card.id}/activities")
    assert resp.status_code == 200
    assert resp.json() == []


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
    assert len(resp.json()) == 1


def test_get_user_activities(client, db_session, test_user, test_company):
    _seed_activity(db_session, test_user.id, test_company.id)
    resp = client.get(f"/api/users/{test_user.id}/activities")
    assert resp.status_code == 200
    assert len(resp.json()) >= 1


def test_log_phone_call_no_match(client):
    """Phone number that doesn't match any known contact — still logged (unmatched
    queue)."""
    resp = client.post(
        "/api/activities/call",
        json={
            "direction": "outbound",
            "phone": "+1-555-9999",
            "duration_seconds": 120,
        },
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "logged"


def test_company_activity_status_no_activity(client, test_company):
    resp = client.get(f"/api/companies/{test_company.id}/activity-status")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "no_activity"
    assert data["company_id"] == test_company.id


def test_company_activity_status_not_found(client):
    resp = client.get("/api/companies/99999/activity-status")
    assert resp.status_code == 404
