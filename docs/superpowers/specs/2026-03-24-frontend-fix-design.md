# Frontend Fix — Production Readiness Process

**Date:** 2026-03-24
**Status:** APPROVED
**Scope:** Full-stack where it touches the user — templates, JS, CSS, Alpine stores, HTMX attributes, and backend routes/services that surface as frontend problems.

---

## Overview

Frontend Fix (FRPRP) is a 6-phase autonomous pipeline that scans, triages, fixes, and verifies the AvailAI frontend for production readiness. It uses every available tool: 15 scan streams, 4 cross-cutting checks, 6 parallel fix subagents, Playwright browser testing, static analysis, and code review agents.

**Execution model:** Fully autonomous. Start it, walk away, read the report when done.

**Estimated runtime:** 60-90 minutes unattended.

---

## Priority Tiers

| Tier | Categories | Weight |
|------|-----------|--------|
| **HIGH (3x)** | Route Integrity, Error Handling, Empty States | Must be near-perfect |
| **MEDIUM (2x)** | Security, Form Feedback, Session Handling | Must meet baseline |
| **STANDARD (1x)** | Template Consistency, Accessibility, Performance, Mobile Parity, Loading States, Navigation Integrity | Improve progressively |

---

## Phase 1: SCAN (~15 min)

Fires 15 parallel scan streams + 4 cross-cutting checks against all 13+ pages at dual viewports (1280px desktop, 375px mobile).

### Scan Streams

| # | Stream | Tool/Agent | What It Finds | Auto-Fix? |
|---|--------|-----------|---------------|-----------|
| 1 | Route Integrity | Playwright MCP (navigate + snapshot), both viewports | 500s, 404s, redirect loops, missing templates, timeout | No |
| 2 | HTMX Contract Validation | Grep (hx-get/hx-post attrs) cross-ref against FastAPI route registry | Broken hx-get targets, hx-target pointing to non-existent DOM IDs, orphan endpoints | Yes |
| 3 | Console & Runtime Errors | Playwright MCP (console_messages + evaluate) per page | JS exceptions, Alpine init failures, HTMX swap/response errors, CSP violations, FOUC | No |
| 4 | Alpine.js Health | Playwright MCP (browser_evaluate) per page | x-data init throws, stores not registered, $persist errors, stale x-ref | No |
| 5 | Empty State Detection | Playwright MCP (snapshot on empty DB) | Blank content, missing empty-state component, tables with no "no results" row | No |
| 6 | XSS & Security | Grep + silent-failure-hunter agent | Unescaped {{ }} in HTMLResponse, reflected input, \|safe on user data, missing rel="noopener", CSRF gaps, CDN without SRI, sensitive data in HTML | Yes |
| 7 | Template Consistency | Grep + code-reviewer agent | Raw status strings instead of macros, inline styles, inconsistent buttons, missing imports | Yes |
| 8 | Template Dependency Graph | Grep (extends/include/from chains) | Full inheritance graph, orphaned templates, circular includes | Yes (dead templates) |
| 9 | Accessibility | Playwright MCP (a11y tree) + playwright skill, both viewports | Missing labels, broken tab order, no-alt images, non-semantic elements, missing ARIA, color contrast below WCAG AA, keyboard unreachable elements | Yes (trivial attrs) |
| 10 | Error Handling Surface | silent-failure-hunter + feature-dev:code-reviewer agents | 200 on failure, bare except:pass in views, missing rollback, blank screen on error, toast not fired | No |
| 11 | Loading & Feedback States | Grep + Playwright MCP | hx-get/hx-post without hx-indicator, no spinner on lazy sections, form submit without disabled state | Yes |
| 12 | Performance | Playwright MCP (network_requests + evaluate) | Payloads >50KB, duplicate requests, missing preload, unbounded queries, stale content after mutation, duplicate Alpine init | No |
| 13 | HTMX History & Navigation | Playwright MCP (navigate_back + snapshot) | Back button stale content, missing hx-push-url, history cache wrong page, forward/back breaks Alpine | No |
| 14 | Form & Mutation Feedback | Playwright MCP (fill_form + snapshot) | No validation messages, server errors not surfaced, toast not firing after CRUD, success without redirect/refresh | No |
| 15 | Session Expiry & Auth Degradation | Playwright MCP (expire cookie + interact) | Expired session → broken HTMX swap vs graceful redirect, raw 401 JSON in HTML context | Yes |

### Cross-Cutting Checks

