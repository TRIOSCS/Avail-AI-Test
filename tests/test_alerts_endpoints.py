"""Endpoint tests for the cross-app alert router (Phase 1).

Covers the per-tab badge endpoint (renders the emerald pill / empty, fail-quiet on an
unknown tab) and the mark-seen endpoint (idempotent upsert + OOB nav-badge response,
fail-quiet on an unknown kind). The `client` fixture is authenticated as `test_user`.
"""

from app.constants import AlertKind
from app.models.alert_seen import AlertSeen


def test_badge_empty_when_no_items(client):
    r = client.get("/v2/partials/alerts/requisitions/badge")
    assert r.status_code == 200
    assert r.text == ""


def test_badge_unknown_tab_is_empty_not_error(client):
    r = client.get("/v2/partials/alerts/nonsense-tab/badge")
    assert r.status_code == 200
    assert r.text == ""


def test_seen_records_row_and_returns_oob_badge(client, db_session, test_user):
    r = client.post("/v2/partials/alerts/offer_confirmed/seen", data={"ref_id": 4242})
    assert r.status_code == 200
    assert 'id="requisitions-nav-badge"' in r.text  # OOB targets the owning tab's badge
    assert 'hx-swap-oob="innerHTML"' in r.text
    rows = (
        db_session.query(AlertSeen)
        .filter_by(user_id=test_user.id, alert_kind=AlertKind.OFFER_CONFIRMED, ref_id=4242)
        .count()
    )
    assert rows == 1


def test_seen_is_idempotent(client, db_session, test_user):
    for _ in range(2):
        client.post("/v2/partials/alerts/offer_confirmed/seen", data={"ref_id": 7})
    rows = (
        db_session.query(AlertSeen)
        .filter_by(user_id=test_user.id, alert_kind=AlertKind.OFFER_CONFIRMED, ref_id=7)
        .count()
    )
    assert rows == 1


def test_seen_unknown_kind_is_empty_not_error(client):
    r = client.post("/v2/partials/alerts/bogus_kind/seen", data={"ref_id": 1})
    assert r.status_code == 200
    assert r.text == ""
