"""Tests for the Activity Scorecard — raw-count aggregation, ranking, route render.

Covers:
  - per-user raw counts (calls / emails / accounts / contacts) + their total
  - channel/direction mapping correctness (only the right rows count)
  - time-range windowing (this_week/month/quarter/all_time)
  - ranking order (total desc, deterministic tie-break) + 1-based ranks
  - the CRM /v2/partials/crm/scorecard route: ALL users → 200 (no manager gate),
    the time-range fragment swap, and the retired settings route → 404
  - the leaderboard table renders the six columns (User/Calls/Emails/Accounts/
    Contacts/Total) and no longer shows a Score / Talk time / IMs column

Depends on: tests/conftest.py fixtures (db_session), app.services.activity_scorecard,
app.models, app.constants.
"""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.constants import Channel, Direction
from app.models import ActivityLog, Company, CustomerSite, SiteContact, User
from app.services.activity_scorecard import (
    DEFAULT_TIME_RANGE,
    TIME_RANGE_LABELS,
    TIME_RANGES,
    ScorecardRow,
    compute_scorecard,
    range_start,
)

UTC = UTC


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


def _make_contacts(db, site, creators):
    """Seed SiteContacts under ``site`` for each creator (may repeat)."""
    db.add_all(
        [
            SiteContact(customer_site_id=site.id, full_name=f"K{i}", created_by_id=creator.id)
            for i, creator in enumerate(creators)
        ]
    )


# ── Row total (the ranking key) ─────────────────────────────────────────────


def test_total_is_the_sum_of_the_four_counts():
    row = ScorecardRow(user_id=1, user=None, calls=2, emails=3, accounts=1, contacts=4)
    assert row.total == 10


# ── Metric aggregation correctness ──────────────────────────────────────────


def test_only_correct_channels_and_directions_count(db_session, users):
    alice = users["buyer"]
    # 2 phone calls — both directions count as calls
    _log(db_session, alice, Channel.PHONE, direction=Direction.OUTBOUND, duration=120)
    _log(db_session, alice, Channel.PHONE, direction=Direction.INBOUND, duration=180)
    # outbound email counts; inbound email does NOT
    _log(db_session, alice, Channel.EMAIL, direction=Direction.OUTBOUND)
    _log(db_session, alice, Channel.EMAIL, direction=Direction.INBOUND)
    # Teams / WeChat / system rows are ignored entirely (no IM metric anymore)
    _log(db_session, alice, Channel.TEAMS, direction=Direction.OUTBOUND)
    _log(db_session, alice, Channel.WECHAT, direction=Direction.OUTBOUND)
    _log(db_session, alice, Channel.SYSTEM, direction=Direction.OUTBOUND)
    db_session.commit()

    rows = compute_scorecard(db_session, "all_time")
    assert len(rows) == 1
    row = rows[0]
    assert row.calls == 2
    assert row.emails == 1
    assert row.accounts == 0
    assert row.contacts == 0
    assert row.total == 3  # calls + emails only


def test_all_four_counts_and_total_combine(db_session, users):
    """A user with every activity type: total = calls + emails + accounts + contacts."""
    alice = users["buyer"]
    _log(db_session, alice, Channel.PHONE, direction=Direction.INBOUND)
    _log(db_session, alice, Channel.PHONE, direction=Direction.OUTBOUND)
    _log(db_session, alice, Channel.EMAIL, direction=Direction.OUTBOUND)
    company = Company(name="Host", created_by_id=alice.id)
    db_session.add(company)
    db_session.flush()
    site = CustomerSite(company_id=company.id, site_name="HQ")
    db_session.add(site)
    db_session.flush()
    _make_contacts(db_session, site, [alice])
    db_session.commit()

    row = compute_scorecard(db_session, "all_time")[0]
    assert (row.calls, row.emails, row.accounts, row.contacts) == (2, 1, 1, 1)
    assert row.total == 5


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
    company = Company(name="Host", created_by_id=None)
    db_session.add(company)
    db_session.flush()
    site = CustomerSite(company_id=company.id, site_name="HQ")
    db_session.add(site)
    db_session.flush()
    _make_contacts(db_session, site, [alice, mgr])
    db_session.commit()

    rows = {r.user_id: r for r in compute_scorecard(db_session, "all_time")}
    assert rows[alice.id].accounts == 2
    assert rows[alice.id].contacts == 1
    assert rows[mgr.id].accounts == 1
    assert rows[mgr.id].contacts == 1


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


