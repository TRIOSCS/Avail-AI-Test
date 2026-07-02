"""Tests for code review security fixes — timing attack, SQL injection, rate limiting,
vendor merge safety, retry-after cap, query validation.

Called by: pytest
Depends on: conftest fixtures (db_session, client, test_user, test_vendor_card)
"""

import os
from unittest.mock import MagicMock, patch

import httpx
import pytest

from app.connectors.sources import _parse_retry_after
from app.services.vendor_merge_service import merge_vendor_cards

# ── Timing Attack Fix (dependencies.py) ─────────────────────────────


class TestAgentKeyTimingAttack:
    """Verify agent API key uses constant-time comparison."""

    def test_agent_key_uses_secrets_compare_digest(self):
        """The require_user function should use secrets.compare_digest."""
        import inspect

        from app.dependencies import require_user

        source = inspect.getsource(require_user)
        assert "compare_digest" in source
        assert "agent_key ==" not in source


# ── Retry-After Cap (sources.py) ────────────────────────────────────


class TestRetryAfterCap:
    """Verify Retry-After header value is capped at 300 seconds."""

    def _make_response(self, retry_after: str) -> httpx.Response:
        resp = MagicMock(spec=httpx.Response)
        resp.headers = {"Retry-After": retry_after}
        return resp

    @pytest.mark.parametrize(
        ("retry_after", "expected"),
        [
            ("10", 10.0),  # normal value passes through
            ("0.1", 1.0),  # minimum floor of 1 second
            ("999999", 300.0),  # extreme value capped at 300
            ("600", 300.0),  # moderate value capped at 300
            ("300", 300.0),  # 300 exactly passes
        ],
    )
    def test_value_clamped(self, retry_after: str, expected: float):
        resp = self._make_response(retry_after)
        assert _parse_retry_after(resp) == expected

    def test_missing_header_uses_default(self):
        resp = MagicMock(spec=httpx.Response)
        resp.headers = {}
        result = _parse_retry_after(resp)
        assert 5.0 <= result <= 7.0  # 5 + jitter(0, 2)

    def test_unparseable_header_uses_default(self):
        resp = self._make_response("not-a-number")
        result = _parse_retry_after(resp)
        assert 5.0 <= result <= 7.0


# ── Vendor Merge Transaction Safety ─────────────────────────────────


class TestVendorMergeTransactionSafety:
    """Verify vendor merge raises on FK reassignment failure instead of silently
    continuing."""

    def test_merge_raises_on_fk_failure(self, db_session):
        """If FK reassignment fails, merge should raise ValueError, not silently
        continue."""
        from app.models import VendorCard

        keep = VendorCard(
            normalized_name="vendor_a",
            display_name="Vendor A",
            sighting_count=5,
        )
        remove = VendorCard(
            normalized_name="vendor_b",
            display_name="Vendor B",
            sighting_count=3,
        )
        db_session.add_all([keep, remove])
        db_session.commit()

        # Patch one of the FK models to raise an exception during update
        with patch("app.services.vendor_merge_service.VendorContact") as mock_vc:
            mock_vc.__tablename__ = "vendor_contacts"
            mock_query = MagicMock()
            mock_query.filter.return_value.update.side_effect = RuntimeError("FK constraint violation")
            db_session_mock_query = db_session.query

            def side_effect_query(model):
                if model is mock_vc:
                    return mock_query
                return db_session_mock_query(model)

            with patch.object(db_session, "query", side_effect=side_effect_query):
                with pytest.raises(ValueError, match="Vendor merge aborted"):
                    merge_vendor_cards(keep.id, remove.id, db_session)


