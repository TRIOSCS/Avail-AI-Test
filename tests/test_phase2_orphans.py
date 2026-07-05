# test_phase2_orphans.py — Phase 2.1 orphaned-endpoint resolution.
#
# What it covers:
#   WIRE  — the three endpoints that were built-but-unwired and are now finished:
#           * vendor response-status control (PATCH .../responses/{id}/status)
#           * log-phone-call form (POST .../log-phone) returning the refreshed activity
#           * nav follow-up badge (GET /v2/partials/follow-ups/badge) mounted in the nav
#   DELETE — the superseded/orphaned routes that were removed are no longer registered,
#            their superseding twins remain, and nothing breaks on import.
#
# Called by: pytest (CI + local). Depends on: conftest fixtures (client, db_session,
#            test_user, test_requisition), app.main.app, app.models.offers.

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from app.constants import ContactStatus, VendorResponseStatus
from app.main import app
from app.models.auth import User
from app.models.offers import Contact as RfqContact
from app.models.offers import VendorResponse
from app.models.sourcing import Requisition

_TEMPLATES = Path(__file__).resolve().parent.parent / "app" / "templates" / "htmx" / "partials"


def _route_registered(path: str, method: str) -> bool:
    method = method.upper()
    return any(getattr(r, "path", "") == path and method in (getattr(r, "methods", set()) or set()) for r in app.routes)


def _make_response(db: Session, user: User, req: Requisition, status: str = "new") -> VendorResponse:
    vr = VendorResponse(
        requisition_id=req.id,
        vendor_name="TestVendor Inc",
        vendor_email="sales@testvendor.com",
        confidence=0.6,
        scanned_by_user_id=user.id,
        status=status,
        received_at=datetime.now(timezone.utc),
        message_id=f"msg-orphan-{id(req)}-{status}",
    )
    db.add(vr)
    db.commit()
    db.refresh(vr)
    return vr


# ══════════════════════════════════════════════════════════════════════════
# WIRE 1 — Vendor response-status control
# ══════════════════════════════════════════════════════════════════════════
class TestResponseStatusControl:
    def test_status_control_renders_on_response_card(
        self, client: TestClient, db_session: Session, test_user: User, test_requisition: Requisition
    ):
        """The responses tab renders the status control that PATCHes .../status."""
        _make_response(db_session, test_user, test_requisition, status="reviewed")
        resp = client.get(f"/v2/partials/requisitions/{test_requisition.id}/tab/responses")
        assert resp.status_code == 200
        body = resp.text
        assert "/responses/" in body and "/status" in body
        assert "hx-patch=" in body
        # The three status actions are offered.
        assert "Flag" in body and "Reviewed" in body and "Reject" in body

    def test_patch_status_updates_and_returns_refreshed_card(
        self, client: TestClient, db_session: Session, test_user: User, test_requisition: Requisition
    ):
        """PATCH .../status persists the new status and swaps the refreshed card."""
        vr = _make_response(db_session, test_user, test_requisition, status="new")
        resp = client.patch(
            f"/v2/partials/requisitions/{test_requisition.id}/responses/{vr.id}/status",
            data={"status": VendorResponseStatus.FLAGGED.value},
        )
        assert resp.status_code == 200
        db_session.refresh(vr)
        assert vr.status == VendorResponseStatus.FLAGGED.value
        # Response body is the refreshed card (with its status control), not just a badge.
        assert f'id="response-{vr.id}"' in resp.text
        assert "/status" in resp.text and "hx-patch=" in resp.text

    def test_patch_status_rejects_invalid_value(
        self, client: TestClient, db_session: Session, test_user: User, test_requisition: Requisition
    ):
        vr = _make_response(db_session, test_user, test_requisition, status="new")
        resp = client.patch(
            f"/v2/partials/requisitions/{test_requisition.id}/responses/{vr.id}/status",
            data={"status": "bogus"},
        )
        assert resp.status_code == 400


# ══════════════════════════════════════════════════════════════════════════
# WIRE 2 — Log a phone call
# ══════════════════════════════════════════════════════════════════════════
class TestLogPhoneCall:
    def test_activity_tab_renders_log_call_form(self, client: TestClient, test_requisition: Requisition):
        """The activity tab exposes a Log call form wired to POST .../log-phone."""
        resp = client.get(f"/v2/partials/requisitions/{test_requisition.id}/tab/activity")
        assert resp.status_code == 200
        assert f"/v2/partials/requisitions/{test_requisition.id}/log-phone" in resp.text
        assert 'name="vendor_name"' in resp.text and 'name="vendor_phone"' in resp.text

    def test_log_phone_posts_and_returns_refreshed_activity(
        self, client: TestClient, db_session: Session, test_requisition: Requisition
    ):
        """Posting the form logs the call and returns the refreshed activity
        timeline."""
        from app.models.intelligence import ActivityLog

        before = db_session.query(ActivityLog).filter(ActivityLog.requisition_id == test_requisition.id).count()
        resp = client.post(
            f"/v2/partials/requisitions/{test_requisition.id}/log-phone",
            data={"vendor_name": "Arrow", "vendor_phone": "+1-555-0100", "notes": "left voicemail"},
        )
        assert resp.status_code == 200
        after = db_session.query(ActivityLog).filter(ActivityLog.requisition_id == test_requisition.id).count()
        assert after == before + 1
        # Refreshed activity tab (contains the timeline + the Log call form again).
        assert "Activity Log" in resp.text
        assert f"/v2/partials/requisitions/{test_requisition.id}/log-phone" in resp.text

    def test_log_phone_requires_name_and_phone(self, client: TestClient, test_requisition: Requisition):
        resp = client.post(
            f"/v2/partials/requisitions/{test_requisition.id}/log-phone",
            data={"vendor_name": "", "vendor_phone": ""},
        )
        assert resp.status_code == 400


