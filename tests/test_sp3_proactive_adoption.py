"""test_sp3_proactive_adoption.py — Tests for SP3 Proactive Adoption changes.

Covers:
  Change 1 — live-polling proactive nav badge:
    - GET /v2/partials/proactive/badge returns a count span when matches exist
    - GET /v2/partials/proactive/badge returns empty string when count == 0
    - mobile_nav.html includes badge container with hx-trigger containing 'every'
      and an explicit hx-target

  Change 2 — Matches empty-state with body copy + "Check again" CTA:
    - Matches empty-state contains the new body copy
    - Matches empty-state contains the "Check again" button
    - POST /v2/partials/proactive/refresh runs a scan and returns the list partial

Called by: pytest
Depends on: app/routers/htmx_views.py, app/templates/htmx/partials/shared/mobile_nav.html,
            app/templates/htmx/partials/proactive/list.html, conftest.py
"""

from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import ProactiveMatch, User

# ── Helpers ────────────────────────────────────────────────────────────


def _make_new_match(db: Session, user: User, test_requisition, test_offer, test_customer_site) -> ProactiveMatch:
    """Create a status=new ProactiveMatch for the test user."""
    match = ProactiveMatch(
        offer_id=test_offer.id,
        requirement_id=test_requisition.id,
        requisition_id=test_requisition.id,
        customer_site_id=test_customer_site.id,
        salesperson_id=user.id,
        mpn="SP3TEST",
        status="new",
    )
    db.add(match)
    db.commit()
    return match


# ── Change 1: Badge route ───────────────────────────────────────────────


class TestProactiveBadgeRoute:
    """GET /v2/partials/proactive/badge — count when NEW matches exist, empty when 0."""

    def test_badge_returns_count_when_new_matches_exist(
        self,
        client: TestClient,
        db_session: Session,
        test_user: User,
        test_requisition,
        test_offer,
        test_customer_site,
    ):
        """Badge renders a non-empty span with the count when there are NEW matches."""
        _make_new_match(db_session, test_user, test_requisition, test_offer, test_customer_site)

        resp = client.get("/v2/partials/proactive/badge", headers={"HX-Request": "true"})

        assert resp.status_code == 200
        assert resp.text.strip() != "", "Badge must not be empty when there are NEW matches"
        # The count fragment contains the number
        assert "1" in resp.text

    def test_badge_returns_empty_when_no_new_matches(self, client: TestClient):
        """Badge returns empty HTML when count == 0 (no orphaned empty pill)."""
        resp = client.get("/v2/partials/proactive/badge", headers={"HX-Request": "true"})

        assert resp.status_code == 200
        assert resp.text.strip() == "", f"Badge must be empty string when count==0, got: {resp.text!r}"

    def test_badge_does_not_count_sent_matches(
        self,
        client: TestClient,
        db_session: Session,
        test_user: User,
        test_requisition,
        test_offer,
        test_customer_site,
    ):
        """Only status=NEW matches contribute to the badge count."""
        match = _make_new_match(db_session, test_user, test_requisition, test_offer, test_customer_site)
        match.status = "sent"
        db_session.commit()

        resp = client.get("/v2/partials/proactive/badge", headers={"HX-Request": "true"})

        assert resp.status_code == 200
        assert resp.text.strip() == "", "Badge must be empty for non-NEW matches"


# ── Change 1: mobile_nav badge container markup ─────────────────────────


class TestMobileNavBadgeMarkup:
    """mobile_nav.html must include the badge container with correct HTMX attrs."""

    def _read_mobile_nav(self) -> str:
        import os

        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        path = os.path.join(repo_root, "app/templates/htmx/partials/shared/mobile_nav.html")
        with open(path, encoding="utf-8") as f:
            return f.read()

    def test_badge_container_present(self):
        """mobile_nav includes the proactive badge container element."""
        html = self._read_mobile_nav()
        assert "proactive-nav-badge" in html, "Badge container with id=proactive-nav-badge must be in mobile_nav.html"

    def test_badge_hx_trigger_contains_every(self):
        """Badge hx-trigger includes an 'every Ns' poll so it live-updates."""
        html = self._read_mobile_nav()
        assert "every 60s" in html, "Badge hx-trigger must contain 'every 60s' for live-polling"

    def test_badge_has_explicit_hx_target(self):
        """Badge has an explicit hx-target (itself) so it does not inherit the page-
        level hx-target='#main-content'."""
        html = self._read_mobile_nav()
        assert 'hx-target="#proactive-nav-badge"' in html, (
            "Badge must carry an explicit hx-target pointing to itself; "
            "without this it would inherit #main-content and replace the whole page"
        )

    def test_badge_hx_get_points_to_badge_endpoint(self):
        """Badge fetches from the correct partial endpoint."""
        html = self._read_mobile_nav()
        assert 'hx-get="/v2/partials/proactive/badge"' in html


