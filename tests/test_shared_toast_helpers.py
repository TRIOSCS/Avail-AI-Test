"""tests/test_shared_toast_helpers.py — the shared showToast HX-Trigger helper pair.

Covers app.routers.htmx._shared.set_toast (the one encoding of the showToast wire
contract; default clobbers, merge=True preserves an existing HX-Trigger — the
prepayments semantics) and toast_error_response (the 200 + HX-Reswap:none error-toast
response the quote/prospecting/prepayment modals share).

Called by: pytest
Depends on: app.routers.htmx._shared, starlette.responses.Response
"""

import json

from starlette.responses import Response

from app.routers.htmx._shared import set_toast, toast_error_response


def test_set_toast_sets_hx_trigger():
    r = Response()
    set_toast(r, "Saved", "success")
    payload = json.loads(r.headers["HX-Trigger"])
    assert payload["showToast"] == {"message": "Saved", "type": "success"}


def test_set_toast_defaults_to_success():
    r = Response()
    set_toast(r, "Done")
    assert json.loads(r.headers["HX-Trigger"])["showToast"]["type"] == "success"


def test_set_toast_returns_the_response_for_chaining():
    r = Response()
    assert set_toast(r, "Done") is r


def test_set_toast_default_clobbers_existing_trigger():
    r = Response()
    r.headers["HX-Trigger"] = "somethingElse"
    set_toast(r, "Saved")
    payload = json.loads(r.headers["HX-Trigger"])
    assert payload == {"showToast": {"message": "Saved", "type": "success"}}


def test_set_toast_merge_preserves_bare_event_name():
    r = Response()
    r.headers["HX-Trigger"] = "awListRefresh"
    set_toast(r, "Submitted", "success", merge=True)
    payload = json.loads(r.headers["HX-Trigger"])
    assert payload["awListRefresh"] is True
    assert payload["showToast"] == {"message": "Submitted", "type": "success"}


def test_set_toast_merge_preserves_comma_separated_events():
    r = Response()
    r.headers["HX-Trigger"] = "evtOne, evtTwo"
    set_toast(r, "Done", merge=True)
    payload = json.loads(r.headers["HX-Trigger"])
    assert payload["evtOne"] is True
    assert payload["evtTwo"] is True
    assert payload["showToast"]["message"] == "Done"


def test_set_toast_merge_preserves_json_dict_and_replaces_old_toast():
    r = Response()
    r.headers["HX-Trigger"] = json.dumps({"otherEvent": {"a": 1}, "showToast": {"message": "old", "type": "info"}})
    set_toast(r, "new", "error", merge=True)
    payload = json.loads(r.headers["HX-Trigger"])
    assert payload["otherEvent"] == {"a": 1}
    assert payload["showToast"] == {"message": "new", "type": "error"}


def test_toast_error_response_shape():
    resp = toast_error_response("It broke")
    assert resp.status_code == 200
    assert resp.body == b""
    assert resp.headers["HX-Reswap"] == "none"
    payload = json.loads(resp.headers["HX-Trigger"])
    assert payload["showToast"] == {"message": "It broke", "type": "error"}
