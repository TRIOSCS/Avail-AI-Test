"""
test_v13_sales.py — v1.3 Sales & Ownership Endpoint Tests

Tests account ownership, open pool, claim flow, strategic toggle,
manager digest, and sales notifications.

Covers: GET my-accounts, at-risk, open-pool; POST claim; PUT strategic;
        GET manager-digest, notifications
Called by: pytest
Depends on: conftest (client, test_company, test_user, sales_user)
"""

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient


# ── Helper: client authenticated as sales user ──────────────────────

@pytest.fixture()
def sales_client(db_session, sales_user):
    """TestClient authenticated as the sales user."""
    from app.database import get_db
    from app.dependencies import require_buyer, require_user
    from app.main import app

    app.dependency_overrides[get_db] = lambda: (yield db_session).__next__() or None
    # Simpler override:
    def _override_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[require_user] = lambda: sales_user
    app.dependency_overrides[require_buyer] = lambda: sales_user

    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture()
def admin_client(db_session, admin_user):
    """TestClient authenticated as admin user."""
    from app.database import get_db
    from app.dependencies import require_buyer, require_user
    from app.main import app

    def _override_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[require_user] = lambda: admin_user
    app.dependency_overrides[require_buyer] = lambda: admin_user

    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


# ═══════════════════════════════════════════════════════════════════════
#  My Accounts / At-Risk / Open Pool
# ═══════════════════════════════════════════════════════════════════════

def test_my_accounts_empty(client):
    resp = client.get("/api/sales/my-accounts")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


def test_at_risk_accounts_empty(client):
    resp = client.get("/api/sales/at-risk")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


def test_open_pool_no_unowned(client, test_company, db_session, test_user):
    """Company with an owner doesn't appear in open pool."""
    test_company.account_owner_id = test_user.id
    db_session.commit()
    resp = client.get("/api/sales/open-pool")
    assert resp.status_code == 200
    ids = [c["company_id"] for c in resp.json()]
    assert test_company.id not in ids


def test_open_pool_shows_unowned(client, test_company):
    """Company with no owner appears in open pool."""
    resp = client.get("/api/sales/open-pool")
    assert resp.status_code == 200
    ids = [c["company_id"] for c in resp.json()]
    assert test_company.id in ids


# ═══════════════════════════════════════════════════════════════════════
#  Claim Account
# ═══════════════════════════════════════════════════════════════════════

def test_claim_account_success(sales_client, test_company):
    resp = sales_client.post(f"/api/sales/claim/{test_company.id}")
    assert resp.status_code == 200
    assert resp.json()["status"] == "claimed"
    assert resp.json()["company_name"] == test_company.name


def test_claim_account_buyer_forbidden(client, test_company):
    """Buyers (non-sales) cannot claim accounts."""
    resp = client.post(f"/api/sales/claim/{test_company.id}")
    assert resp.status_code == 403


def test_claim_account_already_owned(sales_client, db_session, test_company, sales_user):
    test_company.account_owner_id = sales_user.id
    db_session.commit()
    resp = sales_client.post(f"/api/sales/claim/{test_company.id}")
    assert resp.status_code == 409


def test_claim_account_not_found(sales_client):
    resp = sales_client.post("/api/sales/claim/99999")
    assert resp.status_code == 404


# ═══════════════════════════════════════════════════════════════════════
#  Strategic Toggle (admin only)
# ═══════════════════════════════════════════════════════════════════════

def test_toggle_strategic_non_admin_forbidden(client, test_company):
    resp = client.put(f"/api/companies/{test_company.id}/strategic",
                      json={"is_strategic": True})
    assert resp.status_code == 403


def test_toggle_strategic_set_true(admin_client, test_company, monkeypatch):
    monkeypatch.setattr("app.routers.v13_features.settings.admin_emails",
                        ["admin@trioscs.com"])
    resp = admin_client.put(f"/api/companies/{test_company.id}/strategic",
                            json={"is_strategic": True})
    assert resp.status_code == 200
    assert resp.json()["is_strategic"] is True


def test_toggle_strategic_flip(admin_client, test_company, monkeypatch):
    """Sending null flips the current value."""
    monkeypatch.setattr("app.routers.v13_features.settings.admin_emails",
                        ["admin@trioscs.com"])
    resp = admin_client.put(f"/api/companies/{test_company.id}/strategic",
                            json={})
    assert resp.status_code == 200
    assert resp.json()["is_strategic"] is True  # was False, flipped


# ═══════════════════════════════════════════════════════════════════════
#  Manager Digest & Notifications
# ═══════════════════════════════════════════════════════════════════════

def test_manager_digest_non_admin_forbidden(client):
    resp = client.get("/api/sales/manager-digest")
    assert resp.status_code == 403


def test_manager_digest_admin_success(admin_client, monkeypatch):
    monkeypatch.setattr("app.routers.v13_features.settings.admin_emails",
                        ["admin@trioscs.com"])
    resp = admin_client.get("/api/sales/manager-digest")
    assert resp.status_code == 200
    assert isinstance(resp.json(), dict)


def test_sales_notifications_empty(client):
    resp = client.get("/api/sales/notifications")
    assert resp.status_code == 200
    assert resp.json() == []
