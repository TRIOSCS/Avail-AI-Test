"""test_eight_by_eight_strengthen.py — Tests for 8x8 VoIP reverse lookup and CRM
linking.

Tests reverse_lookup_phone() against SiteContact, Company, and VendorCard.
Tests phone normalization, CDR→CRM linking, and extension mapping.

Called by: pytest
Depends on: app/services/eight_by_eight_service.py, app/jobs/eight_by_eight_jobs.py,
            conftest fixtures (db_session, test_user, test_company, test_customer_site)
"""

from datetime import datetime, timezone
from unittest.mock import patch

from sqlalchemy.orm import Session

from app.models import (
    ActivityLog,
    Company,
    CustomerSite,
    Requisition,
    SiteContact,
    User,
    VendorCard,
)
from app.services.eight_by_eight_service import (
    normalize_phone,
    reverse_lookup_phone,
)

# ═══════════════════════════════════════════════════════════════════════
#  PHONE NORMALIZATION
# ═══════════════════════════════════════════════════════════════════════


class TestPhoneNormalization:
    """Test normalize_phone() strips formatting correctly."""

    def test_strips_dashes(self):
        assert normalize_phone("555-123-4567") == "5551234567"

    def test_strips_parens_and_spaces(self):
        assert normalize_phone("(555) 123 4567") == "5551234567"

    def test_strips_plus_one(self):
        assert normalize_phone("+1-555-123-4567") == "5551234567"

    def test_strips_plus_one_no_dashes(self):
        assert normalize_phone("+15551234567") == "5551234567"

    def test_already_clean(self):
        assert normalize_phone("5551234567") == "5551234567"

    def test_empty_string(self):
        assert normalize_phone("") == ""

    def test_dots_as_separators(self):
        assert normalize_phone("555.123.4567") == "5551234567"

    def test_international_non_us(self):
        """Non-US numbers (not 11 digits starting with 1) are kept as-is."""
        assert normalize_phone("+44-20-7946-0958") == "442079460958"


# ═══════════════════════════════════════════════════════════════════════
#  REVERSE LOOKUP — SiteContact
# ═══════════════════════════════════════════════════════════════════════


class TestReverseLookupContact:
    """Test reverse_lookup_phone() matching against SiteContact."""

    def test_matches_site_contact_phone(
        self, db_session: Session, test_company: Company, test_customer_site: CustomerSite
    ):
        """Phone matching a SiteContact returns contact entity with company context."""
        contact = SiteContact(
            customer_site_id=test_customer_site.id,
            full_name="Jane Smith",
            phone="(555) 867-5309",
            is_active=True,
        )
        db_session.add(contact)
        db_session.commit()

        result = reverse_lookup_phone("555-867-5309", db_session)

        assert result is not None
        assert result["entity_type"] == "contact"
        assert result["entity_id"] == contact.id
        assert result["company_id"] == test_company.id
        assert result["contact_name"] == "Jane Smith"


# ═══════════════════════════════════════════════════════════════════════
#  REVERSE LOOKUP — Company
# ═══════════════════════════════════════════════════════════════════════


class TestReverseLookupCompany:
    """Test reverse_lookup_phone() matching against Company."""

    def test_matches_company_phone(self, db_session: Session):
        """Phone matching a Company.phone returns company entity."""
        company = Company(
            name="TechCorp Inc",
            phone="+1 (800) 555-0199",
            is_active=True,
        )
        db_session.add(company)
        db_session.commit()

        result = reverse_lookup_phone("800-555-0199", db_session)

        assert result is not None
        assert result["entity_type"] == "company"
        assert result["entity_id"] == company.id
        assert result["company_id"] == company.id
        assert result["company_name"] == "TechCorp Inc"
        assert result["contact_name"] is None


# ═══════════════════════════════════════════════════════════════════════
#  REVERSE LOOKUP — VendorCard
# ═══════════════════════════════════════════════════════════════════════


class TestReverseLookupVendor:
    """Test reverse_lookup_phone() matching against VendorCard."""

    def test_matches_vendor_phone(self, db_session: Session):
        """Phone matching a VendorCard.phones list returns vendor entity."""
        vendor = VendorCard(
            normalized_name="digikey electronics",
            display_name="DigiKey Electronics",
            phones=["+1-800-344-4539", "218-681-6674"],
            is_blacklisted=False,
        )
        db_session.add(vendor)
        db_session.commit()

        result = reverse_lookup_phone("(800) 344-4539", db_session)

        assert result is not None
        assert result["entity_type"] == "vendor"
        assert result["entity_id"] == vendor.id
        assert result["vendor_card_id"] == vendor.id
        assert result["company_name"] == "DigiKey Electronics"

    def test_skips_blacklisted_vendor(self, db_session: Session):
        """Blacklisted vendors are not returned by reverse lookup."""
        vendor = VendorCard(
            normalized_name="shady parts co",
            display_name="Shady Parts Co",
            phones=["555-000-1111"],
            is_blacklisted=True,
        )
        db_session.add(vendor)
        db_session.commit()

        result = reverse_lookup_phone("555-000-1111", db_session)
        assert result is None


