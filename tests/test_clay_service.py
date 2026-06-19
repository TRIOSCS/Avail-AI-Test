"""Tests for the Clay async enrichment service (app/services/clay_service.py).

Covers: the outbound webhook request (correlation token + secret header),
graceful skip when unconfigured, timing-safe secret verification, and the
inbound callback that routes enriched fields into the EnrichmentQueue.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from app.services import clay_service


def _resp(status=200, text=""):
    r = MagicMock()
    r.status_code = status
    r.text = text
    return r


# ── request_clay_enrichment ──────────────────────────────────────────


class TestRequestClayEnrichment:
    def test_skips_when_webhook_not_configured(self):
        with patch.object(clay_service, "_clay_webhook_url", return_value=""):
            result = asyncio.run(
                clay_service.request_clay_enrichment("example.com", "company", 1)
            )
        assert result["status"] == "skipped"

    def test_rejects_unsupported_entity_type(self):
        with patch.object(clay_service, "_clay_webhook_url", return_value="https://clay/webhook"):
            result = asyncio.run(
                clay_service.request_clay_enrichment("example.com", "widget", 1)
            )
        assert result["status"] == "error"

    def test_success_posts_row_and_stores_token(self):
        with patch.object(clay_service, "_clay_webhook_url", return_value="https://clay/webhook"), \
             patch.object(clay_service, "_clay_secret", return_value="s3cret"), \
             patch.object(clay_service, "set_cached") as mock_set, \
             patch.object(clay_service, "http") as mock_http:
            mock_http.post = AsyncMock(return_value=_resp(202))
            result = asyncio.run(
                clay_service.request_clay_enrichment("example.com", "vendor_card", 7)
            )
        assert result["status"] == "requested"
        token = result["correlation_token"]
        # Correlation stored keyed by token
        stored_key, stored_val = mock_set.call_args.args[0], mock_set.call_args.args[1]
        assert token in stored_key
        assert stored_val == {"entity_type": "vendor_card", "entity_id": 7, "domain": "example.com"}
        # Body carries the domain + token; secret rides in the header
        call = mock_http.post.call_args
        assert call.kwargs["json"]["domain"] == "example.com"
        assert call.kwargs["json"]["correlation_token"] == token
        assert call.kwargs["headers"]["x-clay-secret"] == "s3cret"

    def test_webhook_non_2xx_invalidates_token(self):
        with patch.object(clay_service, "_clay_webhook_url", return_value="https://clay/webhook"), \
             patch.object(clay_service, "_clay_secret", return_value="s"), \
             patch.object(clay_service, "set_cached"), \
             patch.object(clay_service, "invalidate") as mock_inval, \
             patch.object(clay_service, "http") as mock_http:
            mock_http.post = AsyncMock(return_value=_resp(500, "err"))
            result = asyncio.run(
                clay_service.request_clay_enrichment("example.com", "company", 1)
            )
        assert result["status"] == "error"
        mock_inval.assert_called_once()


# ── verify_clay_secret ───────────────────────────────────────────────


class TestVerifyClaySecret:
    def test_rejects_when_no_secret_configured(self):
        with patch.object(clay_service, "_clay_secret", return_value=""):
            assert clay_service.verify_clay_secret("anything") is False

    def test_accepts_matching_secret(self):
        with patch.object(clay_service, "_clay_secret", return_value="abc123"):
            assert clay_service.verify_clay_secret("abc123") is True

    def test_rejects_wrong_secret(self):
        with patch.object(clay_service, "_clay_secret", return_value="abc123"):
            assert clay_service.verify_clay_secret("nope") is False
            assert clay_service.verify_clay_secret(None) is False


# ── handle_clay_callback ─────────────────────────────────────────────


class TestHandleClayCallback:
    def test_missing_token_rejected(self):
        result = clay_service.handle_clay_callback({}, MagicMock())
        assert result["status"] == "rejected"

    def test_unknown_token_rejected(self):
        with patch.object(clay_service, "get_cached", return_value=None):
            result = clay_service.handle_clay_callback(
                {"correlation_token": "abc"}, MagicMock()
            )
        assert result["status"] == "rejected"

    def test_applies_company_fields_and_contacts(self):
        corr = {"entity_type": "company", "entity_id": 42, "domain": "example.com"}
        payload = {
            "correlation_token": "tok",
            "company": {
                "legal_name": "Example Corp",
                "industry": "Electronics",
                "website": "https://example.com",
            },
            "contacts": [
                {"full_name": "Jane Doe", "title": "Buyer",
                 "email": "jane@example.com", "email_confidence": "A1"},
            ],
        }
        db = MagicMock()
        with patch.object(clay_service, "get_cached", return_value=corr), \
             patch.object(clay_service, "invalidate") as mock_inval, \
             patch("app.services.deep_enrichment_service.route_enrichment") as mock_route:
            result = clay_service.handle_clay_callback(payload, db)

        assert result["status"] == "applied"
        assert result["entity_type"] == "company"
        assert set(result["company_fields"]) == {"legal_name", "industry", "website"}
        assert result["contacts"] == 1
        db.commit.assert_called_once()
        mock_inval.assert_called_once()  # one-time token consumed
        # 3 company fields + 1 contact = 4 routed enrichments
        assert mock_route.call_count == 4
        # Contact routed as a new_contact:* field with high confidence
        contact_calls = [c for c in mock_route.call_args_list if "new_contact:" in c.args[3]]
        assert len(contact_calls) == 1
        assert contact_calls[0].kwargs["confidence"] >= 0.85

    def test_flat_company_fields_supported(self):
        corr = {"entity_type": "vendor_card", "entity_id": 5, "domain": "x.com"}
        payload = {"correlation_token": "tok", "industry": "Semiconductors"}
        db = MagicMock()
        with patch.object(clay_service, "get_cached", return_value=corr), \
             patch.object(clay_service, "invalidate"), \
             patch("app.services.deep_enrichment_service.route_enrichment") as mock_route:
            result = clay_service.handle_clay_callback(payload, db)
        assert result["status"] == "applied"
        assert "industry" in result["company_fields"]
        assert mock_route.call_args.kwargs["source"] == "clay"


# ── /api/webhooks/clay endpoint ──────────────────────────────────────


def test_clay_webhook_rejects_bad_secret(client):
    with patch("app.services.clay_service.verify_clay_secret", return_value=False):
        resp = client.post(
            "/api/webhooks/clay",
            json={"correlation_token": "x"},
            headers={"x-clay-secret": "wrong"},
        )
    assert resp.status_code == 403


def test_clay_webhook_accepts_valid_secret(client):
    with patch("app.services.clay_service.verify_clay_secret", return_value=True), \
         patch("app.services.clay_service.handle_clay_callback", return_value={"status": "applied"}):
        resp = client.post(
            "/api/webhooks/clay",
            json={"correlation_token": "tok", "industry": "Electronics"},
            headers={"x-clay-secret": "right"},
        )
    assert resp.status_code == 200
    assert resp.json()["status"] == "applied"


def test_clay_webhook_rejects_non_object_payload(client):
    with patch("app.services.clay_service.verify_clay_secret", return_value=True):
        resp = client.post(
            "/api/webhooks/clay",
            json=["not", "an", "object"],
            headers={"x-clay-secret": "right"},
        )
    assert resp.status_code == 400
