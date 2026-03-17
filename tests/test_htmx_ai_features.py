"""tests/test_htmx_ai_features.py — Tests for AI feature HTMX endpoints.

Covers contact discovery, prospect listing, promote, email parsing,
company intelligence, and RFQ draft generation. All AI service calls
are mocked to avoid real API calls.

Called by: pytest
Depends on: conftest.py fixtures (client, db_session, test_user, etc.)
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import (
    Company,
    ProspectContact,
    Requirement,
    Requisition,
    User,
    VendorCard,
)


@pytest.fixture()
def ai_vendor(db_session: Session) -> VendorCard:
    """Vendor card for AI tests."""
    card = VendorCard(
        normalized_name="test ai vendor",
        display_name="Test AI Vendor",
        domain="aivendor.com",
        sighting_count=10,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(card)
    db_session.commit()
    db_session.refresh(card)
    return card


@pytest.fixture()
def ai_company(db_session: Session) -> Company:
    """Company for AI intel tests."""
    co = Company(
        name="AI Test Corp",
        domain="aitestcorp.com",
        is_active=True,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(co)
    db_session.commit()
    db_session.refresh(co)
    return co


@pytest.fixture()
def ai_requisition(db_session: Session, test_user: User) -> Requisition:
    """Requisition with requirements for RFQ draft tests."""
    req = Requisition(
        name="REQ-AI-001",
        customer_name="AI Test Corp",
        status="open",
        created_by=test_user.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(req)
    db_session.flush()
    item = Requirement(
        requisition_id=req.id,
        primary_mpn="STM32F407",
        target_qty=500,
        target_price=4.50,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(item)
    db_session.commit()
    db_session.refresh(req)
    return req


@pytest.fixture()
def prospect(db_session: Session, ai_vendor: VendorCard) -> ProspectContact:
    """A prospect contact linked to a vendor."""
    pc = ProspectContact(
        vendor_card_id=ai_vendor.id,
        full_name="Jane Doe",
        title="Procurement Manager",
        email="jane@aivendor.com",
        source="web_search",
        confidence="medium",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(pc)
    db_session.commit()
    db_session.refresh(pc)
    return pc


# ── 1. Find contacts ─────────────────────────────────────────────────


class TestFindContacts:
    """POST /partials/vendors/{id}/find-contacts"""

    @patch("app.routers.htmx.ai_features.settings")
    @patch("app.routers.htmx.ai_features.enrich_contacts_websearch", new_callable=AsyncMock, create=True)
    def test_find_contacts_success(
        self, mock_enrich, mock_settings, client: TestClient, ai_vendor: VendorCard
    ):
        mock_settings.ai_features_enabled = "all"
        mock_enrich.return_value = [
            {
                "full_name": "John Smith",
                "title": "Buyer",
                "email": "john@aivendor.com",
                "phone": None,
                "linkedin_url": None,
                "source": "web_search",
                "confidence": "medium",
            }
        ]

        with patch(
            "app.routers.htmx.ai_features.enrich_contacts_websearch",
            mock_enrich,
        ):
            # Use the import inside the endpoint
            with patch(
                "app.services.ai_service.enrich_contacts_websearch",
                mock_enrich,
            ):
                resp = client.post(
                    f"/v2/partials/vendors/{ai_vendor.id}/find-contacts"
                )

        assert resp.status_code == 200
        assert "John Smith" in resp.text or "contact" in resp.text.lower()

    @patch("app.routers.htmx.ai_features.settings")
    def test_find_contacts_ai_disabled(
        self, mock_settings, client: TestClient, ai_vendor: VendorCard
    ):
        mock_settings.ai_features_enabled = "off"
        resp = client.post(
            f"/v2/partials/vendors/{ai_vendor.id}/find-contacts"
        )
        assert resp.status_code == 200
        assert "not enabled" in resp.text

    def test_find_contacts_vendor_not_found(self, client: TestClient):
        with patch("app.routers.htmx.ai_features.settings") as mock_s:
            mock_s.ai_features_enabled = "all"
            resp = client.post("/v2/partials/vendors/99999/find-contacts")
        assert resp.status_code == 404


# ── 2. List prospect contacts ────────────────────────────────────────


class TestListProspectContacts:
    """GET /partials/ai/prospect-contacts"""

    def test_list_by_vendor(
        self, client: TestClient, prospect: ProspectContact, ai_vendor: VendorCard
    ):
        resp = client.get(
            f"/v2/partials/ai/prospect-contacts?vendor_id={ai_vendor.id}"
        )
        assert resp.status_code == 200
        assert "Jane Doe" in resp.text

    def test_list_empty(self, client: TestClient):
        resp = client.get("/v2/partials/ai/prospect-contacts?vendor_id=0")
        assert resp.status_code == 200
        assert "No prospect contacts" in resp.text or "0 prospect" in resp.text


# ── 3. Promote prospect contact ─────────────────────────────────────


class TestPromoteProspectContact:
    """POST /partials/ai/prospect-contacts/{id}/promote"""

    @patch("app.services.ai_offer_service.promote_prospect_contact")
    def test_promote_success(
        self, mock_promote, client: TestClient, prospect: ProspectContact
    ):
        mock_promote.return_value = {
            "promoted_to_type": "vendor_contact",
            "promoted_to_id": 42,
        }
        resp = client.post(
            f"/v2/partials/ai/prospect-contacts/{prospect.id}/promote"
        )
        assert resp.status_code == 200
        assert "vendor_contact" in resp.text
        assert "HX-Trigger" in resp.headers

    @patch("app.services.ai_offer_service.promote_prospect_contact")
    def test_promote_not_found(self, mock_promote, client: TestClient):
        mock_promote.side_effect = ValueError("Prospect contact not found")
        resp = client.post(
            "/v2/partials/ai/prospect-contacts/99999/promote"
        )
        assert resp.status_code == 200
        assert "not found" in resp.text


# ── 4. Parse email ───────────────────────────────────────────────────


class TestParseEmail:
    """POST /partials/ai/parse-email"""

    @patch("app.routers.htmx.ai_features.settings")
    def test_parse_email_success(self, mock_settings, client: TestClient):
        mock_settings.ai_features_enabled = "all"
        mock_result = {
            "quotes": [
                {"mpn": "LM317T", "qty": 1000, "unit_price": 0.45, "lead_time": "2 weeks", "condition": "new"}
            ],
            "overall_confidence": 0.85,
            "email_type": "quote",
            "vendor_notes": "Price valid 30 days",
        }
        with patch(
            "app.services.ai_email_parser.parse_email",
            new_callable=AsyncMock,
            return_value=mock_result,
        ):
            resp = client.post(
                "/v2/partials/ai/parse-email",
                data={
                    "email_body": "We can offer LM317T at $0.45 each for 1000pcs.",
                    "vendor_name": "Arrow",
                },
            )
        assert resp.status_code == 200
        assert "LM317T" in resp.text

    @patch("app.routers.htmx.ai_features.settings")
    def test_parse_email_ai_disabled(self, mock_settings, client: TestClient):
        mock_settings.ai_features_enabled = "off"
        resp = client.post(
            "/v2/partials/ai/parse-email",
            data={"email_body": "some text"},
        )
        assert resp.status_code == 200
        assert "not enabled" in resp.text

    @patch("app.routers.htmx.ai_features.settings")
    def test_parse_email_failure(self, mock_settings, client: TestClient):
        mock_settings.ai_features_enabled = "all"
        with patch(
            "app.services.ai_email_parser.parse_email",
            new_callable=AsyncMock,
            return_value=None,
        ):
            resp = client.post(
                "/v2/partials/ai/parse-email",
                data={"email_body": "gibberish text that cannot be parsed"},
            )
        assert resp.status_code == 200
        assert "Could not extract" in resp.text


# ── 5. Company intel ─────────────────────────────────────────────────


class TestCompanyIntel:
    """GET /partials/companies/{id}/intel"""

    @patch("app.routers.htmx.ai_features.settings")
    def test_company_intel_success(
        self, mock_settings, client: TestClient, ai_company: Company
    ):
        mock_settings.ai_features_enabled = "all"
        mock_intel = {
            "summary": "AI Test Corp manufactures widgets.",
            "revenue": "$50M",
            "employees": "200",
            "products": "Widgets and gadgets",
            "components_they_buy": ["MCUs", "Capacitors"],
            "opportunity_signals": ["Expanding production"],
            "recent_news": ["New factory opened"],
            "sources": ["web"],
        }
        with patch(
            "app.services.ai_service.company_intel",
            new_callable=AsyncMock,
            return_value=mock_intel,
        ):
            resp = client.get(
                f"/v2/partials/companies/{ai_company.id}/intel"
            )
        assert resp.status_code == 200
        assert "AI Test Corp" in resp.text
        assert "widgets" in resp.text.lower()

    @patch("app.routers.htmx.ai_features.settings")
    def test_company_intel_not_found(self, mock_settings, client: TestClient):
        mock_settings.ai_features_enabled = "all"
        resp = client.get("/v2/partials/companies/99999/intel")
        assert resp.status_code == 404


# ── 6. AI draft RFQ ─────────────────────────────────────────────────


class TestAiDraftRfq:
    """POST /partials/requisitions/{id}/ai-draft-rfq"""

    @patch("app.routers.htmx.ai_features.settings")
    def test_draft_rfq_success(
        self, mock_settings, client: TestClient, ai_requisition: Requisition
    ):
        mock_settings.ai_features_enabled = "all"
        mock_body = (
            "We are looking for 500 pcs of STM32F407.\n"
            "Please provide pricing and lead time.\n"
            "Thank you for your prompt attention."
        )
        with patch(
            "app.services.ai_service.draft_rfq",
            new_callable=AsyncMock,
            return_value=mock_body,
        ):
            resp = client.post(
                f"/v2/partials/requisitions/{ai_requisition.id}/ai-draft-rfq",
                data={"vendor_name": "Mouser Electronics"},
            )
        assert resp.status_code == 200
        assert "STM32F407" in resp.text

    @patch("app.routers.htmx.ai_features.settings")
    def test_draft_rfq_not_found(self, mock_settings, client: TestClient):
        mock_settings.ai_features_enabled = "all"
        resp = client.post(
            "/v2/partials/requisitions/99999/ai-draft-rfq",
            data={"vendor_name": "Test"},
        )
        assert resp.status_code == 404

    @patch("app.routers.htmx.ai_features.settings")
    def test_draft_rfq_ai_disabled(
        self, mock_settings, client: TestClient, ai_requisition: Requisition
    ):
        mock_settings.ai_features_enabled = "off"
        resp = client.post(
            f"/v2/partials/requisitions/{ai_requisition.id}/ai-draft-rfq",
            data={"vendor_name": "Test"},
        )
        assert resp.status_code == 200
        assert "not enabled" in resp.text
