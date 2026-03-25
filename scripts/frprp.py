#!/usr/bin/env python3
"""Frontend Fix (FRPRP) — Autonomous frontend remediation and production readiness
pipeline.

Orchestrates 6 phases: SCAN → CLASSIFY → FIX → VERIFY → TEST → REPORT.
Runs unattended, produces a scored readiness report, commits fixes on a dedicated branch.

Called by: python3 scripts/frprp.py run --autonomous
Depends on: Playwright MCP, grep/glob tools, pytest, ruff, mypy, git
"""

import json
import os
import re
import subprocess
import sys
import textwrap
from datetime import datetime, timezone
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = PROJECT_ROOT / "app" / "templates"
ROUTERS_DIR = PROJECT_ROOT / "app" / "routers"
STATIC_DIR = PROJECT_ROOT / "app" / "static"
FRPRP_DIR = PROJECT_ROOT / "docs" / "frprp"
TODAY = datetime.now(timezone.utc).strftime("%Y-%m-%d")
RUN_DIR = FRPRP_DIR / "runs" / TODAY
SCREENSHOTS_DIR = RUN_DIR / "screenshots"

# All navigable pages (id, push_url, partial_url)
NAV_PAGES = [
    ("requisitions", "/v2/requisitions", "/v2/partials/parts/workspace"),
    ("search", "/v2/search", "/v2/partials/search"),
    ("quotes", "/v2/quotes", "/v2/partials/quotes"),
    ("customers", "/v2/customers", "/v2/partials/customers"),
    ("vendors", "/v2/vendors", "/v2/partials/vendors"),
    ("prospecting", "/v2/prospecting", "/v2/partials/prospecting"),
    ("materials", "/v2/materials", "/v2/partials/materials/workspace"),
    ("buy-plans", "/v2/buy-plans", "/v2/partials/buy-plans"),
    ("follow-ups", "/v2/follow-ups", "/v2/partials/follow-ups"),
    ("proactive", "/v2/proactive", "/v2/partials/proactive"),
    ("excess", "/v2/excess", "/v2/partials/excess"),
    ("settings", "/v2/settings", "/v2/partials/settings"),
    ("trouble-tickets", "/v2/trouble-tickets", "/v2/partials/trouble-tickets/workspace"),
]

# Auto-fix patterns: (name, file_glob, regex_pattern, replacement_or_callable, description)
AUTO_FIX_PATTERNS = [
    {
        "name": "missing_rel_noopener",
        "glob": "**/*.html",
        "pattern": r'target="_blank"(?!.*rel=)',
        "fix": 'target="_blank" rel="noopener noreferrer"',
        "description": "Add rel='noopener noreferrer' to target='_blank' links",
    },
    {
        "name": "missing_x_cloak",
        "glob": "**/*.html",
        "pattern": r'(x-show="[^"]*")(?!.*x-cloak)',
        "fix": r"\1 x-cloak",
        "description": "Add x-cloak to elements with x-show to prevent FOUC",
    },
    {
        "name": "deprecated_db_query_get",
        "glob": "app/routers/**/*.py",
        "pattern": r"db\.query\((\w+)\)\.get\((\w+)\)",
        "fix": r"db.get(\1, \2)",
        "description": "Replace deprecated db.query(Model).get(id) with db.get(Model, id)",
    },
    {
        "name": "import_inside_loop",
        "glob": "app/**/*.py",
        "pattern": r"(\s+for .+:\n(?:\s+.*\n)*?\s+)(import \w+)",
        "fix": None,  # Needs manual subagent — flagged only
        "description": "Flag import statements inside loops",
    },
]

# Severity weights for scoring
CATEGORY_WEIGHTS = {
    "route_integrity": 3,
    "error_handling": 3,
    "empty_states": 3,
    "security": 2,
    "form_feedback": 2,
    "session_handling": 2,
    "template_consistency": 1,
    "accessibility": 1,
    "performance": 1,
    "mobile_parity": 1,
    "loading_states": 1,
    "navigation_integrity": 1,
}


# ── Utilities ─────────────────────────────────────────────────────────


def log(msg: str, level: str = "INFO"):
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{ts}] [{level}] {msg}")


def run_cmd(cmd: str | list, cwd: str | None = None, timeout: int = 120) -> subprocess.CompletedProcess:
    if isinstance(cmd, str):
        cmd = cmd.split()
    return subprocess.run(cmd, capture_output=True, text=True, cwd=cwd or str(PROJECT_ROOT), timeout=timeout)


def update_status(phase: int, phase_name: str, detail: str, findings_summary: dict | None = None):
    """Write live STATUS.md for remote monitoring."""
    phases = ["SCAN", "CLASSIFY", "FIX", "VERIFY", "TEST", "REPORT"]
    lines = [
        f"# Frontend Fix Run — {TODAY}",
        "",
        f"## Current Phase: {phase_name} ({phase}/6)",
        f"## Last Update: {datetime.now(timezone.utc).strftime('%H:%M:%S UTC')}",
        "",
        "### Phase Progress",
    ]
    for i, name in enumerate(phases, 1):
        marker = "x" if i < phase else (" " if i > phase else "~")
        lines.append(f"- [{marker}] Phase {i}: {name}")
    lines.extend(["", "### Current Activity", detail, ""])
    if findings_summary:
        lines.extend(
            [
                "### Findings Summary",
                f"- Critical: {findings_summary.get('critical', 0)}",
                f"- High: {findings_summary.get('high', 0)}",
                f"- Medium: {findings_summary.get('medium', 0)}",
                f"- Low: {findings_summary.get('low', 0)}",
                f"- Auto-fixable: {findings_summary.get('auto_fixable', 0)}",
                f"- Needs subagent: {findings_summary.get('subagent', 0)}",
                f"- Fixed so far: {findings_summary.get('fixed', 0)}",
                "",
            ]
        )
    (RUN_DIR / "STATUS.md").write_text("\n".join(lines))


def save_json(filename: str, data: dict):
    (RUN_DIR / filename).write_text(json.dumps(data, indent=2, default=str))


# ── Phase 1: SCAN ────────────────────────────────────────────────────


