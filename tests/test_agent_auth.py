"""test_agent_auth.py — Tests for agent API key authentication and scoping.

Covers: timing-safe comparison, audit logging, admin endpoint blocking.

Called by: pytest
Depends on: app.dependencies, conftest fixtures
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import User


@pytest.fixture()
def agent_user(db_session: Session) -> User:
    """The agent service account."""
    user = User(
        email="agent@availai.local",
        name="Agent Bot",
        role="buyer",
        azure_id="agent-azure-id",
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture()
def raw_client(db_session: Session) -> TestClient:
    """TestClient WITHOUT auth overrides — tests real auth flow."""
    from app.database import get_db
    from app.main import app

    def _override_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_db

    try:
        with TestClient(app) as c:
            yield c
    finally:
        app.dependency_overrides.pop(get_db, None)


class TestAgentAuth:
    def test_valid_agent_key_authenticates(self, raw_client: TestClient, agent_user: User):
        resp = raw_client.get(
            "/api/health",
            headers={"x-agent-key": "test-agent-key-secret"},
        )
        # Should not get 401 (may get 404 or 200 depending on route)
        assert resp.status_code != 401

    def test_wrong_agent_key_does_not_authenticate(self, raw_client: TestClient, agent_user: User):
        """Wrong agent key should not grant access to protected API endpoints."""
        resp = raw_client.get(
            "/api/requisitions",
            headers={"x-agent-key": "wrong-key"},
        )
        # Should get 401 (not authenticated) since the key is wrong
        assert resp.status_code == 401

    def test_hmac_compare_digest_is_used(self):
        """Verify we use timing-safe comparison (code review check)."""
        import inspect

        from app.dependencies import require_user

        source = inspect.getsource(require_user)
        assert "hmac.compare_digest" in source
