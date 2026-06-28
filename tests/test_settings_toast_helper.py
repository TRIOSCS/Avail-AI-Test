# tests/test_settings_toast_helper.py
# Tests for the shared settings_toast HX-Trigger helper.
# Called by: this test suite only (verifies the helper imported from htmx_views).
# Depends on: app.routers.htmx.settings.settings_toast, starlette.responses.Response
import json

from starlette.responses import Response

from app.routers.htmx.settings import settings_toast


def test_settings_toast_sets_hx_trigger():
    r = Response()
    settings_toast(r, "Saved", "success")
    payload = json.loads(r.headers["HX-Trigger"])
    assert payload["showToast"] == {"message": "Saved", "type": "success"}


def test_settings_toast_defaults_to_success():
    r = Response()
    settings_toast(r, "Done")
    assert json.loads(r.headers["HX-Trigger"])["showToast"]["type"] == "success"