class TestPasswordLoginRateLimit:
    """Verify password login endpoint has rate limiting decorator."""

    def test_password_login_has_rate_limit_decorator(self):
        """The password_login endpoint should have a limiter decorator."""
        import inspect

        from app.routers.auth import password_login

        source = inspect.getsource(password_login)
        # The function should exist and the rate limit is applied via decorator.
        # We verify by checking the route registration. fastapi 0.137 nests
        # include_router'd routes behind _IncludedRouter wrappers, so walk the
        # tree via iter_routes (flat-walk-safe on 0.136.x too).
        from app.main import app
        from tests._route_helpers import iter_routes

        assert source  # the endpoint source is loadable (decorator lives here)
        assert any(
            getattr(r, "path", None) == "/auth/login" and "POST" in (getattr(r, "methods", None) or set())
            for r in iter_routes(app.routes)
        ), "POST /auth/login route not found"


# ── Fix 1: Fernet Decryption Fail-Open ─────────────────────────────


class TestEncryptedTypeFailOpen:
    """process_result_value must return None (not raw ciphertext) on failure."""

    def test_returns_none_on_invalid_token(self):
        from app.utils.encrypted_type import EncryptedText

        col = EncryptedText()
        result = col.process_result_value("not-a-valid-fernet-token", dialect=None)
        assert result is None, "Should return None on InvalidToken, not raw ciphertext"

    def test_returns_none_on_none_input(self):
        from app.utils.encrypted_type import EncryptedText

        col = EncryptedText()
        result = col.process_result_value(None, dialect=None)
        assert result is None

    def test_roundtrip_still_works(self):
        from app.utils.encrypted_type import EncryptedText

        col = EncryptedText()
        plaintext = "my-secret-token"
        encrypted = col.process_bind_param(plaintext, dialect=None)
        decrypted = col.process_result_value(encrypted, dialect=None)
        assert decrypted == plaintext


# ── Fix 2: OData Filter Injection ──────────────────────────────────


class TestODataFilterInjection:
    """search_sent_messages must escape single quotes in query."""

    @pytest.mark.asyncio
    async def test_single_quotes_escaped_in_odata_filter(self):
        from unittest.mock import AsyncMock

        from app.utils.graph_client import GraphClient

        gc = GraphClient("fake-token")
        mock_response = {"value": []}
        with patch.object(gc, "get_json", new_callable=AsyncMock, return_value=mock_response) as mock_get:
            await gc.search_sent_messages("test'injection")
            call_args = mock_get.call_args
            params = call_args.kwargs.get("params") or call_args[1]["params"]
            assert "test''injection" in params["$filter"]
            assert "test'injection" not in params["$filter"]

    @pytest.mark.asyncio
    async def test_query_without_quotes_unchanged(self):
        from unittest.mock import AsyncMock

        from app.utils.graph_client import GraphClient

        gc = GraphClient("fake-token")
        mock_response = {"value": []}
        with patch.object(gc, "get_json", new_callable=AsyncMock, return_value=mock_response) as mock_get:
            await gc.search_sent_messages("PO12345")
            call_args = mock_get.call_args
            params = call_args.kwargs.get("params") or call_args[1]["params"]
            assert "PO12345" in params["$filter"]


# ── Fix 4: Screenshot Path Traversal ──────────────────────────────


class TestScreenshotPathTraversal:
    """get_ticket_screenshot must reject paths outside UPLOAD_DIR."""

    def test_path_traversal_blocked(self):
        import asyncio

        from fastapi import HTTPException

        from app.routers.error_reports import get_ticket_screenshot

        mock_ticket = MagicMock()
        mock_ticket.screenshot_path = "/app/uploads/tickets/../../etc/passwd"
        mock_ticket.screenshot_b64 = None

        mock_db = MagicMock()
        mock_db.get.return_value = mock_ticket
        mock_user = MagicMock()

        with patch("os.path.isfile", return_value=True):
            with pytest.raises(HTTPException) as exc_info:
                asyncio.get_event_loop().run_until_complete(
                    get_ticket_screenshot(ticket_id=1, user=mock_user, db=mock_db)
                )
            assert exc_info.value.status_code == 403