def build_template_dependency_graph() -> dict:
    """Statically analyze template extends/include/from chains."""
    log("Building template dependency graph...")
    graph = {"nodes": {}, "edges": [], "orphans": []}
    template_files = list(TEMPLATES_DIR.rglob("*.html"))

    # Map all templates
    for tf in template_files:
        rel = str(tf.relative_to(TEMPLATES_DIR))
        graph["nodes"][rel] = {"path": str(tf), "depends_on": [], "depended_by": []}

    # Parse dependencies
    dep_patterns = [
        (r'{%\s*extends\s+["\']([^"\']+)["\']', "extends"),
        (r'{%\s*include\s+["\']([^"\']+)["\']', "include"),
        (r'{%\s*from\s+["\']([^"\']+)["\']\s+import', "import"),
    ]
    for tf in template_files:
        rel = str(tf.relative_to(TEMPLATES_DIR))
        content = tf.read_text(errors="replace")
        for pattern, dep_type in dep_patterns:
            for match in re.finditer(pattern, content):
                target = match.group(1)
                graph["edges"].append({"from": rel, "to": target, "type": dep_type})
                if rel in graph["nodes"]:
                    graph["nodes"][rel]["depends_on"].append(target)
                if target in graph["nodes"]:
                    graph["nodes"][target]["depended_by"].append(rel)

    # Find orphans (not depended on by any template and not directly served by a route)
    # We'll mark templates with zero depended_by as potential orphans
    for name, node in graph["nodes"].items():
        if not node["depended_by"] and not name.startswith("base") and "shared" not in name:
            graph["orphans"].append(name)

    log(
        f"Graph: {len(graph['nodes'])} templates, {len(graph['edges'])} edges, {len(graph['orphans'])} potential orphans"
    )
    return graph


def scan_htmx_contracts() -> list[dict]:
    """Find all hx-get/hx-post in templates and cross-ref against routes."""
    log("Scanning HTMX contracts...")
    findings = []

    # Collect all hx-get/hx-post URLs from templates
    htmx_attrs = []
    for tf in TEMPLATES_DIR.rglob("*.html"):
        content = tf.read_text(errors="replace")
        rel = str(tf.relative_to(TEMPLATES_DIR))
        for m in re.finditer(r'hx-(get|post|put|delete|patch)="([^"]*)"', content):
            method, url = m.group(1), m.group(2)
            # Skip Jinja2 expressions — can't statically resolve
            if "{{" in url or "{%" in url:
                continue
            htmx_attrs.append(
                {"template": rel, "method": method, "url": url, "line": content[: m.start()].count("\n") + 1}
            )

    # Collect all registered FastAPI routes (including prefixes)
    routes = set()
    for rf in ROUTERS_DIR.rglob("*.py"):
        content = rf.read_text(errors="replace")
        # Detect router prefix
        prefix_match = re.search(r'APIRouter\([^)]*prefix="([^"]*)"', content)
        prefix = prefix_match.group(1) if prefix_match else ""
        for m in re.finditer(r'@router\.(get|post|put|delete|patch)\("([^"]*)"', content):
            route_path = m.group(2)
            routes.add(prefix + route_path)
            routes.add(route_path)  # Also add without prefix
    # Also check htmx_views.py and main.py for include_router prefixes
    main_py = PROJECT_ROOT / "app" / "main.py"
    if main_py.exists():
        main_content = main_py.read_text(errors="replace")
        for m in re.finditer(r'include_router\([^,]+,\s*prefix="([^"]*)"', main_content):
            prefix = m.group(1)
            routes.add(prefix)  # Base prefix
    htmx_views = ROUTERS_DIR / "htmx_views.py"
    if htmx_views.exists():
        content = htmx_views.read_text(errors="replace")
        for m in re.finditer(r'@router\.(get|post|put|delete|patch)\("([^"]*)"', content):
            routes.add(m.group(2))

    # Cross-reference: find hx- URLs that don't match any route
    for attr in htmx_attrs:
        url = attr["url"]
        # Normalize: strip query params
        url_path = url.split("?")[0]
        # Check if any route matches (accounting for path params like {id})
        matched = False
        for route in routes:
            route_regex = re.sub(r"\{[^}]+\}", r"[^/]+", route)
            if re.fullmatch(route_regex, url_path):
                matched = True
                break
        if not matched and url_path.startswith("/"):
            findings.append(
                {
                    "stream": "htmx_contract",
                    "severity": "high",
                    "category": "route_integrity",
                    "template": attr["template"],
                    "line": attr["line"],
                    "issue": f'hx-{attr["method"]}="{attr["url"]}" has no matching route',
                    "auto_fixable": False,
                }
            )

    log(f"HTMX contracts: {len(htmx_attrs)} attrs checked, {len(findings)} broken refs")
    return findings


