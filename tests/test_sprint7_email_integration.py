"""test_sprint7_email_integration.py — Tests for Sprint 7 email integration.

Verifies: Email thread viewer, reply form, AI thread summary,
email intelligence dashboard. In test mode, Graph API calls fail
gracefully — we verify the routes handle errors correctly.

Called by: pytest
Depends on: conftest.py fixtures, app.routers.htmx_views
"""

import pytest
from fastapi.testclient import TestClient

# ── Email Thread Viewer ──────────────────────────────────────────────


class TestThreadViewer:
    def test_thread_viewer_renders(self, client: TestClient):
        # In test mode, Graph API isn't available — route should handle gracefully
        resp = client.get(
            "/v2/partials/emails/thread/AAA123",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        assert "Email Thread" in resp.text


# ── Email Reply ──────────────────────────────────────────────────────


class TestEmailReply:
    @pytest.mark.parametrize(
        "data",
        [
            pytest.param({"to": "", "body": ""}, id="missing_fields"),
            pytest.param({"to": "john@arrow.com", "body": ""}, id="missing_body"),
        ],
    )
    def test_reply_rejects_incomplete(self, client: TestClient, data):
        resp = client.post(
            "/v2/partials/emails/reply",
            data=data,
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 400


# ── AI Thread Summary ────────────────────────────────────────────────


class TestThreadSummary:
    def test_summary_renders(self, client: TestClient):
        # In test mode, the Graph API call will fail, but route should handle gracefully
        resp = client.get(
            "/v2/partials/emails/thread/AAA123/summary",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200


# ── Email Intelligence Dashboard ─────────────────────────────────────


class TestEmailIntelligence:
    def test_dashboard_renders(self, client: TestClient):
        resp = client.get(
            "/v2/partials/email-intelligence",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        assert "AI-classified inbox activity" in resp.text

    def test_dashboard_with_filter(self, client: TestClient):
        resp = client.get(
            "/v2/partials/email-intelligence?classification=offer",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        assert "AI-classified inbox activity" in resp.text

    def test_dashboard_empty_state(self, client: TestClient):
        resp = client.get(
            "/v2/partials/email-intelligence?classification=nonexistent",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        assert "No email intelligence records found" in resp.text
        # Pre-migration spacing (P5.3 empty_state.html dedup): the shared partial's
        # icon_class default adds "mb-4", but this dashboard's hand-rolled markup
        # never had that margin — icon_class must be passed explicitly so the
        # rendered icon stays pixel-identical to pre-dedup.
        assert 'class="mx-auto h-12 w-12 text-gray-300"' in resp.text
        assert 'class="mx-auto mb-4 h-12 w-12 text-gray-300"' not in resp.text
