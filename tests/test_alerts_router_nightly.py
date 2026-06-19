"""test_alerts_router_nightly.py — Coverage boost for app/routers/alerts.py.

Targets missing lines: _badge_html branches, exception-quiet paths in alert_badge
and alert_seen, tab_for_kind returning None.

Called by: pytest
Depends on: tests/conftest.py (client, db_session, test_user)
"""

from __future__ import annotations

import os

os.environ["TESTING"] = "1"
os.environ["RATE_LIMIT_ENABLED"] = "false"

from unittest.mock import patch

# ── _badge_html unit tests ────────────────────────────────────────────


class TestBadgeHtml:
    def test_badge_html_zero_returns_empty(self):
        from app.routers.alerts import _badge_html

        assert _badge_html(0) == ""

    def test_badge_html_positive_contains_count(self):
        from app.routers.alerts import _badge_html

        html = _badge_html(3)
        assert "3" in html
        assert "emerald" in html or "px-" in html  # pill class present


# ── GET /v2/partials/alerts/{tab_key}/badge ───────────────────────────


class TestAlertBadgeEndpoint:
    def test_alert_badge_with_count(self, client):
        """count_for_tab returns 5 → HTML contains '5'."""
        with patch("app.routers.alerts.count_for_tab", return_value=5):
            resp = client.get("/v2/partials/alerts/inbox/badge")
        assert resp.status_code == 200
        assert "5" in resp.text

    def test_alert_badge_zero_returns_empty(self, client):
        """count_for_tab returns 0 → response body is empty string."""
        with patch("app.routers.alerts.count_for_tab", return_value=0):
            resp = client.get("/v2/partials/alerts/inbox/badge")
        assert resp.status_code == 200
        assert resp.text == ""

    def test_alert_badge_exception_is_quiet(self, client):
        """count_for_tab raises → endpoint returns 200 with empty body (no 500)."""
        with patch("app.routers.alerts.count_for_tab", side_effect=RuntimeError("boom")):
            resp = client.get("/v2/partials/alerts/inbox/badge")
        assert resp.status_code == 200
        assert resp.text == ""


# ── POST /v2/partials/alerts/{kind}/seen ─────────────────────────────


class TestAlertSeenEndpoint:
    def test_alert_seen_no_tab_returns_empty(self, client):
        """tab_for_kind returns None → empty HTML response."""
        with (
            patch("app.routers.alerts.record_seen"),
            patch("app.routers.alerts.tab_for_kind", return_value=None),
        ):
            resp = client.post(
                "/v2/partials/alerts/unknown_kind/seen",
                data={"ref_ids": "1"},
            )
        assert resp.status_code == 200
        assert resp.text == ""

    def test_alert_seen_marks_and_returns_badge(self, client):
        """record_seen called, badge OOB span returned with tab-nav-badge id."""
        with (
            patch("app.routers.alerts.record_seen") as mock_seen,
            patch("app.routers.alerts.tab_for_kind", return_value="requisitions"),
            patch("app.routers.alerts.count_for_tab", return_value=2),
        ):
            resp = client.post(
                "/v2/partials/alerts/offer_confirmed/seen",
                data={"ref_ids": "42"},
            )
        assert resp.status_code == 200
        assert mock_seen.called
        assert 'id="requisitions-nav-badge"' in resp.text
        assert 'hx-swap-oob="innerHTML"' in resp.text

    def test_alert_seen_record_seen_exception_is_quiet(self, client):
        """record_seen raises → no 500; badge still returned."""
        with (
            patch("app.routers.alerts.record_seen", side_effect=ValueError("bad")),
            patch("app.routers.alerts.tab_for_kind", return_value="requisitions"),
            patch("app.routers.alerts.count_for_tab", return_value=0),
        ):
            resp = client.post(
                "/v2/partials/alerts/offer_confirmed/seen",
                data={"ref_ids": "7"},
            )
        assert resp.status_code == 200
        # No 500 — cosmetic seen-ping must never crash

    def test_alert_seen_badge_exception_returns_empty(self, client):
        """count_for_tab raises after seen → endpoint returns empty (not 500)."""
        with (
            patch("app.routers.alerts.record_seen"),
            patch("app.routers.alerts.tab_for_kind", return_value="requisitions"),
            patch("app.routers.alerts.count_for_tab", side_effect=RuntimeError("badge fail")),
        ):
            resp = client.post(
                "/v2/partials/alerts/offer_confirmed/seen",
                data={"ref_ids": "99"},
            )
        assert resp.status_code == 200
        assert resp.text == ""

    def test_alert_seen_multiple_ref_ids(self, client):
        """Comma-separated ref_ids: record_seen called for each non-empty token."""
        call_args = []
        with (
            patch("app.routers.alerts.record_seen", side_effect=lambda db, u, k, rid: call_args.append(rid)),
            patch("app.routers.alerts.tab_for_kind", return_value=None),
        ):
            resp = client.post(
                "/v2/partials/alerts/offer_confirmed/seen",
                data={"ref_ids": "10,20,30"},
            )
        assert resp.status_code == 200
        assert sorted(call_args) == [10, 20, 30]
