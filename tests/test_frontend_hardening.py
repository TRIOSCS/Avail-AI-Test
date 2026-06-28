"""test_frontend_hardening.py — Static-analysis regression guards for the High-tier
frontend security/accessibility findings (CODE_REVIEW_NOTES.md: HIGH-FE-1..5, HIGH-
SEC-1).

These lock in fixes that already shipped (commit 62ef9cf9 + this PR) so a future edit
cannot silently reintroduce a CSRF gap, an innerHTML XSS sink, a label-less login input,
an un-labelled modal, or an unverified CDN <script>. They grep the template/static tree
rather than render, so they run without a browser.

Called by: pytest
Depends on: app/templates/**, app/static/htmx_app.js, app/main.py CSRF exempt_urls
"""

import os

os.environ["TESTING"] = "1"

import re
from pathlib import Path

_TEMPLATES_DIR = Path("app/templates")
_HTMX_APP_JS = Path("app/static/htmx_app.js")

# Mutating HTTP verbs that starlette_csrf (app/main.py) requires an x-csrftoken header for.
_MUTATING_METHOD_RX = re.compile(r"method:\s*['\"](?:POST|PUT|PATCH|DELETE)['\"]")
# URL fragments whose endpoints are CSRF-exempt in app/main.py exempt_urls — a raw
# mutating fetch to one of these legitimately ships no csrftoken. Keep in sync with main.py.
_CSRF_EXEMPT_SUBSTR = ("/auth/login", "/auth/callback")


def _templates():
    return sorted(_TEMPLATES_DIR.rglob("*.html"))


# ── HIGH-FE-1 — raw fetch() must carry the CSRF header on mutating calls ──────────


def test_mutating_template_fetches_send_csrf_header():
    """Any template-inline fetch() with a mutating method (POST/PUT/PATCH/DELETE) must
    attach the x-csrftoken header, unless it targets a CSRF-exempt endpoint.

    Raw fetch() bypasses htmx's global htmx:configRequest CSRF listener, so the header
    must be set explicitly or starlette_csrf rejects the request with 403.
    """
    offenders = []
    for p in _templates():
        txt = p.read_text()
        if not _MUTATING_METHOD_RX.search(txt):
            continue
        if "csrftoken" in txt.lower():
            continue
        if any(s in txt for s in _CSRF_EXEMPT_SUBSTR):
            continue
        offenders.append(str(p))
    assert not offenders, (
        "Mutating fetch() without an x-csrftoken header (starlette_csrf rejects these). "
        "Add 'X-CSRFToken': (document.cookie.match(/csrftoken=([^;]+)/)?.[1] || '') to the "
        "fetch headers, or convert to htmx (which carries CSRF via configRequest):\n" + "\n".join(offenders)
    )


def test_trouble_report_submit_is_hardened():
    """The trouble-report submit (moved from the template into htmx_app.js) must keep
    sending CSRF and must swap via htmx, not a raw innerHTML assignment (HIGH-
    FE-1/2)."""
    js = _HTMX_APP_JS.read_text()
    start = js.index("function submitTroubleReport")
    body = js[start : start + 1200]
    assert "X-CSRFToken" in body or "csrfToken" in body, "submitTroubleReport must send the CSRF header on its POST."
    assert "htmx.swap" in body, "submitTroubleReport must render the response via htmx.swap."
    assert not re.search(r"innerHTML\s*=\s*[A-Za-z_$]", body), (
        "submitTroubleReport must not assign a variable to innerHTML."
    )


# ── HIGH-FE-2 — no innerHTML = <variable> XSS sink in templates ───────────────────


def test_no_innerHTML_variable_assignment_in_templates():
    """Forbid `.innerHTML = <identifier>` in templates — assigning a variable that holds
    server/user HTML is the CLAUDE.md-banned XSS sink.

    Clearing via `innerHTML = ''` (a string literal) is allowed; use htmx swap /
    textContent for content.
    """
    rx = re.compile(r"innerHTML\s*=\s*[A-Za-z_$]")
    offenders = []
    for p in _templates():
        for i, line in enumerate(p.read_text().splitlines(), 1):
            if "hx-swap" in line:
                continue
            if rx.search(line):
                offenders.append(f"{p}:{i}: {line.strip()}")
    assert not offenders, (
        "innerHTML assigned from a variable (XSS sink). Use htmx.ajax()/htmx.swap or "
        "textContent instead:\n" + "\n".join(offenders)
    )


# ── HIGH-FE-3 — every cross-origin CDN <script> carries SRI + crossorigin ─────────


def test_requisitions2_cdn_scripts_have_sri():
    """requisitions2/page.html loads its HTMX/Alpine stack from unpkg; every external
    <script src="https://..."> must pin an integrity hash + crossorigin so a CDN
    compromise cannot execute arbitrary code in the user's session."""
    txt = Path("app/templates/requisitions2/page.html").read_text()
    offenders = [
        ln.strip()
        for ln in txt.splitlines()
        if re.search(r"<script[^>]*src=\"https://", ln) and ("integrity=" not in ln or "crossorigin" not in ln)
    ]
    assert not offenders, "External CDN <script> missing integrity/crossorigin (SRI):\n" + "\n".join(offenders)


# ── HIGH-FE-4 — the global modal labels itself for screen readers ────────────────


def test_global_modal_has_aria_labelledby():
    """The shared modal dialog in base.html must expose aria-labelledby pointing at the
    per-modal heading id (modal-title) so screen readers announce a name (WCAG
    4.1.2)."""
    txt = Path("app/templates/htmx/base.html").read_text()
    assert 'role="dialog"' in txt, "base.html must keep the role=dialog modal."
    assert 'aria-labelledby="modal-title"' in txt, (
        'The role=dialog modal in base.html must set aria-labelledby="modal-title".'
    )


# ── HIGH-FE-5 — login inputs are programmatically labelled ───────────────────────


def test_login_inputs_have_associated_labels():
    """login.html email + password inputs must have associated <label for=...> elements;
    placeholder text is not an accessible name."""
    txt = Path("app/templates/htmx/login.html").read_text()
    for field in ("login-email", "login-password"):
        assert f'id="{field}"' in txt, f"login.html missing input id={field}."
        assert f'for="{field}"' in txt, f"login.html missing <label for={field}>."
