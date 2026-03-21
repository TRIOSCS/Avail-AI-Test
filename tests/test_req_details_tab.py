# tests/test_req_details_tab.py
"""Tests for the Req Details tab endpoint and inline save context=tab handling.

Called by: pytest
Depends on: conftest fixtures (client, db_session, test_user)
"""

from datetime import datetime, timezone


def _make_requisition_and_parts(db_session, test_user, num_parts=2, **part_kwargs):
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
        defaults = {
            "requisition_id": reqn.id,
            "primary_mpn": f"MPN-{i:03d}",
            "target_qty": (i + 1) * 100,
            "sourcing_status": "open",
        }
        if i == 0:
            defaults.update(part_kwargs)
        part = Requirement(**defaults)
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


def test_req_details_tab_shows_new_columns(client, db_session, test_user):
    """The sibling table shows Brand, Tgt $, Cust PN, Subs, and Offers columns."""
    from decimal import Decimal

    from app.models import Requirement, Requisition
    from app.models.offers import Offer

    reqn = Requisition(
        name="Col Test",
        status="active",
        urgency="normal",
        customer_name="TestCo",
        created_by=test_user.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(reqn)
    db_session.commit()
    db_session.refresh(reqn)

    part = Requirement(
        requisition_id=reqn.id,
        primary_mpn="LM358",
        brand="Texas Instruments",
        target_qty=500,
        target_price=Decimal("1.25"),
        customer_pn="CUST-001",
        substitutes=["LM358A", "LM358B"],
        sourcing_status="sourcing",
    )
    db_session.add(part)
    db_session.commit()
    db_session.refresh(part)

    offer = Offer(
        requisition_id=reqn.id,
        requirement_id=part.id,
        vendor_name="Digi-Key",
        mpn="LM358",
        unit_price=Decimal("0.85"),
        qty_available=1000,
    )
    db_session.add(offer)
    db_session.commit()

    resp = client.get(f"/v2/partials/parts/{part.id}/tab/req-details")
    assert resp.status_code == 200
    assert "Texas Instruments" in resp.text
    assert "$1.25" in resp.text
    assert "CUST-001" in resp.text
    assert ">2<" in resp.text or ">2 " in resp.text  # 2 subs
    assert "$0.85" in resp.text  # best offer price


def test_req_details_tab_shows_part_specs_section(client, db_session, test_user):
    """The tab shows a Part Specifications section with the 6 spec fields."""
    from app.models import Requirement, Requisition

    reqn = Requisition(
        name="Spec Test",
        status="active",
        urgency="normal",
        customer_name="TestCo",
        created_by=test_user.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(reqn)
    db_session.commit()
    db_session.refresh(reqn)

    part = Requirement(
        requisition_id=reqn.id,
        primary_mpn="LM358",
        customer_pn="CUST-ABC",
        condition="New",
        firmware="v2.1",
        sourcing_status="open",
    )
    db_session.add(part)
    db_session.commit()
    db_session.refresh(part)

    resp = client.get(f"/v2/partials/parts/{part.id}/tab/req-details")
    assert resp.status_code == 200
    assert "Part Specifications" in resp.text
    assert "CUST-ABC" in resp.text
    assert "New" in resp.text
    assert "v2.1" in resp.text
    assert "edit-spec/customer_pn" in resp.text


def test_spec_edit_returns_form(client, db_session, test_user):
    """GET /v2/partials/parts/{id}/edit-spec/{field} returns an edit form."""
    from app.models import Requirement, Requisition

    reqn = Requisition(
        name="Edit Test",
        status="active",
        urgency="normal",
        customer_name="TestCo",
        created_by=test_user.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(reqn)
    db_session.commit()
    db_session.refresh(reqn)

    part = Requirement(
        requisition_id=reqn.id,
        primary_mpn="ABC123",
        firmware="v1.0",
        sourcing_status="open",
    )
    db_session.add(part)
    db_session.commit()
    db_session.refresh(part)

    resp = client.get(f"/v2/partials/parts/{part.id}/edit-spec/firmware?context=tab")
    assert resp.status_code == 200
    assert "v1.0" in resp.text
    assert "save-spec" in resp.text


def test_spec_edit_invalid_field(client, db_session, test_user):
    """GET /v2/partials/parts/{id}/edit-spec/bad_field returns 400."""
    reqn, parts = _make_requisition_and_parts(db_session, test_user)
    resp = client.get(f"/v2/partials/parts/{parts[0].id}/edit-spec/bad_field")
    assert resp.status_code == 400


def test_spec_save_updates_field(client, db_session, test_user):
    """PATCH /v2/partials/parts/{id}/save-spec persists the value."""
    import json

    from app.models import Requirement, Requisition

    reqn = Requisition(
        name="Save Test",
        status="active",
        urgency="normal",
        customer_name="TestCo",
        created_by=test_user.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(reqn)
    db_session.commit()
    db_session.refresh(reqn)

    part = Requirement(
        requisition_id=reqn.id,
        primary_mpn="XYZ789",
        sourcing_status="open",
    )
    db_session.add(part)
    db_session.commit()
    db_session.refresh(part)

    resp = client.patch(
        f"/v2/partials/parts/{part.id}/save-spec",
        data={"field": "packaging", "value": "Tape & Reel"},
    )
    assert resp.status_code == 200
    assert "Tape &amp; Reel" in resp.text or "Tape & Reel" in resp.text

    trigger = json.loads(resp.headers["HX-Trigger"])
    assert "part-updated" in trigger
    assert "showToast" in trigger

    db_session.expire_all()
    updated = db_session.get(Requirement, part.id)
    assert updated.packaging == "Tape & Reel"


def test_spec_save_clears_empty_value(client, db_session, test_user):
    """PATCH with empty value sets field to None."""
    from app.models import Requirement, Requisition

    reqn = Requisition(
        name="Clear Test",
        status="active",
        urgency="normal",
        customer_name="TestCo",
        created_by=test_user.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(reqn)
    db_session.commit()
    db_session.refresh(reqn)

    part = Requirement(
        requisition_id=reqn.id,
        primary_mpn="ABC",
        condition="New",
        sourcing_status="open",
    )
    db_session.add(part)
    db_session.commit()
    db_session.refresh(part)

    resp = client.patch(
        f"/v2/partials/parts/{part.id}/save-spec",
        data={"field": "condition", "value": ""},
    )
    assert resp.status_code == 200

    db_session.expire_all()
    updated = db_session.get(Requirement, part.id)
    assert updated.condition is None


def test_spec_edit_blocked_on_archived_part(client, db_session, test_user):
    """Spec edit and save return 403 for archived parts."""
    reqn, parts = _make_requisition_and_parts(db_session, test_user, num_parts=1)
    parts[0].sourcing_status = "archived"
    db_session.commit()

    resp = client.get(f"/v2/partials/parts/{parts[0].id}/edit-spec/firmware")
    assert resp.status_code == 403

    resp = client.patch(
        f"/v2/partials/parts/{parts[0].id}/save-spec",
        data={"field": "firmware", "value": "v3.0"},
    )
    assert resp.status_code == 403


def test_spec_save_whitespace_only_becomes_null(client, db_session, test_user):
    """PATCH with whitespace-only value sets field to None, not empty string."""
    reqn, parts = _make_requisition_and_parts(db_session, test_user, num_parts=1)

    resp = client.patch(
        f"/v2/partials/parts/{parts[0].id}/save-spec",
        data={"field": "firmware", "value": "   "},
    )
    assert resp.status_code == 200

    db_session.expire_all()
    from app.models import Requirement

    updated = db_session.get(Requirement, parts[0].id)
    assert updated.firmware is None


def test_workspace_tab_bar_includes_req_details(client, db_session, test_user):
    """The workspace template includes the req-details tab in the tab bar."""
    # The workspace partial is loaded as part of the requisition detail page;
    # we test indirectly by checking the template source has the tab.
    import pathlib

    ws = pathlib.Path("app/templates/htmx/partials/parts/workspace.html").read_text()
    assert "req-details" in ws
    assert "REQ Detail" in ws
