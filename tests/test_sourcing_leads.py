"""Tests for canonical sourcing lead flow (lead/evidence/status/feedback)."""

from datetime import datetime, timezone

from app.models import LeadEvidence, LeadFeedbackEvent, Sighting, SourcingLead
from app.services.sourcing_leads import sync_leads_for_sightings, update_lead_status


def _make_sighting(db_session, requirement_id: int, *, source_type: str, vendor: str = "Arrow Electronics") -> Sighting:
    s = Sighting(
        requirement_id=requirement_id,
        vendor_name=vendor,
        vendor_name_normalized=vendor.lower(),
        mpn_matched="LM317T",
        source_type=source_type,
        qty_available=1200,
        unit_price=0.41,
        confidence=0.8,
        score=82.0,
        raw_data={"vendor_url": f"https://example.com/{source_type}/LM317T", "description": "Live stock listing"},
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(s)
    db_session.commit()
    db_session.refresh(s)
    return s


def test_sync_sightings_creates_one_lead_with_multiple_evidence(db_session, test_requisition):
    requirement = test_requisition.requirements[0]
    s1 = _make_sighting(db_session, requirement.id, source_type="digikey")
    s2 = _make_sighting(db_session, requirement.id, source_type="brokerbin")

    synced = sync_leads_for_sightings(db_session, requirement, [s1, s2])
    assert synced == 2

    leads = db_session.query(SourcingLead).filter(SourcingLead.requirement_id == requirement.id).all()
    assert len(leads) == 1
    lead = leads[0]
    assert lead.evidence_count == 2
    assert lead.corroborated is True

    ev_count = db_session.query(LeadEvidence).filter(LeadEvidence.lead_id == lead.id).count()
    assert ev_count == 2


def test_update_lead_status_writes_feedback_event(db_session, test_requisition):
    requirement = test_requisition.requirements[0]
    s1 = _make_sighting(db_session, requirement.id, source_type="digikey", vendor="Future Electronics")
    sync_leads_for_sightings(db_session, requirement, [s1])
    lead = db_session.query(SourcingLead).filter(SourcingLead.requirement_id == requirement.id).first()
    assert lead is not None

    old_conf = lead.confidence_score
    updated = update_lead_status(db_session, lead.id, "has_stock", note="Vendor confirmed 1,200 pcs", actor_user_id=None)
    assert updated is not None
    assert updated.buyer_status == "has_stock"
    assert updated.confidence_score >= old_conf

    events = db_session.query(LeadFeedbackEvent).filter(LeadFeedbackEvent.lead_id == lead.id).all()
    assert len(events) == 1
    assert events[0].status == "has_stock"


def test_lead_endpoints_return_status_and_feedback(client, db_session, test_requisition):
    requirement = test_requisition.requirements[0]
    s1 = _make_sighting(db_session, requirement.id, source_type="digikey", vendor="Avnet")
    sync_leads_for_sightings(db_session, requirement, [s1])
    lead = db_session.query(SourcingLead).filter(SourcingLead.requirement_id == requirement.id).first()
    assert lead is not None

    patch_resp = client.patch(f"/api/leads/{lead.id}/status", json={"status": "contacted"})
    assert patch_resp.status_code == 200
    assert patch_resp.json()["status"] == "contacted"

    fb_resp = client.post(
        f"/api/leads/{lead.id}/feedback",
        json={"note": "Called sales line, awaiting email reply", "contact_method": "phone"},
    )
    assert fb_resp.status_code == 200

    leads_resp = client.get(f"/api/requisitions/{test_requisition.id}/leads")
    assert leads_resp.status_code == 200
    data = leads_resp.json()
    assert data
    assert data[0]["buyer_status"] == "contacted"

    sightings_resp = client.get(f"/api/requisitions/{test_requisition.id}/sightings")
    assert sightings_resp.status_code == 200
    payload = sightings_resp.json().get(str(requirement.id), {})
    rows = payload.get("sightings", [])
    assert any(row.get("lead_id") == lead.id for row in rows)


def test_lead_detail_endpoint_returns_evidence_and_feedback(client, db_session, test_requisition):
    """GET /api/leads/{id} returns full lead detail with evidence and feedback."""
    requirement = test_requisition.requirements[0]
    s1 = _make_sighting(db_session, requirement.id, source_type="digikey", vendor="Detail Test Vendor")
    sync_leads_for_sightings(db_session, requirement, [s1])
    lead = db_session.query(SourcingLead).filter(SourcingLead.requirement_id == requirement.id).first()
    assert lead is not None

    # Add a status change to create a feedback event
    client.patch(f"/api/leads/{lead.id}/status", json={"status": "contacted", "note": "Called vendor"})

    resp = client.get(f"/api/leads/{lead.id}")
    assert resp.status_code == 200
    data = resp.json()

    # Core fields
    assert data["vendor_name"] == "Detail Test Vendor"
    assert data["confidence_band"] in ("high", "medium", "low")
    assert data["buyer_status"] == "contacted"

    # Safety fields
    assert "vendor_safety_band" in data
    assert "vendor_safety_flags" in data

    # Contact fields
    assert "contact_email" in data

    # Evidence
    assert len(data["evidence"]) >= 1
    ev = data["evidence"][0]
    assert ev["source_type"] == "digikey"

    # Feedback history
    assert len(data["feedback_events"]) >= 1
    assert data["feedback_events"][0]["status"] == "contacted"


def test_resync_preserves_buyer_status(db_session, test_requisition):
    """Re-syncing sightings must not overwrite a manually set buyer status."""
    requirement = test_requisition.requirements[0]
    s1 = _make_sighting(db_session, requirement.id, source_type="digikey")
    sync_leads_for_sightings(db_session, requirement, [s1])

    lead = db_session.query(SourcingLead).filter(SourcingLead.requirement_id == requirement.id).first()
    assert lead is not None
    assert lead.buyer_status == "new"

    update_lead_status(db_session, lead.id, "contacted", note="Called vendor")
    db_session.refresh(lead)
    assert lead.buyer_status == "contacted"

    # Re-sync with a new sighting from a different source — status must stay "contacted"
    s2 = _make_sighting(db_session, requirement.id, source_type="brokerbin")
    sync_leads_for_sightings(db_session, requirement, [s2])
    db_session.refresh(lead)
    assert lead.buyer_status == "contacted", "Re-sync should not overwrite manual buyer status"