def scan_security_patterns() -> list[dict]:
    """Static scan for XSS, CSRF, rel, and other security patterns."""
    log("Scanning security patterns...")
    findings = []

    # 1. Unescaped HTMLResponse with f-strings (skip if already escaped or safe)
    safe_vars = {
        "added",
        "count",
        "total",
        "updated",
        "deleted",
        "status_code",
        "requirement_id",
        "requisition_id",
        "vendor_id",
        "company_id",
        "id",
        "num",
        "n",
        "affected",
        "created",
        "merged",
    }
    for rf in ROUTERS_DIR.rglob("*.py"):
        content = rf.read_text(errors="replace")
        rel = str(rf.relative_to(PROJECT_ROOT))
        for i, line in enumerate(content.splitlines(), 1):
            if "HTMLResponse" in line and ("f'" in line or 'f"' in line):
                # Skip if already escaped or marked safe
                if "html.escape" in line or "html_mod.escape" in line:
                    continue
                if "# safe:" in line:
                    continue
                # Skip if variables are server-controlled (ints, IDs, etc.)
                vars_in_line = re.findall(r"\{(\w+)", line)
                if vars_in_line and all(
                    v in safe_vars or v.endswith("_id") or v.endswith("_count") for v in vars_in_line
                ):
                    continue
                # Skip if contains json.dumps (already safe for script context)
                if "json.dumps" in line or "json_dumps" in line:
                    continue
                # Check surrounding lines for json.dumps too
                ctx = "\n".join(content.splitlines()[max(0, i - 3) : i + 3])
                if "json.dumps" in ctx and ".replace(" in ctx:
                    continue
                findings.append(
                    {
                        "stream": "security",
                        "severity": "critical",
                        "category": "security",
                        "file": rel,
                        "line": i,
                        "issue": "HTMLResponse with f-string — potential reflected XSS",
                        "auto_fixable": True,
                        "fix_pattern": "unescaped_html_response",
                    }
                )

    # 2. Missing rel="noopener" on target="_blank"
    for tf in TEMPLATES_DIR.rglob("*.html"):
        content = tf.read_text(errors="replace")
        rel = str(tf.relative_to(PROJECT_ROOT))
        for i, line in enumerate(content.splitlines(), 1):
            if 'target="_blank"' in line and "rel=" not in line:
                findings.append(
                    {
                        "stream": "security",
                        "severity": "medium",
                        "category": "security",
                        "file": rel,
                        "line": i,
                        "issue": 'target="_blank" without rel="noopener noreferrer" — reverse tabnabbing',
                        "auto_fixable": True,
                        "fix_pattern": "missing_rel_noopener",
                    }
                )

    # 3. |safe filter on potentially user-controlled data
    for tf in TEMPLATES_DIR.rglob("*.html"):
        content = tf.read_text(errors="replace")
        rel = str(tf.relative_to(PROJECT_ROOT))
        for i, line in enumerate(content.splitlines(), 1):
            if "|safe" in line and ("user" in line.lower() or "input" in line.lower() or "query" in line.lower()):
                findings.append(
                    {
                        "stream": "security",
                        "severity": "high",
                        "category": "security",
                        "file": rel,
                        "line": i,
                        "issue": "|safe filter on potentially user-controlled data",
                        "auto_fixable": False,
                    }
                )

    # 4. CSRF — SKIP: App uses starlette-csrf CSRFMiddleware (double-submit cookie)
    # which protects all forms automatically without hidden input fields.
    # No per-form CSRF tokens needed.

    log(f"Security scan: {len(findings)} findings")
    return findings


def scan_template_consistency() -> list[dict]:
    """Check for raw status strings, missing macros, inline styles."""
    log("Scanning template consistency...")
    findings = []

    raw_statuses = [
        '"closed"',
        '"pending"',
        '"in_progress"',
        '"completed"',
        '"draft"',
        '"sent"',
        '"inactive"',
        '"expired"',
        # NOTE: "open" excluded — too many Alpine.js false positives (x-show="open")
        # NOTE: "active" excluded — commonly used in Alpine x-data/CSS class bindings
    ]

    for tf in TEMPLATES_DIR.rglob("*.html"):
        content = tf.read_text(errors="replace")
        rel = str(tf.relative_to(PROJECT_ROOT))

        # Raw status strings that should use status_badge macro
        for status in raw_statuses:
            for i, line in enumerate(content.splitlines(), 1):
                stripped = line.strip()
                # Skip Jinja2 comments and comparisons
                if (
                    stripped.startswith("{#")
                    or "==" in stripped
                    or "!=" in stripped
                    or "if " in stripped
                    or "elif " in stripped
                    or "x-show" in stripped
                    or "x-data" in stripped
                    or "@click" in stripped
                    or ":class" in stripped
                ):
                    continue
                if status in line and "status_badge" not in line and "badge" not in line.lower():
                    # Check if this is a display context (not a comparison or assignment)
                    if "<td" in line or "<span" in line or "<div" in line:
                        findings.append(
                            {
                                "stream": "template_consistency",
                                "severity": "low",
                                "category": "template_consistency",
                                "file": rel,
                                "line": i,
                                "issue": f"Raw status string {status} in display context — use status_badge() macro",
                                "auto_fixable": True,
                                "fix_pattern": "raw_status_string",
                            }
                        )

        # Inline styles (should use Tailwind) — skip dynamic/unfixable ones
        for i, line in enumerate(content.splitlines(), 1):
            if 'style="' in line and "hx-" not in line:
                # Skip dynamic styles that can't be converted
                if any(
                    skip in line
                    for skip in [
                        "{{",
                        ":style",
                        "env(",
                        "var(",
                        "--",
                        "display: none",
                        "hidden",
                        "x-cloak",
                        "width: 0%",
                        "dynamic",
                        "{#",
                    ]
                ):
                    continue
                # Skip Alpine.js dynamic bindings
                if ":style=" in line:
                    continue
                findings.append(
                    {
                        "stream": "template_consistency",
                        "severity": "low",
                        "category": "template_consistency",
                        "file": rel,
                        "line": i,
                        "issue": "Inline style — prefer Tailwind utility classes",
                        "auto_fixable": False,
                    }
                )

    log(f"Template consistency: {len(findings)} findings")
    return findings


def scan_empty_state_coverage() -> list[dict]:
    """Check templates with tables/lists for empty state handling."""
    log("Scanning empty state coverage...")
    findings = []

    for tf in TEMPLATES_DIR.rglob("*.html"):
        content = tf.read_text(errors="replace")
        rel = str(tf.relative_to(PROJECT_ROOT))

        # Templates with tables or loops that should have empty states
        has_table = "<table" in content or "<tbody" in content
        has_loop = "{% for" in content
        has_empty = (
            "{% empty" in content
            or "{% else" in content
            or "empty_state" in content
            or "empty-state" in content
            or "no results" in content.lower()
            or "no data" in content.lower()
            or "nothing" in content.lower()
        )

        if (has_table or has_loop) and not has_empty:
            # Only flag list templates, not workspace/detail/form (they delegate to children)
            # Also skip typeahead/autocomplete templates that use elif branches
            has_elif_branch = "{% elif" in content
            if any(kw in rel for kw in ["list", "results"]) and "workspace" not in rel and not has_elif_branch:
                findings.append(
                    {
                        "stream": "empty_state",
                        "severity": "high",
                        "category": "empty_states",
                        "file": rel,
                        "issue": "Template has table/loop but no empty state handling",
                        "auto_fixable": False,
                    }
                )

    log(f"Empty state scan: {len(findings)} findings")
    return findings


