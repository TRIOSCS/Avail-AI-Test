"""Tests for Settings → Connectors tab (Task 3).

Covers:
- GET /v2/partials/settings/connectors — renders all 6 group labels
- A key-based card exposes its env-var field (LUSHA_API_KEY)
- Clay shows Connect link when disconnected
- Single-card route: 200 or 404 depending on existence
- /sources + /api-keys → 302 /connectors
- Admin gate (unauthenticated → 401/403/302)
"""

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
    """Seed enrichment + part-sourcing sources needed by the tests."""
    sources = [
        # Part Sourcing
        ApiSource(
            name="nexar",
            display_name="Nexar",
            category="api",
            source_type="aggregator",
            status="pending",
            env_vars=["NEXAR_CLIENT_ID", "NEXAR_CLIENT_SECRET"],
            credentials={},
        ),
        ApiSource(
            name="brokerbin",
            display_name="BrokerBin",
            category="api",
            source_type="aggregator",
            status="pending",
            env_vars=["BROKERBIN_API_KEY"],
            credentials={},
        ),
        # Enrichment
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
            env_vars=[],
            credentials={},
        ),
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
        ApiSource(
            name="sam_gov_enrichment",
            display_name="SAM.gov",
            category="enrichment",
            source_type="aggregator",
            status="pending",
            env_vars=[],
            credentials={},
        ),
        # AI
        ApiSource(
            name="anthropic",
            display_name="Anthropic / Claude",
            category="api",
            source_type="ai",
            status="pending",
            env_vars=[],
            credentials={},
        ),
        ApiSource(
            name="ai_live_web",
            display_name="AI Web Search",
            category="api",
            source_type="ai",
            status="pending",
            env_vars=[],
            credentials={},
        ),
        # Communications
        ApiSource(
            name="azure_oauth",
            display_name="Azure / M365 OAuth",
            category="auth",
            source_type="oauth",
            status="pending",
            env_vars=[],
            credentials={},
        ),
        ApiSource(
            name="email_mining",
            display_name="Email Mining",
            category="email",
            source_type="aggregator",
            status="pending",
            env_vars=[],
            credentials={},
        ),
        ApiSource(
            name="teams",
            display_name="Teams",
            category="notifications",
            source_type="aggregator",
            status="pending",
            env_vars=[],
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
        # Browser Workers
        ApiSource(
            name="netcomponents",
            display_name="NetComponents",
            category="api",
            source_type="aggregator",
            status="pending",
            env_vars=["NC_USERNAME", "NC_PASSWORD"],
            credentials={},
        ),
        ApiSource(
            name="icsource",
            display_name="IC Source",
            category="api",
            source_type="aggregator",
            status="pending",
            env_vars=["ICS_USERNAME", "ICS_PASSWORD"],
            credentials={},
        ),
        # Manual
        ApiSource(
            name="stock_list",
            display_name="Stock-List Import",
            category="manual",
            source_type="manual",
            status="pending",
            env_vars=[],
            credentials={},
        ),
    ]
    for src in sources:
        db_session.add(src)
    db_session.commit()
    return sources


# ── Autouse stubs ─────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _stub_clay_oauth(monkeypatch):
    """Stub clay_oauth helpers so they don't open a raw SessionLocal connection."""
    import app.routers.htmx_views as v

    monkeypatch.setattr(v.clay_oauth, "is_connected", lambda: False)
    monkeypatch.setattr(v.clay_oauth, "needs_reconnect", lambda: False)


@pytest.fixture()
def admin_client(db_session, admin_user):
    _seed_sources(db_session)
    yield from _make_admin_client(db_session, admin_user)


# ── Tests ─────────────────────────────────────────────────────────────


def test_connectors_tab_renders_groups(admin_client):
    """GET /v2/partials/settings/connectors returns 200 with all 6 group labels."""
    html = admin_client.get("/v2/partials/settings/connectors").text
    for label in ("Part Sourcing", "Enrichment", "AI", "Communications", "Browser Workers", "Manual"):
        assert label in html, f"Expected group label {label!r} in rendered HTML"
    # dead providers must be absent
    assert "rocketreach" not in html.lower()
    assert "clearbit" not in html.lower()


def test_connectors_tab_key_card_has_field(admin_client):
    """A key-based card exposes its env-var field (LUSHA_API_KEY)."""
    html = admin_client.get("/v2/partials/settings/connectors").text
    assert "LUSHA_API_KEY" in html, "Expected LUSHA_API_KEY env-var field in rendered HTML"


def test_connectors_tab_clay_connect(admin_client, monkeypatch):
    """When Clay is disconnected the page shows the /auth/clay/connect link."""
    import app.routers.htmx_views as v

    monkeypatch.setattr(v.clay_oauth, "is_connected", lambda: False)
    monkeypatch.setattr(v.clay_oauth, "needs_reconnect", lambda: False)
    html = admin_client.get("/v2/partials/settings/connectors").text
    assert "/auth/clay/connect" in html


def test_single_card_route(admin_client, db_session):
    """Single-card route returns 200 for an existing source id or 404 for invalid."""
    # Look up the lusha source we seeded
    from app.models import ApiSource as AS

    src = db_session.query(AS).filter_by(name="lusha_enrichment").first()
    assert src is not None, "Lusha source must be seeded"

    r = admin_client.get(f"/v2/partials/settings/connector-card/{src.id}", follow_redirects=False)
    assert r.status_code == 200, f"Expected 200 for existing card id={src.id}, got {r.status_code}"

    r404 = admin_client.get("/v2/partials/settings/connector-card/999999", follow_redirects=False)
    assert r404.status_code == 404, f"Expected 404 for missing card id, got {r404.status_code}"


