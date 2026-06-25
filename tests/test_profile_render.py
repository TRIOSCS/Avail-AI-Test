# tests/test_profile_render.py
# What: render tests for the polished Settings -> Profile tab (no dup email / no
#       "coming soon"; inline-editable name + 8x8 extension posting to
#       /api/user/profile; Notifications card with two toggle endpoints;
#       mailbox-sync friendly copy + disconnected empty state).
# Called by: pytest.
# Depends on: GET /v2/partials/settings/profile (htmx_views.settings_profile_tab),
#             the profile.html + _mailbox_sync_card.html partials, and the
#             `client`, `db_session`, `test_user` fixtures from conftest.


def _html(client):
    resp = client.get("/v2/partials/settings/profile")
    assert resp.status_code == 200
    return resp.text


def test_profile_has_no_coming_soon_or_dup_email(client, db_session, test_user):
    html = _html(client)
    assert "coming soon" not in html.lower()
    # email shows once (under the name) -- the standalone "Email" field is gone.
    assert html.count(test_user.email) == 1


def test_profile_has_notification_toggles(client):
    html = _html(client)
    assert "/api/user/toggle-buyplan-email" in html
    assert "/api/user/toggle-new-offer-alert" in html


def test_profile_has_name_edit_and_extension(client):
    html = _html(client)
    # the name/extension form posts here
    assert "/api/user/profile" in html
    # inline-edit affordance for the display name
    assert 'hx-post="/api/user/profile"' in html
    # 8x8 extension input is present and relabeled to plain language
    assert 'name="extension"' in html
    assert "Click-to-call" in html


def test_profile_notifications_card_present(client):
    html = _html(client)
    assert "Notifications" in html
    assert "buy-plan" in html.lower()
    assert "approved offers" in html.lower()


def test_mailbox_card_connected_copy(client):
    # test_user has m365_connected=True
    html = _html(client)
    assert "Mailbox" in html
    # no disconnected empty state when connected
    assert "Mailbox not connected" not in html


def test_mailbox_card_disconnected_empty_state(client, db_session, test_user):
    test_user.m365_connected = False
    db_session.commit()
    html = _html(client)
    assert "Mailbox not connected" in html


def test_name_with_quotes_does_not_break_alpine_attr(client, db_session, test_user):
    # tojson escapes ' and " so a tricky name stays inside the single-quoted x-data.
    test_user.name = 'O\'Brien "Co"'
    db_session.commit()
    html = _html(client)
    # the x-data attribute opens with a single quote and the json-encoded name
    # must NOT contain a raw apostrophe (would close the attr early).
    start = html.index("x-data='")
    end = html.index("'>", start)
    block = html[start:end]
    assert "\\u0027" in block  # escaped apostrophe from O'Brien
    assert "editingName: false" in block
