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
        ("app/templates/htmx/partials/sourcing/workspace.html", 176),
        ("app/templates/htmx/partials/excess/bid_form.html", 16),
        ("app/templates/htmx/partials/parts/cell_edit.html", 12),
        ("app/templates/htmx/partials/parts/cell_edit.html", 26),
        ("app/templates/htmx/partials/parts/cell_edit.html", 37),
        ("app/templates/htmx/partials/parts/workspace.html", 97),
        ("app/templates/htmx/partials/parts/workspace.html", 103),
        ("app/templates/htmx/partials/parts/list.html", 11),
        ("app/templates/htmx/partials/parts/list.html", 12),
        ("app/templates/htmx/partials/parts/list.html", 138),
        ("app/templates/htmx/partials/parts/list.html", 277),
        ("app/templates/htmx/partials/parts/list.html", 324),
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


# Sites where |tojson sits inside a DOUBLE-quoted Alpine attribute but is SAFE because the
# value it serialises is an int/bool — tojson renders true/false or [1, 2, 3], which contain
# no double quote, so the attribute cannot be broken. Anything else (a string or list of
# strings) MUST use a single-quoted attribute (or a JS Alpine.data factory). Keyed (file, line
# of the attribute's opening). Do NOT add a string/array site here — fix the call site instead.
_TOJSON_IN_DOUBLE_QUOTED_ALPINE_ALLOWLIST: set[tuple[str, int]] = {
    ("app/templates/htmx/partials/quote_builder/modal.html", 10),  # has_customer_site|tojson -> true/false
    ("app/templates/htmx/partials/requisitions/rfq_compose.html", 44),  # vendors|map(id)|list|tojson -> [1,2,3]
    ("app/templates/requisitions2/_table.html", 66),  # requisitions|map(id)|list|tojson -> [1,2,3]
}

# Alpine directive attributes: x-data, x-init, x-bind:x / :x, x-on:x / @x.
_ALPINE_ATTR_TOJSON = re.compile(r'(?:x-data|x-init|x-bind:[\w.:-]+|x-on:[\w.:-]+|@[\w.:-]+|:[\w.-]+)\s*=\s*"([^"]*)"')


def test_no_tojson_in_double_quoted_alpine_attribute():
    """|tojson emits UNESCAPED double quotes (it escapes ', <, >, & — but never ").

    Inside a DOUBLE-quoted Alpine attribute (x-data / x-init / @event / :bind / x-on:)
    the first such double quote closes the attribute early, so Alpine fails to parse the
    expression and the whole component goes inert — a bug class that has shipped
    silently more than once (e.g. the sightings "Send RFQ" modal opened blank). The fix
    is a SINGLE-quoted attribute (tojson escapes ', so single quotes are safe) or a JS
    Alpine.data() factory invoked with the tojson args. See CLAUDE.md.

    Allowlisted sites serialise only an int/bool and therefore cannot emit a double
    quote.
    """
    offenders: list[str] = []
    for path in sorted(Path("app/templates").rglob("*.html")):
        text = path.read_text()
        for m in _ALPINE_ATTR_TOJSON.finditer(text):
            if "tojson" not in m.group(1):
                continue
            line = text[: m.start()].count("\n") + 1
            rel = str(path.relative_to(Path(".")))
            if (rel, line) in _TOJSON_IN_DOUBLE_QUOTED_ALPINE_ALLOWLIST:
                continue
            offenders.append(f"{rel}:{line}: {m.group(0)[:90].strip()}")
    assert not offenders, (
        "|tojson inside a DOUBLE-quoted Alpine attribute breaks Alpine init (tojson emits "
        "unescaped double quotes). Use a SINGLE-quoted attribute or a JS Alpine.data factory:\n" + "\n".join(offenders)
    )


_HX_VALS_JS = re.compile(r"""hx-vals\s*=\s*(['"])js:(.*?)\1""", re.DOTALL)


