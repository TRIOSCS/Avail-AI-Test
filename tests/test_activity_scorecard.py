"""Tests for the Activity Scorecard — service aggregation, scoring, ranking, route.

Covers:
  - per-user metric aggregation (calls/talk/emails/IMs/accounts/contacts)
  - channel/direction mapping correctness (only the right rows count)
  - time-range windowing (this_week/month/quarter/all_time)
  - the weighted score + h:mm talk-time formatting
  - ranking order (score desc, deterministic tie-break) + 1-based ranks
  - the settings/scorecard route: manager/admin → 200, buyer → 403

Depends on: tests/conftest.py fixtures (db_session), app.services.activity_scorecard,
app.models, app.constants.
"""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.constants import Channel, Direction
from app.models import ActivityLog, Company, CustomerSite, SiteContact, User
from app.services.activity_scorecard import (
    DEFAULT_TIME_RANGE,
    TALK_TIME_BUCKET_SECONDS,
    WEIGHTS,
    ScoreWeights,
    compute_score,
    compute_scorecard,
    format_talk_time,
    range_start,
    scoring_formula_parts,
)

UTC = timezone.utc


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture()
def users(db_session):
    """Three users: a buyer, a manager, an admin."""
    rows = [
        User(email="alice@trioscs.com", name="Alice", role="buyer", azure_id="az-a"),
        User(email="mgr@trioscs.com", name="Manager Mo", role="manager", azure_id="az-m"),
        User(email="boss@trioscs.com", name="Admin Ada", role="admin", azure_id="az-ad"),
    ]
    db_session.add_all(rows)
    db_session.commit()
    for r in rows:
        db_session.refresh(r)
    return {"buyer": rows[0], "manager": rows[1], "admin": rows[2]}


def _log(db, user, channel, *, direction=None, duration=None, when=None):
    """Insert one ActivityLog row with the minimum fields the scorecard reads."""
    row = ActivityLog(
        user_id=user.id,
        activity_type="x",  # not read by the scorecard (it keys on channel/direction)
        channel=channel,
        direction=direction,
        duration_seconds=duration,
        created_at=when or datetime.now(UTC),
    )
    db.add(row)
    return row


# ── Pure scoring / formatting ───────────────────────────────────────────────


def test_format_talk_time():
    assert format_talk_time(0) == "0:00"
    assert format_talk_time(None) == "0:00"
    assert format_talk_time(65) == "0:01"
    assert format_talk_time(300) == "0:05"
    assert format_talk_time(3600) == "1:00"
    assert format_talk_time(3665) == "1:01"  # 1h 1m 5s -> drops seconds


def test_compute_score_matches_documented_weights():
    # 2 calls + 10 min talk (=2 buckets) + 3 emails + 4 IMs + 1 account + 2 contacts
    score = compute_score(
        calls=2,
        talk_time_seconds=10 * 60,
        emails_sent=3,
        ims_sent=4,
        accounts_added=1,
        contacts_added=2,
    )
    # 2*1 + 2*1 + 3*1 + 4*0.5 + 1*5 + 2*2 = 2+2+3+2+5+4 = 18
    assert score == 18.0


def test_compute_score_talk_time_is_pro_rated():
    # 7.5 minutes = 1.5 buckets -> 1.5 talk points, nothing else
    assert (
        compute_score(
            calls=0,
            talk_time_seconds=int(7.5 * 60),
            emails_sent=0,
            ims_sent=0,
            accounts_added=0,
            contacts_added=0,
        )
        == 1.5
    )


def test_score_weights_are_the_documented_defaults():
    assert WEIGHTS == ScoreWeights(
        call=1.0,
        talk_time_per_bucket=1.0,
        email=1.0,
        im=0.5,
        account_added=5.0,
        contact_added=2.0,
    )
    assert TALK_TIME_BUCKET_SECONDS == 300


def test_scoring_formula_parts_stay_in_sync_with_weights():
    parts = scoring_formula_parts()
    labels = {p["label"] for p in parts}
    assert labels == {"Call", "Talk time", "Email sent", "IM sent", "Account added", "Contact added"}
    talk = next(p for p in parts if p["label"] == "Talk time")
    assert "5 min" in talk["weight"]
    im = next(p for p in parts if p["label"] == "IM sent")
    assert "0.5" in im["weight"]


# ── Metric aggregation correctness ──────────────────────────────────────────


def test_only_correct_channels_and_directions_count(db_session, users):
    alice = users["buyer"]
    # 2 phone calls (both directions count) with talk time
    _log(db_session, alice, Channel.PHONE, direction=Direction.OUTBOUND, duration=120)
    _log(db_session, alice, Channel.PHONE, direction=Direction.INBOUND, duration=180)
    # outbound email counts; inbound email does NOT
    _log(db_session, alice, Channel.EMAIL, direction=Direction.OUTBOUND)
    _log(db_session, alice, Channel.EMAIL, direction=Direction.INBOUND)
    # outbound Teams + WeChat count as IMs; inbound Teams does NOT
    _log(db_session, alice, Channel.TEAMS, direction=Direction.OUTBOUND)
    _log(db_session, alice, Channel.WECHAT, direction=Direction.OUTBOUND)
    _log(db_session, alice, Channel.TEAMS, direction=Direction.INBOUND)
    # an unrelated channel (system) is ignored entirely
    _log(db_session, alice, Channel.SYSTEM, direction=Direction.OUTBOUND)
    db_session.commit()

    rows = compute_scorecard(db_session, "all_time")
    assert len(rows) == 1
    row = rows[0]
    assert row.calls == 2
    assert row.talk_time_seconds == 300
    assert row.talk_time_hms == "0:05"
    assert row.emails_sent == 1
    assert row.ims_sent == 2


