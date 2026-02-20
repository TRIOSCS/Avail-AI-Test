"""
test_integration_smoke.py — Smoke Tests for Test Infrastructure

Validates that conftest.py fixtures work: DB session, test client,
auth overrides, and model creation.

Called by: pytest
Depends on: conftest.py fixtures
"""

from app.models import User


def test_db_session_creates_tables(db_session):
    """DB session should have all tables available."""
    users = db_session.query(User).all()
    assert users == []  # Empty but queryable


def test_user_fixture(test_user):
    """test_user fixture creates a buyer with an ID."""
    assert test_user.id is not None
    assert test_user.role == "buyer"
    assert test_user.email == "testbuyer@trioscs.com"


def test_sales_user_fixture(sales_user):
    """sales_user fixture creates a sales-role user."""
    assert sales_user.role == "sales"


def test_requisition_fixture(test_requisition):
    """test_requisition has one requirement (LM317T)."""
    assert test_requisition.id is not None
    assert len(test_requisition.requirements) == 1
    assert test_requisition.requirements[0].primary_mpn == "LM317T"


def test_vendor_card_fixture(test_vendor_card):
    """test_vendor_card has normalized name and emails."""
    assert test_vendor_card.normalized_name == "arrow electronics"
    assert "sales@arrow.com" in test_vendor_card.emails


def test_company_fixture(test_company):
    """test_company is created and active."""
    assert test_company.id is not None
    assert test_company.is_active is True


def test_client_health(client):
    """Test client can hit the health endpoint."""
    resp = client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data.get("status") == "ok"


def test_client_auth_override(client):
    """Authenticated endpoints should work via override."""
    resp = client.get("/api/requisitions")
    assert resp.status_code == 200


def test_db_isolation(db_session):
    """Each test gets a fresh DB — no leftover data from other tests."""
    count = db_session.query(User).count()
    assert count == 0  # Nothing from test_user_fixture leaks here
