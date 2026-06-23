# tests/test_connector_service.py
from types import SimpleNamespace

from app.services import connector_service as cs


def _src(**kw):
    base = dict(
        name="nexar",
        category="api",
        source_type="aggregator",
        env_vars=["NEXAR_CLIENT_ID"],
        is_active=True,
        status="live",
        last_error=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def test_control_type_classification():
    assert cs.control_type(_src(name="clay_enrichment")) == "oauth_clay"
    assert cs.control_type(_src(name="eight_by_eight")) == "multi_field"
    assert cs.control_type(_src(name="icsource")) == "browser_login"
    assert cs.control_type(_src(name="netcomponents")) == "browser_login"
    assert cs.control_type(_src(name="azure_oauth")) == "scopes"
    assert cs.control_type(_src(name="teams")) == "scopes"
    assert cs.control_type(_src(name="sam_gov_enrichment", env_vars=[])) == "keyless"
    assert cs.control_type(_src(name="ai_live_web", env_vars=[])) == "keyless"
    assert cs.control_type(_src(name="nexar", env_vars=["NEXAR_CLIENT_ID"])) == "key"


def test_group_mapping():
    assert cs.connector_group(_src(name="nexar", category="api", source_type="aggregator")) == "part_sourcing"
    assert cs.connector_group(_src(name="lusha_enrichment", category="enrichment")) == "enrichment"
    assert cs.connector_group(_src(name="anthropic", category="platform")) in ("ai", "communications")  # see impl note
    assert cs.connector_group(_src(name="icsource")) == "browser_workers"
    assert cs.connector_group(_src(name="stock_list")) == "manual"


def test_eight_by_eight_in_communications_group():
    """8x8 (VoIP) must land in the Communications group — by name and by category."""
    assert cs.connector_group(_src(name="eight_by_eight", category="voip")) == "communications"
    # robust even if the category drifts
    assert cs.connector_group(_src(name="eight_by_eight", category="")) == "communications"
    # the category alone (voip) routes there too
    assert cs.connector_group(_src(name="some_voip", category="voip")) == "communications"


def test_state_live():
    assert (
        cs.connector_state(
            _src(status="live", is_active=True),
            credential_set=True,
            oauth_connected=False,
            needs_reconnect=False,
            keyless=False,
        )
        == "live"
    )


def test_state_error_from_status():
    assert (
        cs.connector_state(
            _src(status="error", is_active=True, last_error="401"),
            credential_set=True,
            oauth_connected=False,
            needs_reconnect=False,
            keyless=False,
        )
        == "error"
    )


def test_state_off_when_inactive():
    assert (
        cs.connector_state(
            _src(status="live", is_active=False),
            credential_set=True,
            oauth_connected=False,
            needs_reconnect=False,
            keyless=False,
        )
        == "off"
    )


def test_state_needs_setup_no_creds():
    assert (
        cs.connector_state(
            _src(is_active=False), credential_set=False, oauth_connected=False, needs_reconnect=False, keyless=False
        )
        == "needs_setup"
    )


def test_state_untested_pending():
    assert (
        cs.connector_state(
            _src(status="pending", is_active=True),
            credential_set=True,
            oauth_connected=False,
            needs_reconnect=False,
            keyless=False,
        )
        == "untested"
    )


def test_state_keyless_is_credentialed():
    assert (
        cs.connector_state(
            _src(status="live", is_active=True),
            credential_set=False,
            oauth_connected=False,
            needs_reconnect=False,
            keyless=True,
        )
        == "live"
    )


def test_state_clay_needs_reconnect():
    assert (
        cs.connector_state(
            _src(name="clay_enrichment", is_active=True),
            credential_set=False,
            oauth_connected=False,
            needs_reconnect=True,
            keyless=False,
        )
        == "needs_reconnect"
    )


def test_state_clay_connected_live():
    assert (
        cs.connector_state(
            _src(name="clay_enrichment", status="live", is_active=True),
            credential_set=False,
            oauth_connected=True,
            needs_reconnect=False,
            keyless=False,
        )
        == "live"
    )