# ═══════════════════════════════════════════════════════════════════════
#  REVERSE LOOKUP — Unknown
# ═══════════════════════════════════════════════════════════════════════


class TestReverseLookupUnknown:
    """Test reverse_lookup_phone() with unknown numbers."""

    def test_unknown_phone_returns_none(self, db_session: Session):
        """Phone number not in any CRM entity returns None."""
        result = reverse_lookup_phone("999-888-7777", db_session)
        assert result is None

    def test_short_phone_returns_none(self, db_session: Session):
        """Phone number with fewer than 7 digits returns None."""
        result = reverse_lookup_phone("123", db_session)
        assert result is None


# ═══════════════════════════════════════════════════════════════════════
#  CDR → CRM LINKING
# ═══════════════════════════════════════════════════════════════════════


class TestCdrLinksToCrm:
    """Test that CDR processing links calls to CRM entities via reverse lookup."""

    def test_cdr_with_known_phone_creates_linked_activity(
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

        with (
            patch("app.services.eight_by_eight_service.get_access_token", return_value="fake-token"),
            patch("app.services.eight_by_eight_service.get_cdrs", return_value=fake_cdrs),
        ):
            from app.jobs.eight_by_eight_jobs import _process_cdrs

            # Need to patch the imports inside _process_cdrs since they're local
            result = _process_cdrs(db_session, _FakeSettings())

        assert result["processed"] == 1
        assert result["matched"] == 1

        # Verify the ActivityLog was linked
        activity = db_session.query(ActivityLog).filter(ActivityLog.external_id == "cdr-link-test-001").first()
        assert activity is not None
        assert activity.company_id == test_company.id
        assert activity.contact_name == "Bob Customer"

    def test_cdr_with_unknown_phone_creates_unlinked_activity(self, db_session: Session):
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

        with (
            patch("app.services.eight_by_eight_service.get_access_token", return_value="fake-token"),
            patch("app.services.eight_by_eight_service.get_cdrs", return_value=fake_cdrs),
        ):
            from app.jobs.eight_by_eight_jobs import _process_cdrs

            result = _process_cdrs(db_session, _FakeSettings())

        assert result["processed"] == 1
        assert result["matched"] == 0

        activity = db_session.query(ActivityLog).filter(ActivityLog.external_id == "cdr-unknown-test-001").first()
        assert activity is not None
        assert activity.company_id is None
        assert activity.vendor_card_id is None


# ═══════════════════════════════════════════════════════════════════════
#  EXTENSION MAPPING
# ═══════════════════════════════════════════════════════════════════════


class TestExtensionMapping:
    """Test get_extension_map() fetches and parses 8x8 user list."""

    def test_extension_maps_to_user(self):
        """Extension mapping returns email for known extensions."""
        from app.services.eight_by_eight_service import get_extension_map

        fake_response = {
            "data": [
                {"extension": "1001", "email": "michael@trio.com"},
                {"extension": "1002", "email": "marcus@trio.com"},
                {"extensionNumber": "1009", "userId": "Martina@trio.com"},
            ]
        }

        with patch("app.services.eight_by_eight_service.httpx.get") as mock_get:
            mock_get.return_value.status_code = 200
            mock_get.return_value.json.return_value = fake_response

            ext_map = get_extension_map("fake-token", _FakeSettings())

        assert ext_map["1001"] == "michael@trio.com"
        assert ext_map["1002"] == "marcus@trio.com"
        assert ext_map["1009"] == "martina@trio.com"  # lowercased
        assert len(ext_map) == 3

    def test_extension_map_handles_api_error(self):
        """Extension mapping returns empty dict on API error."""
        from app.services.eight_by_eight_service import get_extension_map

        with patch("app.services.eight_by_eight_service.httpx.get") as mock_get:
            mock_get.return_value.status_code = 500
            mock_get.return_value.json.return_value = {}

            ext_map = get_extension_map("fake-token", _FakeSettings())

        assert ext_map == {}


# ═══════════════════════════════════════════════════════════════════════
#  CDR LINKS TO OPEN REQUISITION
# ═══════════════════════════════════════════════════════════════════════


class TestCdrLinksToRequisition:
    """Test that CDR processing links to open requisitions for matched companies."""

    def test_cdr_links_to_open_requisition(
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
            status="active",
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

        with (
            patch("app.services.eight_by_eight_service.get_access_token", return_value="fake-token"),
            patch("app.services.eight_by_eight_service.get_cdrs", return_value=fake_cdrs),
        ):
            from app.jobs.eight_by_eight_jobs import _process_cdrs

            result = _process_cdrs(db_session, _FakeSettings())

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