def test_old_routes_redirect(admin_client):
    """GET /…/sources and /…/api-keys must redirect (302/307) to /connectors."""
    for path in ("/v2/partials/settings/sources", "/v2/partials/settings/api-keys"):
        r = admin_client.get(path, follow_redirects=False)
        assert r.status_code in (302, 307), f"Expected redirect for {path}, got {r.status_code}"
        assert "/connectors" in r.headers["location"], (
            f"Expected /connectors in Location for {path}, got {r.headers.get('location')}"
        )


def test_connectors_admin_gated(unauthenticated_client):
    """Connectors tab is admin-only; unauthenticated gets 401/403/302/307."""
    r = unauthenticated_client.get("/v2/partials/settings/connectors", follow_redirects=False)
    assert r.status_code in (401, 403, 302, 307), f"Expected auth-gated status for connectors, got {r.status_code}"


# ── Task 4: microcopy, a11y, button-sizing, targets, encoding ──────────


def test_card_microcopy_and_a11y(admin_client):
    """Reconciled state labels, toggle a11y label, .btn-md sizing, explicit targets,
    Test all."""
    html = admin_client.get("/v2/partials/settings/connectors").text
    # Spec §14.4 reconciled state labels (at least one present given the seed)
    assert "Needs setup" in html or "Live" in html
    # microcopy verbatim from spec §14.4
    assert "Test all" in html
    # toggle a11y label
    assert 'aria-label="Enable' in html
    # buttons sized via .btn-md, never inline px-4 py-2 bg-brand
    assert "btn-md" in html
    assert 'class="px-4 py-2 bg-brand' not in html
    # explicit hx-target on hx controls
    assert "hx-target=" in html


def test_test_all_button_wiring(admin_client):
    """Test-all posts to the bounded endpoint and targets the connectors root."""
    html = admin_client.get("/v2/partials/settings/connectors").text
    assert 'hx-post="/v2/partials/settings/connectors/test-all"' in html
    assert "#connectors-root" in html


def test_clay_card_connect_disconnect(admin_client, monkeypatch):
    """Disconnected → Connect Clay link; connected → Disconnect control."""
    import app.routers.htmx_views as v

    monkeypatch.setattr(v.clay_oauth, "is_connected", lambda: False)
    monkeypatch.setattr(v.clay_oauth, "needs_reconnect", lambda: False)
    html = admin_client.get("/v2/partials/settings/connectors").text
    assert "Connect Clay" in html
    assert "/auth/clay/connect" in html

    monkeypatch.setattr(v.clay_oauth, "is_connected", lambda: True)
    html2 = admin_client.get("/v2/partials/settings/connectors").text
    assert "/auth/clay/disconnect" in html2
    assert "Disconnect" in html2


def test_clay_card_no_key_text_input(admin_client, monkeypatch):
    """Clay uses the oauth_clay control — there must be no CLAY_API_KEY text input."""
    import app.routers.htmx_views as v

    monkeypatch.setattr(v.clay_oauth, "is_connected", lambda: False)
    html = admin_client.get("/v2/partials/settings/connectors").text
    assert "CLAY_API_KEY" not in html


def test_dead_providers_absent(admin_client):
    """RocketReach + Clearbit must not appear anywhere on the page."""
    html = admin_client.get("/v2/partials/settings/connectors").text.lower()
    assert "rocketreach" not in html
    assert "clearbit" not in html


def test_test_all_endpoint_returns_oob_bundle(admin_client, db_session):
    """POST test-all returns 200 with an OOB card bundle for credentialed+active
    sources, tolerating per-source connector failures (no real connectors in tests)."""
    from app.models import ApiSource as AS

    # A keyless source is "testable"; make it active so the sweep includes it.
    sam = db_session.query(AS).filter_by(name="sam_gov_enrichment").first()
    assert sam is not None
    sam.is_active = True
    db_session.commit()

    r = admin_client.post("/v2/partials/settings/connectors/test-all")
    assert r.status_code == 200, f"expected 200, got {r.status_code}: {r.text[:200]}"
    assert 'hx-swap-oob="true"' in r.text
    assert "connector-card-" in r.text


def test_credential_save_round_trip(admin_client, db_session):
    """PUT /api/sources/{name}/credentials stores the credential (matching the encoding
    the Save button sends — JSON body {credentials: {ENV: val}})."""
    from app.services.credential_service import _cred_cache, credential_is_set, get_credential

    _cred_cache.clear()
    r = admin_client.put(
        "/api/sources/lusha_enrichment/credentials",
        json={"credentials": {"LUSHA_API_KEY": "sk-test-roundtrip-123"}},
    )
    assert r.status_code == 200, f"expected 200, got {r.status_code}: {r.text}"
    assert r.json().get("saved") is True
    assert credential_is_set(db_session, "lusha_enrichment", "LUSHA_API_KEY")
    assert get_credential(db_session, "lusha_enrichment", "LUSHA_API_KEY") == "sk-test-roundtrip-123"