def test_hx_vals_js_is_object_literal():
    """`hx-vals="js:..."` MUST be a plain object literal (start with `{`).

    htmx wraps any js: expression that does not start with `{` in `{...}`
    (getValuesForElement), so an IIFE or function call — `js:(function(){...})()`,
    `js:buildVals()` — becomes invalid `{(function(){...})()}`; `Function()` throws and
    htmx SILENTLY aborts the request (it never fires, the indicator hangs forever). This
    is exactly how the materials faceted list stopped loading. Inline the lookups into an
    object literal instead.
    """
    offenders: list[str] = []
    for path in sorted(Path("app/templates").rglob("*.html")):
        text = path.read_text()
        for m in _HX_VALS_JS.finditer(text):
            # Do NOT strip: htmx slices the `js:` prefix and checks indexOf('{') == 0 WITHOUT
            # re-trimming, so `js: {...}` (a space/newline after the colon) breaks too.
            body = m.group(2)
            if not body.startswith("{"):
                line = text[: m.start()].count("\n") + 1
                offenders.append(f"{path.relative_to(Path('.'))}:{line}: got {body[:30]!r}")
    assert not offenders, (
        "hx-vals='js:...' must be an OBJECT LITERAL (start with '{'). htmx wraps a non-'{' "
        "expression in {...}, turning an IIFE/function-call into invalid JS, so the request "
        "silently never fires:\n" + "\n".join(offenders)
    )


