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


def test_ai_enabled_mike_only_blocks_other(other_user):
    mock_settings = _make_settings("mike_only")
    with patch("app.routers.ai.settings", mock_settings):
        from app.routers.ai import _ai_enabled
        assert _ai_enabled(other_user) is False


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
        resp = ai_client.post("/api/ai/find-contacts", json={
            "entity_type": "vendor",
            "entity_id": 1,
        })
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

    with patch("app.routers.ai._ai_enabled", return_value=True), \
         patch("app.connectors.apollo_client.search_contacts", mock_search), \
         patch("app.services.ai_service.enrich_contacts_websearch", mock_web):
        resp = ai_client.post("/api/ai/find-contacts", json={
            "entity_type": "vendor",
            "entity_id": card.id,
        })

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

    with patch("app.routers.ai._ai_enabled", return_value=True), \
         patch("app.connectors.apollo_client.search_contacts", mock_search), \
         patch("app.services.ai_service.enrich_contacts_websearch", mock_web):
        resp = ai_client.post("/api/ai/find-contacts", json={
            "entity_type": "company",
            "entity_id": site.id,
        })

    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    assert data["contacts"][0]["full_name"] == "Bob Jones"


def test_find_contacts_no_entity(ai_client):
    """POST /api/ai/find-contacts with no entity_id returns 400."""
    with patch("app.routers.ai._ai_enabled", return_value=True):
        resp = ai_client.post("/api/ai/find-contacts", json={
            "entity_type": "vendor",
            "entity_id": None,
        })
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
        "parts": [
            {"mpn": "LM317T", "qty": 500, "unit_price": 0.45}
        ],
        "vendor_notes": "Standard lead time 2 weeks",
    }

    mock_parse = AsyncMock(return_value=parse_result)
    mock_extract = MagicMock(return_value=[
        {"vendor_name": "Acme Vendor", "mpn": "LM317T", "qty_available": 500, "unit_price": 0.45}
    ])
    mock_auto = MagicMock(return_value=True)
    mock_review = MagicMock(return_value=False)

    with patch("app.routers.ai._ai_enabled", return_value=True), \
         patch("app.services.response_parser.parse_vendor_response", mock_parse), \
         patch("app.services.response_parser.extract_draft_offers", mock_extract), \
         patch("app.services.response_parser.should_auto_apply", mock_auto), \
         patch("app.services.response_parser.should_flag_review", mock_review):
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

    offers = db_session.query(Offer).filter(
        Offer.requisition_id == req.id
    ).all()
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

    with patch("app.routers.ai._ai_enabled", return_value=True), \
         patch("app.services.ai_service.company_intel", mock_intel):
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
        resp = ai_client.post("/api/ai/draft-rfq", json={
            "vendor_name": "Acme Corp",
            "parts": ["LM317T"],
        })
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

    with patch("app.routers.ai._ai_enabled", return_value=True), \
         patch("app.routers.ai._build_vendor_history", mock_history), \
         patch("app.services.ai_service.draft_rfq", mock_draft):
        resp = ai_client.post("/api/ai/draft-rfq", json={
            "vendor_name": "Acme Corp",
            "parts": ["LM317T"],
        })

    assert resp.status_code == 200
    data = resp.json()
    assert data["available"] is True
    assert "LM317T" in data["body"]
    assert "Acme Corp" in data["body"]
    mock_draft.assert_awaited_once()