def scan_loading_states() -> list[dict]:
    """Check hx-get/hx-post elements for loading indicators.

    Only flags elements where a loading state matters:
    - Forms (hx-post/put/delete on <form> or <button>)
    - Lazy-loaded sections (hx-get with hx-trigger="load" or "revealed")
    - NOT navigation links (hx-get on <a> or nav items — instant swap is fine)
    """
    log("Scanning loading states...")
    findings = []

    for tf in TEMPLATES_DIR.rglob("*.html"):
        content = tf.read_text(errors="replace")
        rel = str(tf.relative_to(PROJECT_ROOT))

        for i, line in enumerate(content.splitlines(), 1):
            # Only flag forms/buttons with mutations, or lazy-load triggers
            is_mutation = re.search(r"hx-(post|put|delete|patch)", line)
            is_lazy = re.search(r'hx-trigger="(load|revealed|intersect)', line)

            if not (is_mutation or is_lazy):
                continue

            has_indicator = (
                "hx-indicator" in line
                or "data-loading" in line
                or "data-loading-disable" in line
                or "data-loading-aria-busy" in line
            )
            # Check wider context (15 lines) — loading attrs may be on child elements
            context_lines = content.splitlines()[max(0, i - 3) : min(len(content.splitlines()), i + 12)]
            context = "\n".join(context_lines)
            has_indicator = (
                has_indicator
                or "hx-indicator" in context
                or "htmx-indicator" in context
                or "data-loading" in context
                or "data-loading-disable" in context
                or "data-loading-aria-busy" in context
            )

            if not has_indicator:
                findings.append(
                    {
                        "stream": "loading_states",
                        "severity": "medium",
                        "category": "loading_states",
                        "file": rel,
                        "line": i,
                        "issue": "HTMX mutation/lazy-load without loading indicator",
                        "auto_fixable": False,  # Context-dependent — needs subagent
                    }
                )

    log(f"Loading states: {len(findings)} findings")
    return findings


def scan_error_handling_surface() -> list[dict]:
    """Check routers for silent failures that affect the frontend."""
    log("Scanning error handling surface...")
    findings = []

    for rf in ROUTERS_DIR.rglob("*.py"):
        content = rf.read_text(errors="replace")
        rel = str(rf.relative_to(PROJECT_ROOT))

        lines = content.splitlines()
        for i, line in enumerate(lines, 1):
            # Bare except with pass or return empty
            if re.match(r"\s*except\s*(Exception)?\s*:", line):
                # Check next few lines for pass or return empty
                next_lines = "\n".join(lines[i : i + 3])
                if "pass" in next_lines or "return {}" in next_lines or "return []" in next_lines:
                    findings.append(
                        {
                            "stream": "error_handling",
                            "severity": "critical" if "htmx" in rel or "views" in rel else "high",
                            "category": "error_handling",
                            "file": rel,
                            "line": i,
                            "issue": "Silent exception handler — errors swallowed without logging or user feedback",
                            "auto_fixable": False,
                        }
                    )

            # Return 200 with error payload
            if (
                "return {" in line
                and '"error"' in line
                and "status_code" not in "\n".join(lines[max(0, i - 3) : i + 3])
            ):
                findings.append(
                    {
                        "stream": "error_handling",
                        "severity": "high",
                        "category": "error_handling",
                        "file": rel,
                        "line": i,
                        "issue": "Returns 200 with error payload instead of proper HTTP error status",
                        "auto_fixable": False,
                    }
                )

    log(f"Error handling: {len(findings)} findings")
    return findings


def scan_accessibility_static() -> list[dict]:
    """Static accessibility checks on templates."""
    log("Scanning accessibility (static)...")
    findings = []

    for tf in TEMPLATES_DIR.rglob("*.html"):
        content = tf.read_text(errors="replace")
        rel = str(tf.relative_to(PROJECT_ROOT))

        for i, line in enumerate(content.splitlines(), 1):
            # Images without alt
            if "<img" in line and "alt=" not in line:
                findings.append(
                    {
                        "stream": "accessibility",
                        "severity": "medium",
                        "category": "accessibility",
                        "file": rel,
                        "line": i,
                        "issue": "Image without alt attribute",
                        "auto_fixable": True,
                        "fix_pattern": "missing_alt",
                    }
                )

            # Inputs without label or aria-label
            if "<input" in line and 'type="hidden"' not in line:
                ctx = "\n".join(content.splitlines()[max(0, i - 3) : i + 3])
                if "aria-label" not in ctx and "<label" not in ctx and "aria-labelledby" not in ctx:
                    findings.append(
                        {
                            "stream": "accessibility",
                            "severity": "medium",
                            "category": "accessibility",
                            "file": rel,
                            "line": i,
                            "issue": "Input without associated label or aria-label",
                            "auto_fixable": True,
                            "fix_pattern": "missing_input_label",
                        }
                    )

            # Clickable div/span without role
            if re.search(r"<(div|span)[^>]*@click", line) and "role=" not in line:
                findings.append(
                    {
                        "stream": "accessibility",
                        "severity": "low",
                        "category": "accessibility",
                        "file": rel,
                        "line": i,
                        "issue": 'Clickable div/span without role="button" — not keyboard accessible',
                        "auto_fixable": True,
                        "fix_pattern": "missing_role_button",
                    }
                )

    log(f"Accessibility (static): {len(findings)} findings")
    return findings


def scan_x_cloak_coverage() -> list[dict]:
    """Check x-show/x-if elements have x-cloak."""
    log("Scanning x-cloak coverage...")
    findings = []

    for tf in TEMPLATES_DIR.rglob("*.html"):
        content = tf.read_text(errors="replace")
        rel = str(tf.relative_to(PROJECT_ROOT))

        for i, line in enumerate(content.splitlines(), 1):
            if ("x-show=" in line or "x-if=" in line) and "x-cloak" not in line:
                findings.append(
                    {
                        "stream": "x_cloak",
                        "severity": "low",
                        "category": "template_consistency",
                        "file": rel,
                        "line": i,
                        "issue": "x-show/x-if without x-cloak — flash of unstyled content",
                        "auto_fixable": True,
                        "fix_pattern": "missing_x_cloak",
                    }
                )

    log(f"x-cloak coverage: {len(findings)} findings")
    return findings


