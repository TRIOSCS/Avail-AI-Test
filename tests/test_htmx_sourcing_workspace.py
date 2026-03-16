"""Tests for sourcing workspace split-panel views.

Covers workspace page, workspace partial, lead list partial, lead panel partial,
and status update routing for workspace context.

Called by: pytest
Depends on: conftest (client, db_session, test_user fixtures), app.models.sourcing_lead
"""

from datetime import datetime, timezone

import pytest
from sqlalchemy.orm import Session

from app.models import Requirement, Requisition, Sighting, User
from app.models.sourcing_lead import LeadEvidence, SourcingLead


@pytest.fixture()
def workspace_data(db_session: Session, test_user: User):
    """Create requisition, requirement, sighting, lead, and evidence for workspace tests."""
    req = Requisition(
        name="Workspace Test Req",
        status="active",
        created_by=test_user.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(req)
    db_session.flush()

    requirement = Requirement(
        requisition_id=req.id,
        primary_mpn="STM32F103",
        target_qty=500,
        sourcing_status="open",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(requirement)
    db_session.flush()

    sighting = Sighting(
        requirement_id=requirement.id,
        vendor_name="Alpha Electronics",
        vendor_name_normalized="alpha_electronics",
        mpn_matched="STM32F103",
        qty_available=2000,
        unit_price=3.2500,
        source_type="nexar",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(sighting)
    db_session.flush()

    lead = SourcingLead(
        lead_id="ld_ws_001",
        requirement_id=requirement.id,
        requisition_id=req.id,
        part_number_requested="STM32F103",
        part_number_matched="STM32F103",
        vendor_name="Alpha Electronics",
        vendor_name_normalized="alpha_electronics",
        primary_source_type="nexar",
        primary_source_name="Nexar",
        confidence_score=85.0,
        confidence_band="high",
        vendor_safety_score=75.0,
        vendor_safety_band="low_risk",
        vendor_safety_summary="Established distributor.",
        contact_email="sales@alpha.com",
        buyer_status="new",
        evidence_count=1,
        corroborated=False,
        reason_summary="High confidence nexar lead",
        source_last_seen_at=datetime.now(timezone.utc),
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(lead)
    db_session.flush()

    evidence = LeadEvidence(
        evidence_id="ev_ws_001",
        lead_id=lead.id,
        signal_type="stock_listing",
        source_type="nexar",
        source_name="Nexar",
        explanation="Nexar API shows stock at Alpha Electronics",
        confidence_impact=17.0,
        verification_state="raw",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(evidence)
    db_session.commit()

    return {"requirement": requirement, "lead": lead, "sighting": sighting}


# ── Workspace Page Tests ─────────────────────────────────────────────


def test_workspace_page_returns_200(client, workspace_data):
    """GET /v2/sourcing/{id}/workspace returns 200 with base page."""
    req_id = workspace_data["requirement"].id
    resp = client.get(f"/v2/sourcing/{req_id}/workspace")
    assert resp.status_code == 200


def test_workspace_partial_returns_split_panel(client, workspace_data):
    """GET /v2/partials/sourcing/{id}/workspace returns workspace layout."""
    req_id = workspace_data["requirement"].id
    resp = client.get(f"/v2/partials/sourcing/{req_id}/workspace")
    assert resp.status_code == 200
    assert "split-right-sourcing" in resp.text
    assert "split-left-sourcing" in resp.text
    assert "lead-row-" in resp.text
    assert "Alpha Electronics" in resp.text


def test_workspace_partial_not_found(client):
    """GET /v2/partials/sourcing/99999/workspace returns 404."""
    resp = client.get("/v2/partials/sourcing/99999/workspace")
    assert resp.status_code == 404


def test_workspace_partial_has_filters(client, workspace_data):
    """Workspace partial includes filter bar."""
    req_id = workspace_data["requirement"].id
    resp = client.get(f"/v2/partials/sourcing/{req_id}/workspace")
    assert resp.status_code == 200
    assert "sourcing-filters" in resp.text
    assert "Confidence" in resp.text


def test_workspace_partial_has_keyboard_nav(client, workspace_data):
    """Workspace includes Alpine keyboard navigation component."""
    req_id = workspace_data["requirement"].id
    resp = client.get(f"/v2/partials/sourcing/{req_id}/workspace")
    assert "sourcingWorkspace" in resp.text
    assert "selectNext" in resp.text


# ── Lead Panel Tests ─────────────────────────────────────────────────


def test_lead_panel_returns_200(client, workspace_data):
    """GET /v2/partials/sourcing/leads/{id}/panel returns condensed detail."""
    lead_id = workspace_data["lead"].id
    resp = client.get(f"/v2/partials/sourcing/leads/{lead_id}/panel")
    assert resp.status_code == 200
    assert "Alpha Electronics" in resp.text
    assert "STM32F103" in resp.text


def test_lead_panel_not_found(client):
    """GET /v2/partials/sourcing/leads/99999/panel returns 404."""
    resp = client.get("/v2/partials/sourcing/leads/99999/panel")
    assert resp.status_code == 404


def test_lead_panel_has_no_breadcrumb(client, workspace_data):
    """Panel detail does not include breadcrumb navigation."""
    lead_id = workspace_data["lead"].id
    resp = client.get(f"/v2/partials/sourcing/leads/{lead_id}/panel")
    assert resp.status_code == 200
    # Should not have breadcrumb div (that's in the full-page detail only)
    assert 'id="breadcrumb"' not in resp.text or "hx-swap-oob" in resp.text


def test_lead_panel_has_evidence(client, workspace_data):
    """Panel includes evidence section."""
    lead_id = workspace_data["lead"].id
    resp = client.get(f"/v2/partials/sourcing/leads/{lead_id}/panel")
    assert "Evidence" in resp.text
    assert "Nexar API shows stock" in resp.text


def test_lead_panel_has_contact(client, workspace_data):
    """Panel includes contact information."""
    lead_id = workspace_data["lead"].id
    resp = client.get(f"/v2/partials/sourcing/leads/{lead_id}/panel")
    assert "sales@alpha.com" in resp.text


def test_lead_panel_has_buyer_actions(client, workspace_data):
    """Panel includes inline buyer action form."""
    lead_id = workspace_data["lead"].id
    resp = client.get(f"/v2/partials/sourcing/leads/{lead_id}/panel")
    assert 'name="status"' in resp.text
    assert "Update" in resp.text


def test_lead_panel_has_collapsible_sections(client, workspace_data):
    """Panel uses Alpine collapse for sections."""
    lead_id = workspace_data["lead"].id
    resp = client.get(f"/v2/partials/sourcing/leads/{lead_id}/panel")
    assert "x-collapse" in resp.text


def test_lead_panel_includes_oob_row(client, workspace_data):
    """Panel includes OOB swap of lead row for highlight."""
    lead_id = workspace_data["lead"].id
    resp = client.get(f"/v2/partials/sourcing/leads/{lead_id}/panel")
    assert f"lead-row-{lead_id}" in resp.text
    assert "hx-swap-oob" in resp.text


# ── Status Update Routing Tests ──────────────────────────────────────


def test_status_update_workspace_panel_target(client, workspace_data):
    """Status update with HX-Target=split-right-sourcing returns panel."""
    lead_id = workspace_data["lead"].id
    resp = client.post(
        f"/v2/partials/sourcing/leads/{lead_id}/status",
        data={"status": "contacted"},
        headers={"HX-Target": "split-right-sourcing"},
    )
    assert resp.status_code == 200
    # Should return panel template (has collapsible sections)
    assert "x-collapse" in resp.text


def test_status_update_lead_row_target(client, workspace_data):
    """Status update with HX-Target=lead-row-{id} returns lead row."""
    lead_id = workspace_data["lead"].id
    resp = client.post(
        f"/v2/partials/sourcing/leads/{lead_id}/status",
        data={"status": "contacted"},
        headers={"HX-Target": f"lead-row-{lead_id}"},
    )
    assert resp.status_code == 200
    assert f"lead-row-{lead_id}" in resp.text
    # Should NOT have collapsible sections (that's the panel, not the row)
    assert "x-collapse" not in resp.text


# ── Workspace List Partial Tests ─────────────────────────────────────


def test_workspace_list_partial(client, workspace_data):
    """GET /v2/partials/sourcing/{id}/workspace-list returns lead rows."""
    req_id = workspace_data["requirement"].id
    resp = client.get(f"/v2/partials/sourcing/{req_id}/workspace-list")
    assert resp.status_code == 200
    assert "lead-row-" in resp.text
    assert "Alpha Electronics" in resp.text


def test_workspace_list_filter_confidence(client, workspace_data):
    """Confidence filter works on workspace list."""
    req_id = workspace_data["requirement"].id
    # Lead has confidence_band="high"
    resp = client.get(f"/v2/partials/sourcing/{req_id}/workspace-list?confidence=low")
    assert resp.status_code == 200
    assert "lead-row-" not in resp.text

    resp = client.get(f"/v2/partials/sourcing/{req_id}/workspace-list?confidence=high")
    assert resp.status_code == 200
    assert "lead-row-" in resp.text


# ── Results View Link Tests ──────────────────────────────────────────


def test_results_has_workspace_link(client, workspace_data):
    """Results view includes link to workspace view."""
    req_id = workspace_data["requirement"].id
    resp = client.get(f"/v2/partials/sourcing/{req_id}")
    assert resp.status_code == 200
    assert "Workspace view" in resp.text
    assert f"/workspace" in resp.text
