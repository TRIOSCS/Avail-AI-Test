"""Tests for POST /api/activity/{activity_id}/call-outcome.

Covers: outcome stamping, is_meaningful logic, note append, 404 guards,
validation errors, and Jinja2 render of the outcome-prompt partial.

Called by: pytest
Depends on: app/routers/activity.py, app/constants.CallOutcome
"""

import os

os.environ["TESTING"] = "1"

from datetime import datetime, timezone

import pytest

from app.constants import ActivityType
from app.models import ActivityLog


@pytest.fixture(autouse=True)
def _clear_rate_limit():
    from app.rate_limit import reset_rate_limit_state

    reset_rate_limit_state()
    yield
    reset_rate_limit_state()


@pytest.fixture
def call_log(db_session, test_user):
    """A bare call_logged ActivityLog owned by test_user."""
    record = ActivityLog(
        user_id=test_user.id,
        activity_type=ActivityType.CALL_LOGGED,
        channel="phone",
        direction="outbound",
        subject="Call to Test",
        is_meaningful=True,
        auto_logged=True,
        occurred_at=datetime.now(timezone.utc),
    )
    db_session.add(record)
    db_session.commit()
    return record


class TestCallOutcomeEndpoint:
    def _post(self, client, activity_id, outcome, note=None):
        payload = {"outcome": outcome}
        if note is not None:
            payload["note"] = note
        return client.post(f"/api/activity/{activity_id}/call-outcome", json=payload)

    def test_connected_stamps_outcome_and_is_meaningful(self, client, db_session, call_log):
        resp = self._post(client, call_log.id, "connected")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        assert resp.json()["outcome"] == "connected"
        db_session.expire(call_log)
        assert call_log.details["call_outcome"] == "connected"
        assert call_log.is_meaningful is True

    def test_left_message_is_meaningful(self, client, db_session, call_log):
        resp = self._post(client, call_log.id, "left_message")
        assert resp.status_code == 200
        db_session.expire(call_log)
        assert call_log.is_meaningful is True

    def test_no_answer_not_meaningful(self, client, db_session, call_log):
        resp = self._post(client, call_log.id, "no_answer")
        assert resp.status_code == 200
        db_session.expire(call_log)
        assert call_log.is_meaningful is False

    def test_voicemail_not_meaningful(self, client, db_session, call_log):
        resp = self._post(client, call_log.id, "voicemail")
        assert resp.status_code == 200
        db_session.expire(call_log)
        assert call_log.is_meaningful is False

    def test_note_appended_to_notes(self, client, db_session, call_log):
        resp = self._post(client, call_log.id, "connected", note="Discussed Q3 quote")
        assert resp.status_code == 200
        db_session.expire(call_log)
        assert "Discussed Q3 quote" in call_log.notes
        assert call_log.details["outcome_note"] == "Discussed Q3 quote"

    def test_outcome_note_absent_when_no_note_submitted(self, client, db_session, call_log):
        """A submit with no note must NOT write outcome_note at all (key absent, not
        null).

        Regression guard: the old code wrote outcome_note: null, which would erase a
        previously-stored note on a second submit.
        """
        resp = self._post(client, call_log.id, "voicemail")
        assert resp.status_code == 200
        db_session.expire(call_log)
        assert "outcome_note" not in (call_log.details or {})

    def test_resubmit_without_note_preserves_existing_note(self, client, db_session, call_log):
        """Second outcome submit with no note must NOT erase a previously-stored
        note."""
        # First submit — stores note
        self._post(client, call_log.id, "connected", note="First note")
        db_session.expire(call_log)
        assert call_log.details["outcome_note"] == "First note"

        # Second submit — no note; outcome_note must survive
        self._post(client, call_log.id, "no_answer")
        db_session.expire(call_log)
        assert call_log.details.get("outcome_note") == "First note"

    def test_404_for_nonexistent_id(self, client):
        resp = self._post(client, 999999, "connected")
        assert resp.status_code == 404

    def test_404_for_wrong_activity_type(self, client, db_session, test_user):
        record = ActivityLog(
            user_id=test_user.id,
            activity_type=ActivityType.EMAIL_SENT,
            channel="email",
            subject="Email to Test",
            occurred_at=datetime.now(timezone.utc),
        )
        db_session.add(record)
        db_session.commit()
        resp = self._post(client, record.id, "connected")
        assert resp.status_code == 404

    def test_404_for_other_user(self, client, db_session, test_user):
        from app.models import User

        other = User(email="other@example.com", name="Other User", is_active=True)
        db_session.add(other)
        db_session.flush()
        record = ActivityLog(
            user_id=other.id,
            activity_type=ActivityType.CALL_LOGGED,
            channel="phone",
            subject="Call",
            occurred_at=datetime.now(timezone.utc),
        )
        db_session.add(record)
        db_session.commit()
        resp = self._post(client, record.id, "connected")
        assert resp.status_code == 404

    def test_422_for_invalid_outcome(self, client, call_log):
        resp = self._post(client, call_log.id, "invalid_outcome")
        assert resp.status_code == 422


class TestCallOutcomePromptTemplate:
    """Verify the Jinja2 partial renders with the expected Alpine bindings."""

    def test_partial_renders_store_bindings(self):
        from jinja2 import Environment, FileSystemLoader

        env = Environment(
            loader=FileSystemLoader("app/templates"),
            autoescape=True,
        )
        tmpl = env.get_template("htmx/partials/shared/call_outcome_prompt.html")
        rendered = tmpl.render()
        # The chip values live in the Alpine JS store (htmx_app.js), not Jinja2 —
        # verify the template wires up the correct store bindings and x-for loop.
        assert "$store.callOutcome.show" in rendered
        assert "$store.callOutcome.chips" in rendered
        assert "$store.callOutcome.submit" in rendered
        assert "$store.callOutcome.dismiss" in rendered
        assert 'x-model="$store.callOutcome.note"' in rendered

    def test_enter_key_does_not_call_submit_null(self):
        """Enter on the note field must NOT call submit(null) — it is blocked."""
        from jinja2 import Environment, FileSystemLoader

        env = Environment(
            loader=FileSystemLoader("app/templates"),
            autoescape=True,
        )
        tmpl = env.get_template("htmx/partials/shared/call_outcome_prompt.html")
        rendered = tmpl.render()
        # The old dead-end pattern must not exist
        assert "submit(null)" not in rendered
