"""Tests for sourcing lead projection (one lead per vendor+part)."""

from datetime import datetime, timedelta, timezone

from app.models import Contact, Requirement, Requisition, User, VendorCard, VendorResponse
from app.services.sourcing_lead_engine import build_requirement_lead_cards
from app.vendor_utils import normalize_vendor_name


def _mk_user(db_session) -> User:
    user = User(
        email="lead-test@trioscs.com",
        name="Lead Test",
        role="buyer",
        azure_id="lead-test-001",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(user)
    db_session.flush()
    return user


def _mk_requirement(db_session) -> Requirement:
    user = _mk_user(db_session)
    req = Requisition(
        name="Lead Test Req",
        customer_name="Lead Customer",
        status="active",
        created_by=user.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(req)
    db_session.flush()
    requirement = Requirement(
        requisition_id=req.id,
        primary_mpn="LM317T",
        normalized_mpn="lm317t",
        target_qty=100,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(requirement)
    db_session.commit()
    db_session.refresh(requirement)
    return requirement


def test_builds_one_lead_per_vendor_per_part(db_session):
    requirement = _mk_requirement(db_session)

    vendor_name = "Digi-Key Electronics"
    db_session.add(
        VendorCard(
            normalized_name=normalize_vendor_name(vendor_name),
            display_name=vendor_name,
            domain="digikey.com",
            emails=["sales@digikey.com"],
            phones=["+1-800-000-0000"],
            vendor_score=82,
            is_blacklisted=False,
            created_at=datetime.now(timezone.utc),
        )
    )
    db_session.commit()

    now = datetime.now(timezone.utc)
    sightings = [
        {
            "id": 1,
            "vendor_name": vendor_name,
            "mpn_matched": "LM317T",
            "source_type": "digikey",
            "confidence_pct": 88,
            "score": 85.0,
            "qty_available": 1000,
            "unit_price": 0.45,
            "currency": "USD",
            "vendor_email": "sales@digikey.com",
            "created_at": now.isoformat(),
        },
        {
            "id": 2,
            "vendor_name": vendor_name,
            "mpn_matched": "LM317T",
            "source_type": "ai_live_web",
            "confidence_pct": 53,
            "score": 40.0,
            "qty_available": 900,
            "unit_price": 0.49,
            "currency": "USD",
            "reasoning": "Recent listing with explicit stock signal.",
            "created_at": (now - timedelta(days=1)).isoformat(),
        },
        {
            "id": 3,
            "vendor_name": "Broker House",
            "mpn_matched": "LM317T",
            "source_type": "brokerbin",
            "confidence_pct": 71,
            "score": 67.0,
            "qty_available": 350,
            "unit_price": 0.47,
            "currency": "USD",
            "created_at": now.isoformat(),
        },
    ]

    leads = build_requirement_lead_cards(requirement, sightings, db_session)
    assert len(leads) == 2

    digi = next(lead for lead in leads if lead["vendor_name"] == vendor_name)
    assert digi["part_requested"] == "LM317T"
    assert digi["lead_confidence_pct"] >= 88
    assert digi["vendor_safety_pct"] >= 60
    assert set(digi["source_attribution"]) == {"digikey", "ai_live_web"}
    assert len(digi["evidence"]) == 2
    assert digi["buyer_status"] == "New"
    assert digi["suggested_next_action"]


def test_status_mapping_includes_no_stock_and_dnc(db_session):
    requirement = _mk_requirement(db_session)

    ok_vendor = "Signal Vendor"
    blocked_vendor = "Blocked Vendor"
    db_session.add_all(
        [
            VendorCard(
                normalized_name=normalize_vendor_name(ok_vendor),
                display_name=ok_vendor,
                emails=["sales@signalvendor.com"],
                vendor_score=70,
                is_blacklisted=False,
                created_at=datetime.now(timezone.utc),
            ),
            VendorCard(
                normalized_name=normalize_vendor_name(blocked_vendor),
                display_name=blocked_vendor,
                vendor_score=40,
                is_blacklisted=True,
                created_at=datetime.now(timezone.utc),
            ),
        ]
    )
    db_session.flush()

    db_session.add(
        Contact(
            requisition_id=requirement.requisition_id,
            user_id=1,
            contact_type="email",
            vendor_name=ok_vendor,
            vendor_name_normalized=normalize_vendor_name(ok_vendor),
            vendor_contact="sales@signalvendor.com",
            status="sent",
            created_at=datetime.now(timezone.utc),
        )
    )
    db_session.add(
        VendorResponse(
            requisition_id=requirement.requisition_id,
            vendor_name=ok_vendor,
            classification="no_stock",
            status="new",
            confidence=0.9,
            created_at=datetime.now(timezone.utc),
            received_at=datetime.now(timezone.utc),
        )
    )
    db_session.commit()

    now = datetime.now(timezone.utc).isoformat()
    sightings = [
        {
            "id": 11,
            "vendor_name": ok_vendor,
            "mpn_matched": "LM317T",
            "source_type": "brokerbin",
            "confidence_pct": 74,
            "score": 65.0,
            "qty_available": 100,
            "unit_price": 0.51,
            "created_at": now,
        },
        {
            "id": 12,
            "vendor_name": blocked_vendor,
            "mpn_matched": "LM317T",
            "source_type": "vendor_affinity",
            "confidence_pct": 49,
            "score": 20.0,
            "created_at": now,
        },
    ]

    leads = build_requirement_lead_cards(requirement, sightings, db_session)
    signal_vendor = next(lead for lead in leads if lead["vendor_name"] == ok_vendor)
    blocked = next(lead for lead in leads if lead["vendor_name"] == blocked_vendor)

    assert signal_vendor["buyer_status"] == "No Stock"
    assert blocked["buyer_status"] == "Do Not Contact"
    assert "do_not_contact_flag" in blocked["risk_flags"]


def test_requirement_sightings_endpoint_returns_lead_cards(client, db_session, test_user):
    req = Requisition(
        name="Lead API Req",
        customer_name="Customer",
        status="active",
        created_by=test_user.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(req)
    db_session.flush()

    requirement = Requirement(
        requisition_id=req.id,
        primary_mpn="LM358N",
        normalized_mpn="lm358n",
        target_qty=50,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(requirement)
    db_session.flush()

    vendor_name = "Merged Vendor"
    vendor_norm = normalize_vendor_name(vendor_name)
    db_session.add(
        VendorCard(
            normalized_name=vendor_norm,
            display_name=vendor_name,
            vendor_score=78,
            emails=["quote@mergedvendor.com"],
            created_at=datetime.now(timezone.utc),
        )
    )
    db_session.flush()

    from app.models import Sighting

    db_session.add_all(
        [
            Sighting(
                requirement_id=requirement.id,
                vendor_name=vendor_name,
                vendor_name_normalized=vendor_norm,
                mpn_matched="LM358N",
                normalized_mpn="lm358n",
                source_type="brokerbin",
                qty_available=500,
                unit_price=0.22,
                confidence=0.8,
                score=80.0,
                created_at=datetime.now(timezone.utc),
            ),
            Sighting(
                requirement_id=requirement.id,
                vendor_name=vendor_name,
                vendor_name_normalized=vendor_norm,
                mpn_matched="LM358N",
                normalized_mpn="lm358n",
                source_type="ai_live_web",
                qty_available=450,
                unit_price=0.24,
                confidence=0.5,
                score=45.0,
                created_at=datetime.now(timezone.utc),
            ),
        ]
    )
    db_session.commit()

    resp = client.get(f"/api/requirements/{requirement.id}/sightings")
    assert resp.status_code == 200
    payload = resp.json()
    assert "lead_cards" in payload
    assert payload["lead_summary"]["total_leads"] == 1
    assert len(payload["lead_cards"]) == 1
    assert payload["lead_cards"][0]["vendor_name"] == vendor_name