# ══════════════════════════════════════════════════════════════════════════
# WIRE 3 — Nav follow-up badge
# ══════════════════════════════════════════════════════════════════════════
class TestFollowUpBadge:
    def test_badge_mounted_in_nav_template(self):
        """mobile_nav.html mounts the follow-up badge with lazy-poll, like
        proactive/alert."""
        nav = (_TEMPLATES / "shared" / "mobile_nav.html").read_text()
        assert 'id="follow-ups-nav-badge"' in nav
        assert 'hx-get="/v2/partials/follow-ups/badge"' in nav
        assert "hx-trigger=" in nav and "every 60s" in nav

    def test_badge_endpoint_counts_stale_contacts(
        self, client: TestClient, db_session: Session, test_user: User, test_requisition: Requisition
    ):
        """A stale email contact surfaces an amber count pill; none → empty response."""
        empty = client.get("/v2/partials/follow-ups/badge")
        assert empty.status_code == 200
        assert empty.text.strip() == ""

        stale = RfqContact(
            requisition_id=test_requisition.id,
            user_id=test_user.id,
            contact_type="email",
            vendor_name="Arrow",
            vendor_contact="sales@arrow.com",
            status=ContactStatus.SENT.value,
            created_at=datetime.now(timezone.utc) - timedelta(days=30),
        )
        db_session.add(stale)
        db_session.commit()

        resp = client.get("/v2/partials/follow-ups/badge")
        assert resp.status_code == 200
        assert "1" in resp.text
        assert "bg-amber-500" in resp.text


# ══════════════════════════════════════════════════════════════════════════
# DELETE — superseded/orphaned routes removed, twins kept, no import breakage
# ══════════════════════════════════════════════════════════════════════════
_DELETED = [
    ("/v2/partials/sightings/{requirement_id}/assign", "PATCH"),
    ("/v2/partials/sourcing/leads/{lead_id}/feedback", "POST"),
    ("/v2/partials/vendors/{vendor_id}/contacts/{contact_id}/timeline", "GET"),
    ("/v2/partials/vendors/{vendor_id}/contact-nudges", "GET"),
    ("/api/vendors/{card_id}/contact-nudges", "GET"),
]

_SUPERSEDING = [
    # sightings: batch-assign supersedes per-requirement assign
    ("/v2/partials/sightings/batch-assign", "POST"),
    ("/v2/partials/sightings/{requirement_id}/advance-status", "PATCH"),
    # lead feedback: the requirements /api path is the live one
    ("/api/leads/{lead_id}/feedback", "POST"),
    # the /api vendor-contact timeline (JSON) stays; only the htmx twin was removed
    ("/api/vendors/{card_id}/contacts/{contact_id}/summary", "GET"),
    # wired endpoints remain registered
    ("/v2/partials/requisitions/{req_id}/responses/{response_id}/status", "PATCH"),
    ("/v2/partials/requisitions/{req_id}/log-phone", "POST"),
    ("/v2/partials/follow-ups/badge", "GET"),
]

_DELETED_TEMPLATES = [
    "vendors/contact_timeline.html",
    "vendors/contact_nudges.html",
    "requisitions/response_status_badge.html",
    "requisitions/phone_log_success.html",
]


class TestDeletedRoutes:
    def test_deleted_routes_are_unregistered(self):
        for path, method in _DELETED:
            assert not _route_registered(path, method), f"{method} {path} should be deleted"

    def test_superseding_routes_still_present(self):
        for path, method in _SUPERSEDING:
            assert _route_registered(path, method), f"{method} {path} should still exist"

    def test_deleted_templates_removed(self):
        for rel in _DELETED_TEMPLATES:
            assert not (_TEMPLATES / rel).exists(), f"{rel} should be deleted"

    def test_approvals_rest_cluster_left_intact(self):
        """The Approvals REST cluster is LEFT (buyplan_hub still builds its decision
        URL)."""
        for path in (
            "/v2/approvals/requests/{id}/decision",
            "/v2/approvals/requests/{id}/reassign",
            "/v2/approvals/requests/{id}/cancel",
            "/v2/approvals/requests",
            "/v2/approvals/requests/{id}",
        ):
            assert any(getattr(r, "path", "") == path for r in app.routes), f"{path} must remain"

    def test_no_import_breakage(self):
        import importlib

        for mod in (
            "app.routers.htmx.offers",
            "app.routers.htmx.vendors",
            "app.routers.htmx.sourcing",
            "app.routers.vendor_contacts",
            "app.routers.sightings",
        ):
            assert importlib.import_module(mod) is not None