| Check | Method | Auto-Fix? |
|---|---|---|
| Error recovery — navigate away after 500 without full refresh | Playwright: inject 500, click nav | No |
| Print styles — quote_report.html, rfq_summary.html render for PDF | Playwright: emulate print media | No |
| Dark mode — all pages with dark: prefix, no invisible text | Playwright: toggle dark class, contrast check | Yes |
| x-cloak — every x-show/x-if has x-cloak to prevent FOUC | Grep: x-show without x-cloak | Yes |

### Scan Execution

```
1. Build template dependency graph (static, no server)
2. Start test server (TESTING=1, empty DB)
3. Authenticate test session via Playwright
4. For each page (13+):
   a. Navigate at 1280px desktop
   b. Capture screenshot
   c. Run browser-based streams (1, 3, 4, 5, 12, 13)
   d. Click every tab, modal trigger, expandable section
   e. Capture sub-screenshots per tab
   f. Resize to 375px mobile
   g. Re-run streams 1, 9 at mobile
   h. Capture mobile screenshot
5. Run static streams (2, 6, 7, 8, 11) across template directory
6. Run code analysis streams (10) across routers/services
7. Cross-correlate findings (console error + route 500 = same root cause)
8. De-duplicate (one finding per root cause)
9. Output scan-report.json + screenshots
```

---

## Phase 2: CLASSIFY (~1 min)

Automated triage via rule matrix — no human input.

| Severity | Has Auto-Fix Pattern? | Action |
|---|---|---|
| Critical | Yes | Fix immediately, re-verify |
| Critical | No | Dispatch subagent |
| High | Yes | Fix immediately, re-verify |
| High | No | Dispatch subagent |
| Medium | Yes | Fix immediately, batch verify |
| Medium | No | Log to "needs human" |
| Low | Yes | Fix immediately, batch verify |
| Low | No | Log only |

Output: fix-plan.json with action assigned to every finding.

---

## Phase 3: FIX (~30-60 min)

### Step 1: Auto-Fix Patterns (deterministic, fast)

| Pattern | Detection | Fix |
|---|---|---|
| Missing rel="noopener" | target="_blank" without rel= | Add rel="noopener noreferrer" |
| Unescaped HTMLResponse | HTMLResponse(f"...{var}...") | Wrap in html.escape() |
| Missing CSRF token | hx-post/put/delete without csrf | Insert csrf hidden input |
| Missing x-cloak | x-show/x-if without x-cloak | Add x-cloak attribute |
| Raw status strings in templates | Inline "open"/"closed" | Replace with status_badge() macro |
| Missing hx-indicator | hx-get/hx-post without indicator | Add hx-indicator + spinner |
| Missing alt on images | img without alt | Add alt="" or descriptive alt |
| Missing form labels | input without label/aria-label | Add aria-label from placeholder |
| \|safe on user data | {{ user_input\|safe }} | Remove \|safe |
| Deprecated db.query().get() | In view functions | Replace with db.get(Model, id) |
| Raw string statuses in routers | "open" instead of enum | Replace with StrEnum constant |
| Import inside loop | import in for/while body | Move to module level |
| Missing rel on anchor target | a target without rel | Add rel="noopener noreferrer" |

### Step 2: Subagent Swarm (6 parallel agents)

| Subagent | Assigned Categories | Tools |
|---|---|---|
| Route Fixer | 500s, missing templates, broken hx-targets | Read, Edit, Write, Grep, Glob |
| Error Surface Agent | Silent failures, missing toasts, 200-on-error | Read, Edit, Grep, silent-failure-hunter |
| Empty State Builder | Missing empty states, blank pages | Read, Edit, Write, frontend-design skill |
| Security Patcher | XSS, CSRF gaps, session handling | Read, Edit, Grep, code-reviewer |
| Accessibility Fixer | Labels, ARIA, focus, contrast | Read, Edit, Playwright MCP |
| Consistency Enforcer | Macro usage, badge patterns, Tailwind | Read, Edit, Grep, Glob |

### Subagent Rules

- May ONLY modify files related to assigned findings
- Never modify config.py, .env, docker-compose.yml, alembic/
- Never delete files without confirming orphaned status
- If fix touches >3 files, run targeted tests first
- Commit each fix: "frprp(F{id}): {description}"
- Max 5 minutes per finding, then mark "needs_human"
- Max 1 retry per finding with different approach

### Fix-Verify Loop

