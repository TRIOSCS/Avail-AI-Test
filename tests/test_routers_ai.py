"""test_routers_ai.py — Tests for AI Intelligence Layer Router.

Tests _ai_enabled gate, RFQ email parsing, and part number normalization.

Covers: ai feature flag modes (off/mike_only/all),
        the surviving /api/ai/* HTTP endpoints
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# _ai_enabled tests
# ---------------------------------------------------------------------------


def _make_settings(flag: str, admin_emails: list[str] | None = None):
    s = SimpleNamespace(
        ai_features_enabled=flag,
        admin_emails=admin_emails or ["mike@trioscs.com"],
    )
    return s


@pytest.mark.parametrize(
    ("flag", "email", "expected"),
    [
        pytest.param("off", "mike@trioscs.com", False, id="off"),
        pytest.param("all", "buyer@trioscs.com", True, id="all"),
        pytest.param("mike_only", "mike@trioscs.com", True, id="mike_only_allows_mike"),
        pytest.param("mike_only", "buyer@trioscs.com", False, id="mike_only_blocks_non_allowlisted_user"),
        pytest.param("mike_only", "MIKE@TRIOSCS.COM", True, id="mike_only_case_insensitive"),
    ],
)
def test_ai_enabled(flag, email, expected):
    user = SimpleNamespace(email=email, id=1, name="User", role="admin")
    with patch("app.routers.ai.settings", _make_settings(flag)):
        from app.routers.ai import _ai_enabled

        assert _ai_enabled(user) is expected


# ---------------------------------------------------------------------------
# Fixtures for HTTP endpoint tests
# ---------------------------------------------------------------------------


@pytest.fixture()
def ai_test_user(db_session):
    """Buyer user for AI endpoint tests (distinct from conftest test_user)."""
    from app.models import User

    user = User(
        email="testbuyer@trioscs.com",
        name="Test Buyer",
        role="buyer",
        azure_id="test-001",
        m365_connected=True,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture()
def ai_client(db_session, ai_test_user):
    """TestClient with AI features enabled."""
    from app.database import get_db
    from app.dependencies import require_buyer, require_user
    from app.main import app

    def _override_db():
        yield db_session

    def _override_user():
        return ai_test_user

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[require_user] = _override_user
    app.dependency_overrides[require_buyer] = _override_user

    try:
        with TestClient(app) as c:
            yield c
    finally:
        for dep in [get_db, require_user, require_buyer]:
            app.dependency_overrides.pop(dep, None)


# ---------------------------------------------------------------------------
# Parse Email
# ---------------------------------------------------------------------------


def test_parse_email_ai_disabled(ai_client):
    """POST /api/ai/parse-email with AI off returns 403."""
    with patch("app.routers.ai._ai_enabled", return_value=False):
        resp = ai_client.post(
            "/api/ai/parse-email",
            json={
                "email_body": "We offer LM317T at $0.50",
            },
        )
    assert resp.status_code == 403


def test_parse_email_success(ai_client):
    """POST /api/ai/parse-email parses email into quotes."""
    parse_result = {
        "quotes": [{"part_number": "LM317T", "unit_price": 0.50}],
        "overall_confidence": 0.85,
        "email_type": "quote",
        "vendor_notes": "Standard terms",
    }

    with (
        patch("app.routers.ai._ai_enabled", return_value=True),
        patch("app.services.ai_email_parser.parse_email", new_callable=AsyncMock, return_value=parse_result),
        patch("app.services.ai_email_parser.should_auto_apply", return_value=True),
        patch("app.services.ai_email_parser.should_flag_review", return_value=False),
    ):
        resp = ai_client.post(
            "/api/ai/parse-email",
            json={
                "email_body": "We offer LM317T at $0.50",
                "email_subject": "RE: RFQ",
                "vendor_name": "Test Vendor",
            },
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["parsed"] is True
    assert data["auto_apply"] is True


def test_parse_email_no_result(ai_client):
    """POST /api/ai/parse-email returns parsed=False when parser returns None."""
    with (
        patch("app.routers.ai._ai_enabled", return_value=True),
        patch("app.services.ai_email_parser.parse_email", new_callable=AsyncMock, return_value=None),
    ):
        resp = ai_client.post(
            "/api/ai/parse-email",
            json={
                "email_body": "Out of office auto-reply",
            },
        )

    assert resp.status_code == 200
    assert resp.json()["parsed"] is False


# ---------------------------------------------------------------------------
# Normalize Parts
# ---------------------------------------------------------------------------


def test_normalize_parts_ai_disabled(ai_client):
    """POST /api/ai/normalize-parts with AI off returns 403."""
    with patch("app.routers.ai._ai_enabled", return_value=False):
        resp = ai_client.post(
            "/api/ai/normalize-parts",
            json={
                "parts": ["LM317T"],
            },
        )
    assert resp.status_code == 403


def test_normalize_parts_success(ai_client):
    """POST /api/ai/normalize-parts returns normalized parts."""
    normalized = [{"original": "LM317T", "normalized": "LM317T", "manufacturer": "TI"}]

    with (
        patch("app.routers.ai._ai_enabled", return_value=True),
        patch("app.services.ai_part_normalizer.normalize_parts", new_callable=AsyncMock, return_value=normalized),
    ):
        resp = ai_client.post(
            "/api/ai/normalize-parts",
            json={
                "parts": ["LM317T"],
            },
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] == 1
