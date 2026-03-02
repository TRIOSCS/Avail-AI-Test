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


@pytest.mark.asyncio
async def test_enrich_unknown_vendor_card(db_session):
    """Enrich with nonexistent vendor card should return error, not crash."""
    with patch("app.services.apollo_sync_service.settings") as ms:
        ms.apollo_api_key = "test-key"
        result = await enrich_selected_contacts(
            apollo_ids=["abc"],
            vendor_card_id=99999,
            db=db_session,
        )
    assert result["enriched"] == 0
    assert "error" in result


@pytest.mark.asyncio
async def test_sync_no_contacts_with_email(db_session):
    """Sync with no emailable contacts should return zero synced."""
    with patch("app.services.apollo_sync_service.settings") as ms:
        ms.apollo_api_key = "test-key"
        result = await sync_contacts_to_apollo(db=db_session)
    assert result["synced"] == 0


@pytest.mark.asyncio
async def test_discover_masks_all_emails():
    """All discovered contacts should have masked emails; contacts without email get None."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "people": [
            {
                "id": "1",
                "first_name": "A",
                "last_name": "B",
                "email": "a.b@secret.com",
                "title": "Buyer",
            },
            {
                "id": "2",
                "first_name": "C",
                "last_name": "D",
                "title": "Procurement Manager",
            },
        ],
        "pagination": {"total_entries": 2},
    }
    with patch(
        "app.services.apollo_sync_service.http.post",
        new_callable=AsyncMock,
        return_value=mock_resp,
    ):
        with patch("app.services.apollo_sync_service.settings") as ms:
            ms.apollo_api_key = "test-key"
            result = await discover_contacts("secret.com")
    for c in result["contacts"]:
        assert "a.b@secret.com" not in str(c)
    assert result["contacts"][0]["email_masked"] is not None
    assert result["contacts"][1]["email_masked"] is None


@pytest.mark.asyncio
async def test_discover_exception_handling():
    """Discovery should catch exceptions and return empty result."""
    with patch(
        "app.services.apollo_sync_service.http.post",
        new_callable=AsyncMock,
        side_effect=Exception("Connection timeout"),
    ):
        with patch("app.services.apollo_sync_service.settings") as ms:
            ms.apollo_api_key = "test-key"
            result = await discover_contacts("fail.com")
    assert result["total_found"] == 0
    assert "Connection timeout" in result.get("note", "")


@pytest.mark.asyncio
async def test_get_credits_no_api_key():
    """Credits with no API key should return zeros."""
    with patch("app.services.apollo_sync_service.settings") as ms:
        ms.apollo_api_key = ""
        result = await get_credits()
    assert result["lead_credits_remaining"] == 0
    assert "note" in result


@pytest.mark.asyncio
async def test_get_credits_api_error():
    """Credits should handle API error gracefully."""
    mock_resp = MagicMock()
    mock_resp.status_code = 500
    mock_resp.text = "Server Error"

    with patch(
        "app.services.apollo_sync_service.http.get",
        new_callable=AsyncMock,
        return_value=mock_resp,
    ):
        with patch("app.services.apollo_sync_service.settings") as ms:
            ms.apollo_api_key = "test-key"
            result = await get_credits()
    assert result["lead_credits_remaining"] == 0
    assert "note" in result


@pytest.mark.asyncio
async def test_get_credits_403_master_key_hint():
    """Credits 403 should return helpful master key message."""
    mock_resp = MagicMock()
    mock_resp.status_code = 403
    mock_resp.text = "API_INACCESSIBLE"

    with patch(
        "app.services.apollo_sync_service.http.get",
        new_callable=AsyncMock,
        return_value=mock_resp,
    ):
        with patch("app.services.apollo_sync_service.settings") as ms:
            ms.apollo_api_key = "test-key"
            result = await get_credits()
    assert result["lead_credits_remaining"] == 0
    assert "master key" in result["note"]


@pytest.mark.asyncio
async def test_get_credits_exception():
    """Credits should catch exceptions."""
    with patch(
        "app.services.apollo_sync_service.http.get",
        new_callable=AsyncMock,
        side_effect=Exception("Timeout"),
    ):
        with patch("app.services.apollo_sync_service.settings") as ms:
            ms.apollo_api_key = "test-key"
            result = await get_credits()
    assert result["lead_credits_remaining"] == 0


@pytest.mark.asyncio
async def test_enrich_no_api_key(db_session):
    """Enrich with no API key should return empty."""
    with patch("app.services.apollo_sync_service.settings") as ms:
        ms.apollo_api_key = ""
        result = await enrich_selected_contacts(
            apollo_ids=["abc"], vendor_card_id=1, db=db_session
        )
    assert result["enriched"] == 0


@pytest.mark.asyncio
async def test_enrich_api_error_skips_contact(db_session):
    """Enrich should skip contacts that return non-200."""
    from app.models import VendorCard

    vc = VendorCard(display_name="Test", normalized_name="test_err", source="manual")
    db_session.add(vc)
    db_session.commit()

    mock_resp = MagicMock()
    mock_resp.status_code = 404

    mock_credits = MagicMock()
    mock_credits.status_code = 200
    mock_credits.json.return_value = {
        "effective_num_lead_credits": 95,
        "num_lead_credits_used": 0,
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
                    apollo_ids=["bad_id"],
                    vendor_card_id=vc.id,
                    db=db_session,
                )
    assert result["enriched"] == 0


@pytest.mark.asyncio
async def test_enrich_missing_person_in_response(db_session):
    """Enrich should skip when response has no person data."""
    from app.models import VendorCard

    vc = VendorCard(display_name="Test", normalized_name="test_np", source="manual")
    db_session.add(vc)
    db_session.commit()

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"person": None}

    mock_credits = MagicMock()
    mock_credits.status_code = 200
    mock_credits.json.return_value = {
        "effective_num_lead_credits": 95,
        "num_lead_credits_used": 0,
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
                    apollo_ids=["no_person"],
                    vendor_card_id=vc.id,
                    db=db_session,
                )
    assert result["enriched"] == 0


@pytest.mark.asyncio
async def test_enrich_upsert_existing_contact(db_session):
    """Enrich should update existing contact rather than create duplicate."""
    from app.models import VendorCard, VendorContact

    vc = VendorCard(display_name="Acme", normalized_name="acme_up", source="manual")
    db_session.add(vc)
    db_session.flush()

    existing = VendorContact(
        vendor_card_id=vc.id,
        full_name="Jane Doe",
        email="jane@acme.com",
        source="manual",
        first_name="Jane",
        last_name="Doe",
    )
    db_session.add(existing)
    db_session.commit()

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "person": {
            "id": "abc",
            "first_name": "Jane",
            "last_name": "Doe",
            "title": "VP Procurement",
            "email": "jane@acme.com",
            "email_status": "verified",
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
                    apollo_ids=["abc"],
                    vendor_card_id=vc.id,
                    db=db_session,
                )

    assert result["enriched"] == 1
    # Should have updated existing, not created new
    count = db_session.query(VendorContact).filter_by(vendor_card_id=vc.id).count()
    assert count == 1
    updated = db_session.query(VendorContact).filter_by(vendor_card_id=vc.id).first()
    assert updated.source == "apollo"
    assert updated.is_verified is True


@pytest.mark.asyncio
async def test_enrich_exception_per_contact(db_session):
    """Enrich should catch per-contact exceptions and continue."""
    from app.models import VendorCard

    vc = VendorCard(display_name="Test", normalized_name="test_ex", source="manual")
    db_session.add(vc)
    db_session.commit()

    with patch(
        "app.services.apollo_sync_service.http.post",
        new_callable=AsyncMock,
        side_effect=Exception("Network error"),
    ):
        with patch(
            "app.services.apollo_sync_service.http.get",
            new_callable=AsyncMock,
            return_value=MagicMock(
                status_code=200,
                json=MagicMock(
                    return_value={
                        "effective_num_lead_credits": 95,
                        "num_lead_credits_used": 0,
                        "effective_num_direct_dial_credits": 160,
                        "num_direct_dial_credits_used": 0,
                        "effective_num_ai_credits": 5000,
                        "num_ai_credits_used": 0,
                    }
                ),
            ),
        ):
            with patch("app.services.apollo_sync_service.settings") as ms:
                ms.apollo_api_key = "test-key"
                result = await enrich_selected_contacts(
                    apollo_ids=["crash"],
                    vendor_card_id=vc.id,
                    db=db_session,
                )
    assert result["enriched"] == 0


@pytest.mark.asyncio
async def test_sync_no_api_key(db_session):
    """Sync with no API key should return zeros."""
    with patch("app.services.apollo_sync_service.settings") as ms:
        ms.apollo_api_key = ""
        result = await sync_contacts_to_apollo(db=db_session)
    assert result["synced"] == 0
    assert "note" in result


@pytest.mark.asyncio
async def test_sync_handles_422_and_errors(db_session):
    """Sync should track skipped (422) and error responses separately."""
    from app.models import VendorCard, VendorContact

    vc = VendorCard(display_name="Multi", normalized_name="multi_sync", source="manual")
    db_session.add(vc)
    db_session.flush()

    for i, email in enumerate(["a@test.com", "b@test.com", "c@test.com"]):
        db_session.add(
            VendorContact(
                vendor_card_id=vc.id,
                full_name=f"Person {i}",
                email=email,
                source="manual",
            )
        )
    db_session.commit()

    call_count = 0

    async def mock_post(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        resp = MagicMock()
        if call_count == 1:
            resp.status_code = 200
        elif call_count == 2:
            resp.status_code = 422
        else:
            resp.status_code = 500
        return resp

    with patch(
        "app.services.apollo_sync_service.http.post",
        new_callable=AsyncMock,
        side_effect=mock_post,
    ):
        with patch("app.services.apollo_sync_service.settings") as ms:
            ms.apollo_api_key = "test-key"
            result = await sync_contacts_to_apollo(db=db_session)

    assert result["synced"] == 1
    assert result["skipped"] == 1
    assert result["errors"] == 1


@pytest.mark.asyncio
async def test_sync_exception_per_contact(db_session):
    """Sync should catch per-contact exceptions."""
    from app.models import VendorCard, VendorContact

    vc = VendorCard(display_name="Err", normalized_name="err_sync", source="manual")
    db_session.add(vc)
    db_session.flush()

    db_session.add(
        VendorContact(
            vendor_card_id=vc.id,
            full_name="Crash",
            email="crash@test.com",
            source="manual",
        )
    )
    db_session.commit()

    with patch(
        "app.services.apollo_sync_service.http.post",
        new_callable=AsyncMock,
        side_effect=Exception("Timeout"),
    ):
        with patch("app.services.apollo_sync_service.settings") as ms:
            ms.apollo_api_key = "test-key"
            result = await sync_contacts_to_apollo(db=db_session)

    assert result["errors"] == 1


def test_extract_phone_direct():
    """_extract_phone should prefer phone_number field."""
    from app.services.apollo_sync_service import _extract_phone

    assert _extract_phone({"phone_number": "+1234"}) == "+1234"


def test_extract_phone_from_list():
    """_extract_phone should prefer direct_dial > mobile > work."""
    from app.services.apollo_sync_service import _extract_phone

    person = {
        "phone_numbers": [
            {"type": "work", "sanitized_number": "+1111"},
            {"type": "mobile", "sanitized_number": "+2222"},
        ]
    }
    assert _extract_phone(person) == "+2222"


def test_extract_phone_fallback():
    """_extract_phone should fall back to first phone if no type matches."""
    from app.services.apollo_sync_service import _extract_phone

    person = {
        "phone_numbers": [
            {"type": "other", "sanitized_number": "+9999"},
        ]
    }
    assert _extract_phone(person) == "+9999"


def test_extract_phone_none():
    """_extract_phone should return None when no phone data."""
    from app.services.apollo_sync_service import _extract_phone

    assert _extract_phone({}) is None
