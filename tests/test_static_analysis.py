"""test_static_analysis.py — Static-analysis tests that lock in HTMX conventions across
the codebase.

These tests grep the source tree for anti-patterns that have caused
self-trigger loops, missing loading indicators, and stale-response
swaps in the past. Each rule is documented in
``docs/htmx-conventions.md``; the tests below enforce the convention
mechanically so a future regression fails CI before it ships.

Called by: pytest
Depends on: app/routers/sightings.py, app/templates, app/static/htmx_app.js
"""

import os

os.environ["TESTING"] = "1"

import re
from pathlib import Path


def test_broker_publish_calls_have_source_gate():
    """Every endpoint in sightings router that calls broker.publish must accept a
    'source' query param so SSE-triggered calls can suppress publication and break the
    self-trigger loop."""
    src = Path("app/routers/sightings.py").read_text()
    lines = src.splitlines()
    publish_lines = [(i, line) for i, line in enumerate(lines, 1) if "broker.publish(" in line]

    for lineno, line in publish_lines:
        # Walk back to find the enclosing function signature
        i = lineno - 2
        while i >= 0 and not re.match(r"^\s*(async\s+)?def\s+\w+", lines[i]):
            i -= 1
        if i < 0:
            continue
        # Find end of signature (the line ending with `):`)
        sig_end = i
        while sig_end < len(lines) and not lines[sig_end].rstrip().endswith("):"):
            sig_end += 1
        sig_block = "\n".join(lines[i : sig_end + 1])
        # The helper itself defines `broker.publish` inside `_publish_if_user_source` —
        # that one's allowed without a `source` param in the signature.
        fn_name_match = re.match(r"^\s*(async\s+)?def\s+(\w+)", lines[i])
        fn_name = fn_name_match.group(2) if fn_name_match else ""
        if fn_name == "_publish_if_user_source":
            continue
        assert "source" in sig_block, (
            f"broker.publish at line {lineno} is in function '{fn_name}' "
            f"which has no 'source' parameter — add the SSE gate per docs/htmx-conventions.md."
        )


def test_htmx_ajax_calls_have_indicator():
    """Every htmx.ajax(...) call site in templates and JS must pass an
    indicator: option (HTMX 2.x does not auto-read hx-indicator for
    imperative calls)."""
    paths = list(Path("app/templates").rglob("*.html")) + [Path("app/static/htmx_app.js")]
    # Pre-existing call sites grandfathered in. Adding new sites without an
    # indicator: option will fail this test — fix the call site, don't extend
    # the allowlist. Drain the list as those sites get fixed.
    allowlist: set[tuple[str, int]] = {
        ("app/templates/htmx/base.html", 56),
        ("app/templates/requisitions2/_inline_cell.html", 16),
        ("app/templates/htmx/partials/sightings/vendor_modal.html", 26),
        ("app/templates/htmx/partials/sightings/vendor_modal.html", 44),
        ("app/templates/htmx/partials/sourcing/workspace.html", 177),
        ("app/templates/htmx/partials/excess/bid_form.html", 16),
        ("app/templates/htmx/partials/parts/cell_edit.html", 12),
        ("app/templates/htmx/partials/parts/cell_edit.html", 26),
        ("app/templates/htmx/partials/parts/cell_edit.html", 37),
        ("app/templates/htmx/partials/parts/workspace.html", 83),
        ("app/templates/htmx/partials/parts/workspace.html", 89),
        ("app/templates/htmx/partials/parts/list.html", 11),
        ("app/templates/htmx/partials/parts/list.html", 12),
        ("app/templates/htmx/partials/parts/list.html", 138),
        ("app/templates/htmx/partials/parts/list.html", 295),
        ("app/templates/htmx/partials/parts/list.html", 342),
        ("app/templates/htmx/partials/parts/tabs/req_details.html", 209),
    }

    failures: list[str] = []
    for p in paths:
        if not p.exists():
            continue
        content = p.read_text()
        lines = content.splitlines()
        for i, line in enumerate(lines, 1):
            m = re.search(r"\bhtmx\.ajax\(", line)
            if not m:
                continue
            # Collect up to 5 lines forward looking for the closing paren
            block = "\n".join(lines[i - 1 : min(len(lines), i + 5)])
            close = block.find(")", m.end())
            if close < 0:
                # Couldn't find close in window — skip (likely template macro)
                continue
            call_text = block[: close + 1]
            rel = str(p.relative_to(Path(".")))
            if (rel, i) in allowlist:
                continue
            if "indicator:" not in call_text:
                failures.append(f"{rel}:{i} — htmx.ajax missing indicator: option")

    assert not failures, "\n".join(failures)


# Sightings partials that target a context-sensitive detail panel and
# therefore require the X-Rendered-Req-Id correlation header so the
# client-side beforeSwap guard can drop stale responses. Add to this
# set when a new partial joins the same family of swaps.
_REQ_ID_HEADERED_SIGHTINGS_PARTIALS = (
    "htmx/partials/sightings/detail.html",
    "htmx/partials/sightings/activity_section.html",
)


