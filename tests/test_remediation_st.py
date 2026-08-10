"""tests/test_remediation_st.py — QC 2026-08-10 P2 success-theater (RFQ honesty).

The RFQ results screen must NOT show a green "sent" banner when a missing Microsoft 365
token meant nothing was actually emailed — it shows an amber "NOT sent — reconnect
Microsoft 365" banner instead. (The route can't be exercised for the no-token path under
TESTING, which forces the send branch, so this asserts the template renders each outcome
honestly.)
"""

from tests.conftest import engine  # noqa: F401


def _render(**ctx):
    from app.template_env import templates

    base = {
        "total_sent": 0,
        "total_not_sent": 0,
        "total_failed": 0,
        "sent_results": [],
        "not_sent_results": [],
        "failed_results": [],
        "req": None,
        "request": None,
    }
    base.update(ctx)
    return templates.env.get_template("htmx/partials/requisitions/rfq_results.html").render(**base)


def test_no_token_shows_not_sent_not_a_green_sent_banner():
    html = _render(total_not_sent=2, not_sent_results=[{"vendor": "Acme", "email": "a@x.com", "status": "not_sent"}])
    assert "NOT sent" in html
    assert "Microsoft 365 not connected" in html
    assert "RFQ sent to" not in html  # the lie is gone — no green "sent" banner


def test_real_send_still_shows_sent_banner():
    html = _render(total_sent=3, sent_results=[{"vendor": "Acme", "email": "a@x.com", "status": "sent"}])
    assert "RFQ sent to 3 vendor(s)" in html
    assert "NOT sent" not in html


def test_failed_sends_show_a_failure_banner():
    html = _render(total_failed=1, failed_results=[{"vendor": "Bad", "email": "b@x.com", "status": "failed"}])
    assert "failed to send" in html