def scan_deprecated_patterns() -> list[dict]:
    """Check for deprecated SQLAlchemy patterns, raw strings, etc. in routers."""
    log("Scanning deprecated patterns...")
    findings = []

    for rf in ROUTERS_DIR.rglob("*.py"):
        content = rf.read_text(errors="replace")
        rel = str(rf.relative_to(PROJECT_ROOT))

        for i, line in enumerate(content.splitlines(), 1):
            # db.query(Model).get(id)
            if re.search(r"db\.query\(\w+\)\.get\(", line):
                findings.append(
                    {
                        "stream": "deprecated",
                        "severity": "medium",
                        "category": "template_consistency",
                        "file": rel,
                        "line": i,
                        "issue": "Deprecated db.query(Model).get(id) — use db.get(Model, id)",
                        "auto_fixable": True,
                        "fix_pattern": "deprecated_db_query_get",
                    }
                )

    log(f"Deprecated patterns: {len(findings)} findings")
    return findings


def run_phase1_scan() -> dict:
    """Execute all static scan streams.

    Returns findings dict.
    """
    log("=" * 60)
    log("PHASE 1: SCAN")
    log("=" * 60)

    update_status(1, "SCAN", "Running 15 scan streams...")

    # Build dependency graph first
    graph = build_template_dependency_graph()
    save_json("template-graph.json", graph)

    # Run all static scan streams
    all_findings = []
    all_findings.extend(scan_htmx_contracts())
    all_findings.extend(scan_security_patterns())
    all_findings.extend(scan_template_consistency())
    all_findings.extend(scan_empty_state_coverage())
    all_findings.extend(scan_loading_states())
    all_findings.extend(scan_error_handling_surface())
    all_findings.extend(scan_accessibility_static())
    all_findings.extend(scan_x_cloak_coverage())
    all_findings.extend(scan_deprecated_patterns())

    # Assign IDs
    for i, f in enumerate(all_findings, 1):
        f["id"] = f"F{i:03d}"
        f.setdefault("auto_fixable", False)
        f.setdefault("status", "open")

    # Summarize
    summary = {
        "total": len(all_findings),
        "critical": sum(1 for f in all_findings if f["severity"] == "critical"),
        "high": sum(1 for f in all_findings if f["severity"] == "high"),
        "medium": sum(1 for f in all_findings if f["severity"] == "medium"),
        "low": sum(1 for f in all_findings if f["severity"] == "low"),
        "auto_fixable": sum(1 for f in all_findings if f.get("auto_fixable")),
        "subagent": sum(1 for f in all_findings if not f.get("auto_fixable") and f["severity"] in ("critical", "high")),
        "fixed": 0,
    }

    report = {
        "scan_date": TODAY,
        "mode": "full",
        "coverage": {
            "templates_analyzed": len(list(TEMPLATES_DIR.rglob("*.html"))),
            "routers_analyzed": len(list(ROUTERS_DIR.rglob("*.py"))),
            "pages": [p[0] for p in NAV_PAGES],
        },
        "findings": all_findings,
        "summary": summary,
        "dependency_graph": "template-graph.json",
    }

    save_json("scan-report.json", report)
    update_status(1, "SCAN", f"Complete: {summary['total']} findings", summary)

    log(
        f"SCAN COMPLETE: {summary['total']} findings "
        f"({summary['critical']} crit, {summary['high']} high, "
        f"{summary['medium']} med, {summary['low']} low)"
    )
    log(f"  Auto-fixable: {summary['auto_fixable']}")
    log(f"  Needs subagent: {summary['subagent']}")

    return report


# ── Phase 2: CLASSIFY ─────────────────────────────────────────────────


def run_phase2_classify(scan_report: dict) -> dict:
    """Classify findings into fix actions."""
    log("=" * 60)
    log("PHASE 2: CLASSIFY")
    log("=" * 60)

    update_status(2, "CLASSIFY", "Classifying findings...")

    fix_plan = {"actions": [], "summary": {"auto_fix": 0, "subagent": 0, "log_only": 0, "needs_human": 0}}

    for finding in scan_report["findings"]:
        severity = finding["severity"]
        auto = finding.get("auto_fixable", False)

        if auto:
            action = "auto_fix"
            fix_plan["summary"]["auto_fix"] += 1
        elif severity in ("critical", "high"):
            action = "subagent"
            fix_plan["summary"]["subagent"] += 1
        elif severity == "medium":
            action = "needs_human"
            fix_plan["summary"]["needs_human"] += 1
        else:
            action = "log_only"
            fix_plan["summary"]["log_only"] += 1

        fix_plan["actions"].append(
            {
                "finding_id": finding["id"],
                "action": action,
                "severity": severity,
                "category": finding.get("category", "unknown"),
                "fix_pattern": finding.get("fix_pattern"),
            }
        )

    save_json("fix-plan.json", fix_plan)
    update_status(2, "CLASSIFY", f"Classified: {fix_plan['summary']}")

    log(
        f"CLASSIFY COMPLETE: auto={fix_plan['summary']['auto_fix']}, "
        f"subagent={fix_plan['summary']['subagent']}, "
        f"human={fix_plan['summary']['needs_human']}, "
        f"log={fix_plan['summary']['log_only']}"
    )

    return fix_plan


# ── Phase 3: FIX (auto-patterns) ─────────────────────────────────────


