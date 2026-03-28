"""Tests for TT-105: Input validation on Create New User form.

Verifies server-side validation for email format, name length limits,
and empty field rejection on POST /api/admin/users.

Called by: pytest
Depends on: app/routers/admin/users.py, conftest fixtures
"""

import pytest
from fastapi.testclient import TestClient


@pytest.mark.skip(reason="Route POST /api/admin/users removed — admin users router no longer mounted")
class TestCreateUserValidation:
    """Server-side validation for POST /api/admin/users."""

    def _admin_client(self, db_session, admin_user):
        """Build a TestClient with admin auth overrides."""
        from app.database import get_db
        from app.dependencies import require_admin, require_user
        from app.main import app

        def _override_db():
            yield db_session

        app.dependency_overrides[get_db] = _override_db
        app.dependency_overrides[require_user] = lambda: admin_user
        app.dependency_overrides[require_admin] = lambda: admin_user
        return TestClient(app)

    def test_valid_user_creation(self, db_session, admin_user):
        """Happy path: valid name, email, role creates user."""
        c = self._admin_client(db_session, admin_user)
        resp = c.post("/api/admin/users", json={"name": "Jane Doe", "email": "jane@example.com", "role": "buyer"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "Jane Doe"
        assert data["email"] == "jane@example.com"
        assert data["role"] == "buyer"

    def test_invalid_email_no_at(self, db_session, admin_user):
        """Email without @ is rejected with 400."""
        c = self._admin_client(db_session, admin_user)
        resp = c.post("/api/admin/users", json={"name": "Jane", "email": "notanemail"})
        assert resp.status_code == 400
        assert "valid email" in resp.json()["error"].lower()

    def test_invalid_email_no_domain(self, db_session, admin_user):
        """Email with @ but no domain part is rejected."""
        c = self._admin_client(db_session, admin_user)
        resp = c.post("/api/admin/users", json={"name": "Jane", "email": "jane@"})
        assert resp.status_code == 400

    def test_invalid_email_spaces(self, db_session, admin_user):
        """Email with spaces is rejected."""
        c = self._admin_client(db_session, admin_user)
        resp = c.post("/api/admin/users", json={"name": "Jane", "email": "jane doe@example.com"})
        assert resp.status_code == 400

    def test_name_too_long(self, db_session, admin_user):
        """Name exceeding 100 chars is rejected with 400."""
        c = self._admin_client(db_session, admin_user)
        long_name = "A" * 101
        resp = c.post("/api/admin/users", json={"name": long_name, "email": "jane@example.com"})
        assert resp.status_code == 400
        assert "100 characters" in resp.json()["error"]

    def test_name_exactly_100_chars(self, db_session, admin_user):
        """Name at exactly 100 chars is accepted."""
        c = self._admin_client(db_session, admin_user)
        name = "A" * 100
        resp = c.post("/api/admin/users", json={"name": name, "email": "exact100@example.com"})
        assert resp.status_code == 200
        assert resp.json()["name"] == name

    def test_empty_name(self, db_session, admin_user):
        """Empty name (whitespace only) is rejected."""
        c = self._admin_client(db_session, admin_user)
        resp = c.post("/api/admin/users", json={"name": "   ", "email": "jane@example.com"})
        assert resp.status_code == 400
        assert "required" in resp.json()["error"].lower()

    def test_empty_email(self, db_session, admin_user):
        """Empty email is rejected."""
        c = self._admin_client(db_session, admin_user)
        resp = c.post("/api/admin/users", json={"name": "Jane", "email": "   "})
        assert resp.status_code == 400

    def test_duplicate_email_still_409(self, db_session, admin_user):
        """Duplicate email still returns 409 (not broken by new validation)."""
        c = self._admin_client(db_session, admin_user)
        c.post("/api/admin/users", json={"name": "First", "email": "dup@example.com"})
        resp = c.post("/api/admin/users", json={"name": "Second", "email": "dup@example.com"})
        assert resp.status_code == 409

    def test_email_normalized_lowercase(self, db_session, admin_user):
        """Email is normalized to lowercase."""
        c = self._admin_client(db_session, admin_user)
        resp = c.post("/api/admin/users", json={"name": "Jane", "email": "Jane@Example.COM"})
        assert resp.status_code == 200
        assert resp.json()["email"] == "jane@example.com"