# ── Change 2: Matches empty-state content ──────────────────────────────


class TestMatchesEmptyState:
    """Matches empty-state must include the required copy and CTA."""

    def _render_empty_matches(self) -> str:
        from app.template_env import templates

        return templates.get_template("htmx/partials/proactive/list.html").render(
            matches=[],
            sent=[],
            tab="matches",
            match_count=0,
            success_msg="",
            current_view="proactive",
            user_name="Test",
            user_email="test@test.com",
        )

    def test_empty_state_title(self):
        """Empty-state shows the new title copy."""
        html = self._render_empty_matches()
        assert "No proactive matches yet" in html

    def test_empty_state_body_copy(self):
        """Empty-state body explains how matches work."""
        html = self._render_empty_matches()
        assert "flag a customer" in html or "vendor stock" in html, (
            "Empty-state body must explain the matching mechanic"
        )

    def test_empty_state_check_again_button(self):
        """Empty-state includes a 'Check again' CTA button."""
        html = self._render_empty_matches()
        assert "Check again" in html

    def test_empty_state_check_again_posts_to_refresh(self):
        """'Check again' button posts to the refresh endpoint."""
        html = self._render_empty_matches()
        assert "/v2/partials/proactive/refresh" in html

    def test_sent_tab_empty_state_unchanged(self):
        """Sent tab empty-state is not affected by the matches empty-state change."""
        from app.template_env import templates

        html = templates.get_template("htmx/partials/proactive/list.html").render(
            matches=[],
            sent=[],
            tab="sent",
            match_count=0,
            success_msg="",
            current_view="proactive",
            user_name="Test",
            user_email="test@test.com",
        )
        assert "No offers sent yet" in html or "Select matches" in html


# ── Change 2: POST /v2/partials/proactive/refresh ──────────────────────


class TestProactiveRefreshRoute:
    """POST /v2/partials/proactive/refresh — triggers scan and returns list partial."""

    def test_refresh_runs_scan_and_returns_list(self, client: TestClient):
        """Refresh endpoint calls run_proactive_scan and returns the matches partial."""
        # The endpoint does `from ..services.proactive_matching import run_proactive_scan`
        # inside the function body — patch at the source module.
        with patch(
            "app.services.proactive_matching.run_proactive_scan",
            return_value={"scanned_offers": 3, "matches_created": 1},
        ):
            resp = client.post(
                "/v2/partials/proactive/refresh",
                headers={"HX-Request": "true"},
            )

        assert resp.status_code == 200
        # Returns the list partial (has the tab bar)
        assert "Matches" in resp.text or "proactive" in resp.text.lower()

    def test_refresh_returns_matches_tab_by_default(self, client: TestClient):
        """Refresh always returns the matches tab view."""
        with patch(
            "app.services.proactive_matching.run_proactive_scan",
            return_value={"scanned_offers": 0, "matches_created": 0},
        ):
            resp = client.post(
                "/v2/partials/proactive/refresh",
                headers={"HX-Request": "true"},
            )

        assert resp.status_code == 200
        # The matches tab is active (bg-brand-500)
        assert "No proactive matches yet" in resp.text or "Matches" in resp.text

    def test_refresh_shows_new_match_after_scan(
        self,
        client: TestClient,
        db_session: Session,
        test_user: User,
        test_requisition,
        test_offer,
        test_customer_site,
    ):
        """After a scan that creates a new match, refresh shows it in the list."""
        match = _make_new_match(db_session, test_user, test_requisition, test_offer, test_customer_site)

        with patch(
            "app.services.proactive_matching.run_proactive_scan",
            return_value={"scanned_offers": 1, "matches_created": 1},
        ):
            resp = client.post(
                "/v2/partials/proactive/refresh",
                headers={"HX-Request": "true"},
            )

        assert resp.status_code == 200
        # Match is in the db — list should show it
        assert "SP3TEST" in resp.text
