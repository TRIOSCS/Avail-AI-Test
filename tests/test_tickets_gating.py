"""Gating tests for the admin-only Tickets triage console.

Verifies that non-admin users cannot reach the Tickets workspace/list/detail
view partials (a maintainer triage console that also leaks other users' reports),
while the floating "Report a Problem" submission flow stays open to all logins.

Called by: pytest.
Depends on: conftest fixtures (client, nonadmin_client).
"""


def test_nonadmin_blocked_from_tickets_workspace(nonadmin_client):
    assert nonadmin_client.get("/v2/partials/trouble-tickets/workspace").status_code == 403


def test_nonadmin_blocked_from_tickets_list(nonadmin_client):
    assert nonadmin_client.get("/v2/partials/trouble-tickets/list").status_code == 403


def test_admin_can_load_tickets_workspace(client):
    assert client.get("/v2/partials/trouble-tickets/workspace").status_code == 200


def test_nonadmin_can_still_submit_report(nonadmin_client):
    # the floating "Report a Problem" form must stay open to all
    assert nonadmin_client.get("/api/trouble-tickets/form").status_code == 200


def test_tickets_tab_button_hidden_for_nonadmin(nonadmin_client):
    # settings index renders with is_admin False -> no Tickets tab button
    html = nonadmin_client.get("/v2/partials/settings").text
    assert "trouble-tickets/workspace" not in html
