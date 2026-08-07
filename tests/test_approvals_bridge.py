"""test_approvals_bridge.py — the parallel JSON approval-request API is GONE (W4.3).

History: QP Phase C1 retired the read-only buy-plan bridge and made the JSON queue
engine-native; W4.3 then deleted the whole parallel JSON API (routers/approvals.py —
list / detail / decision / reassign / cancel). The ONE approvals surface is the
Approvals Workspace at /v2/approvals (routers/htmx/approvals_hub.py), whose decide
POSTs live on the existing buy_plans / prepayments routes. Reassign died with it:
any-of routing makes it redundant (any eligible approver just acts).

Covers: every former /v2/approvals/requests* route is absent from the route registry
and a live request 404s (route gone, not 405/500) — the test_qp_lock_matrix pattern.

Called by: pytest
Depends on: conftest (client), app.main (route registry), tests._route_helpers.
"""

import pytest

from app.main import app
from tests._route_helpers import iter_routes

_GONE_PATHS = (
    "/v2/approvals/requests",
    "/v2/approvals/requests/{id}",
    "/v2/approvals/requests/{id}/decision",
    "/v2/approvals/requests/{id}/reassign",
    "/v2/approvals/requests/{id}/cancel",
)


def test_json_approval_api_removed_from_registry() -> None:
    """None of the parallel JSON API's five routes is registered anymore."""
    paths = {getattr(route, "path", None) for route in iter_routes(app.routes)}
    for gone in _GONE_PATHS:
        assert gone not in paths, f"{gone} should have died with W4.3"


def test_json_approval_list_get_404s(client) -> None:
    """A live GET to the former JSON list 404s (route gone, not 405/500)."""
    assert client.get("/v2/approvals/requests").status_code == 404


def test_json_approval_detail_get_404s(client) -> None:
    assert client.get("/v2/approvals/requests/1").status_code == 404


@pytest.mark.parametrize("action", ["decision", "reassign", "cancel"])
def test_json_approval_action_posts_404(client, action: str) -> None:
    """A live POST to any former JSON action route 404s."""
    resp = client.post(f"/v2/approvals/requests/1/{action}", data={"action": "approve"})
    assert resp.status_code == 404