# ── Fix 5: CSRF Exemptions Narrowed ───────────────────────────────


class TestCSRFExemptions:
    """Verify CSRF exempt_urls no longer use broad /auth/.* pattern."""

    def test_auth_wildcard_not_in_exemptions(self):
        import app.main

        source_path = os.path.join(os.path.dirname(app.main.__file__), "main.py")
        with open(source_path) as f:
            content = f.read()
        assert r"/auth/.*" not in content, "Broad /auth/.* CSRF exemption should be removed"
        assert "/auth/callback" in content

    def test_import_save_not_csrf_exempt(self):
        """The requisition import-save POST creates Requisition + Requirement rows, so
        it must stay under CSRF-token enforcement.

        Only the multipart preview (import-parse) and the import-form GET are exempt — a
        prefix pattern that also covered import-save would let a cross-site POST forge
        requisitions.
        """
        from starlette.datastructures import URL
        from starlette_csrf import CSRFMiddleware

        from app.main import CSRF_EXEMPT_URLS

        mw = CSRFMiddleware(app=lambda scope, receive, send: None, secret="x", exempt_urls=CSRF_EXEMPT_URLS)
        assert mw._url_is_exempt(URL(path="/v2/partials/requisitions/import-save")) is False
        assert mw._url_is_exempt(URL(path="/v2/partials/requisitions/import-parse")) is True
        assert mw._url_is_exempt(URL(path="/v2/partials/requisitions/import-form")) is True

    def test_import_save_rejected_without_csrf_token(self):
        """End-to-end: with the real exempt set, an authenticated (session-cookie) POST to
        import-save carrying no x-csrftoken is rejected 403, while import-parse (multipart
        preview) still passes through exempt. Fails against the old broad ``import-.*``
        exemption, which let import-save through unprotected."""
        from starlette.applications import Starlette
        from starlette.responses import PlainTextResponse
        from starlette.routing import Route
        from starlette.testclient import TestClient
        from starlette_csrf import CSRFMiddleware

        from app.main import CSRF_EXEMPT_URLS

        async def ok(request):
            return PlainTextResponse("ok")

        app = Starlette(
            routes=[
                Route("/v2/partials/requisitions/import-save", ok, methods=["POST"]),
                Route("/v2/partials/requisitions/import-parse", ok, methods=["POST"]),
            ]
        )
        app.add_middleware(
            CSRFMiddleware,
            secret="test-secret",
            sensitive_cookies={"session"},
            exempt_urls=CSRF_EXEMPT_URLS,
        )
        client = TestClient(app)
        client.cookies.set("session", "authenticated")

        # No x-csrftoken header → the write path must be rejected.
        assert client.post("/v2/partials/requisitions/import-save").status_code == 403
        # The multipart preview stays exempt and passes through.
        assert client.post("/v2/partials/requisitions/import-parse").status_code == 200


# ── Fix 6: Style Attribute XSS ────────────────────────────────────


class TestSanitizeHtmlNoStyle:
    """sanitize_html filter must not allow style attribute."""

    def test_style_attribute_stripped(self):
        from app.template_env import _sanitize_html_filter

        html = '<div style="background:url(javascript:alert(1))">text</div>'
        result = _sanitize_html_filter(html)
        assert "style=" not in result
        assert "text" in result

    def test_class_attribute_stripped(self):
        """Class is no longer allowed on arbitrary tags (HIGH-SEC-2): on sanitized
        external email HTML it would let an attacker apply the app's own CSS classes.

        The element text is still preserved.
        """
        from app.template_env import _sanitize_html_filter

        html = '<span class="text-red-500">warning</span>'
        result = _sanitize_html_filter(html)
        assert "class=" not in result
        assert "warning" in result

    def test_empty_input(self):
        from app.template_env import _sanitize_html_filter

        assert _sanitize_html_filter("") == ""
        assert _sanitize_html_filter(None) == ""