```
for each finding:
    apply fix (auto-pattern or subagent)
    re-scan ONLY affected page/template
    if fixed → mark resolved, commit, move on
    if still broken → retry once (different approach)
    if still broken → mark "needs_human", move on

Circuit breaker: 5 consecutive failures → stop fixing, write report
```

---

## Phase 4: VERIFY (~10 min)

Full re-scan (all 15 streams) against the fixed codebase.

- Diff against Phase 1 scan to confirm fixes
- Any NEW findings = regressions, flagged as critical
- Regressions trigger immediate fix attempt (1 retry)
- Output: verify-report.json

---

## Phase 5: TEST (~5 min)

```
1. Full pytest suite: TESTING=1 pytest tests/ -v
2. Playwright E2E smoke: npx playwright test --project=smoke
3. Ruff on changed files: ruff check <changed_files>
4. Mypy on changed files: mypy <changed_files>
```

If tests fail:
- Identify which fix caused it (git bisect on fix commits)
- Revert that commit
- Mark finding as "needs_human"
- Re-run failing tests to confirm green

---

## Phase 6: REPORT (~1 min)

### Production Readiness Score

```
Category Breakdown (weighted):
  Route Integrity ........... __/100  (3x weight)
  Error Handling ............  __/100  (3x weight)
  Empty States ..............  __/100  (3x weight)
  Security ..................  __/100  (2x weight)
  Form Feedback .............  __/100  (2x weight)
  Session Handling ..........  __/100  (2x weight)
  Template Consistency ......  __/100  (1x weight)
  Accessibility .............  __/100  (1x weight)
  Performance ...............  __/100  (1x weight)
  Mobile Parity .............  __/100  (1x weight)
  Loading States ............  __/100  (1x weight)
  Navigation Integrity ......  __/100  (1x weight)

Gate: PASS (>= 70) / CONDITIONAL (50-69) / FAIL (< 50)
```

### Output Files

```
docs/frprp/runs/YYYY-MM-DD/
├── STATUS.md              # Live progress (updated throughout run)
├── REPORT.md              # Final human-readable report
├── manifest.json          # Run config, base commit, branch
├── scan-report.json       # Phase 1 raw findings
├── fix-plan.json          # Phase 2 classification
├── fix-log.json           # Phase 3 action log
├── verify-report.json     # Phase 4 re-scan
├── test-results.json      # Phase 5 test/lint
├── score.json             # Phase 6 readiness score
└── screenshots/           # Visual evidence per page
```

### Notifications

- Final report written to REPORT.md
- STATUS.md updated with final state
- Webhook POST if FRPRP_WEBHOOK_URL configured
- Summary line to stdout

---

## Pre-Flight Checks

```
1. git working tree clean (no uncommitted changes)
2. Docker running (test server needs DB)
3. Playwright browsers installed
4. Current test suite passes (abort if red)
5. Create branch: frprp/remediation-YYYY-MM-DD
6. Create output dir: docs/frprp/runs/YYYY-MM-DD/
7. Write run manifest
```

## Safety Rails

- All work on dedicated git branch (not main)
- Each fix is a separate commit with finding ID
- No DB migrations — template/JS/CSS/router only
- No config.py, .env, docker-compose.yml changes
- Fixes touching >3 files require targeted test pass
- Full test suite at end before declaring success
- Circuit breaker stops runaway failures

## Crash Recovery

```
frprp resume    # Reads manifest, picks up where it left off
```

Manifest tracks: resolved/pending/failed findings, active phase, subagent assignments, last commit.

## Diff-Aware Repeat Mode

```
frprp run --diff    # Scan only changed files + dependents since last run
```

Uses template dependency graph + git diff against last run's base commit.

## Historical Tracking

Each run appends to docs/frprp/history.json for score trendline across runs.

---

## CLI Reference

```bash
frprp run --autonomous          # Full pipeline, walk away
frprp run --scan-only           # Phase 1-2 only (health check)
frprp run --fix-only            # Phase 3 only (from existing scan)
frprp run --stream <name>       # Single stream scan
frprp run --page <url>          # Single page, all streams
frprp resume                    # Pick up crashed run
frprp report                    # Regenerate report
frprp score                     # Show readiness score
frprp history                   # Show score trendline
frprp fix --auto                # Auto-fix patterns only
frprp fix --finding F001        # Fix one finding
frprp verify --finding F001     # Re-scan one finding
frprp certify                   # Phase 6 gate only
```