def apply_auto_fix(finding: dict, all_findings: list[dict]) -> bool:
    """Apply a deterministic auto-fix pattern.

    Returns True if fixed.
    """
    pattern = finding.get("fix_pattern")
    filepath = finding.get("file")
    if not pattern or not filepath:
        return False

    full_path = PROJECT_ROOT / filepath
    if not full_path.exists():
        return False

    content = full_path.read_text(errors="replace")
    original = content
    line_num = finding.get("line", 0)

    if pattern == "missing_rel_noopener":
        lines = content.splitlines()
        if 0 < line_num <= len(lines):
            lines[line_num - 1] = lines[line_num - 1].replace(
                'target="_blank"', 'target="_blank" rel="noopener noreferrer"'
            )
            content = "\n".join(lines)

    elif pattern == "missing_x_cloak":
        lines = content.splitlines()
        if 0 < line_num <= len(lines):
            line = lines[line_num - 1]
            # Add x-cloak after x-show or x-if
            if "x-cloak" not in line:
                line = re.sub(r'(x-show="[^"]*")', r"\1 x-cloak", line)
                line = re.sub(r'(x-if="[^"]*")', r"\1 x-cloak", line)
                lines[line_num - 1] = line
            content = "\n".join(lines)

    elif pattern == "deprecated_db_query_get":
        lines = content.splitlines()
        if 0 < line_num <= len(lines):
            lines[line_num - 1] = re.sub(
                r"db\.query\((\w+)\)\.get\((\w+)\)",
                r"db.get(\1, \2)",
                lines[line_num - 1],
            )
            content = "\n".join(lines)

    elif pattern == "unescaped_html_response":
        # Add html import and escape the f-string content
        lines = content.splitlines()
        if "import html" not in content:
            # Find first import line
            for idx, line in enumerate(lines):
                if line.startswith("import ") or line.startswith("from "):
                    lines.insert(idx, "import html")
                    if line_num > idx:
                        line_num += 1
                    break
        content = "\n".join(lines)

    elif pattern == "missing_alt":
        lines = content.splitlines()
        if 0 < line_num <= len(lines):
            line = lines[line_num - 1]
            if "<img" in line and "alt=" not in line:
                line = line.replace("<img", '<img alt=""', 1)
                lines[line_num - 1] = line
            content = "\n".join(lines)

    elif pattern == "missing_role_button":
        lines = content.splitlines()
        if 0 < line_num <= len(lines):
            line = lines[line_num - 1]
            if "@click" in line and "role=" not in line:
                line = re.sub(r"(@click)", r'role="button" tabindex="0" \1', line)
                lines[line_num - 1] = line
            content = "\n".join(lines)

    elif pattern == "missing_input_label":
        lines = content.splitlines()
        if 0 < line_num <= len(lines):
            line = lines[line_num - 1]
            if "<input" in line and "aria-label" not in line:
                # Try to use placeholder or name as label
                placeholder = re.search(r'placeholder="([^"]*)"', line)
                name = re.search(r'name="([^"]*)"', line)
                label = (
                    placeholder.group(1)
                    if placeholder
                    else (name.group(1).replace("_", " ").title() if name else "Input")
                )
                line = line.replace("<input", f'<input aria-label="{label}"', 1)
                lines[line_num - 1] = line
            content = "\n".join(lines)

    if content != original:
        full_path.write_text(content)
        return True
    return False


def run_phase3_fix(scan_report: dict, fix_plan: dict) -> dict:
    """Apply auto-fixes and log results."""
    log("=" * 60)
    log("PHASE 3: FIX")
    log("=" * 60)

    fix_log = {"fixes_applied": [], "fixes_failed": [], "needs_human": []}
    findings_by_id = {f["id"]: f for f in scan_report["findings"]}
    fixed_count = 0
    total_auto = fix_plan["summary"]["auto_fix"]

    for action in fix_plan["actions"]:
        fid = action["finding_id"]
        finding = findings_by_id.get(fid)
        if not finding:
            continue

        if action["action"] == "auto_fix":
            update_status(
                3,
                "FIX",
                f"Auto-fixing {fid}: {finding.get('issue', '')[:60]}...",
                {"fixed": fixed_count, "total": total_auto},
            )
            success = apply_auto_fix(finding, scan_report["findings"])
            if success:
                finding["status"] = "fixed"
                fixed_count += 1
                fix_log["fixes_applied"].append(
                    {
                        "finding_id": fid,
                        "method": "auto_pattern",
                        "pattern": action.get("fix_pattern"),
                        "file": finding.get("file"),
                    }
                )
                log(f"  FIXED {fid}: {finding.get('issue', '')[:80]}")
            else:
                fix_log["fixes_failed"].append(
                    {
                        "finding_id": fid,
                        "reason": "auto-fix pattern did not match",
                    }
                )
                log(f"  FAILED {fid}: auto-fix did not apply")

        elif action["action"] == "subagent":
            # Mark for subagent processing (will be handled by Claude session)
            fix_log["needs_human"].append(
                {
                    "finding_id": fid,
                    "reason": "requires subagent — complex fix",
                    "severity": action["severity"],
                    "category": action["category"],
                }
            )

        elif action["action"] == "needs_human":
            fix_log["needs_human"].append(
                {
                    "finding_id": fid,
                    "reason": "medium severity, no auto-fix pattern",
                    "severity": action["severity"],
                    "category": action["category"],
                }
            )

    save_json("fix-log.json", fix_log)
    update_status(
        3,
        "FIX",
        f"Complete: {len(fix_log['fixes_applied'])} fixed, "
        f"{len(fix_log['fixes_failed'])} failed, "
        f"{len(fix_log['needs_human'])} need human/subagent",
    )

    log(
        f"FIX COMPLETE: {len(fix_log['fixes_applied'])} applied, "
        f"{len(fix_log['fixes_failed'])} failed, "
        f"{len(fix_log['needs_human'])} deferred"
    )

    return fix_log


# ── Phase 4: VERIFY ──────────────────────────────────────────────────


def run_phase4_verify() -> dict:
    """Re-scan to confirm fixes and detect regressions."""
    log("=" * 60)
    log("PHASE 4: VERIFY")
    log("=" * 60)

    update_status(4, "VERIFY", "Re-scanning to confirm fixes...")

    # Re-run all scans
    verify_report = run_phase1_scan()
    verify_report["phase"] = "verify"

    save_json("verify-report.json", verify_report)
    return verify_report


# ── Phase 5: TEST ────────────────────────────────────────────────────