def test_accounts_and_contacts_counted_by_creator(db_session, users):
    alice, mgr = users["buyer"], users["manager"]
    db_session.add_all(
        [
            Company(name="C1", created_by_id=alice.id),
            Company(name="C2", created_by_id=alice.id),
            Company(name="C3", created_by_id=mgr.id),
            Company(name="C-null", created_by_id=None),  # no creator -> not counted
        ]
    )
    site = CustomerSite(company_id=None, site_name="HQ")  # company_id NOT NULL? set below
    company = Company(name="Host", created_by_id=None)
    db_session.add(company)
    db_session.flush()
    site.company_id = company.id
    db_session.add(site)
    db_session.flush()
    db_session.add_all(
        [
            SiteContact(customer_site_id=site.id, full_name="K One", created_by_id=alice.id),
            SiteContact(customer_site_id=site.id, full_name="K Two", created_by_id=mgr.id),
        ]
    )
    db_session.commit()

    rows = {r.user_id: r for r in compute_scorecard(db_session, "all_time")}
    assert rows[alice.id].accounts_added == 2
    assert rows[alice.id].contacts_added == 1
    assert rows[mgr.id].accounts_added == 1
    assert rows[mgr.id].contacts_added == 1


def test_time_range_windows_out_old_rows(db_session, users):
    alice = users["buyer"]
    now = datetime.now(UTC)
    old = now - timedelta(days=40)  # before this_month, before this_week
    _log(db_session, alice, Channel.PHONE, duration=60, when=old)
    _log(db_session, alice, Channel.PHONE, duration=60, when=now)
    db_session.commit()

    assert compute_scorecard(db_session, "all_time")[0].calls == 2
    # this_month / this_week should exclude the 40-day-old row
    month = compute_scorecard(db_session, "this_month")
    assert month and month[0].calls == 1


def test_null_user_activity_excluded(db_session, users):
    row = ActivityLog(user_id=None, activity_type="x", channel=Channel.PHONE, duration_seconds=60)
    db_session.add(row)
    db_session.commit()
    assert compute_scorecard(db_session, "all_time") == []


# ── Ranking ─────────────────────────────────────────────────────────────────


def test_ranking_is_score_desc_with_ranks_assigned(db_session, users):
    alice, mgr, admin = users["buyer"], users["manager"], users["admin"]
    # admin: 1 account added = 5 pts (highest)
    db_session.add(Company(name="A-co", created_by_id=admin.id))
    # mgr: 3 calls = 3 pts (middle)
    for _ in range(3):
        _log(db_session, mgr, Channel.PHONE)
    # alice: 1 outbound email = 1 pt (lowest)
    _log(db_session, alice, Channel.EMAIL, direction=Direction.OUTBOUND)
    db_session.commit()

    rows = compute_scorecard(db_session, "all_time")
    assert [r.user_id for r in rows] == [admin.id, mgr.id, alice.id]
    assert [r.rank for r in rows] == [1, 2, 3]
    assert [r.score for r in rows] == [5.0, 3.0, 1.0]


def test_tie_break_is_deterministic_by_name(db_session, users):
    alice, mgr = users["buyer"], users["manager"]  # "Alice" < "Manager Mo"
    _log(db_session, alice, Channel.PHONE)
    _log(db_session, mgr, Channel.PHONE)
    db_session.commit()
    rows = compute_scorecard(db_session, "all_time")
    assert [r.score for r in rows] == [1.0, 1.0]
    assert [r.user.name for r in rows] == ["Alice", "Manager Mo"]
    assert [r.rank for r in rows] == [1, 2]


# ── Route render + authz ────────────────────────────────────────────────────


def _client_as(db_session, user) -> TestClient:
    """A TestClient authed (via require_user override) as the given user."""
    from app.database import get_db
    from app.dependencies import require_user
    from app.main import app

    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[require_user] = lambda: user
    return TestClient(app)


def test_scorecard_route_renders_for_manager(db_session, users):
    mgr = users["manager"]
    _log(db_session, users["buyer"], Channel.PHONE, duration=300)
    db_session.commit()
    from app.database import get_db
    from app.dependencies import require_user
    from app.main import app

    client = _client_as(db_session, mgr)
    try:
        resp = client.get("/v2/partials/settings/scorecard")
        assert resp.status_code == 200
        body = resp.text
        assert "Activity Scorecard" in body
        assert "Score" in body
        assert "Alice" in body  # the contributor appears in the leaderboard table
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(require_user, None)


def test_scorecard_route_honors_time_range_param(db_session, users):
    from app.database import get_db
    from app.dependencies import require_user
    from app.main import app

    client = _client_as(db_session, users["admin"])
    try:
        resp = client.get("/v2/partials/settings/scorecard?time_range=this_quarter")
        assert resp.status_code == 200
        assert "This quarter" in resp.text
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(require_user, None)


def test_scorecard_route_forbidden_for_buyer(db_session, users):
    from app.database import get_db
    from app.dependencies import require_user
    from app.main import app

    client = _client_as(db_session, users["buyer"])
    try:
        resp = client.get("/v2/partials/settings/scorecard")
        assert resp.status_code == 403
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(require_user, None)


def test_range_start_all_time_is_none():
    assert range_start("all_time") is None


def test_default_time_range_constant():
    assert DEFAULT_TIME_RANGE == "this_month"
