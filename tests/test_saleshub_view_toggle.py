# tests/test_saleshub_view_toggle.py
"""Tests for the Sales-Hub relabel + view toggle (finding REQ-12).

The split-panel parts workspace stays the canonical "Sales Hub"; the flat
requisitions list is relabeled "Requisitions list" and both surfaces carry a
segmented toggle. Clean full-page URLs: /v2/requisitions -> workspace,
/v2/requisitions?view=list -> flat list.

Called by: pytest
Depends on: conftest fixtures (client, db_session, test_user)
"""

from datetime import datetime, timezone
from unittest.mock import patch


def _make_req(db_session, test_user):
    from app.models import Requisition

    req = Requisition(
        name="Toggle Req",
        status="open",
        urgency="normal",
        customer_name="Acme",
        created_by=test_user.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(req)
    db_session.commit()
    return req


# ── Full-page routing: ?view=list picks the list, default picks the workspace ──


def test_full_page_default_loads_workspace(client, test_user):
    # v2_page authenticates via get_user (session), not require_user — patch it.
    with patch("app.routers.htmx_views.get_user", return_value=test_user):
        resp = client.get("/v2/requisitions")
    assert resp.status_code == 200
    # Default shell lazy-loads the workspace, not the flat list.
    assert 'hx-get="/v2/partials/parts/workspace"' in resp.text
    assert 'hx-get="/v2/partials/requisitions"' not in resp.text


def test_full_page_view_list_loads_list_partial(client, test_user):
    with patch("app.routers.htmx_views.get_user", return_value=test_user):
        resp = client.get("/v2/requisitions?view=list")
    assert resp.status_code == 200
    # ?view=list flips the shell's lazy-load target to the flat list partial.
    assert 'hx-get="/v2/partials/requisitions"' in resp.text


# ── Workspace keeps "Sales Hub" and gains the toggle ──


def test_workspace_partial_keeps_sales_hub_and_has_toggle(client):
    resp = client.get("/v2/partials/parts/workspace")
    assert resp.status_code == 200
    assert "Sales Hub" in resp.text
    # Toggle present with both segments and the clean list push URL.
    assert "Workspace" in resp.text
    assert "List view" in resp.text
    assert 'hx-push-url="/v2/requisitions?view=list"' in resp.text


# ── List is relabeled "Requisitions list", no longer "Sales Hub" ──


def test_list_partial_relabeled_and_has_toggle(client):
    resp = client.get("/v2/partials/requisitions")
    assert resp.status_code == 200
    assert "Requisitions list" in resp.text
    assert "Sales Hub" not in resp.text
    # Toggle back to the workspace is discoverable from the list.
    assert "Workspace" in resp.text
    assert 'hx-push-url="/v2/requisitions"' in resp.text


# ── Detail back-link relabeled + pushes the clean list URL ──


def test_detail_back_link_relabeled(client, db_session, test_user):
    req = _make_req(db_session, test_user)
    resp = client.get(f"/v2/partials/requisitions/{req.id}")
    assert resp.status_code == 200
    assert "Requisitions list" in resp.text
    assert 'hx-push-url="/v2/requisitions?view=list"' in resp.text
