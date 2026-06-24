# test_vendor_activity.py — TDD tests for vendor activity tab + manual note feed.
# Tests: tab renders, add-note posts, cadence neutrality, auth gate.
# Called by: pytest (TESTING=1)
# Depends on: conftest fixtures (client, db_session, test_user, test_vendor_card,
#             unauthenticated_client), ActivityLog, VendorCard

from datetime import datetime, timezone

import pytest

from app.constants import ActivityType
from app.models import ActivityLog

# ── Fixtures ─────────────────────────────────────────────────────────


@pytest.fixture()
def vendor_with_note(db_session, test_vendor_card, test_user):
    """A VendorCard that already has one NOTE activity logged against it."""
    log = ActivityLog(
        user_id=test_user.id,
        activity_type=ActivityType.NOTE,
        channel="manual",
        vendor_card_id=test_vendor_card.id,
        notes="Pre-existing vendor note",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(log)
    db_session.commit()
    return test_vendor_card


# ── Tests ─────────────────────────────────────────────────────────────


class TestVendorActivityTab:
    def test_vendor_activity_tab_renders(self, client, vendor_with_note):
        """GET activity tab partial for a vendor → 200, activity_row appears."""
        resp = client.get(f"/v2/partials/vendors/{vendor_with_note.id}/tab/activity")
        assert resp.status_code == 200
        body = resp.text
        # Must contain the outer container id so htmx can target it
        assert f"vendor-activity-tab-{vendor_with_note.id}" in body
        # The pre-existing note's text should appear somewhere in the rendered feed
        assert "Pre-existing vendor note" in body

    def test_vendor_activity_tab_empty_state(self, client, test_vendor_card):
        """Activity tab for vendor with no activity → 200 with empty state message."""
        resp = client.get(f"/v2/partials/vendors/{test_vendor_card.id}/tab/activity")
        assert resp.status_code == 200
        body = resp.text
        # Should show empty state text (no activities)
        assert "No activity" in body or "no activity" in body.lower()

    def test_vendor_activity_tab_404_for_unknown_vendor(self, client):
        """Unknown vendor id → 404."""
        resp = client.get("/v2/partials/vendors/99999/tab/activity")
        assert resp.status_code == 404

    def test_vendor_add_note_posts_and_renders(self, client, db_session, test_vendor_card):
        """POST add-note → 200, note body appears in response, ActivityLog row
        created."""
        note_text = "Spoke to sales rep about Q3 allocation"
        resp = client.post(
            f"/v2/partials/vendors/{test_vendor_card.id}/activity/add-note",
            data={"notes": note_text},
        )
        assert resp.status_code == 200
        body = resp.text
        # The refreshed tab should contain our note text
        assert note_text in body

        # Confirm the DB row was written
        log = (
            db_session.query(ActivityLog)
            .filter(
                ActivityLog.vendor_card_id == test_vendor_card.id,
                ActivityLog.notes == note_text,
            )
            .first()
        )
        assert log is not None
        assert log.activity_type == ActivityType.NOTE

    def test_vendor_add_note_empty_body_returns_error(self, client, test_vendor_card):
        """POST add-note with blank text → does NOT create a DB row, returns error
        fragment."""
        resp = client.post(
            f"/v2/partials/vendors/{test_vendor_card.id}/activity/add-note",
            data={"notes": "   "},
        )
        # Should be a 200 with an inline error, not a 500
        assert resp.status_code == 200
        assert "required" in resp.text.lower()

    def test_vendor_add_note_is_cadence_neutral(self, client, db_session, test_vendor_card):
        """Posting a vendor note does NOT update last_outbound_at (cadence-neutral)."""
        # Snapshot cadence fields before
        db_session.refresh(test_vendor_card)
        before_outbound = test_vendor_card.last_outbound_at

        resp = client.post(
            f"/v2/partials/vendors/{test_vendor_card.id}/activity/add-note",
            data={"notes": "Internal commentary — cadence should not advance"},
        )
        assert resp.status_code == 200

        # Re-read from DB
        db_session.refresh(test_vendor_card)
        after_outbound = test_vendor_card.last_outbound_at

        # last_outbound_at must not have changed
        assert before_outbound == after_outbound

    def test_vendor_add_note_requires_auth(self, unauthenticated_client, test_vendor_card):
        """Unauthenticated POST add-note → 401 or 403."""
        resp = unauthenticated_client.post(
            f"/v2/partials/vendors/{test_vendor_card.id}/activity/add-note",
            data={"notes": "Should be blocked"},
        )
        assert resp.status_code in (401, 403)

    def test_vendor_add_note_requires_auth_get_form(self, unauthenticated_client, test_vendor_card):
        """Unauthenticated GET add-note-form → 401 or 403."""
        resp = unauthenticated_client.get(f"/v2/partials/vendors/{test_vendor_card.id}/activity/add-note-form")
        assert resp.status_code in (401, 403)