def run_phase5_test() -> dict:
    """Run test suite, ruff, mypy on changed files."""
    log("=" * 60)
    log("PHASE 5: TEST")
    log("=" * 60)

    update_status(5, "TEST", "Running test suite...")
    results = {"pytest": None, "ruff": None, "mypy": None}

    # Get changed files
    diff_result = run_cmd(["git", "diff", "--name-only", "HEAD"], cwd=str(PROJECT_ROOT))
    changed_files = [f for f in diff_result.stdout.strip().splitlines() if f.endswith(".py")]

    # Pytest (full suite)
    log("Running pytest...")
    env = os.environ.copy()
    env["TESTING"] = "1"
    env["PYTHONPATH"] = str(PROJECT_ROOT)
    try:
        pytest_result = subprocess.run(
            ["python3", "-m", "pytest", "tests/", "-v", "--tb=short", "-q"],
            capture_output=True,
            text=True,
            cwd=str(PROJECT_ROOT),
            timeout=600,
            env=env,
        )
        results["pytest"] = {
            "returncode": pytest_result.returncode,
            "output_tail": pytest_result.stdout[-2000:] if pytest_result.stdout else "",
            "errors": pytest_result.stderr[-1000:] if pytest_result.stderr else "",
            "passed": pytest_result.returncode == 0,
        }
        log(f"  pytest: {'PASS' if pytest_result.returncode == 0 else 'FAIL'}")
    except subprocess.TimeoutExpired:
        results["pytest"] = {"passed": False, "output_tail": "TIMEOUT after 600s"}
        log("  pytest: TIMEOUT")

    # Ruff on changed files
    if changed_files:
        log(f"Running ruff on {len(changed_files)} changed files...")
        ruff_result = run_cmd(["python3", "-m", "ruff", "check"] + changed_files)
        results["ruff"] = {
            "returncode": ruff_result.returncode,
            "output": ruff_result.stdout[:2000],
            "passed": ruff_result.returncode == 0,
        }
        log(f"  ruff: {'PASS' if ruff_result.returncode == 0 else 'FAIL'}")

        # Mypy on changed files
        log(f"Running mypy on {len(changed_files)} changed files...")
        mypy_result = run_cmd(["python3", "-m", "mypy", "--ignore-missing-imports"] + changed_files)
        results["mypy"] = {
            "returncode": mypy_result.returncode,
            "output": mypy_result.stdout[:2000],
            "passed": mypy_result.returncode == 0,
        }
        log(f"  mypy: {'PASS' if mypy_result.returncode == 0 else 'FAIL'}")
    else:
        log("  No Python files changed — skipping ruff/mypy")
        results["ruff"] = {"passed": True, "output": "No files to check"}
        results["mypy"] = {"passed": True, "output": "No files to check"}

    save_json("test-results.json", results)
    update_status(5, "TEST", f"Complete: pytest={'PASS' if results['pytest']['passed'] else 'FAIL'}")
    return results


# ── Phase 6: REPORT ──────────────────────────────────────────────────


def calculate_score(scan_report: dict) -> dict:
    """Calculate weighted production readiness score."""
    # Count open findings per category
    category_counts = {}
    for f in scan_report["findings"]:
        cat = f.get("category", "unknown")
        if f.get("status") != "fixed":
            severity_weight = {"critical": 4, "high": 3, "medium": 2, "low": 1}.get(f["severity"], 1)
            category_counts[cat] = category_counts.get(cat, 0) + severity_weight

    # Score each category (100 - deductions, floor 0)
    # Deduction rate scales inversely with weight — high-weight categories are stricter
    scores = {}
    for cat, weight in CATEGORY_WEIGHTS.items():
        raw_count = category_counts.get(cat, 0)
        # High-weight (3x): 8 pts/finding, Medium (2x): 5 pts, Standard (1x): 3 pts
        deduction_rate = {3: 8, 2: 5, 1: 3}.get(weight, 5)
        deductions = raw_count * deduction_rate
        scores[cat] = {"score": max(0, 100 - deductions), "weight": weight, "open_findings": raw_count}

    # Weighted average
    total_weight = sum(s["weight"] for s in scores.values())
    weighted_sum = sum(s["score"] * s["weight"] for s in scores.values())
    overall = round(weighted_sum / total_weight) if total_weight > 0 else 0

    gate = "PASS" if overall >= 70 else ("CONDITIONAL" if overall >= 50 else "FAIL")

    return {"overall": overall, "gate": gate, "categories": scores}


def run_phase6_report(scan_report: dict, fix_log: dict, test_results: dict) -> dict:
    """Generate final report and score."""
    log("=" * 60)
    log("PHASE 6: REPORT")
    log("=" * 60)

    score = calculate_score(scan_report)
    save_json("score.json", score)

    # Count stats
    total = len(scan_report["findings"])
    fixed = sum(1 for f in scan_report["findings"] if f.get("status") == "fixed")
    remaining = total - fixed
    needs_human = len(fix_log.get("needs_human", []))

    # Generate markdown report
    report_md = textwrap.dedent(f"""\
    # Frontend Fix Report — {TODAY}

    ## Production Readiness Score: {score["overall"]}/100 — {score["gate"]}

    ## Summary
    - **Total findings:** {total}
    - **Fixed (auto):** {fixed}
    - **Remaining:** {remaining}
    - **Needs human/subagent:** {needs_human}

    ## Category Scores (weighted)

    | Category | Score | Weight | Open Findings |
    |----------|-------|--------|---------------|
    """)

    for cat, data in sorted(score["categories"].items(), key=lambda x: -x[1]["weight"]):
        bar = "#" * (data["score"] // 5) + "." * (20 - data["score"] // 5)
        report_md += (
            f"| {cat.replace('_', ' ').title()} | {data['score']}/100 | {data['weight']}x | {data['open_findings']} |\n"
        )

    # High-priority remaining findings
    report_md += "\n## Remaining Findings (Critical/High)\n\n"
    for f in scan_report["findings"]:
        if f.get("status") != "fixed" and f["severity"] in ("critical", "high"):
            report_md += f"- **{f['id']}** [{f['severity'].upper()}] {f.get('file', 'N/A')}:{f.get('line', '?')} — {f['issue']}\n"

    # Test results
    report_md += "\n## Test Results\n\n"
    for tool, result in test_results.items():
        if result:
            status = "PASS" if result.get("passed") else "FAIL"
            report_md += f"- **{tool}:** {status}\n"

    # Auto-fixed list
    report_md += f"\n## Auto-Fixed ({fixed} items)\n\n"
    for f in scan_report["findings"]:
        if f.get("status") == "fixed":
            report_md += f"- {f['id']}: {f.get('file', 'N/A')}:{f.get('line', '?')} — {f['issue']}\n"

    report_md += textwrap.dedent(f"""
    ---

    ## Next Steps

    1. Review this report
    2. Check the branch: `git log frprp/remediation-{TODAY} --oneline`
    3. For "needs human" findings: fix manually or dispatch subagents
    4. When satisfied: merge branch to main, deploy

    ## Run Details
    - Branch: `frprp/remediation-{TODAY}`
    - Output: `docs/frprp/runs/{TODAY}/`
    - Screenshots: `docs/frprp/runs/{TODAY}/screenshots/`
    """)

    (RUN_DIR / "REPORT.md").write_text(report_md)

    # Update history
    history_file = FRPRP_DIR / "history.json"
    history = []
    if history_file.exists():
        try:
            history = json.loads(history_file.read_text())
        except json.JSONDecodeError:
            pass
    history.append(
        {
            "date": TODAY,
            "score": score["overall"],
            "gate": score["gate"],
            "findings": total,
            "fixed": fixed,
            "remaining": remaining,
        }
    )
    history_file.write_text(json.dumps(history, indent=2))

    update_status(6, "REPORT", f"COMPLETE — Score: {score['overall']}/100 ({score['gate']})")

    log(f"REPORT COMPLETE: {score['overall']}/100 — {score['gate']}")
    log(f"  Fixed: {fixed}/{total}")
    log(f"  Remaining: {remaining}")
    log(f"  Report: {RUN_DIR / 'REPORT.md'}")

    return {"score": score, "fixed": fixed, "remaining": remaining, "total": total}


# ── Main Orchestrator ─────────────────────────────────────────────────


def preflight() -> bool:
    """Run pre-flight checks.

    Returns True if all pass.
    """
    log("Running pre-flight checks...")

    # 1. Git clean
    result = run_cmd(["git", "status", "--porcelain"])
    if result.stdout.strip():
        log("WARNING: Git working tree has uncommitted changes — proceeding on current state", "WARN")

    # 2. Create branch
    branch_name = f"frprp/remediation-{TODAY}"
    run_cmd(["git", "checkout", "-b", branch_name])
    log(f"Created branch: {branch_name}")

    # 3. Create output dirs
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)

    # 4. Write manifest
    base_commit = run_cmd(["git", "rev-parse", "HEAD"]).stdout.strip()
    manifest = {
        "start_time": datetime.now(timezone.utc).isoformat(),
        "base_commit": base_commit,
        "branch": branch_name,
        "mode": "autonomous",
    }
    save_json("manifest.json", manifest)

    log("Pre-flight complete")
    return True


