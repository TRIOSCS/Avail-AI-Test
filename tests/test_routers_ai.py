"""
test_routers_ai.py — Tests for AI Intelligence Layer Router

Tests _ai_enabled gate, _build_vendor_history helper, contact enrichment,
prospect management, response parsing, company intel, and RFQ drafting.

Covers: ai feature flag modes (off/mike_only/all), vendor history aggregation,
        all /api/ai/* HTTP endpoints
"""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# _ai_enabled tests
# ---------------------------------------------------------------------------


@pytest.fixture
def mike_user():
    u = SimpleNamespace(email="mike@trioscs.com", id=1, name="Mike", role="admin")
    return u


@pytest.fixture
def other_user():
    u = SimpleNamespace(email="buyer@trioscs.com", id=2, name="Buyer", role="buyer")
    return u


def _make_settings(flag: str, admin_emails: list[str] | None = None):
    s = SimpleNamespace(
        ai_features_enabled=flag,
        admin_emails=admin_emails or ["mike@trioscs.com"],
    )
    return s


def test_ai_enabled_off(mike_user):
    with patch("app.routers.ai.settings", _make_settings("off")):
        from app.routers.ai import _ai_enabled

        assert _ai_enabled(mike_user) is False


def test_ai_enabled_all(other_user):
    with patch("app.routers.ai.settings", _make_settings("all")):
        from app.routers.ai import _ai_enabled

        assert _ai_enabled(other_user) is True


def test_ai_enabled_mike_only_allows_mike(mike_user):
    mock_settings = _make_settings("mike_only")
    with patch("app.routers.ai.settings", mock_settings):
        from app.routers.ai import _ai_enabled

        assert _ai_enabled(mike_user) is True


def test_ai_enabled_mike_only_allows_all(other_user):
    mock_settings = _make_settings("mike_only")
    with patch("app.routers.ai.settings", mock_settings):
        from app.routers.ai import _ai_enabled

        assert _ai_enabled(other_user) is True


def test_ai_enabled_mike_only_case_insensitive():
    user = SimpleNamespace(email="MIKE@TRIOSCS.COM", id=1, name="Mike", role="admin")
    mock_settings = _make_settings("mike_only")
    with patch("app.routers.ai.settings", mock_settings):
        from app.routers.ai import _ai_enabled

        assert _ai_enabled(user) is True


# ---------------------------------------------------------------------------
# _build_vendor_history tests
# ---------------------------------------------------------------------------


def test_build_vendor_history_no_card():
    """Unknown vendor returns empty dict."""
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = None

    with patch("app.routers.ai.normalize_vendor_name", return_value="acme"):
        from app.routers.ai import _build_vendor_history

        result = _build_vendor_history("Acme Corp", db)
    assert result == {}


def test_build_vendor_history_with_card():
    """Known vendor returns aggregated stats."""
    card = SimpleNamespace(
        engagement_score=78.5,
        response_velocity_hours=4.2,
    )
    last_contact = SimpleNamespace(
        created_at=datetime(2026, 2, 10, tzinfo=timezone.utc),
    )

    db = MagicMock()
    # first query().filter().first() = card
    # second query(func.count()).filter().scalar() = rfq count
    # third query(func.count()).filter().scalar() = offer count
    # fourth query(func.max()).filter().scalar() = last contact date
    call_results = iter([card, 15, 3, last_contact.created_at])

    def side_effect(*a, **kw):
        mock = MagicMock()
        val = next(call_results)
        if isinstance(val, int):
            mock.filter.return_value.scalar.return_value = val
        elif isinstance(val, datetime):
            mock.filter.return_value.scalar.return_value = val
        else:
            mock.filter.return_value.first.return_value = val
        return mock

    db.query.side_effect = side_effect

    with patch("app.routers.ai.normalize_vendor_name", return_value="acme"):
        from app.routers.ai import _build_vendor_history

        result = _build_vendor_history("Acme Corp", db)

    assert result["total_rfqs"] == 15
    assert result["total_offers"] == 3
    assert result["last_contact_date"] == "2026-02-10"
    assert result["engagement_score"] == 78.5


# ---------------------------------------------------------------------------
# Fixtures for HTTP endpoint tests
# ---------------------------------------------------------------------------