def test_sightings_template_responses_set_rendered_req_id():
    """Every template_response() rendering a context-sensitive sightings partial must
    set the X-Rendered-Req-Id header so the client htmx:beforeSwap stale-response guard
    works."""
    src = Path("app/routers/sightings.py").read_text()
    lines = src.splitlines()
    failures: list[str] = []
    for i, line in enumerate(lines, 1):
        # Match both the migrated `template_response(` and any direct
        # `templates.TemplateResponse(` that might creep back in.
        if "template_response(" not in line and "TemplateResponse(" not in line:
            continue
        if not any(p in line for p in _REQ_ID_HEADERED_SIGHTINGS_PARTIALS):
            continue
        # Look forward up to 8 lines for X-Rendered-Req-Id assignment
        window = "\n".join(lines[i - 1 : min(len(lines), i + 8)])
        if "X-Rendered-Req-Id" not in window:
            failures.append(f"sightings.py:{i} — template_response without X-Rendered-Req-Id within 8 lines")
    assert not failures, "\n".join(failures)


# Note: a `test_connectors_raise_on_hard_auth_errors` test belongs to the
# parallel connector hard-errors PR (#106) — it depends on connector source
# changes that are not part of this hunt PR. Adding the connector enforcement
# here without those source changes would break CI on main. Ship the test in
# the same commit as the source changes it locks in.


def test_htmx_views_like_patterns_are_escaped():
    """HIGH-SEC-3: every ILIKE/LIKE pattern built from a user-supplied
    search term in htmx_views.py must escape LIKE wildcards.

    Two enforced invariants:
      1. No f-string that interpolates a bare `.strip()` term into a
         `%...%` pattern (must go through escape_like / SearchBuilder.safe).
      2. Every `.ilike(...)` / `.like(...)` call that takes a `%`-wrapped
         pattern variable must pass an explicit `escape=` character so the
         backslash escaping done by escape_like is actually honoured.
    """
    src = Path("app/routers/htmx_views.py").read_text()
    lines = src.splitlines()
    failures: list[str] = []

    # Rule 1: forbid `f"%{<term>.strip()}%"` — unescaped wildcard injection.
    bad_fstring = re.compile(r'f"%\{[^}]*\.strip\(\)\}%"')
    for i, line in enumerate(lines, 1):
        if bad_fstring.search(line):
            failures.append(
                f"htmx_views.py:{i} — f-string builds a LIKE pattern from a raw "
                f".strip() term; wrap it in escape_like()."
            )

    # Rule 2: an .ilike(<var>) / .like(<var>) on a %-wrapped pattern must
    # carry escape=. We only flag calls whose argument is a known
    # escaped-pattern variable name.
    escaped_pattern_vars = ("term", "pattern", "safe")
    call_re = re.compile(r"\.i?like\(\s*([A-Za-z_][A-Za-z0-9_]*)\s*([,)])")
    for i, line in enumerate(lines, 1):
        for m in call_re.finditer(line):
            varname, after = m.group(1), m.group(2)
            if varname not in escaped_pattern_vars:
                continue
            if after == ")":  # no further args → no escape= present
                failures.append(
                    f"htmx_views.py:{i} — .ilike({varname}) has no escape= argument; "
                    f'use .ilike({varname}, escape="\\\\").'
                )

    assert not failures, "\n".join(failures)


def test_no_asyncio_run_in_htmx_views():
    """HIGH-BE-4: asyncio.run() must never appear in htmx_views.py — it
    creates a fresh event loop and fails/blocks inside request or
    background-task contexts. Use `await` on the coroutine instead.
    """
    src = Path("app/routers/htmx_views.py").read_text()
    offenders = [i for i, line in enumerate(src.splitlines(), 1) if "asyncio.run(" in line]
    assert not offenders, f"asyncio.run( found in htmx_views.py at line(s): {offenders}"


def test_templates_never_reference_static_public_prefix():
    """Production serves /static/* from the Vite *build* output (app/static/dist/,
    copied into Caddy's static_files volume by docker-entrypoint.sh). Vite flattens its
    public/ directory into the output root, so files that live at
    app/static/public/<name> are served at /static/<name> — the `public/` segment NEVER
    appears in a served URL. Caddy answers /static/* directly from that volume and never
    proxies to the FastAPI source mount, so a /static/public/... URL is a guaranteed 404
    in production.

    Regression guard for commit 69066c67, which mistakenly rewrote the logo references
    from /static/avail_logo*.png to /static/public/avail_logo*.png and broke every logo
    on the live site.
    """
    offenders = []
    for path in Path("app/templates").rglob("*.html"):
        for i, line in enumerate(path.read_text().splitlines(), 1):
            if "/static/public/" in line:
                offenders.append(f"{path}:{i}: {line.strip()}")
    assert not offenders, (
        "Templates must reference Vite-flattened static URLs (/static/<name>), not the "
        "source-only /static/public/<name> path, which 404s through Caddy in production:\n" + "\n".join(offenders)
    )


def test_standalone_pages_register_csrf_listener():
    """Standalone page templates that issue mutating HTMX requests but do not
    unconditionally load htmx_app.js must register their own htmx:configRequest CSRF
    listener — otherwise starlette_csrf rejects every hx-post/patch/delete (CRIT-FE-1).

    requisitions2/page.html loads requisitions2.js for exactly this reason.
    """
    js = Path("app/static/js/requisitions2.js").read_text()
    assert "htmx:configRequest" in js, (
        "requisitions2.js must register an htmx:configRequest listener that "
        "attaches the x-csrftoken header — page.html does not always load "
        "htmx_app.js, which carries the shared listener."
    )
    assert "x-csrftoken" in js, "requisitions2.js CSRF listener must set the x-csrftoken header"