def test_ranking_is_total_desc_with_ranks_assigned(db_session, users):
    alice, mgr, admin = users["buyer"], users["manager"], users["admin"]
    # alice: 5 phone calls -> total 5 (highest)
    for _ in range(5):
        _log(db_session, alice, Channel.PHONE)
    # mgr: 3 outbound emails -> total 3 (middle)
    for _ in range(3):
        _log(db_session, mgr, Channel.EMAIL, direction=Direction.OUTBOUND)
    # admin: 1 account added -> total 1 (lowest)
    db_session.add(Company(name="A-co", created_by_id=admin.id))
    db_session.commit()

    rows = compute_scorecard(db_session, "all_time")
    assert [r.user_id for r in rows] == [alice.id, mgr.id, admin.id]
    assert [r.rank for r in rows] == [1, 2, 3]
    assert [r.total for r in rows] == [5, 3, 1]


def test_tie_break_is_deterministic_by_name(db_session, users):
    alice, mgr = users["buyer"], users["manager"]  # "Alice" < "Manager Mo"
    _log(db_session, alice, Channel.PHONE)
    _log(db_session, mgr, Channel.PHONE)
    db_session.commit()
    rows = compute_scorecard(db_session, "all_time")
    assert [r.total for r in rows] == [1, 1]
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


def test_scorecard_route_renders_six_columns(db_session, users):
    _log(db_session, users["buyer"], Channel.PHONE, duration=300)
    _log(db_session, users["buyer"], Channel.EMAIL, direction=Direction.OUTBOUND)
    db_session.commit()
    from app.database import get_db
    from app.dependencies import require_user
    from app.main import app

    client = _client_as(db_session, users["manager"])
    try:
        resp = client.get("/v2/partials/crm/scorecard")
        assert resp.status_code == 200
        body = resp.text
        assert "Activity Scorecard" in body
        assert "Alice" in body  # the contributor appears in the leaderboard table
        # The six data columns are present.
        for header in (">User<", ">Calls<", ">Emails<", ">Accounts<", ">Contacts<", ">Total<"):
            assert header in body, header
        # The dropped columns are gone.
        for gone in (">Score<", ">Talk time<", ">IMs<"):
            assert gone not in body, gone
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(require_user, None)


def test_scorecard_route_honors_time_range_param(db_session, users):
    from app.database import get_db
    from app.dependencies import require_user
    from app.main import app

    client = _client_as(db_session, users["admin"])
    try:
        resp = client.get("/v2/partials/crm/scorecard?time_range=this_quarter")
        assert resp.status_code == 200
        assert "This quarter" in resp.text
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(require_user, None)


def test_scorecard_route_visible_to_buyer(db_session, users):
    """The CRM scorecard has no manager gate — a buyer/sales user gets 200, not 403."""
    from app.database import get_db
    from app.dependencies import require_user
    from app.main import app

    client = _client_as(db_session, users["buyer"])
    try:
        resp = client.get("/v2/partials/crm/scorecard")
        assert resp.status_code == 200
        assert "Activity Scorecard" in resp.text
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(require_user, None)


def test_scorecard_time_range_selector_swaps_only_the_table_fragment(db_session, users):
    """An HX-Request triggered by the time_range select returns the bare table fragment
    (no page header / selector chrome), so it swaps into #scorecard-table in place."""
    _log(db_session, users["buyer"], Channel.PHONE)
    db_session.commit()
    from app.database import get_db
    from app.dependencies import require_user
    from app.main import app

    client = _client_as(db_session, users["buyer"])
    try:
        resp = client.get(
            "/v2/partials/crm/scorecard?time_range=this_week",
            headers={"HX-Request": "true", "HX-Trigger-Name": "time_range"},
        )
        assert resp.status_code == 200
        body = resp.text
        # The fragment is the table only — no header / selector chrome.
        assert ">Total<" in body
        assert "Activity Scorecard" not in body
        assert 'id="scorecard-root"' not in body
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(require_user, None)


def test_old_settings_scorecard_route_is_gone(db_session, users):
    """The scorecard moved to the CRM tab; the retired settings route no longer
    exists."""
    from app.database import get_db
    from app.dependencies import require_user
    from app.main import app

    client = _client_as(db_session, users["admin"])
    try:
        resp = client.get("/v2/partials/settings/scorecard")
        assert resp.status_code == 404
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(require_user, None)


# ── Time-range helpers ──────────────────────────────────────────────────────


def test_range_start_all_time_is_none():
    assert range_start("all_time") is None


def test_default_time_range_constant():
    assert DEFAULT_TIME_RANGE == "this_month"
    assert set(TIME_RANGES) == set(TIME_RANGE_LABELS)
