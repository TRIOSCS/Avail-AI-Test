"""
test_v13_activity_ownership.py — Activity Log & Ownership Endpoint Tests

Covers: company/vendor/user activity endpoints, phone call logging,
activity status, sales ownership (my-accounts, at-risk, open-pool,
claim, strategic toggle, notifications).

Called by: pytest
Depends on: conftest.py fixtures, app.routers.v13_features
"""

from datetime import datetime, timedelta, timezone

from app.models import ActivityLog, Company, User


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
        db_session, test_user.id, company_id=None,
        vendor_card_id=test_vendor_card.id, subject="Vendor RFQ",
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
    """Phone number that doesn't match any known contact — still logged (unmatched queue)."""
    resp = client.post("/api/activities/call", json={
        "direction": "outbound",
        "phone": "+1-555-9999",
        "duration_seconds": 120,
    })
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


# ═══════════════════════════════════════════════════════════════════════
#  SALES OWNERSHIP ENDPOINTS
# ═══════════════════════════════════════════════════════════════════════


def test_my_accounts_empty(client):
    resp = client.get("/api/sales/my-accounts")
    assert resp.status_code == 200
    assert resp.json() == []


def test_open_pool_includes_unowned(client, db_session, test_company):
    """Company with no owner should appear in open pool."""
    test_company.account_owner_id = None
    db_session.commit()
    resp = client.get("/api/sales/open-pool")
    assert resp.status_code == 200
    names = [c["company_name"] for c in resp.json()]
    assert test_company.name in names


def test_at_risk_accounts(client):
    resp = client.get("/api/sales/at-risk")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


def test_claim_account_requires_sales_role(client, test_company):
    """Buyer role can't claim — only sales."""
    test_company.account_owner_id = None
    resp = client.post(f"/api/sales/claim/{test_company.id}")
    assert resp.status_code == 403


def test_claim_account_success(client, db_session, test_company):
    """Sales user can claim an unowned account."""
    from app.dependencies import require_user
    from app.main import app

    sales = User(
        email="sales@trioscs.com", name="Sales Guy",
        role="sales", azure_id="az-sales-01",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(sales)
    db_session.commit()
    db_session.refresh(sales)

    test_company.account_owner_id = None
    db_session.commit()

    app.dependency_overrides[require_user] = lambda: sales
    resp = client.post(f"/api/sales/claim/{test_company.id}")
    app.dependency_overrides[require_user] = lambda: db_session.query(User).filter_by(
        email="testbuyer@trioscs.com").first()

    assert resp.status_code == 200
    assert resp.json()["status"] == "claimed"


def test_claim_already_owned(client, db_session, test_company, test_user):
    """Can't claim a company that already has an owner."""
    test_company.account_owner_id = test_user.id
    db_session.commit()

    from app.dependencies import require_user
    from app.main import app
    sales = User(
        email="sales2@trioscs.com", name="Sales Two",
        role="sales", azure_id="az-sales-02",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(sales)
    db_session.commit()
    db_session.refresh(sales)

    app.dependency_overrides[require_user] = lambda: sales
    resp = client.post(f"/api/sales/claim/{test_company.id}")
    app.dependency_overrides[require_user] = lambda: db_session.query(User).filter_by(
        email="testbuyer@trioscs.com").first()

    assert resp.status_code == 409


def test_toggle_strategic_requires_admin(client, test_company):
    """Non-admin user gets 403."""
    resp = client.put(f"/api/companies/{test_company.id}/strategic", json={"is_strategic": True})
    assert resp.status_code == 403


def test_toggle_strategic_as_admin(client, db_session, test_user, test_company, monkeypatch):
    """Admin can toggle strategic flag."""
    test_user.role = "admin"
    db_session.commit()
    resp = client.put(f"/api/companies/{test_company.id}/strategic", json={"is_strategic": True})
    assert resp.status_code == 200
    assert resp.json()["is_strategic"] is True


def test_notifications_empty(client):
    resp = client.get("/api/sales/notifications")
    assert resp.status_code == 200
    assert resp.json() == []
