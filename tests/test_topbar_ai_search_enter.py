"""shell-03 regression: the global-search 'Press Enter for AI-powered search' hint
must actually be wired.

The no-results state (search_results.html) tells the user to press Enter for AI
search, and POST /v2/partials/search/ai is fully built — but the topbar input had
no Enter handler, so the endpoint was reachable only from tests and the hint was
dead. The topbar input now wires @keydown.enter to an htmx.ajax POST against the AI
endpoint, targeting the same #global-search-results dropdown as type-ahead.

Called by: pytest
Depends on: app.template_env.templates, app.routers.htmx_views.ai_search_endpoint
"""

from unittest.mock import AsyncMock, patch

from app.template_env import templates


def _render_topbar() -> str:
    return templates.get_template("htmx/partials/shared/topbar.html").render(user_name="Tester")


def test_topbar_wires_enter_to_ai_search():
    """The search input fires the AI endpoint on Enter (fulfilling the hint)."""
    html = _render_topbar()
    assert "@keydown.enter" in html
    assert "/v2/partials/search/ai" in html


def test_topbar_ai_search_targets_results_dropdown():
    """Enter posts into the same dropdown type-ahead uses, and sends the query."""
    html = _render_topbar()
    # The Enter handler must target the global-search results container and pass the
    # current input value as q so the endpoint (q: str = Form("")) receives it.
    assert "target: '#global-search-results'" in html
    assert "q: $el.value" in html


def test_ai_search_endpoint_still_serves(client, db_session):
    """The endpoint the hint now reaches responds 200 (safe Claude-unavailable path)."""
    with (
        patch(
            "app.services.global_search_service.claude_structured",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch("app.services.global_search_service._get_ai_cache", return_value=None),
        patch("app.services.global_search_service._set_ai_cache"),
    ):
        resp = client.post("/v2/partials/search/ai", data={"q": "LM358"})
    assert resp.status_code == 200
