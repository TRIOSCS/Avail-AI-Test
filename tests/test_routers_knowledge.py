"""Knowledge router auth tests.

Purpose: Verify /api/knowledge config endpoints enforce admin authorization.
Description: Exercises GET/PUT config flows with admin and non-admin users.
Business rules enforced:
- Non-admin users cannot update knowledge config.
- Admin users can update config values without server errors.
Called-by: pytest test runner
Depends-on: app/routers/knowledge.py, app/models/knowledge.py, tests/conftest.py
"""

from fastapi.testclient import TestClient


def test_update_knowledge_config_requires_admin(db_session, sales_user):
    """Non-admin users receive 403 on config update."""
    from app.database import get_db
    from app.dependencies import require_user
    from app.main import app

    def _override_db():
        yield db_session

    def _override_user():
        return sales_user

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[require_user] = _override_user
    with TestClient(app) as c:
        resp = c.put("/api/knowledge/config", json={"test_key": "value"})
    app.dependency_overrides.clear()
    assert resp.status_code == 403


def test_update_knowledge_config_admin_ok(db_session, admin_user):
    """Admin users can update and read config successfully."""
    from app.database import get_db
    from app.dependencies import require_user
    from app.main import app

    def _override_db():
        yield db_session

    def _override_user():
        return admin_user

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[require_user] = _override_user
    with TestClient(app) as c:
        put_resp = c.put("/api/knowledge/config", json={"unit_test_key": "123"})
        get_resp = c.get("/api/knowledge/config")
    app.dependency_overrides.clear()

    assert put_resp.status_code == 200
    assert put_resp.json()["ok"] is True
    assert get_resp.status_code == 200
    assert get_resp.json().get("unit_test_key") == "123"
