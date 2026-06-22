"""Tests for the Clay enrichment service (app/services/clay_service.py).

The webhook round-trip (request_enrichment / verify_secret / verify_signature /
/api/webhooks/clay endpoint) was removed when Clay moved to the MCP connector.
These tests cover the retained helpers: handle_callback (for potential future
re-use), _add_vendor_contacts, _add_site_contacts, and _confidence_from_marker.
"""

from unittest.mock import MagicMock, patch

from app.services import clay_service

# ── Removal assertions ───────────────────────────────────────────────


def test_clay_webhook_path_removed():
    """request_enrichment and the secret/signature helpers must be gone."""
    assert not hasattr(clay_service, "request_enrichment")
    assert not hasattr(clay_service, "verify_secret")
    assert not hasattr(clay_service, "verify_signature")
    assert not hasattr(clay_service, "_webhook_url")
    assert not hasattr(clay_service, "_secret")


# ── handle_callback ──────────────────────────────────────────────────


class TestHandleCallback:
    def test_missing_token(self):
        assert clay_service.handle_callback({}, MagicMock())["status"] == "rejected"

    def test_unknown_token(self):
        with patch.object(clay_service, "get_cached", return_value=None):
            out = clay_service.handle_callback({"correlation_token": "t"}, MagicMock())
        assert out["reason"] == "unknown_or_expired_token"

    def test_consumed_token_rejected(self):
        with patch.object(clay_service, "get_cached", return_value={"consumed": True}):
            out = clay_service.handle_callback({"correlation_token": "t"}, MagicMock())
        assert out["reason"] == "token_already_used"

    def test_applies_to_vendor(self, db_session, test_vendor_card):
        from app.models import VendorContact

        corr = {"entity_type": "vendor_card", "entity_id": test_vendor_card.id, "domain": "arrow.com"}
        payload = {
            "correlation_token": "tok",
            "company": {"legal_name": "Arrow Electronics Inc", "industry": "Distribution"},
            "contacts": [
                {"full_name": "Jane Buyer", "title": "Buyer", "email": "jane@arrow.com", "email_confidence": "A"}
            ],
        }
        with patch.object(clay_service, "get_cached", return_value=corr), patch.object(clay_service, "set_cached"):
            out = clay_service.handle_callback(payload, db_session)
        assert out["status"] == "applied"
        assert "legal_name" in out["company_fields"]
        assert out["contacts"] == 1
        assert (
            db_session.query(VendorContact)
            .filter_by(vendor_card_id=test_vendor_card.id, email="jane@arrow.com")
            .count()
            == 1
        )

    def test_applies_to_company_with_site(self, db_session, test_company, test_customer_site):
        from app.models import SiteContact

        corr = {"entity_type": "company", "entity_id": test_company.id, "domain": "acme.com"}
        payload = {
            "correlation_token": "tok",
            "industry": "Electronics",  # flat firmographic
            "contacts": [{"full_name": "Sam Sourcing", "email": "sam@acme.com"}],
        }
        with patch.object(clay_service, "get_cached", return_value=corr), patch.object(clay_service, "set_cached"):
            out = clay_service.handle_callback(payload, db_session)
        assert out["status"] == "applied"
        assert out["contacts"] == 1
        assert (
            db_session.query(SiteContact)
            .filter_by(customer_site_id=test_customer_site.id, email="sam@acme.com")
            .count()
            == 1
        )