def test_dockerfile_cache_bust_precedes_source_copies():
    """deploy.sh dropped --no-cache (PR #211); template freshness now relies entirely on
    a per-deploy BUILD_COMMIT cache-bust placed BEFORE the source COPYs in each
    Dockerfile stage.

    If a future edit moves a `COPY app/...` above the bust, that layer caches on
    content alone and a deploy can SILENTLY ship stale templates (the build-tag check only
    verifies the env var, not file content). Enforce the ordering mechanically.
    """
    lines = Path("Dockerfile").read_text().splitlines()

    def first(pred) -> int:
        return next((i for i, ln in enumerate(lines) if pred(ln)), -1)

    # Builder stage: `RUN echo "$BUILD_COMMIT"` must precede the static/template COPYs.
    builder_bust = first(lambda ln: 'echo "$BUILD_COMMIT"' in ln)
    static_copy = first(lambda ln: ln.strip().startswith("COPY app/static"))
    tmpl_copy = first(lambda ln: ln.strip().startswith("COPY app/templates"))
    assert builder_bust != -1, (
        'builder-stage BUILD_COMMIT cache-bust (RUN echo "$BUILD_COMMIT") is gone — without '
        "--no-cache the Vite build can reuse a stale template layer."
    )
    assert builder_bust < static_copy, "COPY app/static must come AFTER the builder BUILD_COMMIT cache-bust"
    assert builder_bust < tmpl_copy, "COPY app/templates must come AFTER the builder BUILD_COMMIT cache-bust"

    # Stage 2: an `ARG BUILD_COMMIT` cache-bust must precede `COPY app/ app/`.
    arg_idxs = [i for i, ln in enumerate(lines) if ln.strip().startswith("ARG BUILD_COMMIT")]
    app_copy = first(lambda ln: ln.strip() == "COPY app/ app/")
    assert arg_idxs and app_copy != -1, "ARG BUILD_COMMIT / COPY app/ app/ structure changed unexpectedly"
    assert any(a < app_copy for a in arg_idxs), (
        "COPY app/ app/ must come AFTER an ARG BUILD_COMMIT cache-bust (else stage-2 app code caches stale)"
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
    js = Path("app/static/public/js/requisitions2.js").read_text()
    assert "htmx:configRequest" in js, (
        "requisitions2.js must register an htmx:configRequest listener that "
        "attaches the x-csrftoken header — page.html does not always load "
        "htmx_app.js, which carries the shared listener."
    )
    assert "x-csrftoken" in js, "requisitions2.js CSRF listener must set the x-csrftoken header"


def test_requisitions2_js_is_published_under_public():
    """requisitions2/page.html unconditionally loads /static/js/requisitions2.js, which
    carries the page-only Alpine components rq2Page and resizableTable (and the CSRF
    listener) — components NOT present in the htmx_app bundle.

    Production serves /static/* from Vite's build output, and Vite copies ONLY its
    publicDir (app/static/public/) into that tree verbatim, preserving subdirs (e.g.
    public/icons/ -> /static/icons/). The script therefore must live under
    app/static/public/js/ or it 404s through Caddy in production despite existing in the
    source tree — which is exactly what happened while it sat at app/static/js/.

    The page.html URL (/static/js/requisitions2.js) is unchanged by this location: Vite
    flattens publicDir into the served root, so public/js/requisitions2.js is served at
    /static/js/requisitions2.js.
    """
    served = Path("app/static/public/js/requisitions2.js")
    assert served.exists(), (
        "requisitions2.js must live under app/static/public/js/ so Vite publishes it to "
        "/static/js/requisitions2.js; only files under publicDir reach the served tree."
    )
    # It must NOT linger at the old, unpublished top-level location.
    assert not Path("app/static/js/requisitions2.js").exists(), (
        "Stale copy at app/static/js/requisitions2.js — that path is never published to "
        "the served static tree. Keep a single copy under app/static/public/js/."
    )


def test_materials_fold_state_defaults_pinned():
    """The materials rail collapse policy is a set of JS defaults, not template markup:
    the Data-confidence (trust) fold opens by default; the heavy folds (sourcing / more
    attributes) stay closed. The template tests only pin that the folds exist and their
    order — without this guard a fold-state refactor could flip the booleans back with
    zero test failure, silently undoing the expanded-by-default trust filter.

    The confidence key is the ROTATED 'mat_confidence_open2': @alpinejs/persist writes
    the CURRENT value to storage on init, so every browser that loaded the page under
    the old `persistOr(false, 'mat_confidence_open')` carries a persisted `false` that
    would override a new `true` default on the same key. htmx_app.js must keep removing
    the legacy key so a revert can't resurrect those stale values.
    """
    src = Path("app/static/htmx_app.js").read_text()
    assert re.search(r"confidenceOpen:\s*persistOr\(true,\s*'mat_confidence_open2'\)", src), (
        "Data-confidence fold must default OPEN under the rotated key "
        "'mat_confidence_open2' — trust is the headline filter, expanded by default."
    )
    assert re.search(r"sourcingOpen:\s*persistOr\(false,\s*'mat_sourcing_open'\)", src), (
        "Sourcing-signals fold must default CLOSED per the collapse policy."
    )
    assert re.search(r"moreAttrsOpen:\s*persistOr\(false,\s*'mat_more_attrs_open'\)", src), (
        "More-attributes fold must default CLOSED per the collapse policy."
    )
    assert "localStorage.removeItem('mat_confidence_open')" in src, (
        "htmx_app.js must drop the legacy 'mat_confidence_open' localStorage key — "
        "prior visitors carry a persisted `false` under it that would re-collapse the "
        "trust fold if the key were ever reused."
    )


# ─────────────────────────────────────────────────────────────────────────
# Design-system consistency guards
#
# Lock the drift the front-end UX pass consolidated onto the canonical
# component layer (app/static/styles.css `@layer components` +
# shared/_macros.html): badge/button/card/table spacing, radius, shadow, type
# scale, focus ring, and the unified-border !important hack.
#
# High-volume drift is enforced as a COUNT RATCHET — green today, and it can
# only SHRINK as the per-page sweeps drain it. When a sweep drains its slice,
# LOWER the matching BASELINE; never raise one. Structural facts (the classes
# exist, the modal uses the canonical close) are asserted directly.
# ─────────────────────────────────────────────────────────────────────────

_TEMPLATES = sorted(Path("app/templates").rglob("*.html"))


def _tpl_substring_count(needle: str) -> int:
    return sum(p.read_text().count(needle) for p in _TEMPLATES)


def _tpl_regex_count(pattern: str, exclude: set[str] | None = None) -> int:
    rx = re.compile(pattern)
    skip = exclude or set()
    return sum(len(rx.findall(p.read_text())) for p in _TEMPLATES if p.name not in skip)


def test_design_system_classes_defined():
    """The canonical component layer must exist in styles.css.

    The macros and every page sweep depend on these classes; if a refactor drops one,
    dozens of templates silently lose their styling.
    """
    css = Path("app/static/styles.css").read_text()
    required = [
        ".btn",
        ".btn-primary",
        ".btn-secondary",
        ".btn-danger",
        ".btn-ghost",
        ".btn-sm",
        ".btn-md",
        ".btn-lg",
        ".badge",
        ".chip",
        ".badge-mini",
        ".card",
        ".card-sm",
        ".card-lg",
        ".input",
        ".input-focus",
        ".modal-header",
        ".modal-body",
        ".modal-footer",
        ".modal-close",
        ".h1",
        ".h2",
        ".h3",
        ".h4",
        ".form-label",
        ".text-secondary",
        ".text-tertiary",
        ".table-cell",
        ".compact-cell",
        ".border-line-base",
        ".border-line-subtle",
    ]
    missing = [sel for sel in required if not re.search(r"(?m)^\s*" + re.escape(sel) + r"[\s:{]", css)]
    assert not missing, f"canonical component classes missing from styles.css: {missing}"


def test_styles_important_count_capped():
    """The `.border-gray-* !important` unified-border hack (2 of the 6) is the
    conversion target for the border-gray-* → border-line-* sweep; the rest are
    deliberate animation/display resets.

    Cap the count so no NEW !important is added and the hack can't quietly return after
    removal. Comments are stripped so prose mentions of the word don't count.
    """
    css = re.sub(r"/\*.*?\*/", "", Path("app/static/styles.css").read_text(), flags=re.S)
    count = css.count("!important")
    assert count <= 6, (
        f"styles.css has {count} !important declarations (cap 6). Don't add "
        f"!important — use a component class or specificity."
    )


def test_tiny_text_does_not_grow():
    """Text-[10px] is below the readability floor (text-[11px]); the mobile-nav labels
    are the one intentional exception.

    Ratchet — sweeps lower this as they bump 10px → text-xs / text-[11px].
    """
    BASELINE = 56
    count = _tpl_substring_count("text-[10px]")
    assert count <= BASELINE, (
        f"text-[10px] usages rose to {count} (baseline {BASELINE}). Use text-xs "
        f"or text-[11px]; 10px is below the readability floor."
    )


def test_low_contrast_secondary_text_does_not_grow():
    """Secondary/body copy should use text-gray-600 (or .text-secondary), not the lower-
    contrast text-gray-500.

    Ratchet only (decorative icon grays are fine) — caps growth rather than banning
    outright.
    """
    BASELINE = 409
    count = _tpl_substring_count("text-gray-500")
    assert count <= BASELINE, (
        f"text-gray-500 usages rose to {count} (baseline {BASELINE}). Prefer "
        f"text-gray-600 / .text-secondary for readable secondary text."
    )


def test_focus_ring_1_does_not_grow():
    """One focus-ring spec: ring-2 (see .input / .btn). Ratchet down the legacy
    ring-1 usages; never add a new one."""
    BASELINE = 65
    count = _tpl_substring_count("focus:ring-1")
    assert count <= BASELINE, (
        f"focus:ring-1 usages rose to {count} (baseline {BASELINE}). Use the "
        f".input / .input-focus / .btn focus spec (ring-2)."
    )


def test_inline_table_cell_padding_does_not_grow():
    """Table cells should use the locked .table-cell / .compact-cell utilities, not
    inline px-/py- (which is how cell padding drifted).

    Ratchet.
    """
    BASELINE = 523
    count = _tpl_regex_count(r'<t[dh][^>]*class="[^"]*\bp[xy]-[0-9]')
    assert count <= BASELINE, (
        f"inline-padded <td>/<th> rose to {count} (baseline {BASELINE}). Use "
        f".table-cell / .compact-cell* instead of inline px-/py- on cells."
    )


def test_inline_button_sizing_does_not_grow():
    """Buttons should size via .btn-sm/md/lg (or the btn_* macros), not inline px-/py-.

    Macro files are the canonical source and are excluded. Ratchet.
    """
    BASELINE = 269
    count = _tpl_regex_count(r'<button[^>]*class="[^"]*\bp[xy]-[0-9]', exclude={"_macros.html"})
    assert count <= BASELINE, (
        f"inline-sized <button> rose to {count} (baseline {BASELINE}). Use .btn-sm/md/lg or the btn_* macros."
    )


def test_modal_uses_canonical_close_class():
    """The global modal close button uses the .modal-close component (not the old
    top-2.5 right-2.5 magic numbers), so every modal's close affordance is positioned
    consistently."""
    html = Path("app/templates/htmx/base.html").read_text()
    assert 'class="modal-close"' in html, "global modal close button must use .modal-close"
    assert "top-2.5 right-2.5" not in html, "modal close button still uses magic-number positioning — use .modal-close"
