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


def test_mailbox_card_auth_error_shows_reconnect(client, db_session, test_user):
    """A dead sign-in surfaces an actionable reconnect link, not a generic snag."""
    from app.services.m365_status import REASON_AUTH

    test_user.m365_connected = True
    test_user.access_token = "tok"
    test_user.token_expires_at = None
    test_user.m365_error_reason = REASON_AUTH
    db_session.commit()
    html = _html(client)
    assert 'href="/auth/login"' in html
    assert "Reconnect Microsoft 365" in html
    # the old generic copy is gone
    assert "hit a snag" not in html.lower()


def test_mailbox_card_transient_error_no_reconnect(client, db_session, test_user):
    """A transient error reads as self-healing — no reconnect, no raw text, no snag."""
    from app.services.m365_status import REASON_TRANSIENT

    test_user.m365_connected = True
    test_user.access_token = "tok"
    test_user.token_expires_at = None
    test_user.m365_error_reason = REASON_TRANSIENT
    db_session.commit()
    html = _html(client)
    assert REASON_TRANSIENT in html
    assert "hit a snag" not in html.lower()
    # transient must not push the user to reconnect
    assert "Reconnect Microsoft 365" not in html


def test_mailbox_card_never_shows_generic_snag(client, db_session, test_user):
    """Regression: the generic 'we hit a snag' banner is fully retired."""
    test_user.m365_connected = True
    test_user.m365_error_reason = "Inbox scan timed out"  # legacy raw value
    db_session.commit()
    html = _html(client)
    assert "hit a snag" not in html.lower()


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


# ── Pan/zoom face-centering cropper ───────────────────────────────────────────


class TestAvatarCropper:
    """The profile-photo card mounts the vanilla-Alpine pan/zoom cropper, which replaces
    the old auto-submit-on-file-pick flow with a circular crop viewport that posts a
    512² JPEG/PNG to the unchanged /api/user/avatar route."""

    def test_mounts_cropper_component(self, client):
        html = _html(client)
        # The Alpine factory is wired with the existing upload URL + 2 MB cap.
        assert "avatarCropper('/api/user/avatar'" in html

    def test_file_pick_opens_cropper_not_autosubmit(self, client):
        """Picking a file now calls openFile() (loads into the crop modal) instead of
        requestSubmit() on a form (the pre-cropper auto-upload behavior)."""
        html = _html(client)
        assert 'x-ref="fileInput"' in html
        assert '@change="openFile($event)"' in html
        # The old auto-submit-on-change wiring is gone.
        assert "requestSubmit()" not in html

    def test_has_circular_crop_viewport_and_zoom(self, client):
        html = _html(client)
        # Canvas-backed circular viewport with the dimmed mask + a zoom range slider.
        assert "avatar-crop-stage" in html
        assert "avatar-crop-mask" in html
        assert 'x-ref="canvas"' in html
        assert 'type="range"' in html
        assert 'x-model.number="zoomPct"' in html

    def test_has_pan_and_pinch_handlers(self, client):
        html = _html(client)
        # Pan (mouse + touch) and zoom (wheel + pinch) are all wired on the stage.
        assert "pointerDown($event)" in html
        assert "wheel($event)" in html
        assert "touchStart($event)" in html
        assert "touchMove($event)" in html

    def test_modal_has_save_and_cancel(self, client):
        html = _html(client)
        assert 'class="btn btn-sm btn-primary"' in html
        assert '@click="save()"' in html
        assert '@click="close()"' in html

    def test_card_refreshes_on_avatar_updated_event(self, client):
        """Cropper upload + Remove both dispatch the kebab `avatar-updated` event the
        card listens for to refresh its preview (camelCase avatarUpdated never reaches
        Alpine's @-binding)."""
        html = _html(client)
        assert "@avatar-updated.window=" in html
        assert "applyAvatar(" in html

    def test_upload_route_is_unchanged(self, client):
        html = _html(client)
        # The cropper posts to the same magic-byte-guarded endpoint; no new route.
        assert "/api/user/avatar" in html
