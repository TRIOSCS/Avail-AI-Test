"""Tests for Settings → API Keys cards: Explorium, Apollo, Hunter."""

import os

os.environ["TESTING"] = "1"

import pytest
from fastapi.testclient import TestClient

from app.models import ApiSource

# ── Helpers ──────────────────────────────────────────────────────────


def _make_admin_client(db_session, admin_user):
    """Return a TestClient authenticated as admin, overriding auth deps."""
    from app.database import get_db
    from app.dependencies import require_admin, require_settings_access, require_user
    from app.main import app

    def _db():
        yield db_session

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[require_user] = lambda: admin_user
    app.dependency_overrides[require_admin] = lambda: admin_user
    app.dependency_overrides[require_settings_access] = lambda: admin_user

    try:
        client = TestClient(app)
        yield client
    finally:
        for dep in [get_db, require_user, require_admin, require_settings_access]:
            app.dependency_overrides.pop(dep, None)


def _seed_sources(db_session) -> list[ApiSource]:
    """Seed the three new enrichment sources needed by the tests."""
    sources = [
        ApiSource(
            name="explorium_enrichment",
            display_name="Explorium",
            category="enrichment",
            source_type="aggregator",
            status="pending",
            env_vars=["EXPLORIUM_API_KEY"],
            credentials={},
        ),
        ApiSource(
            name="apollo_enrichment",
            display_name="Apollo",
            category="enrichment",
            source_type="aggregator",
            status="pending",
            env_vars=["APOLLO_API_KEY"],
            credentials={},
        ),
        ApiSource(
            name="hunter_enrichment",
            display_name="Hunter",
            category="enrichment",
            source_type="aggregator",
            status="pending",
            env_vars=["HUNTER_API_KEY"],
            credentials={},
        ),
        # Lusha + Clay are referenced by the template context — seed them too
        ApiSource(
            name="lusha_enrichment",
            display_name="Lusha",
            category="enrichment",
            source_type="aggregator",
            status="pending",
            env_vars=["LUSHA_API_KEY"],
            credentials={},
        ),
        ApiSource(
            name="clay_enrichment",
            display_name="Clay",
            category="enrichment",
            source_type="aggregator",
            status="pending",
            env_vars=["CLAY_API_KEY"],
            credentials={},
        ),
        ApiSource(
            name="eight_by_eight",
            display_name="8x8 VoIP",
            category="voip",
            source_type="aggregator",
            status="pending",
            env_vars=[
                "EIGHT_BY_EIGHT_API_KEY",
                "EIGHT_BY_EIGHT_PBX_ID",
                "EIGHT_BY_EIGHT_USERNAME",
                "EIGHT_BY_EIGHT_PASSWORD",
                "EIGHT_BY_EIGHT_TIMEZONE",
            ],
            credentials={},
        ),
    ]
    for src in sources:
        db_session.add(src)
    db_session.commit()
    return sources


@pytest.fixture(autouse=True)
def _stub_clay_oauth(monkeypatch):
    """Stub clay_oauth helpers so they don't open a raw SessionLocal connection (which
    bypasses the test-DB override and hits a missing table)."""
    import app.routers.htmx_views as v

    monkeypatch.setattr(v.clay_oauth, "is_connected", lambda: False)
    monkeypatch.setattr(v.clay_oauth, "needs_reconnect", lambda: False)


@pytest.fixture()
def admin_client(db_session, admin_user):
    _seed_sources(db_session)
    yield from _make_admin_client(db_session, admin_user)


# ── Tests ─────────────────────────────────────────────────────────────


def test_api_keys_tab_renders_new_cards(admin_client):
    """GET /v2/partials/settings/api-keys returns 200 and includes all three new key
    names."""
    r = admin_client.get("/v2/partials/settings/api-keys")
    assert r.status_code == 200
    for name in ("EXPLORIUM_API_KEY", "APOLLO_API_KEY", "HUNTER_API_KEY"):
        assert name in r.text, f"Expected {name!r} in rendered HTML"


def test_put_explorium_credentials(admin_client):
    """PUT /api/sources/explorium_enrichment/credentials with a key returns 200."""
    r = admin_client.put(
        "/api/sources/explorium_enrichment/credentials",
        json={"credentials": {"EXPLORIUM_API_KEY": "testval"}},
    )
    assert r.status_code == 200


def test_put_apollo_credentials(admin_client):
    """PUT /api/sources/apollo_enrichment/credentials with a key returns 200."""
    r = admin_client.put(
        "/api/sources/apollo_enrichment/credentials",
        json={"credentials": {"APOLLO_API_KEY": "testval"}},
    )
    assert r.status_code == 200


def test_put_hunter_credentials(admin_client):
    """PUT /api/sources/hunter_enrichment/credentials with a key returns 200."""
    r = admin_client.put(
        "/api/sources/hunter_enrichment/credentials",
        json={"credentials": {"HUNTER_API_KEY": "testval"}},
    )
    assert r.status_code == 200


def test_clay_card_shows_connect_when_disconnected(admin_client, monkeypatch):
    import app.routers.htmx_views as v

    monkeypatch.setattr(v.clay_oauth, "is_connected", lambda: False)
    monkeypatch.setattr(v.clay_oauth, "needs_reconnect", lambda: False)
    html = admin_client.get("/v2/partials/settings/api-keys").text
    assert "/auth/clay/connect" in html and "Connect Clay" in html
    assert "CLAY_API_KEY" not in html  # the old key input is gone


def test_clay_card_shows_connected(admin_client, monkeypatch):
    import app.routers.htmx_views as v

    monkeypatch.setattr(v.clay_oauth, "is_connected", lambda: True)
    monkeypatch.setattr(v.clay_oauth, "needs_reconnect", lambda: False)
    html = admin_client.get("/v2/partials/settings/api-keys").text
    assert "Connected" in html and "/auth/clay/disconnect" in html
