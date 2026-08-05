"""tests/test_proactive_park.py — W2 Proactive park acceptance (spec §4/§5.4/§8).

Proactive parks WHOLE behind its existing proactive_matching_enabled flag:
- config default is now False (the park switch — wave DB refreshes can't resurrect it)
- /v2/proactive (full page) and every /v2/partials/proactive* route 404 while off
- the matching engine's live entry point (trigger_rematch_on_offer_approval) no-ops
- the nav badge key stays in NAV_BADGE_KEYS but counts 0 while off (test_nav_badges)
- flag back on → the workspace routes come back (comeback trigger: Proactive
  revival / Wave-4 Deals-badge decision)

Called by: pytest
Depends on: tests/conftest.py (client, db_session, proactive_flag_on),
            app/routers/htmx/proactive.py, app/routers/htmx_views.py,
            app/services/proactive_matching.py, app/config.py
"""

from unittest.mock import patch

from fastapi.testclient import TestClient

from tests.conftest import engine  # noqa: F401


class TestParkSwitchDefault:
    """The code-level default is the park switch."""

    def test_config_default_is_off(self):
        from app.config import Settings

        assert Settings.model_fields["proactive_matching_enabled"].default is False

    def test_startup_seed_matches_park_default(self):
        """Fresh-DB system_config seed must not resurrect the flag."""
        import inspect

        from app import startup

        src = inspect.getsource(startup._seed_system_config)
        assert '("proactive_matching_enabled", "false"' in src


class TestRoutes404WhileParked:
    """Flag off (the default) → the whole workspace surface 404s.

    The full page authenticates via get_user (session), not require_user — patch it like
    TestV2FullPages does, so the 404 is proven for an AUTHED user (an anonymous hit
    renders the login shell at 200 regardless of module state).
    """

    def test_full_page_404(self, client: TestClient, test_user):
        with patch("app.routers.htmx_views.get_user", return_value=test_user):
            assert client.get("/v2/proactive").status_code == 404

    def test_list_partial_404(self, client: TestClient):
        assert client.get("/v2/partials/proactive").status_code == 404

    def test_refresh_404(self, client: TestClient):
        assert client.post("/v2/partials/proactive/refresh").status_code == 404

    def test_scorecard_404(self, client: TestClient):
        assert client.get("/v2/partials/proactive/scorecard").status_code == 404


class TestComebackPath:
    """Flag on → the parked surface returns without code changes."""

    def test_full_page_200_when_enabled(self, client: TestClient, test_user, proactive_flag_on):
        with patch("app.routers.htmx_views.get_user", return_value=test_user):
            resp = client.get("/v2/proactive")
        assert resp.status_code == 200
        assert 'hx-get="/v2/partials/proactive"' in resp.text

    def test_list_partial_200_when_enabled(self, client: TestClient, proactive_flag_on):
        assert client.get("/v2/partials/proactive").status_code == 200


class TestEngineParked:
    """The live engine entry point no-ops while parked."""

    def test_trigger_rematch_noop_and_runs_no_matching(self, db_session):
        from app.services.proactive_matching import trigger_rematch_on_offer_approval

        class _Offer:
            material_card_id = 123
            id = 1

        with patch("app.services.proactive_matching.find_matches_for_offer") as find:
            assert trigger_rematch_on_offer_approval(db_session, _Offer()) == 0
        assert not find.called
