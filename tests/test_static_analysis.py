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
        ("app/templates/htmx/base.html", 57),
        ("app/templates/htmx/partials/parts/cell_edit.html", 12),
        ("app/templates/htmx/partials/parts/cell_edit.html", 26),
        ("app/templates/htmx/partials/parts/cell_edit.html", 37),
        ("app/templates/htmx/partials/parts/workspace.html", 114),
        ("app/templates/htmx/partials/parts/workspace.html", 120),
        ("app/templates/htmx/partials/parts/list.html", 11),
        ("app/templates/htmx/partials/parts/list.html", 12),
        ("app/templates/htmx/partials/parts/list.html", 170),
        ("app/templates/htmx/partials/parts/list.html", 387),
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


def test_hx_vals_js_has_no_alpine_magics():
    """`hx-vals="js:..."` runs in htmx's eval context (event + `this` = element), NOT
    Alpine's — so Alpine magics like `$el`, `$store`, `$refs`, `$data` are UNDEFINED
    there and throw, silently aborting the request.

    Regression (SET-01): the system-settings toggles used `$el.checked`, so every
    toggle no-op'd. Use `event.target` / `this` instead.

    `$store` is included: the last remaining `$store` use in `hx-vals="js:..."` (the
    sightings batch-refresh button, SIGHT-BATCH) was converted to a form with an Alpine
    `:value` bind, so no `hx-vals js:` may reference `$store` (undefined in htmx eval).
    """
    magic = re.compile(r"\$(el|store|refs|data|dispatch|nextTick|watch)\b")
    offenders: list[str] = []
    for path in sorted(Path("app/templates").rglob("*.html")):
        text = path.read_text()
        for m in _HX_VALS_JS.finditer(text):
            hit = magic.search(m.group(2))
            if hit:
                line = text[: m.start()].count("\n") + 1
                offenders.append(f"{path.relative_to(Path('.'))}:{line}: Alpine {hit.group(0)} in hx-vals js:")
    assert not offenders, (
        "hx-vals='js:...' must not reference Alpine magics (undefined in htmx eval → "
        "request silently aborts). Use event.target / this:\n" + "\n".join(offenders)
    )


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


_HX_VERB_ATTR = re.compile(r"\bhx-(?:post|get|put|delete)\s*=", re.IGNORECASE)
_TEMPLATE_TAG = re.compile(r"<template\b([^>]*)>|</template>", re.IGNORECASE)


def _blank_template_comments(text: str) -> str:
    """Blank Jinja {# #} and HTML <!-- --> comment bodies (newlines preserved) so prose
    that MENTIONS `<template x-if>` or an hx-verb — e.g. the explanatory comments the
    BP-1 fix itself adds — is not parsed as live markup."""

    def repl(m: "re.Match[str]") -> str:
        return re.sub(r"[^\n]", " ", m.group(0))

    text = re.sub(r"\{#.*?#\}", repl, text, flags=re.DOTALL)
    return re.sub(r"<!--.*?-->", repl, text, flags=re.DOTALL)


def _hx_verb_in_xif_offenders(raw: str) -> list[int]:
    """Return the line numbers of every hx-(post|get|put|delete) attribute that sits
    INSIDE a ``<template x-if=...>...</template>`` block (nesting-aware)."""
    text = _blank_template_comments(raw)
    # Build the character spans covered by an x-if <template> (including nested templates).
    stack: list[tuple[int, bool]] = []
    xif_spans: list[tuple[int, int]] = []
    for m in _TEMPLATE_TAG.finditer(text):
        if m.group(0).lower().startswith("</template"):
            if stack:
                open_end, is_xif = stack.pop()
                if is_xif:
                    xif_spans.append((open_end, m.start()))
        else:
            is_xif = bool(re.search(r"\bx-if\b", m.group(1)))
            stack.append((m.end(), is_xif))
    offenders: list[int] = []
    for hm in _HX_VERB_ATTR.finditer(text):
        pos = hm.start()
        if any(start <= pos < end for start, end in xif_spans):
            offenders.append(text[:pos].count("\n") + 1)
    return offenders


