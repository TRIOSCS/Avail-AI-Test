# tests/test_req_details_tab.py
"""Tests for the Req Details tab endpoint and inline save context=tab handling.

Called by: pytest
Depends on: conftest fixtures (client, db_session, test_user)
"""

from datetime import datetime, timezone


def _make_requisition_and_parts(db_session, test_user, num_parts=2):
    """Helper: create a requisition with sibling parts."""
    from app.models import Requirement, Requisition

    reqn = Requisition(
        name="Test Req",
        status="active",
        urgency="normal",
        customer_name="Acme Corp",
        created_by=test_user.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(reqn)
    db_session.commit()
    db_session.refresh(reqn)

    parts = []
    for i in range(num_parts):
        part = Requirement(
            requisition_id=reqn.id,
            primary_mpn=f"MPN-{i:03d}",
            target_qty=(i + 1) * 100,
            sourcing_status="open",
        )
        db_session.add(part)
        parts.append(part)
    db_session.commit()
    for p in parts:
        db_session.refresh(p)

    return reqn, parts


def test_req_details_tab_returns_html(client, db_session, test_user):
    """GET /v2/partials/parts/{id}/tab/req-details returns requisition info and sibling
    table."""
    reqn, parts = _make_requisition_and_parts(db_session, test_user)

    resp = client.get(f"/v2/partials/parts/{parts[0].id}/tab/req-details")
    assert resp.status_code == 200
    assert "Requisition Info" in resp.text
    assert "Test Req" in resp.text
    assert "Acme Corp" in resp.text


def test_req_details_tab_shows_sibling_parts(client, db_session, test_user):
    """The tab lists all sibling parts on the same requisition."""
    reqn, parts = _make_requisition_and_parts(db_session, test_user, num_parts=3)

    resp = client.get(f"/v2/partials/parts/{parts[0].id}/tab/req-details")
    assert resp.status_code == 200
    for part in parts:
        assert part.primary_mpn in resp.text


def test_req_details_tab_highlights_current_part(client, db_session, test_user):
    """The current part row should have a highlight class."""
    reqn, parts = _make_requisition_and_parts(db_session, test_user)

    resp = client.get(f"/v2/partials/parts/{parts[0].id}/tab/req-details")
    assert resp.status_code == 200
    assert "bg-brand-50" in resp.text


def test_req_details_tab_404_for_missing_part(client, db_session, test_user):
    """Returns 404 when requirement_id does not exist."""
    resp = client.get("/v2/partials/parts/999999/tab/req-details")
    assert resp.status_code == 404


def test_req_details_tab_shows_editable_fields(client, db_session, test_user):
    """Editable fields have hx-get attributes pointing to inline edit endpoints."""
    reqn, parts = _make_requisition_and_parts(db_session, test_user)

    resp = client.get(f"/v2/partials/parts/{parts[0].id}/tab/req-details")
    assert resp.status_code == 200
    assert f"/v2/partials/requisitions/{reqn.id}/edit/name?context=tab" in resp.text
    assert f"/v2/partials/requisitions/{reqn.id}/edit/status?context=tab" in resp.text
    assert f"/v2/partials/requisitions/{reqn.id}/edit/urgency?context=tab" in resp.text


def test_inline_edit_cell_tab_context(client, db_session, test_user):
    """GET /v2/partials/requisitions/{id}/edit/{field}?context=tab returns form with tab
    context."""
    reqn, _ = _make_requisition_and_parts(db_session, test_user)

    resp = client.get(f"/v2/partials/requisitions/{reqn.id}/edit/name?context=tab")
    assert resp.status_code == 200
    assert 'name="context" value="tab"' in resp.text


def test_inline_save_tab_context_returns_trigger(client, db_session, test_user):
    """PATCH with context=tab returns empty body with HX-Trigger for
    reqDetailsRefresh."""
    import json

    reqn, _ = _make_requisition_and_parts(db_session, test_user)

    resp = client.patch(
        f"/v2/partials/requisitions/{reqn.id}/inline",
        data={"field": "name", "value": "Updated Name", "context": "tab"},
    )
    assert resp.status_code == 200
    assert resp.text == ""

    trigger = json.loads(resp.headers["HX-Trigger"])
    assert trigger["reqDetailsRefresh"] is True
    assert "showToast" in trigger


def test_inline_save_tab_context_updates_urgency(client, db_session, test_user):
    """PATCH urgency with context=tab persists the change."""
    import json

    from app.models import Requisition

    reqn, _ = _make_requisition_and_parts(db_session, test_user)

    resp = client.patch(
        f"/v2/partials/requisitions/{reqn.id}/inline",
        data={"field": "urgency", "value": "hot", "context": "tab"},
    )
    assert resp.status_code == 200

    trigger = json.loads(resp.headers["HX-Trigger"])
    assert trigger["reqDetailsRefresh"] is True

    db_session.expire_all()
    updated = db_session.get(Requisition, reqn.id)
    assert updated.urgency == "hot"


def test_workspace_tab_bar_includes_req_details(client, db_session, test_user):
    """The workspace template includes the req-details tab in the tab bar."""
    # The workspace partial is loaded as part of the requisition detail page;
    # we test indirectly by checking the template source has the tab.
    import pathlib

    ws = pathlib.Path("app/templates/htmx/partials/parts/workspace.html").read_text()
    assert "req-details" in ws
    assert "REQ Detail" in ws
