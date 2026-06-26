"""test_eight_by_eight_strengthen.py — Tests for 8x8 VoIP reverse lookup and CRM
linking.

Tests reverse_lookup_phone() against SiteContact, Company, and VendorCard.
Tests phone normalization, CDR→CRM linking, and extension mapping.

Called by: pytest
Depends on: app/services/eight_by_eight_service.py, app/jobs/eight_by_eight_jobs.py,
            conftest fixtures (db_session, test_user, test_company, test_customer_site)
"""

from contextlib import contextmanager
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.orm import Session

from app.models import (
    ActivityLog,
    Company,
    CustomerSite,
    Requisition,
    SiteContact,
    User,
)
from app.services.eight_by_eight_service import normalize_phone

# ═══════════════════════════════════════════════════════════════════════
#  PHONE NORMALIZATION
# ═══════════════════════════════════════════════════════════════════════


class TestPhoneNormalization:
    """Test normalize_phone() strips formatting correctly."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            pytest.param("555-123-4567", "5551234567", id="strips_dashes"),
            pytest.param("(555) 123 4567", "5551234567", id="strips_parens_and_spaces"),
            pytest.param("+1-555-123-4567", "5551234567", id="strips_plus_one"),
            pytest.param("+15551234567", "5551234567", id="strips_plus_one_no_dashes"),
            pytest.param("5551234567", "5551234567", id="already_clean"),
            pytest.param("", "", id="empty_string"),
            pytest.param("555.123.4567", "5551234567", id="dots_as_separators"),
            # Non-US numbers (not 11 digits starting with 1) are kept as-is.
            pytest.param("+44-20-7946-0958", "442079460958", id="international_non_us"),
        ],
    )
    def test_normalize_phone(self, raw, expected):
        assert normalize_phone(raw) == expected


# ═══════════════════════════════════════════════════════════════════════
#  CDR → CRM LINKING
# ═══════════════════════════════════════════════════════════════════════


class TestCdrLinksToCrm:
    """Test that CDR processing links calls to CRM entities via reverse lookup."""

    async def test_cdr_with_known_phone_creates_linked_activity(
        self, db_session: Session, test_company: Company, test_customer_site: CustomerSite
    ):
        """A CDR whose external phone matches a SiteContact should produce an
        ActivityLog with company_id and contact_name set."""
        # Create a user with 8x8 enabled
        user = User(
            email="buyer8x8@trioscs.com",
            name="8x8 Buyer",
            role="buyer",
            azure_id="azure-8x8-test",
            eight_by_eight_extension="1001",
            eight_by_eight_enabled=True,
        )
        db_session.add(user)
        db_session.flush()

        # Create a SiteContact with a known phone
        contact = SiteContact(
            customer_site_id=test_customer_site.id,
            full_name="Bob Customer",
            phone="(305) 555-1234",
            is_active=True,
        )
        db_session.add(contact)
        db_session.commit()

        # Mock 8x8 API calls and call _process_cdrs
        fake_cdrs = [
            {
                "callId": "cdr-link-test-001",
                "caller": "3055551234",
                "callee": "1001",
                "callerName": "",
                "calleeName": "8x8 Buyer",
                "direction": "Incoming",
                "startTimeUTC": str(int(datetime.now(timezone.utc).timestamp() * 1000)),
                "talkTimeMS": 120000,
                "missed": "Answered",
                "answered": "Answered",
                "departments": ["Sales"],
            }
        ]

        with _patch_8x8_api(fake_cdrs):
            from app.jobs.eight_by_eight_jobs import _process_cdrs

            # Need to patch the imports inside _process_cdrs since they're local
            result = await _process_cdrs(db_session, _FakeSettings())

        assert result["processed"] == 1
        assert result["matched"] == 1

        # Verify the ActivityLog was linked
        activity = db_session.query(ActivityLog).filter(ActivityLog.external_id == "cdr-link-test-001").first()
        assert activity is not None
        assert activity.company_id == test_company.id
        assert activity.contact_name == "Bob Customer"

    async def test_cdr_with_unknown_phone_creates_unlinked_activity(self, db_session: Session):
        """A CDR with an unknown phone should create an ActivityLog without CRM
        links."""
        user = User(
            email="buyer8x8b@trioscs.com",
            name="8x8 Buyer B",
            role="buyer",
            azure_id="azure-8x8-test-b",
            eight_by_eight_extension="1002",
            eight_by_eight_enabled=True,
        )
        db_session.add(user)
        db_session.commit()

        fake_cdrs = [
            {
                "callId": "cdr-unknown-test-001",
                "caller": "1002",
                "callee": "9998887777",
                "callerName": "8x8 Buyer B",
                "calleeName": "Unknown Caller",
                "direction": "Outgoing",
                "startTimeUTC": str(int(datetime.now(timezone.utc).timestamp() * 1000)),
                "talkTimeMS": 60000,
                "missed": "Answered",
                "answered": "Answered",
                "departments": [],
            }
        ]

        with _patch_8x8_api(fake_cdrs):
            from app.jobs.eight_by_eight_jobs import _process_cdrs

            result = await _process_cdrs(db_session, _FakeSettings())

        assert result["processed"] == 1
        assert result["matched"] == 0

        activity = db_session.query(ActivityLog).filter(ActivityLog.external_id == "cdr-unknown-test-001").first()
        assert activity is not None
        assert activity.company_id is None
        assert activity.vendor_card_id is None


# ═══════════════════════════════════════════════════════════════════════
#  CDR LINKS TO OPEN REQUISITION
# ═══════════════════════════════════════════════════════════════════════


class TestCdrLinksToRequisition:
    """Test that CDR processing links to open requisitions for matched companies."""

    async def test_cdr_links_to_open_requisition(
        self,
        db_session: Session,
        test_company: Company,
        test_customer_site: CustomerSite,
    ):
        """When a matched company has an open requisition, the ActivityLog should have
        requisition_id set."""
        user = User(
            email="buyer8x8req@trioscs.com",
            name="8x8 Buyer Req",
            role="buyer",
            azure_id="azure-8x8-req-test",
            eight_by_eight_extension="1005",
            eight_by_eight_enabled=True,
        )
        db_session.add(user)
        db_session.flush()

        # Create open requisition for the test company
        req = Requisition(
            name="REQ-OPEN-001",
            customer_site_id=test_customer_site.id,
            status="open",
            created_by=user.id,
        )
        db_session.add(req)

        # Create a company phone match
        test_company.phone = "(305) 555-9999"
        db_session.commit()

        fake_cdrs = [
            {
                "callId": "cdr-req-link-001",
                "caller": "3055559999",
                "callee": "1005",
                "callerName": "",
                "calleeName": "8x8 Buyer Req",
                "direction": "Incoming",
                "startTimeUTC": str(int(datetime.now(timezone.utc).timestamp() * 1000)),
                "talkTimeMS": 90000,
                "missed": "Answered",
                "answered": "Answered",
                "departments": [],
            }
        ]

        with _patch_8x8_api(fake_cdrs):
            from app.jobs.eight_by_eight_jobs import _process_cdrs

            result = await _process_cdrs(db_session, _FakeSettings())

        assert result["processed"] == 1
        assert result["matched"] == 1

        activity = db_session.query(ActivityLog).filter(ActivityLog.external_id == "cdr-req-link-001").first()
        assert activity is not None
        assert activity.company_id == test_company.id
        assert activity.requisition_id == req.id


# ═══════════════════════════════════════════════════════════════════════
#  HELPERS
# ═══════════════════════════════════════════════════════════════════════


class _FakeSettings:
    """Minimal settings stub for 8x8 tests."""

    eight_by_eight_api_key = "fake-api-key"
    eight_by_eight_username = "fake-user"
    eight_by_eight_password = "fake-pass"
    eight_by_eight_timezone = "America/New_York"
    eight_by_eight_enabled = True
    eight_by_eight_poll_interval_minutes = 30


@contextmanager
def _patch_8x8_api(fake_cdrs):
    """Patch the 8x8 token + CDR fetch calls used by _process_cdrs."""
    with (
        patch(
            "app.services.eight_by_eight_service.get_access_token",
            new=AsyncMock(return_value="fake-token"),
        ),
        patch(
            "app.services.eight_by_eight_service.get_cdrs",
            new=AsyncMock(return_value=fake_cdrs),
        ),
    ):
        yield


def _mock_async_client(*, get_status=200, get_json=None):
    """Build a patch target replacing httpx.AsyncClient for GET-based calls."""
    from unittest.mock import MagicMock

    resp = MagicMock()
    resp.status_code = get_status
    resp.json.return_value = get_json or {}

    client = MagicMock()
    client.get = AsyncMock(return_value=resp)
    client.post = AsyncMock(return_value=resp)

    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=client)
    cm.__aexit__ = AsyncMock(return_value=False)
    return MagicMock(return_value=cm)
