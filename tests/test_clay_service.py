"""Tests for the Clay async enrichment service (app/services/clay_service.py).

Covers the outbound webhook request (token + secret, circuit, quota), secret + HMAC
verification, and the inbound callback applying firmographics + contacts.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from app.services import clay_service


def _resp(status=200):
    r = MagicMock()
    r.status_code = status
    return r


# ── request_enrichment ───────────────────────────────────────────────


class TestRequestEnrichment:
    def test_skips_when_disabled(self):
        with patch.object(clay_service, "enabled_and_configured", return_value=False):
            out = asyncio.run(clay_service.request_enrichment("x.com", "company", 1))
        assert out["status"] == "skipped"

    def test_skips_when_circuit_open(self):
        with (
            patch.object(clay_service, "enabled_and_configured", return_value=True),
            patch.object(clay_service, "circuit_open", return_value=True),
        ):
            out = asyncio.run(clay_service.request_enrichment("x.com", "company", 1))
        assert out["status"] == "skipped"
        assert out["reason"] == "circuit_open"

    def test_success_posts_and_stores_token(self):
        with (
            patch.object(clay_service, "enabled_and_configured", return_value=True),
            patch.object(clay_service, "circuit_open", return_value=False),
            patch.object(clay_service, "_webhook_url", return_value="https://clay/webhook"),
            patch.object(clay_service, "_secret", return_value="s3cret"),
            patch.object(clay_service, "set_cached") as mock_set,
            patch.object(clay_service, "http") as mock_http,
        ):
            mock_http.post = AsyncMock(return_value=_resp(202))
            out = asyncio.run(clay_service.request_enrichment("x.com", "vendor_card", 9))
        assert out["status"] == "requested"
        token = out["correlation_token"]
        assert token in mock_set.call_args.args[0]
        assert mock_set.call_args.args[1] == {"entity_type": "vendor_card", "entity_id": 9, "domain": "x.com"}
        call = mock_http.post.call_args
        assert call.kwargs["json"]["correlation_token"] == token
        assert call.kwargs["headers"]["x-clay-secret"] == "s3cret"

    def test_quota_trips_circuit(self):
        with (
            patch.object(clay_service, "enabled_and_configured", return_value=True),
            patch.object(clay_service, "circuit_open", return_value=False),
            patch.object(clay_service, "_webhook_url", return_value="https://clay/webhook"),
            patch.object(clay_service, "_secret", return_value="s"),
            patch.object(clay_service, "set_cached"),
            patch.object(clay_service, "trip_circuit") as mock_trip,
            patch.object(clay_service, "http") as mock_http,
        ):
            mock_http.post = AsyncMock(return_value=_resp(429))
            out = asyncio.run(clay_service.request_enrichment("x.com", "company", 1))
        assert out["status"] == "error"
        mock_trip.assert_called_once()


# ── secret / signature ───────────────────────────────────────────────


class TestVerify:
    def test_secret_rejects_when_unconfigured(self):
        with patch.object(clay_service, "_secret", return_value=""):
            assert clay_service.verify_secret("x") is False

    def test_secret_match(self):
        with patch.object(clay_service, "_secret", return_value="abc"):
            assert clay_service.verify_secret("abc") is True
            assert clay_service.verify_secret("nope") is False

    def test_signature(self):
        import hashlib
        import hmac

        body = b'{"correlation_token":"t"}'
        sig = hmac.new(b"k", body, hashlib.sha256).hexdigest()
        with patch.object(clay_service, "_secret", return_value="k"):
            assert clay_service.verify_signature(body, sig) is True
            assert clay_service.verify_signature(body, "sha256=" + sig) is True
            assert clay_service.verify_signature(body, "bad") is False


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


# ── /api/webhooks/clay endpoint ──────────────────────────────────────


def test_endpoint_rejects_bad_secret(client):
    with patch("app.services.clay_service.verify_secret", return_value=False):
        r = client.post("/api/webhooks/clay", json={"correlation_token": "t"}, headers={"x-clay-secret": "wrong"})
    assert r.status_code == 403


def test_endpoint_oversize_payload(client):
    big = "x" * (clay_service.MAX_CALLBACK_BYTES + 1)
    with patch("app.services.clay_service.verify_secret", return_value=True):
        r = client.post(
            "/api/webhooks/clay",
            content=('{"correlation_token":"t","p":"%s"}' % big).encode(),
            headers={"x-clay-secret": "ok", "Content-Type": "application/json"},
        )
    assert r.status_code == 413


def test_endpoint_bad_signature(client):
    with (
        patch("app.services.clay_service.verify_secret", return_value=True),
        patch("app.services.clay_service.verify_signature", return_value=False),
    ):
        r = client.post(
            "/api/webhooks/clay",
            json={"correlation_token": "t"},
            headers={"x-clay-secret": "ok", "x-clay-signature": "bad"},
        )
    assert r.status_code == 403


def test_endpoint_accepts_valid(client):
    with (
        patch("app.services.clay_service.verify_secret", return_value=True),
        patch("app.services.clay_service.handle_callback", return_value={"status": "applied"}),
    ):
        r = client.post(
            "/api/webhooks/clay", json={"correlation_token": "t", "industry": "X"}, headers={"x-clay-secret": "ok"}
        )
    assert r.status_code == 200
    assert r.json()["status"] == "applied"


# ── Additional branch coverage ────────────────────────────────────────────────


class TestRequestEnrichmentEdgeCases:
    def test_unsupported_entity_type_returns_error(self):
        with patch.object(clay_service, "enabled_and_configured", return_value=True):
            with patch.object(clay_service, "circuit_open", return_value=False):
                out = asyncio.run(clay_service.request_enrichment("x.com", "contact", 1))
        assert out["status"] == "error"
        assert "unsupported" in out["reason"]

    def test_network_error_returns_error(self):
        with (
            patch.object(clay_service, "enabled_and_configured", return_value=True),
            patch.object(clay_service, "circuit_open", return_value=False),
            patch.object(clay_service, "_webhook_url", return_value="https://clay/hook"),
            patch.object(clay_service, "_secret", return_value=""),
            patch.object(clay_service, "set_cached"),
            patch.object(clay_service, "http") as mock_http,
        ):
            mock_http.post = AsyncMock(side_effect=Exception("connection refused"))
            out = asyncio.run(clay_service.request_enrichment("x.com", "company", 1))
        assert out["status"] == "error"
        assert "connection refused" in out["reason"]

    def test_non_2xx_response_returns_error(self):
        with (
            patch.object(clay_service, "enabled_and_configured", return_value=True),
            patch.object(clay_service, "circuit_open", return_value=False),
            patch.object(clay_service, "_webhook_url", return_value="https://clay/hook"),
            patch.object(clay_service, "_secret", return_value=""),
            patch.object(clay_service, "set_cached"),
            patch.object(clay_service, "http") as mock_http,
        ):
            mock_http.post = AsyncMock(return_value=_resp(400))
            out = asyncio.run(clay_service.request_enrichment("x.com", "company", 1))
        assert out["status"] == "error"
        assert "400" in out["reason"]


class TestVerifySignatureEdgeCases:
    def test_signature_without_prefix(self):
        import hashlib
        import hmac

        body = b'{"x":1}'
        raw_sig = hmac.new(b"secret", body, hashlib.sha256).hexdigest()
        with patch.object(clay_service, "_secret", return_value="secret"):
            assert clay_service.verify_signature(body, raw_sig) is True

    def test_empty_provided_returns_false(self):
        with patch.object(clay_service, "_secret", return_value="secret"):
            assert clay_service.verify_signature(b"body", None) is False


class TestConfidenceFromMarker:
    def test_none_returns_70(self):
        assert clay_service._confidence_from_marker(None) == 70

    def test_int_above_1(self):
        assert clay_service._confidence_from_marker(90) == 90

    def test_float_below_1(self):
        assert clay_service._confidence_from_marker(0.8) == 80

    def test_string_high(self):
        assert clay_service._confidence_from_marker("high") == 90

    def test_string_medium(self):
        assert clay_service._confidence_from_marker("b") == 70

    def test_string_low(self):
        assert clay_service._confidence_from_marker("c") == 40

    def test_string_unknown_returns_default(self):
        assert clay_service._confidence_from_marker("unknown_val") == 70


class TestHandleCallbackEdgeCases:
    def test_company_not_found(self):
        corr = {"entity_type": "company", "entity_id": 99999, "domain": "missing.com"}
        db = MagicMock()
        db.get.return_value = None
        with (
            patch.object(clay_service, "get_cached", return_value=corr),
            patch.object(clay_service, "set_cached"),
        ):
            out = clay_service.handle_callback({"correlation_token": "tok"}, db)
        assert out["status"] == "rejected"
        assert out["reason"] == "company_not_found"

    def test_commit_failure_returns_error(self, db_session, test_vendor_card):
        corr = {"entity_type": "vendor_card", "entity_id": test_vendor_card.id, "domain": "co.com"}
        payload = {"correlation_token": "tok"}
        with (
            patch.object(clay_service, "get_cached", return_value=corr),
            patch.object(clay_service, "set_cached"),
            patch("app.enrichment_service.apply_enrichment_to_vendor", return_value=[]),
            patch.object(db_session, "commit", side_effect=Exception("db error")),
        ):
            out = clay_service.handle_callback(payload, db_session)
        assert out["status"] == "error"
        assert out["reason"] == "commit_failed"


class TestAddVendorContactsEdgeCases:
    def test_skip_non_dict_contacts(self, db_session, test_vendor_card):
        from app.models import VendorContact

        contacts = ["not-a-dict", 42]
        count = clay_service._add_vendor_contacts(db_session, VendorContact, test_vendor_card.id, contacts)
        assert count == 0

    def test_skip_contact_with_no_email_and_no_name(self, db_session, test_vendor_card):
        from app.models import VendorContact

        contacts = [{"email": "", "full_name": None}]
        count = clay_service._add_vendor_contacts(db_session, VendorContact, test_vendor_card.id, contacts)
        assert count == 0

    def test_skip_duplicate_email(self, db_session, test_vendor_card, test_vendor_contact):
        from app.models import VendorContact

        existing_email = test_vendor_contact.email
        contacts = [{"email": existing_email, "full_name": "Jane"}]
        count = clay_service._add_vendor_contacts(db_session, VendorContact, test_vendor_card.id, contacts)
        assert count == 0


class TestAddSiteContactsEdgeCases:
    def test_skip_contact_with_no_name(self, db_session, test_customer_site):
        from app.models import SiteContact

        contacts = [{"full_name": None, "name": None, "email": "x@y.com"}]
        count = clay_service._add_site_contacts(db_session, SiteContact, test_customer_site.id, contacts)
        assert count == 0

    def test_skip_non_dict(self, db_session, test_customer_site):
        from app.models import SiteContact

        count = clay_service._add_site_contacts(db_session, SiteContact, test_customer_site.id, ["bad"])
        assert count == 0
