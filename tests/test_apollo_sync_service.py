"""Tests for Apollo sync service.

Tests discovery, enrichment, sync, and enrollment logic with mocked API calls.
Called by: pytest
Depends on: app.services.apollo_sync_service, app.connectors.apollo_client
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.services.apollo_sync_service import (
    discover_contacts,
    enrich_selected_contacts,
    get_credits,
    sync_contacts_to_apollo,
)


@pytest.mark.asyncio
async def test_discover_contacts_returns_masked_emails():
    """Discovery should return contacts with masked emails (not full emails)."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "people": [
            {
                "id": "abc123",
                "first_name": "Jane",
                "last_name": "Doe",
                "title": "VP Procurement",
                "email": "jane.doe@acme.com",
                "linkedin_url": "https://linkedin.com/in/janedoe",
                "seniority": "vp",
                "organization": {"name": "Acme Corp"},
            }
        ],
        "pagination": {"total_entries": 1},
    }

    with patch(
        "app.services.apollo_sync_service.http.post",
        new_callable=AsyncMock,
        return_value=mock_resp,
    ):
        with patch("app.services.apollo_sync_service.settings") as mock_settings:
            mock_settings.apollo_api_key = "test-key"
            result = await discover_contacts(
                "acme.com", title_keywords=["procurement"], max_results=10
            )

    assert result["total_found"] == 1
    assert len(result["contacts"]) == 1
    c = result["contacts"][0]
    assert c["apollo_id"] == "abc123"
    assert c["full_name"] == "Jane Doe"
    assert "jane.doe@acme.com" not in str(c)
    assert c["email_masked"] == "j***@acme..."


@pytest.mark.asyncio
async def test_discover_contacts_no_api_key():
    """Discovery should return empty when no API key is configured."""
    with patch("app.services.apollo_sync_service.settings") as mock_settings:
        mock_settings.apollo_api_key = ""
        result = await discover_contacts("acme.com")

    assert result["total_found"] == 0
    assert result["contacts"] == []


@pytest.mark.asyncio
async def test_discover_contacts_api_error():
    """Discovery should return empty on API error, not raise."""
    mock_resp = MagicMock()
    mock_resp.status_code = 500
    mock_resp.text = "Internal Server Error"

    with patch(
        "app.services.apollo_sync_service.http.post",
        new_callable=AsyncMock,
        return_value=mock_resp,
    ):
        with patch("app.services.apollo_sync_service.settings") as mock_settings:
            mock_settings.apollo_api_key = "test-key"
            result = await discover_contacts("acme.com")

    assert result["total_found"] == 0


@pytest.mark.asyncio
async def test_get_credits():
    """Credits endpoint should parse Apollo profile response."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "id": "user1",
        "effective_num_lead_credits": 95,
        "num_lead_credits_used": 5,
        "effective_num_direct_dial_credits": 160,
        "num_direct_dial_credits_used": 10,
        "effective_num_ai_credits": 5000,
        "num_ai_credits_used": 0,
    }

    with patch(
        "app.services.apollo_sync_service.http.get",
        new_callable=AsyncMock,
        return_value=mock_resp,
    ):
        with patch("app.services.apollo_sync_service.settings") as mock_settings:
            mock_settings.apollo_api_key = "test-key"
            result = await get_credits()

    assert result["lead_credits_remaining"] == 90
    assert result["direct_dial_remaining"] == 150


@pytest.mark.asyncio
async def test_enrich_selected_contacts(db_session):
    """Enrich should call people/match and return contact details with credit tracking."""
    from app.models import VendorCard

    vc = VendorCard(display_name="Acme", normalized_name="acme", source="manual")
    db_session.add(vc)
    db_session.commit()

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "person": {
            "id": "abc123",
            "first_name": "Jane",
            "last_name": "Doe",
            "title": "VP Procurement",
            "email": "jane@acme.com",
            "email_status": "verified",
            "phone_numbers": [
                {"type": "direct_dial", "sanitized_number": "+15551234"}
            ],
            "linkedin_url": "https://linkedin.com/in/janedoe",
        }
    }

    mock_credits = MagicMock()
    mock_credits.status_code = 200
    mock_credits.json.return_value = {
        "effective_num_lead_credits": 95,
        "num_lead_credits_used": 1,
        "effective_num_direct_dial_credits": 160,
        "num_direct_dial_credits_used": 0,
        "effective_num_ai_credits": 5000,
        "num_ai_credits_used": 0,
    }

    with patch(
        "app.services.apollo_sync_service.http.post",
        new_callable=AsyncMock,
        return_value=mock_resp,
    ):
        with patch(
            "app.services.apollo_sync_service.http.get",
            new_callable=AsyncMock,
            return_value=mock_credits,
        ):
            with patch("app.services.apollo_sync_service.settings") as ms:
                ms.apollo_api_key = "test-key"
                result = await enrich_selected_contacts(
                    apollo_ids=["abc123"],
                    vendor_card_id=vc.id,
                    db=db_session,
                )

    assert result["enriched"] == 1
    assert len(result["contacts"]) == 1
    assert result["contacts"][0]["email"] == "jane@acme.com"


@pytest.mark.asyncio
async def test_sync_contacts_to_apollo(db_session):
    """Sync should push AvailAI contacts to Apollo with dedup enabled."""
    from app.models import VendorCard, VendorContact

    vc = VendorCard(display_name="Acme", normalized_name="acme_sync", source="manual")
    db_session.add(vc)
    db_session.flush()

    contact = VendorContact(
        vendor_card_id=vc.id,
        full_name="John Smith",
        email="john@acme.com",
        source="manual",
        first_name="John",
        last_name="Smith",
        title="Buyer",
    )
    db_session.add(contact)
    db_session.commit()

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"contact": {"id": "apollo_new_1"}}

    with patch(
        "app.services.apollo_sync_service.http.post",
        new_callable=AsyncMock,
        return_value=mock_resp,
    ):
        with patch("app.services.apollo_sync_service.settings") as ms:
            ms.apollo_api_key = "test-key"
            result = await sync_contacts_to_apollo(db=db_session)

    assert result["synced"] == 1
    assert result["skipped"] == 0
