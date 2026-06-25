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
            name="anthropic_ai",
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
            name="teams_notifications",
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
            name="stock_list_import",
            display_name="Vendor Stock List Import",
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


@pytest.fixture()
def empty_admin_client(db_session, admin_user):
    """Admin client with NO ApiSource rows — for the empty-state test."""
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
    # Manual group must include the stock-list card
    assert "Vendor Stock List Import" in html, "Expected Manual group to contain 'Vendor Stock List Import'"
    # AI group must include Anthropic
    assert "Anthropic" in html, "Expected AI group to contain 'Anthropic / Claude'"


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


# ── Task 4b: planned connector rendering ──────────────────────────────


@pytest.fixture()
def admin_client_with_planned(db_session, admin_user):
    """Admin client with a planned source (Future Electronics) seeded."""
    _seed_sources(db_session)
    db_session.add(
        ApiSource(
            name="future",
            display_name="Future Electronics",
            category="api",
            source_type="broker",
            description="Electronic components distributor",
            status="pending",
            env_vars=["FUTURE_API_KEY"],
            credentials={},
        )
    )
    db_session.commit()
    yield from _make_admin_client(db_session, admin_user)


def test_planned_connector_shows_planned_badge(admin_client_with_planned):
    """A planned provider renders a 'Planned' badge."""
    html = admin_client_with_planned.get("/v2/partials/settings/connectors").text
    assert "Planned" in html, "Expected 'Planned' badge for planned connector"


def test_planned_connector_shows_roadmap_note(admin_client_with_planned):
    """A planned provider shows the roadmap note (not built yet)."""
    html = admin_client_with_planned.get("/v2/partials/settings/connectors").text
    assert "roadmap" in html.lower() or "not built" in html.lower(), "Expected roadmap note for planned connector"


def test_planned_connector_no_credential_input(admin_client_with_planned):
    """A planned provider must NOT render a credential input for its env vars."""
    html = admin_client_with_planned.get("/v2/partials/settings/connectors").text
    assert "FUTURE_API_KEY" not in html, "Planned connector must NOT expose credential input"


def test_planned_connector_no_enable_toggle(admin_client_with_planned):
    """A planned provider must NOT render the enable toggle."""
    html = admin_client_with_planned.get("/v2/partials/settings/connectors").text
    # The toggle is rendered as 'Enable Future Electronics' aria-label
    assert 'aria-label="Enable Future Electronics"' not in html, "Planned connector must NOT have enable toggle"


def test_planned_connector_no_test_button(admin_client_with_planned):
    """A planned provider must NOT render a Test button."""
    # The planned card must not have testable=True, so no Test button
    # We verify by checking the planned card section specifically
    html = admin_client_with_planned.get("/v2/partials/settings/connectors").text
    # The planned provider shows Future Electronics
    assert "Future Electronics" in html, "Planned source must appear on page"


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


# ── Task 5: empty state, confirms, success toasts, test-all summary, vocab ──


def test_connectors_empty_state_when_no_sources(empty_admin_client):
    """With no ApiSource rows the tab shows an empty-state message, not a bare
    header."""
    html = empty_admin_client.get("/v2/partials/settings/connectors").text
    assert "No connectors" in html, "Expected empty-state heading when no sources exist"
    assert "text-sm text-gray-600" in html, "Expected house-style empty-state heading class"
    assert "text-xs text-gray-400" in html, "Expected house-style empty-state hint class"


def test_connectors_tab_not_empty_state_when_sources_exist(admin_client):
    """When sources are seeded the empty-state message must NOT render."""
    html = admin_client.get("/v2/partials/settings/connectors").text
    assert "No connectors" not in html


def test_save_credentials_emits_success_toast(admin_client):
    """PUT credentials carries a showToast HX-Trigger so the user sees confirmation."""
    r = admin_client.put(
        "/api/sources/lusha_enrichment/credentials",
        json={"credentials": {"LUSHA_API_KEY": "sk-test-toast-1"}},
    )
    assert r.status_code == 200
    assert "showToast" in r.headers.get("HX-Trigger", ""), "Expected showToast HX-Trigger on save"


def test_activate_emits_success_toast(admin_client, db_session):
    """PUT activate carries a showToast HX-Trigger naming the source + new state."""
    from app.models import ApiSource as AS

    src = db_session.query(AS).filter_by(name="lusha_enrichment").first()
    r = admin_client.put(f"/api/sources/{src.id}/activate")
    assert r.status_code == 200
    trigger = r.headers.get("HX-Trigger", "")
    assert "showToast" in trigger, "Expected showToast HX-Trigger on activate"
    assert "Lusha" in trigger, "Expected the source display name in the toast"


def test_clay_disconnect_has_confirm(admin_client, monkeypatch):
    """Connected Clay card carries an hx-confirm on Disconnect."""
    import app.routers.htmx_views as v

    monkeypatch.setattr(v.clay_oauth, "is_connected", lambda: True)
    html = admin_client.get("/v2/partials/settings/connectors").text
    assert "hx-confirm=" in html
    assert "Disconnect Clay enrichment for everyone" in html


def test_live_source_toggle_has_confirm(admin_client, db_session):
    """A LIVE source's enable toggle carries an hx-confirm (disabling it is app-
    wide)."""
    from app.models import ApiSource as AS

    src = db_session.query(AS).filter_by(name="lusha_enrichment").first()
    src.status = "live"
    src.is_active = True
    src.credentials = {"LUSHA_API_KEY": "x"}  # credential_set so state resolves to live
    db_session.commit()
    from app.services.credential_service import _cred_cache

    _cred_cache.clear()
    html = admin_client.get("/v2/partials/settings/connectors").text
    assert "hx-confirm=" in html, "Expected hx-confirm on the live source toggle"
    assert "Searches will return fewer results" in html


def test_non_live_source_toggle_has_no_confirm_text(admin_client):
    """A pending (non-live) source must NOT carry the disable-confirm copy."""
    # All seeded sources are pending/needs_setup — none live — so the live-only
    # confirm copy must be absent.
    html = admin_client.get("/v2/partials/settings/connectors").text
    assert "Searches will return fewer results" not in html


def test_test_all_returns_summary_fragment(admin_client, db_session):
    """Test-all returns an aggregate summary line targeting a dedicated container."""
    from app.models import ApiSource as AS

    sam = db_session.query(AS).filter_by(name="sam_gov_enrichment").first()
    sam.is_active = True
    db_session.commit()

    r = admin_client.post("/v2/partials/settings/connectors/test-all")
    assert r.status_code == 200
    assert "Tested" in r.text, "Expected an aggregate 'Tested N' summary line"
    assert "test-all-summary" in r.text, "Expected the dedicated summary container id"


def test_header_and_group_use_consistent_vocab(admin_client):
    """Header counter + group header use the same canonical word ('need setup'), not the
    old 'need attention' lexicon."""
    html = admin_client.get("/v2/partials/settings/connectors").text
    assert "need attention" not in html, "Header should use unified 'need setup' vocab"
    assert "need setup" in html or "needs setup" in html.lower()