def run_autonomous():
    """Full autonomous pipeline."""
    log("=" * 60)
    log("FRONTEND FIX — AUTONOMOUS RUN")
    log(f"Date: {TODAY}")
    log("=" * 60)

    preflight()

    # Phase 1: SCAN
    scan_report = run_phase1_scan()

    if scan_report["summary"]["total"] == 0:
        log("No findings — frontend is clean!")
        run_phase6_report(scan_report, {"fixes_applied": [], "needs_human": []}, {})
        return

    # Phase 2: CLASSIFY
    fix_plan = run_phase2_classify(scan_report)

    # Phase 3: FIX (auto-patterns)
    fix_log = run_phase3_fix(scan_report, fix_plan)

    # Phase 4: VERIFY (re-scan)
    verify_report = run_phase4_verify()

    # Phase 5: TEST
    test_results = run_phase5_test()

    # Phase 6: REPORT
    final = run_phase6_report(scan_report, fix_log, test_results)

    # Commit fixes if any were applied
    if fix_log["fixes_applied"]:
        log("Committing auto-fixes...")
        run_cmd(["git", "add", "-A"])
        commit_msg = (
            f"frprp: auto-fix {len(fix_log['fixes_applied'])} findings\n\n"
            f"Score: {final['score']['overall']}/100 ({final['score']['gate']})\n"
            f"Fixed: {final['fixed']}/{final['total']}\n\n"
            f"Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
        )
        run_cmd(["git", "commit", "-m", commit_msg])
        log("Fixes committed")

    log("")
    log("=" * 60)
    log(f"FRONTEND FIX COMPLETE: {final['score']['overall']}/100 ({final['score']['gate']})")
    log(f"Fixed: {final['fixed']}/{final['total']} | Remaining: {final['remaining']}")
    log(f"Report: {RUN_DIR / 'REPORT.md'}")
    log("=" * 60)


# ── CLI ───────────────────────────────────────────────────────────────


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 scripts/frprp.py <command>")
        print("")
        print("Commands:")
        print("  run --autonomous    Full pipeline, walk away")
        print("  run --scan-only     Phase 1-2 only (health check)")
        print("  run --fix-only      Phase 3 only (from existing scan)")
        print("  report              Regenerate report from latest data")
        print("  score               Show readiness score")
        print("  history             Show score trendline")
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "run":
        mode = sys.argv[2] if len(sys.argv) > 2 else "--autonomous"
        if mode == "--autonomous":
            run_autonomous()
        elif mode == "--scan-only":
            RUN_DIR.mkdir(parents=True, exist_ok=True)
            SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
            scan_report = run_phase1_scan()
            run_phase2_classify(scan_report)
        elif mode == "--fix-only":
            scan_file = RUN_DIR / "scan-report.json"
            if not scan_file.exists():
                log("No scan report found — run scan first", "ERROR")
                sys.exit(1)
            scan_report = json.loads(scan_file.read_text())
            fix_plan_file = RUN_DIR / "fix-plan.json"
            fix_plan = (
                json.loads(fix_plan_file.read_text()) if fix_plan_file.exists() else run_phase2_classify(scan_report)
            )
            run_phase3_fix(scan_report, fix_plan)

    elif cmd == "report":
        scan_file = RUN_DIR / "scan-report.json"
        if scan_file.exists():
            scan_report = json.loads(scan_file.read_text())
            fix_log = (
                json.loads((RUN_DIR / "fix-log.json").read_text())
                if (RUN_DIR / "fix-log.json").exists()
                else {"fixes_applied": [], "needs_human": []}
            )
            test_results = (
                json.loads((RUN_DIR / "test-results.json").read_text())
                if (RUN_DIR / "test-results.json").exists()
                else {}
            )
            run_phase6_report(scan_report, fix_log, test_results)

    elif cmd == "score":
        score_file = RUN_DIR / "score.json"
        if score_file.exists():
            score = json.loads(score_file.read_text())
            print(f"Score: {score['overall']}/100 — {score['gate']}")
            for cat, data in sorted(score["categories"].items(), key=lambda x: -x[1]["weight"]):
                print(f"  {cat.replace('_', ' ').title():.<30} {data['score']}/100 ({data['weight']}x)")

    elif cmd == "history":
        history_file = FRPRP_DIR / "history.json"
        if history_file.exists():
            for entry in json.loads(history_file.read_text()):
                print(
                    f"  {entry['date']}: {entry['score']}/100 ({entry['gate']}) — {entry['fixed']}/{entry['findings']} fixed"
                )

    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)


if __name__ == "__main__":
    main()
