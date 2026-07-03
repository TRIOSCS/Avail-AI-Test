"""test_prepayment_config_keys.py — the prepayment-notification config keys (Task 4).

The accounting/AP group inboxes + the Teams webhook URL are curated System settings
(app/routers/admin/system.py SYSTEM_SETTINGS_META) so an admin can set them and
prepayment_notifications can read them at notify time.

Called by: pytest
Depends on: app.routers.admin.system.SYSTEM_SETTINGS_META.
"""

from app.constants import UserRole
from app.routers.admin.system import SYSTEM_SETTINGS_META

_KEYS = ("accounting_group_email", "ap_group_email", "prepayment_teams_webhook")


def test_prepayment_notification_keys_registered():
    for k in _KEYS:
        assert k in SYSTEM_SETTINGS_META


def test_prepayment_notification_keys_are_string_typed_admin_settings():
    for k in _KEYS:
        meta = SYSTEM_SETTINGS_META[k]
        assert meta["type"] == "string"
        assert meta.get("default", "") == ""  # empty default → channel skipped until set
        assert meta["label"]  # a human-facing label is present for the admin control


def test_system_tab_renders_string_inputs_for_notification_keys(client, test_user):
    """The System settings tab renders each notification key as an editable text
    control."""
    test_user.role = UserRole.ADMIN
    html = client.get("/v2/partials/settings/system").text
    for k in _KEYS:
        assert f"/api/admin/config/{k}" in html  # its inline write form is present
    assert "Accounting group email" in html
    assert "Prepayment Teams webhook" in html
