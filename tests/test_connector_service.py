# tests/test_connector_service.py
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.services import connector_service as cs


def _worker_row(**kw):
    """A worker-status singleton row (TbfWorkerStatus-shaped) for worker_health()."""
    base = dict(
        is_running=True,
        last_heartbeat=datetime.now(timezone.utc),
        last_search_at=None,
        circuit_breaker_open=False,
        circuit_breaker_reason=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


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
    assert cs.control_type(_src(name="thebrokersite")) == "browser_login"
    assert cs.control_type(_src(name="azure_oauth")) == "scopes"
    # teams_notifications is a webhook/key connector (needs TEAMS_WEBHOOK_URL) — NOT scopes.
    assert cs.control_type(_src(name="teams_notifications", env_vars=["TEAMS_WEBHOOK_URL"])) == "key"
    assert cs.control_type(_src(name="sam_gov_enrichment", env_vars=[])) == "keyless"
    assert cs.control_type(_src(name="ai_live_web", env_vars=[])) == "keyless"
    assert cs.control_type(_src(name="nexar", env_vars=["NEXAR_CLIENT_ID"])) == "key"


def test_email_mining_is_flag_not_key():
    """email_mining's env var (EMAIL_MINING_ENABLED) is a boolean flag, not a secret —
    it must classify as keyless (on/off toggle), never 'key' (a masked field that would
    encrypt a bogus credential)."""
    src = _src(name="email_mining", env_vars=["EMAIL_MINING_ENABLED"])
    assert cs.control_type(src) == "keyless"
    assert cs.is_keyless(src) is True


def test_teams_is_keyed_not_keyless():
    """teams_notifications needs a TEAMS_WEBHOOK_URL field — it must not read as keyless
    (which would offer no way to enter/rotate the webhook)."""
    src = _src(name="teams_notifications", env_vars=["TEAMS_WEBHOOK_URL", "TEAMS_TEAM_ID"])
    assert cs.control_type(src) == "key"
    assert cs.is_keyless(src) is False
    # Still lands in the Communications group (by category).
    assert cs.connector_group(_src(name="teams_notifications", category="platform")) == "communications"


def test_group_mapping():
    assert cs.connector_group(_src(name="nexar", category="api", source_type="aggregator")) == "part_sourcing"
    assert cs.connector_group(_src(name="lusha_enrichment", category="enrichment")) == "enrichment"
    assert cs.connector_group(_src(name="anthropic_ai", category="platform")) == "ai"
    assert cs.connector_group(_src(name="icsource")) == "browser_workers"
    assert cs.connector_group(_src(name="thebrokersite")) == "browser_workers"
    assert cs.connector_group(_src(name="stock_list_import")) == "manual"


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


# ── Task 4b: planned roadmap state ───────────────────────────────────

# thebrokersite moved from _PLANNED to _BROWSER (TBF browser worker shipped) — see
# app/services/connector_service.py and BROWSER_WORKER_SOURCES.
_PLANNED_NAMES = ["findchips", "future", "heilind", "lcsc", "rochester", "verical"]


def test_control_type_planned_for_all_planned_names():
    """control_type() returns 'planned' for every name in _PLANNED."""
    for name in _PLANNED_NAMES:
        result = cs.control_type(_src(name=name, env_vars=["SOME_KEY"]))
        assert result == "planned", f"Expected 'planned' for {name!r}, got {result!r}"


def test_control_type_planned_takes_priority_over_key():
    """Planned check must fire BEFORE keyless/key logic, even when env_vars is set."""
    for name in _PLANNED_NAMES:
        result = cs.control_type(_src(name=name, env_vars=["FUTURE_API_KEY"]))
        assert result == "planned"


def test_control_type_planned_no_env_vars():
    """Planned check fires even when env_vars is empty."""
    for name in _PLANNED_NAMES:
        result = cs.control_type(_src(name=name, env_vars=[]))
        assert result == "planned"


def test_connector_state_planned():
    """connector_state() returns 'planned' when control_type is 'planned'."""
    for name in _PLANNED_NAMES:
        src = _src(name=name, env_vars=[], is_active=False, status="pending")
        result = cs.connector_state(
            src,
            credential_set=False,
            oauth_connected=False,
            needs_reconnect=False,
            keyless=False,
        )
        assert result == "planned", f"Expected 'planned' for {name!r}, got {result!r}"


def test_connector_state_planned_ignores_active_flag():
    """Planned state must win even if is_active=True."""
    src = _src(name="future", env_vars=[], is_active=True, status="live")
    result = cs.connector_state(
        src,
        credential_set=True,
        oauth_connected=False,
        needs_reconnect=False,
        keyless=False,
    )
    assert result == "planned"


# ── Worker-aware status (TBF / NetComponents / ICsource browser workers) ──────


def test_worker_backed_sources_mapping():
    """The three browser-worker sources map to their worker keys; others don't."""
    for name in ("thebrokersite", "netcomponents", "icsource"):
        assert cs.is_worker_backed(_src(name=name))
        assert name in cs.WORKER_BACKED_SOURCES
    assert not cs.is_worker_backed(_src(name="nexar"))
    assert not cs.is_worker_backed(_src(name="mouser"))
    assert cs.WORKER_BACKED_SOURCES == {"thebrokersite": "tbf", "netcomponents": "nc", "icsource": "ics"}


def test_worker_health_healthy_recent_heartbeat():
    """Running worker with a fresh heartbeat and closed breaker is healthy."""
    v = cs.worker_health(_worker_row(last_heartbeat=datetime.now(timezone.utc) - timedelta(seconds=30)))
    assert v["healthy"] is True
    assert v["problem"] is None
    assert v["heartbeat_age_secs"] is not None and v["heartbeat_age_secs"] < 120


def test_worker_health_stale_heartbeat_unhealthy():
    """A heartbeat older than the stale threshold is unhealthy with a 'stalled'
    reason."""
    old = datetime.now(timezone.utc) - timedelta(minutes=40)
    v = cs.worker_health(_worker_row(last_heartbeat=old))
    assert v["healthy"] is False
    assert "stalled" in v["problem"].lower()


def test_worker_health_not_running_unhealthy():
    v = cs.worker_health(_worker_row(is_running=False))
    assert v["healthy"] is False
    assert "not running" in v["problem"].lower()


def test_worker_health_circuit_breaker_open_unhealthy():
    v = cs.worker_health(_worker_row(circuit_breaker_open=True, circuit_breaker_reason="Blocked by site"))
    assert v["healthy"] is False
    assert v["problem"] == "Blocked by site"


def test_worker_health_missing_row_unhealthy():
    v = cs.worker_health(None)
    assert v["healthy"] is False
    assert v["problem"]
    assert v["heartbeat_age_secs"] is None


def test_worker_health_no_heartbeat_unhealthy():
    v = cs.worker_health(_worker_row(last_heartbeat=None))
    assert v["healthy"] is False
    assert v["heartbeat_age_secs"] is None


def test_worker_health_naive_datetime_is_treated_as_utc():
    """A naive (tz-less) heartbeat must not crash — it's coerced to UTC."""
    naive = (datetime.now(timezone.utc) - timedelta(seconds=20)).replace(tzinfo=None)
    v = cs.worker_health(_worker_row(last_heartbeat=naive))
    assert v["healthy"] is True


def test_state_worker_active_when_worker_healthy():
    """A worker-backed source with a healthy worker reads 'worker_active', not
    'live'/'error'."""
    src = _src(name="thebrokersite", env_vars=["TBF_USERNAME", "TBF_PASSWORD"], status="live", is_active=True)
    state = cs.connector_state(
        src,
        credential_set=False,  # no direct key — must NOT matter
        oauth_connected=False,
        needs_reconnect=False,
        keyless=False,
        worker={"healthy": True},
    )
    assert state == "worker_active"


def test_state_worker_down_when_worker_unhealthy():
    """A worker-backed source whose worker is stalled reads 'worker_down', never
    'live'."""
    src = _src(name="netcomponents", env_vars=["NC_USERNAME"], status="live", is_active=True)
    state = cs.connector_state(
        src,
        credential_set=True,
        oauth_connected=False,
        needs_reconnect=False,
        keyless=False,
        worker={"healthy": False, "problem": "No heartbeat for 40 min — worker stalled"},
    )
    assert state == "worker_down"


def test_state_worker_backed_never_needs_setup_without_key():
    """A keyless worker-backed source must NEVER read 'needs_setup' just for lacking a
    key."""
    src = _src(name="icsource", env_vars=[], status="pending", is_active=True)
    state = cs.connector_state(
        src,
        credential_set=False,
        oauth_connected=False,
        needs_reconnect=False,
        keyless=True,
        worker={"healthy": True},
    )
    assert state == "worker_active"
    assert state != "needs_setup"


def test_state_worker_backed_off_when_inactive():
    """An operator-disabled worker source reads 'off' (not worker_down)."""
    src = _src(name="thebrokersite", status="live", is_active=False)
    state = cs.connector_state(
        src,
        credential_set=True,
        oauth_connected=False,
        needs_reconnect=False,
        keyless=False,
        worker={"healthy": True},
    )
    assert state == "off"


def test_keyless_direct_api_still_needs_setup_without_access():
    """Regression guard: a NON-worker keyless/keyed source still surfaces needs_setup
    when it has no access — the worker carve-out must not leak to direct APIs."""
    src = _src(name="mouser", env_vars=["MOUSER_API_KEY"], status="pending", is_active=False)
    state = cs.connector_state(
        src,
        credential_set=False,
        oauth_connected=False,
        needs_reconnect=False,
        keyless=False,
    )
    assert state == "needs_setup"
