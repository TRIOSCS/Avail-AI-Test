"""tests/test_stale_guard.py — Tests for app/services/stale_guard.py (Phase 0.3).

Covers: stale_token serialization (aware/naive parity — SQLite vs PostgreSQL),
ensure_not_stale skip/pass/raise behavior, the non-destructive 409 conflict
response (HX-Reswap none + HX-Trigger showToast), the stale_token Jinja2 global,
and the 409 skip hook in htmx_app.js.
"""

import json
from datetime import UTC, datetime, timedelta, timezone

import pytest
from sqlalchemy.orm import Session

from app.models.auth import User
from app.services.stale_guard import (
    STALE_TOAST_MESSAGE,
    StaleEditError,
    ensure_not_stale,
    stale_conflict_response,
    stale_token,
)
from tests.conftest import _buyplan_line as _line
from tests.conftest import _buyplan_plan as _plan
from tests.conftest import _buyplan_req as _req


class _Obj:
    def __init__(self, updated_at):
        self.updated_at = updated_at


class TestStaleToken:
    def test_aware_datetime_iso_utc(self):
        obj = _Obj(datetime(2026, 7, 17, 12, 30, tzinfo=UTC))
        assert stale_token(obj) == "2026-07-17T12:30:00+00:00"

    def test_naive_assumed_utc_matches_aware(self):
        # Risk 8: SQLite returns naive datetimes, PG aware — same instant, same token.
        naive = _Obj(datetime(2026, 7, 17, 12, 30))
        aware = _Obj(datetime(2026, 7, 17, 12, 30, tzinfo=UTC))
        assert stale_token(naive) == stale_token(aware)

    def test_non_utc_offset_normalized(self):
        est = timezone(timedelta(hours=-5))
        offset = datetime(2026, 7, 17, 7, 30, tzinfo=est)
        assert stale_token(_Obj(offset)) == "2026-07-17T12:30:00+00:00"

    def test_none_updated_at_is_empty(self):
        assert stale_token(_Obj(None)) == ""

    def test_object_without_updated_at_is_empty(self):
        assert stale_token(object()) == ""


class TestEnsureNotStale:
    def test_empty_expected_skips(self):
        obj = _Obj(datetime(2026, 7, 17, 12, 30, tzinfo=UTC))
        ensure_not_stale(obj, "")  # no raise
        ensure_not_stale(obj, None)  # no raise

    def test_matching_token_passes(self):
        obj = _Obj(datetime(2026, 7, 17, 12, 30, tzinfo=UTC))
        ensure_not_stale(obj, stale_token(obj))  # no raise

    def test_mismatch_raises(self):
        obj = _Obj(datetime(2026, 7, 17, 12, 30, tzinfo=UTC))
        with pytest.raises(StaleEditError) as excinfo:
            ensure_not_stale(obj, "2026-07-17T11:00:00+00:00")
        assert excinfo.value.expected == "2026-07-17T11:00:00+00:00"
        assert excinfo.value.actual == "2026-07-17T12:30:00+00:00"

    def test_round_trip_on_real_line(self, db_session: Session, test_user: User):
        req = _req(db_session, test_user)
        plan = _plan(db_session, req)
        line = _line(db_session, plan)
        token = stale_token(line)
        ensure_not_stale(line, token)  # fresh — passes
        # Another session edits the line → updated_at moves → token is stale.
        line.updated_at = datetime(2030, 1, 1, tzinfo=UTC)
        db_session.commit()
        db_session.refresh(line)
        if token:  # a line created without updated_at yields "" (skip semantics)
            with pytest.raises(StaleEditError):
                ensure_not_stale(line, token)


class TestStaleConflictResponse:
    def test_409_non_destructive_with_toast_trigger(self):
        response = stale_conflict_response()
        assert response.status_code == 409
        assert response.headers["HX-Reswap"] == "none"
        trigger = json.loads(response.headers["HX-Trigger"])
        assert trigger["showToast"]["message"] == STALE_TOAST_MESSAGE
        assert trigger["showToast"]["type"] == "warning"
        assert response.body == b""


class TestWiring:
    def test_jinja_global_registered(self):
        from app.template_env import templates

        assert templates.env.globals["stale_token"] is stale_token

    def test_htmx_app_js_skips_409_generic_toast(self):
        src = open("app/static/htmx_app.js").read()
        handler = src.split("htmx:responseError")[1][:600]
        assert "status === 409" in handler  # generic 4xx toast must skip stale 409s