def test_no_hx_verb_inside_template_x_if():
    """BP-1 defect class: an ``hx-post/hx-get/hx-put/hx-delete`` on an element INSIDE a
    ``<template x-if=...>...</template>`` block is DEAD.

    htmx 2.x processes nodes only at load / after-swap; a ``<template>``'s content is an
    inert DocumentFragment, and Alpine's x-if clone is inserted WITHOUT being handed to
    ``htmx.process`` — so the hx-* attribute is never wired and the element fires zero
    requests (a buy-plan confirm button that silently did nothing; an inline-edit form whose
    Save was dead). The fix is either an imperative ``htmx.ajax(...)`` in ``@click`` (read at
    click time) or switching ``<template x-if>`` to a plain ``<div x-show>`` (which keeps the
    element in the server-rendered DOM so htmx processes it at swap time).

    Allowlist NOTHING — a hit is a genuinely dead control. Convert the call site.
    """
    offenders: list[str] = []
    for path in sorted(Path("app/templates").rglob("*.html")):
        for line in _hx_verb_in_xif_offenders(path.read_text()):
            offenders.append(f"{path.relative_to(Path('.'))}:{line}")
    assert not offenders, (
        "hx-post/get/put/delete found on an element INSIDE a <template x-if> (htmx never "
        "processes template-fragment content, so the control is DEAD). Use an imperative "
        "htmx.ajax(...) @click, or switch the wrapper to <div x-show>:\n" + "\n".join(offenders)
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
    BASELINE = 33  # tightened to current count after the UI accent/readability program (was 58)
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
    BASELINE = 399  # tightened to current count after the UI accent/readability program (was 427)
    count = _tpl_substring_count("text-gray-500")
    assert count <= BASELINE, (
        f"text-gray-500 usages rose to {count} (baseline {BASELINE}). Prefer "
        f"text-gray-600 / .text-secondary for readable secondary text."
    )


def test_focus_ring_1_does_not_grow():
    """One focus-ring spec: ring-2 (see .input / .btn). Ratchet down the legacy
    ring-1 usages; never add a new one."""
    BASELINE = 55  # tightened to current count after the UI program (was 66)
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
    BASELINE = 417  # tightened to current count after the UI program (was 527)
    count = _tpl_regex_count(r'<t[dh][^>]*class="[^"]*\bp[xy]-[0-9]')
    assert count <= BASELINE, (
        f"inline-padded <td>/<th> rose to {count} (baseline {BASELINE}). Use "
        f".table-cell / .compact-cell* instead of inline px-/py- on cells."
    )


def test_inline_button_sizing_does_not_grow():
    """Buttons should size via .btn-sm/md/lg (or the btn_* macros), not inline px-/py-.

    Macro files are the canonical source and are excluded. Ratchet.
    """
    BASELINE = 226  # Approvals rework Phase F-2 retired the old lens templates (_board/_supervise/_tab_*), draining their inline-padded scope/filter pills; was 229 (+1 Pipeline scope-toggle pill), 228 (+2 My Queue filter-chips), 224 after the UI program (was 280)
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


# Page-shell width policy:
# dense data pages fill the viewport via .page-fluid; reading/form pages keep a comfortable
# ~1152px measure via .page-readable. The shell wrapper must carry the semantic class rather
# than an ad-hoc `max-w-*xl mx-auto` cap (which leaves empty gutters on wide monitors).
_PAGE_FLUID_SHELLS = (
    "app/templates/htmx/partials/admin/spec_codes_pending.html",
    "app/templates/htmx/partials/buy_plans/detail.html",
    "app/templates/htmx/partials/buy_plans/hub.html",
    "app/templates/htmx/partials/dashboard.html",
    "app/templates/htmx/partials/emails/intelligence_dashboard.html",
    "app/templates/htmx/partials/follow_ups/list.html",
    "app/templates/htmx/partials/materials/detail.html",
    "app/templates/htmx/partials/proactive/list.html",
    "app/templates/htmx/partials/prospecting/list.html",
    "app/templates/htmx/partials/quotes/detail.html",
    "app/templates/htmx/partials/requisitions/detail.html",
    "app/templates/htmx/partials/requisitions/list.html",
    "app/templates/htmx/partials/search/full_results.html",
    "app/templates/htmx/partials/settings/index.html",
    "app/templates/htmx/partials/tickets/workspace.html",
    "app/templates/htmx/partials/vendors/detail.html",
    "app/templates/htmx/partials/vendors/list.html",
    "app/templates/htmx/partials/offers/review_queue.html",
    # CRM account detail — full-width contacts-forward layout (twin of vendors/detail);
    # renders standalone in #main-content or inside the CDM workspace right panel.
    "app/templates/htmx/partials/customers/detail.html",
)
_PAGE_READABLE_SHELLS = (
    "app/templates/htmx/partials/admin/data_ops.html",
    "app/templates/htmx/partials/knowledge/list.html",
    "app/templates/htmx/partials/proactive/prepare.html",
    "app/templates/htmx/partials/prospecting/detail.html",
    "app/templates/htmx/partials/search/dossier_shell.html",
    "app/templates/htmx/partials/sourcing/lead_detail.html",
    "app/templates/htmx/partials/tickets/detail.html",
)


def test_width_classes_defined_in_styles():
    """The semantic page-width classes are the single source of truth for horizontal
    space usage — they must exist in the Tailwind component layer."""
    css = Path("app/static/styles.css").read_text()
    assert ".page-fluid" in css, ".page-fluid must be defined in styles.css"
    assert ".page-readable" in css, ".page-readable must be defined in styles.css"


def test_page_shells_use_width_classes():
    """Every classified page-shell carries its semantic width class.

    Reverting a shell to an ad-hoc `max-w-*xl mx-auto` cap (re-introducing wide-monitor
    gutters) removes the class and trips this guard.
    """
    offenders = []
    for rel in _PAGE_FLUID_SHELLS:
        if "page-fluid" not in Path(rel).read_text():
            offenders.append(f"{rel}: missing .page-fluid on shell wrapper")
    for rel in _PAGE_READABLE_SHELLS:
        if "page-readable" not in Path(rel).read_text():
            offenders.append(f"{rel}: missing .page-readable on shell wrapper")
    assert not offenders, "page-shells lost their width class:\n" + "\n".join(offenders)


def test_nav_poll_badges_optout_of_push_url():
    """Bottom-nav badges poll (hx-trigger="...every...") and are nested inside the nav
    <a> elements, which carry hx-push-url="{{ href }}".

    htmx makes hx-push-url INHERITABLE, so without an opt-out each badge poll pushes its
    parent nav item's URL to the address bar — silently rewriting the URL on load and
    every 60s, so refresh/bookmark/back land on the wrong page. Each polling badge must
    set hx-push-url="false".
    """
    html = Path("app/templates/htmx/partials/shared/mobile_nav.html").read_text()
    # The inheritance hazard exists only because the nav <a> pushes a URL.
    assert 'hx-push-url="{{ href }}"' in html, "nav <a> hx-push-url contract changed — revisit this guard"
    badges = re.findall(r"<span[^>]*hx-get=\"[^\"]*/badge\"[^>]*>", html, re.DOTALL)
    assert badges, "expected bottom-nav badge spans with hx-get to a /badge endpoint"
    offenders = [b[:100] for b in badges if 'hx-push-url="false"' not in b]
    assert not offenders, (
        "bottom-nav badge poll spans inherit the nav <a>'s hx-push-url and rewrite the "
        'address bar; add hx-push-url="false":\n' + "\n".join(offenders)
    )


def _func_source(path: str, func_name: str) -> str:
    """Return the source text of a top-level function by name (AST, line-shift
    proof)."""
    import ast

    src = Path(path).read_text()
    tree = ast.parse(src)
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func_name:
            return ast.get_source_segment(src, node)
    raise AssertionError(f"{func_name} not found in {path}")


def test_delete_vendor_cards_does_not_null_notnull_cascade_children():
    """REGRESSION (CRITICAL, structural): the SQLite test DB ignores NOT-NULL-on-UPDATE,
    so a functional test alone can't prove the bug stays fixed.

    Lock it in structurally: the
    four NOT-NULL ondelete=CASCADE children of a vendor card must NOT be enumerated in
    ``delete_vendor_cards``' detach/NULL list — NULLing them raises NotNullViolation on
    Postgres. They cascade-delete with the card via ``db.delete(card)``.
    """
    body = _func_source("app/services/vendor_merge_service.py", "delete_vendor_cards")
    # A NOT-NULL child enumerated as a real detach tuple — (Model, "vendor_card_id") — is
    # the bug. Match that exact tuple syntax so prose/comment mentions of the model names
    # (which explain WHY they're excluded) don't false-trip the guard.
    notnull_children = ["VendorContact", "VendorReview", "VendorMetricsSnapshot", "BuyerVendorStats"]
    offenders = [m for m in notnull_children if f'({m}, "vendor_card_id")' in body]
    assert not offenders, (
        "delete_vendor_cards lists NOT-NULL ondelete=CASCADE children in its detach/NULL "
        f"loop — NULLing these raises NotNullViolation on Postgres: {offenders}. "
        "Remove them; db.delete(card) cascade-deletes them."
    )
    # And the cascade path itself must still be present (both cards explicitly deleted).
    assert "db.delete(card_a)" in body and "db.delete(card_b)" in body, (
        "delete_vendor_cards must db.delete() both cards so the DB ON DELETE CASCADE removes the NOT-NULL children."
    )


def test_dedup_services_fail_closed_on_detach_error():
    """REGRESSION (MEDIUM): both delete-both services must FAIL CLOSED — a detach/purge
    error re-raises so the route rolls back, never deleting the parents and silently
    orphaning/losing dependent rows.

    Guard: every ``except`` in the detach/purge loops
    re-raises (no fail-open ``logger.warning`` + fall-through).
    """
    for path, func in (
        ("app/services/company_merge_service.py", "delete_companies"),
        ("app/services/vendor_merge_service.py", "delete_vendor_cards"),
    ):
        body = _func_source(path, func)
        # The detach/purge region precedes the parent db.delete().
        region = re.split(r"db\.delete\(\w+_a\)", body)[0]
        # Every except in that region must re-raise. A bare logger.warning with no raise is
        # the fail-open data-loss path we removed.
        for m in re.finditer(r"except Exception as e:(.*?)(?=\n {0,8}\w|\Z)", region, re.DOTALL):
            block = m.group(1)
            assert "raise" in block, f"{func}: detach/purge except block fails open (no re-raise):\n{block}"


def test_resizable_modal_review_fixes_locked_in():
    """REGRESSION (#461 adversarial-review follow-ups): lock in the five resizable-modal
    polish fixes at the source level so a future refactor can't silently revert them.

    Runtime behavior for (b) re-clamp and (c) pointercancel teardown is exercised
    against the shipped Alpine factory in tests/frontend/resizable-modal.test.ts; the
    MIN_H/MIN_W floor (d) in tests/frontend/modal-geometry.test.ts. This test guards the
    template/CSS/JS-source invariants those behaviors depend on (and adds the only
    coverage for the purely-presentational fixes (a) the visible grip and (e) the shrunk
    picker).

    (a) VISIBLE drag grip — a real, painted handle, not the old transparent overlay
    strip. (b) Re-clamp a floating panel on window resize (listener added AND torn
    down). (c) Tear down the drag on pointercancel (not only pointerup) — no stuck-drag
    state. (d) Min-height raised off the sliver value (240 -> 400) so a modal stays
    usable. (e) New-Requisition customer-picker dropdown shrunk to max-h-40.
    """
    base = Path("app/templates/htmx/base.html").read_text()
    css = Path("app/static/styles.css").read_text()
    js = Path("app/static/htmx_app.js").read_text()
    geom = Path("app/static/modal_geometry.js").read_text()
    unified = Path("app/templates/htmx/partials/requisitions/unified_modal.html").read_text()

    # (a) Visible grip: markup wired to startMove with a painted handle child, and the
    # handle has a real (non-transparent) background — i.e. NOT an invisible drag strip.
    assert 'class="modal-grip"' in base and "startMove($event)" in base, (
        "base.html lost the drag-to-move grip wired to startMove()."
    )
    assert "modal-grip-handle" in base, "base.html grip lost its visible handle child."
    handle = re.search(r"\.modal-grip-handle\s*\{([^}]*)\}", css)
    assert handle, ".modal-grip-handle CSS rule is missing — the grip would be invisible."
    handle_body = handle.group(1)
    assert (
        "background:" in handle_body and "transparent" not in handle_body and "background: none" not in handle_body
    ), (
        "the grip handle must paint a visible background so users can see the drag "
        "affordance — it must not revert to an invisible/transparent strip."
    )

    # (b) Window-resize re-clamp: a 'resize' listener is added AND removed (no leak), and
    # the handler clamps the panel back onto the viewport.
    assert "addEventListener('resize'" in js and "removeEventListener('resize'" in js, (
        "resizableModal must add AND remove a window 'resize' listener (re-clamp + no leak)."
    )
    assert "clampToViewport(" in js, "the resize handler must clampToViewport() the panel."

    # (c) pointercancel teardown: bound and removed alongside pointerup.
    assert "addEventListener('pointercancel'" in js and "removeEventListener('pointercancel'" in js, (
        "drag must tear down on pointercancel (not only pointerup) to avoid stuck-drag state."
    )

    # (d) Raised min-height: can't collapse to a sliver.
    min_h = re.search(r"MIN_H\s*=\s*(\d+)", geom)
    assert min_h and int(min_h.group(1)) >= 360, (
        "MIN_H must stay raised (>=360) so a modal can't be shrunk to an unusable sliver; "
        f"got {min_h.group(1) if min_h else 'missing'}."
    )

    # (e) Customer-picker dropdown shrunk: the picker's absolute dropdown (the only one in
    # this template using `left-0 right-0`) must cap at max-h-40, not a taller max-h-48/56/...
    picker_dropdown = next(
        (ln for ln in unified.splitlines() if "left-0 right-0" in ln and "max-h-" in ln),
        None,
    )
    assert picker_dropdown is not None, "customer-picker dropdown row not found in unified_modal.html."
    assert "max-h-40" in picker_dropdown, (
        f"customer-picker dropdown must stay shrunk at max-h-40; got: {picker_dropdown.strip()!r}"
    )