@pytest.fixture()
def ai_test_user(db_session):
    """Buyer user for AI endpoint tests (distinct from conftest test_user)."""
    from app.models import User

    user = User(
        email="testbuyer@trioscs.com",
        name="Test Buyer",
        role="buyer",
        azure_id="test-001",
        m365_connected=True,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture()
def ai_client(db_session, ai_test_user):
    """TestClient with AI features enabled."""
    from app.database import get_db
    from app.dependencies import require_buyer, require_user
    from app.main import app

    def _override_db():
        yield db_session

    def _override_user():
        return ai_test_user

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[require_user] = _override_user
    app.dependency_overrides[require_buyer] = _override_user

    with TestClient(app) as c:
        yield c

    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Contact Enrichment (5 tests)
# ---------------------------------------------------------------------------


def test_find_contacts_ai_disabled(ai_client):
    """POST /api/ai/find-contacts with AI off returns 403."""
    with patch("app.routers.ai._ai_enabled", return_value=False):
        resp = ai_client.post(
            "/api/ai/find-contacts",
            json={
                "entity_type": "vendor",
                "entity_id": 1,
            },
        )
    assert resp.status_code == 403
    assert "not enabled" in resp.json()["error"].lower()


def test_find_contacts_vendor_entity(ai_client, db_session):
    """Find contacts for a vendor entity via Apollo mock."""
    from app.models import VendorCard

    card = VendorCard(
        normalized_name="acme supply",
        display_name="Acme Supply",
        domain="acmesupply.com",
    )
    db_session.add(card)
    db_session.commit()
    db_session.refresh(card)

    apollo_results = [
        {
            "full_name": "Alice Smith",
            "title": "Sales Manager",
            "email": "alice@acmesupply.com",
            "email_status": "valid",
            "phone": "+1-555-1234",
            "linkedin_url": "https://linkedin.com/in/alice",
            "source": "apollo",
            "confidence": "high",
        },
    ]

    mock_search = AsyncMock(return_value=apollo_results)
    mock_web = AsyncMock(return_value=[])

    with (
        patch("app.routers.ai._ai_enabled", return_value=True),
        patch("app.connectors.apollo_client.search_contacts", mock_search),
        patch("app.services.ai_service.enrich_contacts_websearch", mock_web),
    ):
        resp = ai_client.post(
            "/api/ai/find-contacts",
            json={
                "entity_type": "vendor",
                "entity_id": card.id,
            },
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    assert data["contacts"][0]["full_name"] == "Alice Smith"
    assert len(data["saved_ids"]) == 1


def test_find_contacts_company_entity(ai_client, db_session):
    """Find contacts for a company site entity."""
    from app.models import Company, CustomerSite

    co = Company(name="Beta Corp", domain="betacorp.com", is_active=True)
    db_session.add(co)
    db_session.flush()

    site = CustomerSite(company_id=co.id, site_name="Beta HQ")
    db_session.add(site)
    db_session.commit()
    db_session.refresh(site)

    apollo_results = [
        {
            "full_name": "Bob Jones",
            "title": "Procurement Lead",
            "email": "bob@betacorp.com",
            "source": "apollo",
            "confidence": "medium",
        },
    ]

    mock_search = AsyncMock(return_value=apollo_results)
    mock_web = AsyncMock(return_value=[])

    with (
        patch("app.routers.ai._ai_enabled", return_value=True),
        patch("app.connectors.apollo_client.search_contacts", mock_search),
        patch("app.services.ai_service.enrich_contacts_websearch", mock_web),
    ):
        resp = ai_client.post(
            "/api/ai/find-contacts",
            json={
                "entity_type": "company",
                "entity_id": site.id,
            },
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    assert data["contacts"][0]["full_name"] == "Bob Jones"


def test_find_contacts_no_entity(ai_client):
    """POST /api/ai/find-contacts with no entity_id returns 400."""
    with patch("app.routers.ai._ai_enabled", return_value=True):
        resp = ai_client.post(
            "/api/ai/find-contacts",
            json={
                "entity_type": "vendor",
                "entity_id": None,
            },
        )
    assert resp.status_code == 400


def test_list_prospects(ai_client, db_session):
    """GET /api/ai/prospect-contacts returns prospect list for vendor."""
    from app.models import ProspectContact, VendorCard

    card = VendorCard(
        normalized_name="delta parts",
        display_name="Delta Parts",
    )
    db_session.add(card)
    db_session.flush()

    pc1 = ProspectContact(
        vendor_card_id=card.id,
        full_name="Carol White",
        title="VP Sales",
        email="carol@deltaparts.com",
        source="apollo",
        confidence="high",
    )
    pc2 = ProspectContact(
        vendor_card_id=card.id,
        full_name="Dave Brown",
        title="Account Manager",
        email="dave@deltaparts.com",
        source="web_search",
        confidence="medium",
    )
    db_session.add_all([pc1, pc2])
    db_session.commit()

    resp = ai_client.get(
        "/api/ai/prospect-contacts",
        params={"entity_type": "vendor", "entity_id": card.id},
    )

    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 2
    names = {c["full_name"] for c in data}
    assert "Carol White" in names
    assert "Dave Brown" in names


# ---------------------------------------------------------------------------
# Prospect Management (4 tests)
# ---------------------------------------------------------------------------


def test_save_prospect(ai_client, db_session, ai_test_user):
    """POST /api/ai/prospect-contacts/{id}/save marks contact as saved."""
    from app.models import ProspectContact

    pc = ProspectContact(
        full_name="Eve Green",
        title="Buyer",
        email="eve@example.com",
        source="apollo",
        confidence="high",
    )
    db_session.add(pc)
    db_session.commit()
    db_session.refresh(pc)

    resp = ai_client.post(f"/api/ai/prospect-contacts/{pc.id}/save")

    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["id"] == pc.id
    assert data["contact"]["full_name"] == "Eve Green"

    db_session.refresh(pc)
    assert pc.is_saved is True
    assert pc.saved_by_id == ai_test_user.id


def test_save_prospect_not_found(ai_client):
    """POST /api/ai/prospect-contacts/99999/save returns 404."""
    resp = ai_client.post("/api/ai/prospect-contacts/99999/save")
    assert resp.status_code == 404


def test_delete_prospect(ai_client, db_session):
    """DELETE /api/ai/prospect-contacts/{id} removes the contact."""
    from app.models import ProspectContact

    pc = ProspectContact(
        full_name="Frank Black",
        title="Engineer",
        email="frank@example.com",
        source="web_search",
        confidence="low",
    )
    db_session.add(pc)
    db_session.commit()
    db_session.refresh(pc)
    pc_id = pc.id

    resp = ai_client.delete(f"/api/ai/prospect-contacts/{pc_id}")

    assert resp.status_code == 200
    assert resp.json()["ok"] is True

    assert db_session.get(ProspectContact, pc_id) is None


def test_delete_prospect_not_found(ai_client):
    """DELETE /api/ai/prospect-contacts/99999 returns 404."""
    resp = ai_client.delete("/api/ai/prospect-contacts/99999")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Prospect Promotion (4 tests)
# ---------------------------------------------------------------------------


def test_promote_to_vendor_contact(ai_client, db_session, ai_test_user):
    """POST /api/ai/prospect-contacts/{id}/promote creates VendorContact."""
    from app.models import ProspectContact, VendorCard, VendorContact

    card = VendorCard(normalized_name="promo vendor", display_name="Promo Vendor")
    db_session.add(card)
    db_session.flush()

    pc = ProspectContact(
        vendor_card_id=card.id,
        full_name="Jane Promo",
        title="Sales Director",
        email="jane@promo.com",
        phone="555-1234",
        source="apollo",
        confidence="high",
    )
    db_session.add(pc)
    db_session.commit()
    db_session.refresh(pc)

    resp = ai_client.post(f"/api/ai/prospect-contacts/{pc.id}/promote")
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["promoted_to_type"] == "vendor_contact"
    assert data["promoted_to_id"] is not None

    db_session.refresh(pc)
    assert pc.is_saved is True
    assert pc.saved_by_id == ai_test_user.id
    assert pc.promoted_to_type == "vendor_contact"

    vc = db_session.get(VendorContact, data["promoted_to_id"])
    assert vc is not None
    assert vc.full_name == "Jane Promo"
    assert vc.email == "jane@promo.com"
    assert vc.source == "prospect_promote"


def test_promote_to_site_contact(ai_client, db_session, ai_test_user):
    """POST /api/ai/prospect-contacts/{id}/promote creates SiteContact."""
    from app.models import Company, CustomerSite, ProspectContact, SiteContact

    co = Company(name="Promo Co")
    db_session.add(co)
    db_session.flush()
    site = CustomerSite(company_id=co.id, site_name="HQ")
    db_session.add(site)
    db_session.flush()

    pc = ProspectContact(
        customer_site_id=site.id,
        full_name="Bob Site",
        title="Buyer",
        email="bob@promoco.com",
        source="web_search",
        confidence="medium",
    )
    db_session.add(pc)
    db_session.commit()
    db_session.refresh(pc)

    resp = ai_client.post(f"/api/ai/prospect-contacts/{pc.id}/promote")
    assert resp.status_code == 200
    data = resp.json()
    assert data["promoted_to_type"] == "site_contact"

    sc = db_session.get(SiteContact, data["promoted_to_id"])
    assert sc is not None
    assert sc.full_name == "Bob Site"
    assert sc.customer_site_id == site.id


def test_promote_no_fk_returns_400(ai_client, db_session):
    """POST promote with no vendor_card_id or customer_site_id returns 400."""
    from app.models import ProspectContact

    pc = ProspectContact(
        full_name="Orphan Contact",
        source="manual",
        confidence="low",
    )
    db_session.add(pc)
    db_session.commit()
    db_session.refresh(pc)

    resp = ai_client.post(f"/api/ai/prospect-contacts/{pc.id}/promote")
    assert resp.status_code == 400


def test_promote_not_found(ai_client):
    """POST /api/ai/prospect-contacts/99999/promote returns 404."""
    resp = ai_client.post("/api/ai/prospect-contacts/99999/promote")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Parse Response (4 tests)
# ---------------------------------------------------------------------------


def test_parse_response_ai_disabled(ai_client):
    """POST /api/ai/parse-response/{id} with AI off returns 403."""
    with patch("app.routers.ai._ai_enabled", return_value=False):
        resp = ai_client.post("/api/ai/parse-response/1")
    assert resp.status_code == 403


def test_parse_response_not_found(ai_client):
    """POST /api/ai/parse-response/99999 returns 404."""
    with patch("app.routers.ai._ai_enabled", return_value=True):
        resp = ai_client.post("/api/ai/parse-response/99999")
    assert resp.status_code == 404


def test_parse_response_success(ai_client, db_session):
    """POST /api/ai/parse-response/{id} parses and returns structured offers."""
    from app.models import Requisition, VendorResponse

    req = Requisition(
        name="REQ-AI-001",
        customer_name="Test Customer",
        status="open",
        created_by=1,
    )
    db_session.add(req)
    db_session.flush()

    vr = VendorResponse(
        requisition_id=req.id,
        vendor_name="Acme Vendor",
        vendor_email="vendor@acme.com",
        subject="RE: RFQ LM317T",
        body="We can offer LM317T qty 500 at $0.45 each.",
        received_at=datetime.now(timezone.utc),
    )
    db_session.add(vr)
    db_session.commit()
    db_session.refresh(vr)

    parse_result = {
        "overall_classification": "quote_provided",
        "confidence": 0.92,
        "parts": [{"mpn": "LM317T", "qty": 500, "unit_price": 0.45}],
        "vendor_notes": "Standard lead time 2 weeks",
    }

    mock_parse = AsyncMock(return_value=parse_result)
    mock_extract = MagicMock(
        return_value=[{"vendor_name": "Acme Vendor", "mpn": "LM317T", "qty_available": 500, "unit_price": 0.45}]
    )
    mock_auto = MagicMock(return_value=True)
    mock_review = MagicMock(return_value=False)

    with (
        patch("app.routers.ai._ai_enabled", return_value=True),
        patch("app.services.response_parser.parse_vendor_response", mock_parse),
        patch("app.services.response_parser.extract_draft_offers", mock_extract),
        patch("app.services.response_parser.should_auto_apply", mock_auto),
        patch("app.services.response_parser.should_flag_review", mock_review),
    ):
        resp = ai_client.post(f"/api/ai/parse-response/{vr.id}")

    assert resp.status_code == 200
    data = resp.json()
    assert data["parsed"] is True
    assert data["classification"] == "quote_provided"
    assert data["confidence"] == 0.92
    assert data["auto_apply"] is True
    assert data["needs_review"] is False
    assert len(data["draft_offers"]) == 1


def test_save_parsed_offers(ai_client, db_session, ai_test_user):
    """POST /api/ai/save-parsed-offers creates Offer records."""
    from app.models import Offer, Requisition

    req = Requisition(
        name="REQ-AI-002",
        customer_name="Test Customer",
        status="open",
        created_by=ai_test_user.id,
    )
    db_session.add(req)
    db_session.commit()
    db_session.refresh(req)

    payload = {
        "requisition_id": req.id,
        "response_id": None,
        "offers": [
            {
                "vendor_name": "Acme Vendor",
                "mpn": "LM317T",
                "qty_available": 500,
                "unit_price": 0.45,
                "currency": "USD",
            },
            {
                "vendor_name": "Acme Vendor",
                "mpn": "LM7805",
                "qty_available": 1000,
                "unit_price": 0.30,
                "currency": "USD",
            },
        ],
    }

    resp = ai_client.post("/api/ai/save-parsed-offers", json=payload)

    assert resp.status_code == 200
    data = resp.json()
    assert data["created"] == 2
    assert len(data["offer_ids"]) == 2

    offers = db_session.query(Offer).filter(Offer.requisition_id == req.id).all()
    assert len(offers) == 2
    assert all(o.source == "ai_parsed" for o in offers)
    assert all(o.entered_by_id == ai_test_user.id for o in offers)
    assert all(o.status == "pending_review" for o in offers)


# ---------------------------------------------------------------------------
# Intel & RFQ Draft (5 tests)
# ---------------------------------------------------------------------------


def test_company_intel_ai_disabled(ai_client):
    """GET /api/ai/company-intel with AI off returns 403."""
    with patch("app.routers.ai._ai_enabled", return_value=False):
        resp = ai_client.get("/api/ai/company-intel", params={"company_name": "Acme"})
    assert resp.status_code == 403


def test_company_intel_no_name(ai_client):
    """GET /api/ai/company-intel without company_name returns 400."""
    with patch("app.routers.ai._ai_enabled", return_value=True):
        resp = ai_client.get("/api/ai/company-intel")
    assert resp.status_code == 400


def test_company_intel_success(ai_client):
    """GET /api/ai/company-intel returns intel data when available."""
    intel_data = {
        "summary": "Acme Corp is a major electronics distributor.",
        "employee_count": "500-1000",
        "headquarters": "Dallas, TX",
        "specialties": ["semiconductors", "passives"],
    }

    mock_intel = AsyncMock(return_value=intel_data)

    with (
        patch("app.routers.ai._ai_enabled", return_value=True),
        patch("app.services.ai_service.company_intel", mock_intel),
    ):
        resp = ai_client.get(
            "/api/ai/company-intel",
            params={"company_name": "Acme Corp", "domain": "acmecorp.com"},
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["available"] is True
    assert data["intel"]["summary"] == "Acme Corp is a major electronics distributor."
    assert "semiconductors" in data["intel"]["specialties"]
    mock_intel.assert_awaited_once_with("Acme Corp", "acmecorp.com")


def test_draft_rfq_ai_disabled(ai_client):
    """POST /api/ai/draft-rfq with AI off returns 403."""
    with patch("app.routers.ai._ai_enabled", return_value=False):
        resp = ai_client.post(
            "/api/ai/draft-rfq",
            json={
                "vendor_name": "Acme Corp",
                "parts": ["LM317T"],
            },
        )
    assert resp.status_code == 403


def test_draft_rfq_success(ai_client, db_session):
    """POST /api/ai/draft-rfq returns a draft email body."""
    draft_text = (
        "Dear Acme Corp,\n\n"
        "We would like to request a quote for the following parts:\n"
        "- LM317T (1000 pcs)\n\n"
        "Please provide pricing and lead time.\n\n"
        "Best regards,\nTest Buyer"
    )

    mock_draft = AsyncMock(return_value=draft_text)
    mock_history = MagicMock(return_value={"total_rfqs": 5, "engagement_score": 80.0})

    with (
        patch("app.routers.ai._ai_enabled", return_value=True),
        patch("app.routers.ai._build_vendor_history", mock_history),
        patch("app.services.ai_service.draft_rfq", mock_draft),
    ):
        resp = ai_client.post(
            "/api/ai/draft-rfq",
            json={
                "vendor_name": "Acme Corp",
                "parts": ["LM317T"],
            },
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["available"] is True
    assert "LM317T" in data["body"]
    assert "Acme Corp" in data["body"]
    mock_draft.assert_awaited_once()


# ---------------------------------------------------------------------------
# Additional coverage tests
# ---------------------------------------------------------------------------


def test_list_prospects_company_entity(ai_client, db_session):
    """GET /api/ai/prospect-contacts returns prospects for company site."""
    from app.models import Company, CustomerSite, ProspectContact

    co = Company(name="Prospect Co", is_active=True)
    db_session.add(co)
    db_session.flush()

    site = CustomerSite(company_id=co.id, site_name="Prospect HQ")
    db_session.add(site)
    db_session.flush()

    pc = ProspectContact(
        customer_site_id=site.id,
        full_name="Site Contact",
        email="sc@prospect.com",
        source="apollo",
        confidence="high",
    )
    db_session.add(pc)
    db_session.commit()

    resp = ai_client.get(
        "/api/ai/prospect-contacts",
        params={"entity_type": "company", "entity_id": site.id},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) >= 1


def test_list_prospects_no_entity(ai_client):
    """GET /api/ai/prospect-contacts without entity_type returns 400."""
    resp = ai_client.get("/api/ai/prospect-contacts")
    assert resp.status_code == 400


def test_save_prospect_with_notes(ai_client, db_session, ai_test_user):
    """POST /api/ai/prospect-contacts/{id}/save with notes payload."""
    from app.models import ProspectContact

    pc = ProspectContact(
        full_name="Notes Person",
        email="notes@example.com",
        source="web_search",
        confidence="medium",
    )
    db_session.add(pc)
    db_session.commit()
    db_session.refresh(pc)

    resp = ai_client.post(
        f"/api/ai/prospect-contacts/{pc.id}/save",
        json={"notes": "Key contact for procurement"},
    )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    db_session.refresh(pc)
    assert pc.notes == "Key contact for procurement"


def test_parse_response_no_result(ai_client, db_session):
    """Parser returns None -> parsed=False."""
    from app.models import VendorResponse

    vr = VendorResponse(
        vendor_name="Empty Vendor",
        vendor_email="empty@v.com",
        subject="No content",
        body="Out of office",
        received_at=datetime.now(timezone.utc),
    )
    db_session.add(vr)
    db_session.commit()
    db_session.refresh(vr)

    mock_parse = AsyncMock(return_value=None)

    with (
        patch("app.routers.ai._ai_enabled", return_value=True),
        patch("app.services.response_parser.parse_vendor_response", mock_parse),
    ):
        resp = ai_client.post(f"/api/ai/parse-response/{vr.id}")

    assert resp.status_code == 200
    assert resp.json()["parsed"] is False


def test_parse_response_with_rfq_context(ai_client, db_session):
    """Parser uses RFQ context from requisition's requirements."""
    from app.models import Requirement, Requisition, VendorResponse

    req = Requisition(
        name="REQ-CTX",
        customer_name="Test",
        status="open",
        created_by=1,
    )
    db_session.add(req)
    db_session.flush()

    r = Requirement(
        requisition_id=req.id,
        primary_mpn="CTX-PART",
        target_qty=100,
        target_price=1.0,
    )
    db_session.add(r)
    db_session.flush()

    vr = VendorResponse(
        requisition_id=req.id,
        vendor_name="Context Vendor",
        vendor_email="ctx@v.com",
        subject="RE: RFQ",
        body="CTX-PART 100pcs at $0.95",
        received_at=datetime.now(timezone.utc),
    )
    db_session.add(vr)
    db_session.commit()
    db_session.refresh(vr)

    parse_result = {
        "overall_classification": "quote_provided",
        "confidence": 0.88,
        "parts": [{"mpn": "CTX-PART", "qty": 100, "unit_price": 0.95}],
    }

    with (
        patch("app.routers.ai._ai_enabled", return_value=True),
        patch("app.services.response_parser.parse_vendor_response", new_callable=AsyncMock, return_value=parse_result),
        patch("app.services.response_parser.extract_draft_offers", return_value=[]),
        patch("app.services.response_parser.should_auto_apply", return_value=False),
        patch("app.services.response_parser.should_flag_review", return_value=True),
    ):
        resp = ai_client.post(f"/api/ai/parse-response/{vr.id}")

    assert resp.status_code == 200
    assert resp.json()["parsed"] is True


def test_company_intel_not_available(ai_client):
    """GET /api/ai/company-intel returns available=False when intel is None."""
    mock_intel = AsyncMock(return_value=None)

    with (
        patch("app.routers.ai._ai_enabled", return_value=True),
        patch("app.services.ai_service.company_intel", mock_intel),
    ):
        resp = ai_client.get(
            "/api/ai/company-intel",
            params={"company_name": "Unknown Corp"},
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["available"] is False


def test_draft_rfq_not_available(ai_client, db_session):
    """POST /api/ai/draft-rfq returns available=False when draft is None."""
    mock_draft = AsyncMock(return_value=None)

    with (
        patch("app.routers.ai._ai_enabled", return_value=True),
        patch("app.routers.ai._build_vendor_history", return_value={}),
        patch("app.services.ai_service.draft_rfq", mock_draft),
    ):
        resp = ai_client.post(
            "/api/ai/draft-rfq",
            json={
                "vendor_name": "Acme Corp",
                "parts": ["LM317T"],
            },
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["available"] is False


def test_parse_email_ai_disabled(ai_client):
    """POST /api/ai/parse-email with AI off returns 403."""
    with patch("app.routers.ai._ai_enabled", return_value=False):
        resp = ai_client.post(
            "/api/ai/parse-email",
            json={
                "email_body": "We offer LM317T at $0.50",
            },
        )
    assert resp.status_code == 403


def test_parse_email_success(ai_client):
    """POST /api/ai/parse-email parses email into quotes."""
    parse_result = {
        "quotes": [{"part_number": "LM317T", "unit_price": 0.50}],
        "overall_confidence": 0.85,
        "email_type": "quote",
        "vendor_notes": "Standard terms",
    }

    with (
        patch("app.routers.ai._ai_enabled", return_value=True),
        patch("app.services.ai_email_parser.parse_email", new_callable=AsyncMock, return_value=parse_result),
        patch("app.services.ai_email_parser.should_auto_apply", return_value=True),
        patch("app.services.ai_email_parser.should_flag_review", return_value=False),
    ):
        resp = ai_client.post(
            "/api/ai/parse-email",
            json={
                "email_body": "We offer LM317T at $0.50",
                "email_subject": "RE: RFQ",
                "vendor_name": "Test Vendor",
            },
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["parsed"] is True
    assert data["auto_apply"] is True


def test_parse_email_no_result(ai_client):
    """POST /api/ai/parse-email returns parsed=False when parser returns None."""
    with (
        patch("app.routers.ai._ai_enabled", return_value=True),
        patch("app.services.ai_email_parser.parse_email", new_callable=AsyncMock, return_value=None),
    ):
        resp = ai_client.post(
            "/api/ai/parse-email",
            json={
                "email_body": "Out of office auto-reply",
            },
        )

    assert resp.status_code == 200
    assert resp.json()["parsed"] is False


def test_normalize_parts_ai_disabled(ai_client):
    """POST /api/ai/normalize-parts with AI off returns 403."""
    with patch("app.routers.ai._ai_enabled", return_value=False):
        resp = ai_client.post(
            "/api/ai/normalize-parts",
            json={
                "parts": ["LM317T"],
            },
        )
    assert resp.status_code == 403


def test_normalize_parts_success(ai_client):
    """POST /api/ai/normalize-parts returns normalized parts."""
    normalized = [{"original": "LM317T", "normalized": "LM317T", "manufacturer": "TI"}]

    with (
        patch("app.routers.ai._ai_enabled", return_value=True),
        patch("app.services.ai_part_normalizer.normalize_parts", new_callable=AsyncMock, return_value=normalized),
    ):
        resp = ai_client.post(
            "/api/ai/normalize-parts",
            json={
                "parts": ["LM317T"],
            },
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] == 1


def test_enrich_person_ai_disabled(ai_client):
    """POST /api/ai/enrich-person with AI off returns 403."""
    with patch("app.routers.ai._ai_enabled", return_value=False):
        resp = ai_client.post(
            "/api/ai/enrich-person",
            json={
                "email": "test@example.com",
            },
        )
    assert resp.status_code == 403


def test_enrich_person_success(ai_client):
    """POST /api/ai/enrich-person returns enriched person data."""
    person_data = {
        "full_name": "John Doe",
        "title": "VP Sales",
        "email": "john@acme.com",
        "linkedin_url": "https://linkedin.com/in/johndoe",
    }

    with (
        patch("app.routers.ai._ai_enabled", return_value=True),
        patch("app.connectors.apollo_client.enrich_person", new_callable=AsyncMock, return_value=person_data),
    ):
        resp = ai_client.post(
            "/api/ai/enrich-person",
            json={
                "email": "john@acme.com",
            },
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["full_name"] == "John Doe"


def test_enrich_person_no_match(ai_client):
    """POST /api/ai/enrich-person returns 404 when no match found."""
    with (
        patch("app.routers.ai._ai_enabled", return_value=True),
        patch("app.connectors.apollo_client.enrich_person", new_callable=AsyncMock, return_value=None),
    ):
        resp = ai_client.post(
            "/api/ai/enrich-person",
            json={
                "email": "nobody@nowhere.com",
            },
        )

    assert resp.status_code == 404


def test_draft_rfq_email_ai_disabled(ai_client):
    """POST /api/ai/draft-rfq-email with AI off returns 403."""
    with patch("app.routers.ai._ai_enabled", return_value=False):
        resp = ai_client.post(
            "/api/ai/draft-rfq-email",
            json={
                "vendor_name": "Test",
                "buyer_name": "Buyer",
                "parts": [{"part_number": "LM317T", "quantity": 100}],
            },
        )
    assert resp.status_code == 403


def test_draft_rfq_email_success(ai_client):
    """POST /api/ai/draft-rfq-email returns subject and body."""
    result = {"subject": "RFQ: LM317T", "body": "Dear vendor..."}

    with (
        patch("app.routers.ai._ai_enabled", return_value=True),
        patch("app.services.ai_email_drafter.draft_rfq_email", new_callable=AsyncMock, return_value=result),
    ):
        resp = ai_client.post(
            "/api/ai/draft-rfq-email",
            json={
                "vendor_name": "Test Vendor",
                "buyer_name": "Test Buyer",
                "parts": [{"part_number": "LM317T", "quantity": 100}],
            },
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["available"] is True
    assert data["subject"] == "RFQ: LM317T"


def test_draft_rfq_email_not_available(ai_client):
    """POST /api/ai/draft-rfq-email returns available=False when drafter fails."""
    with (
        patch("app.routers.ai._ai_enabled", return_value=True),
        patch("app.services.ai_email_drafter.draft_rfq_email", new_callable=AsyncMock, return_value=None),
    ):
        resp = ai_client.post(
            "/api/ai/draft-rfq-email",
            json={
                "vendor_name": "Test Vendor",
                "buyer_name": "Test Buyer",
                "parts": [{"part_number": "LM317T", "quantity": 100}],
            },
        )

    assert resp.status_code == 200
    assert resp.json()["available"] is False


def test_compare_quotes_ai_disabled(ai_client):
    """POST /api/ai/compare-quotes with AI off returns 403."""
    with patch("app.routers.ai._ai_enabled", return_value=False):
        resp = ai_client.post(
            "/api/ai/compare-quotes",
            json={
                "part_number": "LM317T",
                "quotes": [
                    {"vendor_name": "A", "unit_price": 0.50},
                    {"vendor_name": "B", "unit_price": 0.45},
                ],
            },
        )
    assert resp.status_code == 403


def test_compare_quotes_success(ai_client):
    """POST /api/ai/compare-quotes returns comparison result."""
    result = {"recommendation": "Vendor B", "savings": "10%"}

    with (
        patch("app.routers.ai._ai_enabled", return_value=True),
        patch("app.services.ai_quote_analyzer.compare_quotes", new_callable=AsyncMock, return_value=result),
    ):
        resp = ai_client.post(
            "/api/ai/compare-quotes",
            json={
                "part_number": "LM317T",
                "quotes": [
                    {"vendor_name": "A", "unit_price": 0.50},
                    {"vendor_name": "B", "unit_price": 0.45},
                ],
            },
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["available"] is True
    assert data["recommendation"] == "Vendor B"


def test_compare_quotes_not_available(ai_client):
    """POST /api/ai/compare-quotes returns available=False when comparison fails."""
    with (
        patch("app.routers.ai._ai_enabled", return_value=True),
        patch("app.services.ai_quote_analyzer.compare_quotes", new_callable=AsyncMock, return_value=None),
    ):
        resp = ai_client.post(
            "/api/ai/compare-quotes",
            json={
                "part_number": "LM317T",
                "quotes": [
                    {"vendor_name": "A", "unit_price": 0.50},
                    {"vendor_name": "B", "unit_price": 0.45},
                ],
            },
        )

    assert resp.status_code == 200
    assert resp.json()["available"] is False


def test_save_parsed_offers_with_mpn_matching(ai_client, db_session, ai_test_user):
    """Save parsed offers matches MPNs to existing requirements."""
    from app.models import Offer, Requirement, Requisition

    req = Requisition(
        name="REQ-MATCH",
        customer_name="Test",
        status="open",
        created_by=ai_test_user.id,
    )
    db_session.add(req)
    db_session.flush()

    r = Requirement(
        requisition_id=req.id,
        primary_mpn="LM317T",
        target_qty=500,
    )
    db_session.add(r)
    db_session.commit()
    db_session.refresh(req)
    db_session.refresh(r)

    payload = {
        "requisition_id": req.id,
        "response_id": None,
        "offers": [
            {
                "vendor_name": "Test",
                "mpn": "LM317T",
                "qty_available": 500,
                "unit_price": 0.45,
            },
        ],
    }

    resp = ai_client.post("/api/ai/save-parsed-offers", json=payload)
    assert resp.status_code == 200
    data = resp.json()
    assert data["created"] == 1

    # Verify the offer was linked to the requirement
    offer = (
        db_session.query(Offer)
        .filter(
            Offer.requisition_id == req.id,
        )
        .first()
    )
    assert offer.requirement_id == r.id


def test_find_contacts_websearch_fallback(ai_client, db_session):
    """When Apollo returns fewer than 3 results, web search is invoked."""
    from app.models import VendorCard

    card = VendorCard(
        normalized_name="fallback supply",
        display_name="Fallback Supply",
        domain="fallback.com",
    )
    db_session.add(card)
    db_session.commit()
    db_session.refresh(card)

    apollo_results = [
        {"full_name": "One Person", "email": "one@fallback.com", "source": "apollo", "confidence": "high"},
    ]
    web_results = [
        {"full_name": "Web Person", "email": "web@fallback.com", "source": "web_search", "confidence": "medium"},
    ]

    with (
        patch("app.routers.ai._ai_enabled", return_value=True),
        patch("app.connectors.apollo_client.search_contacts", new_callable=AsyncMock, return_value=apollo_results),
        patch("app.services.ai_service.enrich_contacts_websearch", new_callable=AsyncMock, return_value=web_results),
    ):
        resp = ai_client.post(
            "/api/ai/find-contacts",
            json={
                "entity_type": "vendor",
                "entity_id": card.id,
            },
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 2  # Apollo + web search combined


# ---------------------------------------------------------------------------
# Freeform paste parsing (6 tests)
# ---------------------------------------------------------------------------


def test_parse_freeform_rfq_ai_disabled(ai_client):
    """POST /api/ai/parse-freeform-rfq with AI off returns 403."""
    with patch("app.routers.ai._ai_enabled", return_value=False):
        resp = ai_client.post(
            "/api/ai/parse-freeform-rfq",
            json={"raw_text": "Need LM317T x500, LM7805 x1000"},
        )
    assert resp.status_code == 403


def test_parse_freeform_rfq_success(ai_client):
    """POST /api/ai/parse-freeform-rfq returns RFQ template."""
    template = {
        "name": "Acme Project - Feb 2026",
        "customer_name": "Acme Corp",
        "deadline": "2026-02-28",
        "requirements": [
            {"primary_mpn": "LM317T", "target_qty": 500, "target_price": 0.50},
            {"primary_mpn": "LM7805", "target_qty": 1000},
        ],
    }
    with (
        patch("app.routers.ai._ai_enabled", return_value=True),
        patch(
            "app.services.freeform_parser_service.parse_freeform_rfq",
            new_callable=AsyncMock,
            return_value=template,
        ),
    ):
        resp = ai_client.post(
            "/api/ai/parse-freeform-rfq",
            json={"raw_text": "Acme needs LM317T x500 at $0.50, LM7805 x1000. Due Feb 28."},
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["parsed"] is True
    assert data["template"]["name"] == "Acme Project - Feb 2026"
    assert len(data["template"]["requirements"]) == 2


def test_parse_freeform_rfq_no_result(ai_client):
    """POST /api/ai/parse-freeform-rfq returns parsed=False when parser returns None."""
    with (
        patch("app.routers.ai._ai_enabled", return_value=True),
        patch(
            "app.services.freeform_parser_service.parse_freeform_rfq",
            new_callable=AsyncMock,
            return_value=None,
        ),
    ):
        resp = ai_client.post(
            "/api/ai/parse-freeform-rfq",
            json={"raw_text": "Out of office auto-reply"},
        )
    assert resp.status_code == 200
    assert resp.json()["parsed"] is False


def test_parse_freeform_offer_ai_disabled(ai_client):
    """POST /api/ai/parse-freeform-offer with AI off returns 403."""
    with patch("app.routers.ai._ai_enabled", return_value=False):
        resp = ai_client.post(
            "/api/ai/parse-freeform-offer",
            json={"raw_text": "LM317T 500pcs @ $0.45"},
        )
    assert resp.status_code == 403


def test_parse_freeform_offer_success(ai_client, db_session):
    """POST /api/ai/parse-freeform-offer returns offer template."""
    from app.models import Requisition

    req = Requisition(
        name="REQ-OFFER",
        customer_name="Test",
        status="open",
        created_by=1,
    )
    db_session.add(req)
    db_session.commit()
    db_session.refresh(req)

    template = {
        "vendor_name": "Acme Vendor",
        "offers": [
            {"mpn": "LM317T", "qty_available": 500, "unit_price": 0.45, "currency": "USD"},
            {"mpn": "LM7805", "qty_available": 1000, "unit_price": 0.30},
        ],
    }
    with (
        patch("app.routers.ai._ai_enabled", return_value=True),
        patch(
            "app.services.freeform_parser_service.parse_freeform_offer",
            new_callable=AsyncMock,
            return_value=template,
        ),
    ):
        resp = ai_client.post(
            "/api/ai/parse-freeform-offer",
            json={"raw_text": "LM317T 500 @ $0.45, LM7805 1000 @ $0.30", "requisition_id": req.id},
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["parsed"] is True
    assert data["template"]["vendor_name"] == "Acme Vendor"
    assert len(data["template"]["offers"]) == 2


def test_apply_freeform_rfq_success(ai_client, db_session, ai_test_user):
    """POST /api/ai/apply-freeform-rfq creates requisition + requirements."""
    from app.models import Company, CustomerSite, Requirement, Requisition

    co = Company(name="Apply Co", is_active=True)
    db_session.add(co)
    db_session.flush()
    site = CustomerSite(company_id=co.id, site_name="Apply HQ")
    db_session.add(site)
    db_session.commit()
    db_session.refresh(site)

    payload = {
        "name": "Apply Test RFQ",
        "customer_site_id": site.id,
        "customer_name": "Apply Co",
        "deadline": "2026-03-15",
        "requirements": [
            {"primary_mpn": "LM317T", "target_qty": 500, "target_price": 0.50},
            {"primary_mpn": "LM7805", "target_qty": 1000},
        ],
    }
    resp = ai_client.post("/api/ai/apply-freeform-rfq", json=payload)
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] is not None
    assert data["name"] == "Apply Test RFQ"
    assert data["requirements_added"] == 2

    req = db_session.get(Requisition, data["id"])
    assert req is not None
    assert req.customer_site_id == site.id
    reqs = db_session.query(Requirement).filter(Requirement.requisition_id == req.id).all()
    assert len(reqs) == 2
    mpns = {r.primary_mpn for r in reqs}
    assert "LM317T" in mpns
    assert "LM7805" in mpns


def test_apply_freeform_rfq_no_site(ai_client):
    """POST /api/ai/apply-freeform-rfq without customer_site_id returns 400."""
    resp = ai_client.post(
        "/api/ai/apply-freeform-rfq",
        json={
            "name": "Test",
            "requirements": [{"primary_mpn": "LM317T", "target_qty": 100}],
        },
    )
    assert resp.status_code == 400


def test_save_freeform_offers_success(ai_client, db_session, ai_test_user):
    """POST /api/ai/save-freeform-offers creates Offer records."""
    from app.models import Offer, Requisition

    req = Requisition(
        name="REQ-FREEFORM",
        customer_name="Test",
        status="open",
        created_by=ai_test_user.id,
    )
    db_session.add(req)
    db_session.commit()
    db_session.refresh(req)

    payload = {
        "requisition_id": req.id,
        "offers": [
            {"vendor_name": "Freeform Vendor", "mpn": "LM317T", "qty_available": 500, "unit_price": 0.45},
            {"vendor_name": "Freeform Vendor", "mpn": "LM7805", "qty_available": 1000, "unit_price": 0.30},
        ],
    }
    resp = ai_client.post("/api/ai/save-freeform-offers", json=payload)
    assert resp.status_code == 200
    data = resp.json()
    assert data["created"] == 2
    assert len(data["offer_ids"]) == 2

    offers = db_session.query(Offer).filter(Offer.requisition_id == req.id).all()
    assert len(offers) == 2
    assert all(o.source == "freeform_parsed" for o in offers)
    assert all(o.entered_by_id == ai_test_user.id for o in offers)
