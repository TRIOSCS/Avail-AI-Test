# Remediation Plan — Issue Backlog & Phased Fix Plan

<!-- What: Captured list of known issues/defects with a phased remediation plan.
     Who reads it: Claude Code sessions executing fixes; humans reviewing scope.
     Depends on: docs/APP_MAP_*.md for architecture context. -->

**Status:** COLLECTING — issues are being recorded from user notes. Phasing is
drafted only after collection closes and each item is verified against current code.

**Shipped in this PR (2026-07-21):** ISS-021 (fast-uri bump), ISS-022 (export
lockdown via new `EXPORT_BULK_DATA` key — manager/admin only on the 5 dataset
exports; quote docs untouched), ISS-024 ("+ add here" removed; `preselect_site_id`
plumbing KEPT — still used by the Sites tab "+ Add first contact",
`site_card.html:69`), ISS-025 (Find Contacts dedupe via
`app/services/contact_dedup.py`, vendor worker + customer status-route filter),
ISS-027 (all 5 sub-items; per-site contact count retained — load-bearing per
existing tests).

**Branch:** `claude/issues-remediation-plan-tdd147`
**Started:** 2026-07-21

---

## Issue Backlog

<!-- Template per issue:
### ISS-NNN: <short title>
- **Reported:** <date> (user note, verbatim gist)
- **Area:** backend | frontend | data | infra | integration
- **Severity:** P0 data-loss/security | P1 broken workflow | P2 degraded UX/perf | P3 cleanup
- **Symptom:** what the user sees
- **Suspected root cause:** with exact file paths (verified: yes/no)
- **Notes:** anything else relevant
-->

### ISS-001: Graph API 400 — OneOnOne chat creation with fewer than 2 members
- **Reported:** 2026-07-21 (Sentry AVAILAI-TC, 110 events, ongoing — last seen 1h ago)
- **Area:** integration (Microsoft Graph)
- **Severity:** P1
- **Symptom:** `app/utils/graph_client.py` `_request_with_retry` logs repeated
  `Graph 400: Creation of 'OneOnOne' chat requires 2 members`.
- **Suspected root cause:** a Teams chat-creation call is being built with only one
  member — likely the sender==recipient case or a missing/unresolved AAD user id.
  (verified: no)

### ISS-002: ENABLE_PASSWORD_LOGIN active outside tests (acknowledged auth bypass)
- **Reported:** 2026-07-21 (Sentry AVAILAI-TF / AVAILAI-DF, ongoing — last seen 1h ago)
- **Area:** backend (auth)
- **Severity:** P0 security
- **Symptom:** startup logs CRITICAL: password login enabled in non-test mode with
  `ALLOW_PASSWORD_LOGIN_RISK=true` (`app/startup.py:run_startup_migrations`).
- **Suspected root cause:** env flag left on in the deployed environment; needs either
  removal from prod `.env` or a hard fail in production mode. (verified: no)

### ISS-003: Graph API 400 — Contacts delta query uses unsupported $orderby
- **Reported:** 2026-07-21 (Sentry AVAILAI-JV, recurring — last seen 2 days ago)
- **Area:** integration (Microsoft Graph)
- **Severity:** P1
- **Symptom:** `Graph 400: ErrorInvalidUrlQuery — $orderby not supported with change
  tracking over the 'Contacts' resource`.
- **Suspected root cause:** the Contacts delta-sync request includes `$orderby`, which
  Graph change tracking rejects; the sync presumably never completes. (verified: no)

### ISS-004: Inbox scan timeout (90s) for specific user — scans silently skipped
- **Reported:** 2026-07-21 (Sentry AVAILAI-Q, recurring — last seen 4 days ago)
- **Area:** backend (jobs/email mining)
- **Severity:** P2
- **Symptom:** `app/jobs/core_jobs.py:_safe_scan` — "Inbox scan TIMEOUT for user 3
  (90s) — skipping"; that user's inbox mining repeatedly never runs.
- **Suspected root cause:** oversized mailbox or slow Graph paging exceeding the fixed
  90s budget; needs incremental checkpointing or a larger/adaptive budget. (verified: no)

### ISS-005: Test-run events polluting production Sentry
- **Reported:** 2026-07-21 (Sentry — many issues with `testserver` culprits, sqlite
  "no such table", `RuntimeError: boom`, MagicMock TypeErrors)
- **Area:** infra (observability)
- **Severity:** P2
- **Symptom:** the majority of unresolved Sentry issues are artifacts of test runs
  (e.g. AVAILAI-95, -K1/K2, -PR, -P0, -QS, -EP), burying real production errors.
- **Suspected root cause:** `SENTRY_DSN` reaches the app under `TESTING=1` (or CI);
  Sentry init in `app/main.py` lifespan needs a TESTING guard, and the stale test
  issues need bulk-resolving. (verified: no)

### ISS-006: "Task was destroyed but it is pending!" asyncio warnings
- **Reported:** 2026-07-21 (Sentry AVAILAI-PC/-PB/-K1/-K2, 28 days ago)
- **Area:** backend (async lifecycle)
- **Severity:** P3 (unless reproduced in prod — currently looks test-correlated)
- **Symptom:** asyncio tasks garbage-collected while pending, surfaced via Loguru→Sentry.
- **Suspected root cause:** fire-and-forget `asyncio.create_task` without holding a
  reference / without cancellation on shutdown (materials filters partial and others).
  (verified: no)

### ISS-007: Calendar scan does full re-scan instead of delta sync (deferred TODO)
- **Reported:** 2026-07-21 (code sweep — `app/jobs/email_jobs.py:82`)
- **Area:** backend (jobs / Graph integration)
- **Severity:** P3 (efficiency; deliberate Phase-3 deferral)
- **Symptom:** calendar scan re-reads everything each run; the in-code TODO says to
  switch to `/me/calendarView/delta` for incremental updates/cancellations using the
  SyncState plumbing already in place.
- **Suspected root cause:** known deferred work, not a defect. (verified: yes — only
  actionable TODO marker in `app/`)

### ISS-008: tbf_worker README describes phase-2 stubs that don't exist in code
- **Reported:** 2026-07-21 (code sweep — `app/services/tbf_worker/README.md:9`)
- **Area:** docs
- **Severity:** P3
- **Symptom:** README promises `# TODO(phase2)` placeholder selectors and a
  `NotImplementedError: phase2: selectors` failure mode, but no such markers exist in
  the module's Python files — doc and code have drifted.
- **Suspected root cause:** stubs were implemented (or scaffold changed) without
  updating the README. (verified: yes)

### ISS-009: Buy-plan detail modals are dead (Submit/Approve/Reject/Halt/Cancel)
- **Reported:** 2026-07-21 (docs sweep — `docs/audit/2026-07-02-production-polish-review.md` BP-1, P0)
- **Area:** frontend (HTMX/Alpine)
- **Severity:** P1 broken workflow (source: P0 release-blocker)
- **Symptom:** all five buy-plan detail modal action buttons do nothing.
- **Suspected root cause:** `hx-post` inside Alpine `<template x-if>` is never processed
  by htmx (`app/templates/buy_plans/detail.html:161`). (verified: no — may be fixed by
  recent remediation waves; reconcile before phasing)

### ISS-010: Quotes built from Offers tab email/PDF an empty line-item table
- **Reported:** 2026-07-21 (docs sweep — 2026-07-02 review OQ-01, P0)
- **Area:** backend (quotes)
- **Severity:** P0 correctness (customer-facing wrong output)
- **Symptom:** quote create writes `QuoteLine` rows, but send/export read the
  `quote.line_items` JSON — so the emailed/PDF quote has no lines.
- **Suspected root cause:** dual line-item storage out of sync (`app/routers/htmx/offers.py:331`).
  (verified: no — reconcile)

### ISS-011: Multi-select "Build Quote" from requisitions list is dead
- **Reported:** 2026-07-21 (docs sweep — 2026-07-02 review OQ-02, P0)
- **Area:** frontend + backend routing
- **Severity:** P1 broken workflow (source: P0)
- **Symptom:** selecting 2+ requisitions and clicking Build Quote does nothing.
- **Suspected root cause:** triply dead — route shadowed (422), missing swap target, no
  listener (`app/templates/requisitions/list.html:206`). (verified: no — reconcile)

### ISS-012: Kebab "Mark Lost" marks the requisition WON
- **Reported:** 2026-07-21 (docs sweep — 2026-07-02 review REQ-01, P0)
- **Area:** frontend + data integrity
- **Severity:** P0 (writes wrong status to DB; "lost" unreachable in UI)
- **Symptom:** kebab menu "Mark Lost" POSTs to `/action/won`.
- **Suspected root cause:** copy-paste of the won action's URL
  (`app/templates/requisitions/req_row.html:174`). (verified: no — reconcile)

### ISS-013: System-settings boolean toggles never save
- **Reported:** 2026-07-21 (docs sweep — 2026-07-02 review SET-01, P0)
- **Area:** frontend (HTMX)
- **Severity:** P1 broken workflow (source: P0)
- **Symptom:** flipping a system-settings toggle issues no PUT.
- **Suspected root cause:** `hx-vals js:` references Alpine `$el`, undefined in htmx's
  plain-JS eval (`app/templates/settings/system.html:44`) — matches the known
  hx-on/Alpine-magic anti-pattern. (verified: no — reconcile)

### ISS-014: 2026-07-02 production-polish review — 47 P1 findings with no closure tracking
- **Reported:** 2026-07-21 (docs sweep — `docs/audit/2026-07-02-production-polish-review.md`)
- **Area:** cross-cutting
- **Severity:** P1 umbrella (reconciliation task)
- **Symptom:** the review logged 255 findings (5 P0 / 47 P1 / 103 P2 / 100 P3) and the
  doc has no status/disposition field; no companion remediation record exists. Notable
  P1 clusters: resell flows (RS-1 price leak, RS-2..4), CRM prospecting dead flows
  (F1–F9), quote defects (OQ-03..05), REQ-02..06, PERF-1 (sync Anthropic call blocks
  event loop ≤30s), PERF-2 N+1, PERF-3 full-table load, CI-1/OPS-2 (nightly host suite
  red 4+ nights, alert goes nowhere), OPS-1 (no off-site backup).
- **Suspected root cause:** audit ran without a remediation ledger; some findings were
  likely fixed by later waves (e.g. PRs #774–#776). Action: reconcile each P0/P1
  against current code, mark dispositions in the source doc, then fold true positives
  into this plan. (verified: no)

### ISS-015: CI deploy leaves TBF browser worker running stale code
- **Reported:** 2026-07-21 (docs sweep — `docs/audit/2026-07-18-non-production-code-audit.md`)
- **Area:** infra (deploy)
- **Severity:** P1
- **Symptom:** `.github/workflows/deploy.yml:114-116` restarts host worker units but
  omits `avail-tbf-worker.service` and `avail-xvfb.service` (which the browser workers
  `Requires=`); CI-driven releases leave the TBF worker on old code.
- **Suspected root cause:** service list drifted from the systemd unit set; `deploy.sh`
  and `deploy.yml` restart lists need a single source of truth. (verified: no)

### ISS-016: Dead ORM models whose documented consumers don't exist
- **Reported:** 2026-07-21 (docs sweep — 2026-07-18 audit)
- **Area:** data / cleanup
- **Severity:** P3
- **Symptom:** `FacetAudit` (writer `app/management/audit_facets.py` missing) and
  `KnowledgeConfig` (consumer `teams_qa_service.py` missing) — stale docstrings, tables
  with no producers/consumers.
- **Suspected root cause:** features removed or never landed; models left behind.
  Decide: implement the consumer or drop model + migration. (verified: no)

### ISS-017: Dead code & asset cleanup (test-only modules, orphaned scripts/assets)
- **Reported:** 2026-07-21 (docs sweep — 2026-07-18 audit)
- **Area:** cleanup
- **Severity:** P3
- **Symptom:** 10 modules imported only by tests (`buyplan_service.py`,
  `contact_quality.py`, `engagement_scorer.py`, `presence_service.py`,
  `vendor_email_lookup.py`, `utils/sanitize.py`, monitoring/human_behavior in workers),
  duplicate `static/public/sw.js`, orphaned `icon-512.png`, unreferenced
  `post-deploy.sh`/`update.sh` and three never-installed cron scripts.
- **Suspected root cause:** accumulation; needs quarantine-before-delete per
  `docs/BRANCH_AND_CI_WORKFLOW.md`. (verified: no)

### ISS-018: ~175 assertion-theater tests remain (status-200-only)
- **Reported:** 2026-07-21 (docs sweep — `docs/CODE_AUDIT_AND_HARDENING_PLAN.md` P6.1, partial)
- **Area:** tests
- **Severity:** P2 (test trustworthiness)
- **Symptom:** ~125 offenders in `tests/test_htmx_views.py` + ~50 in
  `tests/test_htmx_views_nightly30.py` assert only `status_code == 200`, masking
  regressions.
- **Suspected root cause:** retrofit stopped after 13 sourcing-filter tests. (verified: no)

### ISS-019: Hardening-plan remnants — StrEnum enforcement + local-import hoist unconfirmed
- **Reported:** 2026-07-21 (docs sweep — CODE_AUDIT_AND_HARDENING_PLAN P2.5 / P4.6)
- **Area:** backend cleanup
- **Severity:** P3
- **Symptom:** `OfferCondition` raw-string sites in `sightings.py` and
  `htmx/requisitions.py:673` handed to a "parallel P3 agent" with no closure record;
  ~180 function-local imports hoist confirmed only for the split god-files.
- **Suspected root cause:** partial execution of the plan; needs a verification pass
  and tick-or-fix. (verified: no)

### ISS-020: Pre-rollout deferred tech-debt table (7 items)
- **Reported:** 2026-07-21 (docs sweep — `docs/PRE_ROLLOUT_CHECKLIST.md`)
- **Area:** mixed
- **Severity:** P3 (explicitly deferred, none rollout-blocking)
- **Symptom:** `test_api_health.py` 4 stale-fixture failures on main; ESLint
  browser-globals errors; mypy full-tree ~2080 errors; duplicate
  `ENABLE_PASSWORD_LOGIN` line in `.env` (ties to ISS-002); `.env.example` drift
  (35 extras / 7 missing); Sourcengine + eBay connectors disabled; TLS renewal
  strategy undecided.
- **Suspected root cause:** consciously deferred post-rollout debt; schedule it.
  (verified: no)

### ISS-021: npm audit high — fast-uri host-confusion advisory failing the security CI job
- **Reported:** 2026-07-21 (CI security job on PR #779)
- **Area:** infra (dependencies)
- **Severity:** P1 (blocks CI green on every PR)
- **Symptom:** `npm audit --audit-level=high` fails on transitive dep `fast-uri`
  3.0.0–3.1.2 (GHSA-4c8g-83qw-93j6, host confusion via failed IDN canonicalization).
- **Resolution:** lockfile bumped to fast-uri 3.1.4 via `npm audit fix` on this branch;
  audit now reports 0 vulnerabilities. **FIXED in this PR.**

### ISS-022: Bulk data exports must be manager/admin-only (anti data-theft)
- **Reported:** 2026-07-21 (user note — CRM walkthrough)
- **Area:** CRM / backend (authz)
- **Severity:** P1 (data-exfiltration control)
- **Symptom:** any interactive role can export accounts, contacts, vendors, and
  requirements. Current state (verified: yes):
  - `AccessKey.EXPORT_DATA` gate exists but `ROLE_ACCESS_DEFAULTS`
    (`app/constants.py:409-422`) grants it to buyer/sales/trader/manager — blocks nobody.
  - Ungated entirely (only `require_user`): vendors export
    (`app/routers/htmx/vendors.py:254`), requisitions export
    (`app/routers/htmx/requisitions.py:459`), sightings export
    (`app/routers/sightings.py:792`).
  - Gated: CRM companies/contacts CSV (`app/routers/crm/export.py:143,175`),
    quote-builder Excel/PDF (`app/routers/quote_builder.py:262,298`).
- **Decision (user, 2026-07-21):** split the gate. Bulk dataset exports (companies,
  contacts, vendors, requirements, sightings) → manager+admin only. Quote-builder
  Excel/PDF (single-deal customer documents) stay open to sales.
- **Fix shape:** new capability key (e.g. `EXPORT_BULK_DATA`) defaulting to
  MANAGER/ADMIN; apply to the five dataset export routes (incl. the three ungated
  ones); leave `EXPORT_DATA` on quote docs; hide export buttons in templates for
  users without the key (same source-of-truth pattern as `has_buyer_role`).

### ISS-023: AI-assisted import for CRM data (feature)
- **Reported:** 2026-07-21 (user note — CRM walkthrough)
- **Area:** CRM (feature request)
- **Severity:** P2 (wanted capability, not a defect)
- **Note:** while exports get locked down (ISS-022), imports must stay broadly
  available and should be AI-assisted — e.g. upload a spreadsheet/CSV of
  accounts/contacts/vendors and have AI map columns, normalize, dedupe against
  existing records before insert. Scope/spec TBD in planning phase.

### ISS-024: Remove duplicate "+ add here" affordance on customer Contacts tab
- **Reported:** 2026-07-21 (user note — CRM walkthrough; explicit approval to remove)
- **Area:** CRM / frontend
- **Severity:** P3 (UI cleanup)
- **Symptom:** the Contacts tab has both "+ Add Contact" (controls bar,
  `app/templates/htmx/partials/customers/tabs/contacts_tab.html:83`) and a per-site
  "+ add here" in each section header
  (`.../customers/tabs/_contacts_grouped_list.html:69`) that open the same form.
- **Fix shape:** remove "+ add here" (and its `preselect_site_id` plumbing in
  `app/routers/htmx/companies/contacts.py:545` /
  `_contact_form.html:62` if then unused); keep "+ Add Contact"; form's site select
  defaults to HQ/first site. (verified: yes)

### ISS-025: "Find Contacts" suggests contacts that already exist in the system
- **Reported:** 2026-07-21 (user note — CRM walkthrough)
- **Area:** CRM (contact discovery)
- **Severity:** P2
- **Symptom:** AI/provider contact discovery re-suggests people who are already
  saved contacts. Both surfaces affected (verified: yes):
  - Vendor Find Contacts tab: `_run_vendor_find_contacts`
    (`app/routers/htmx/vendors.py:1067`) dedupes only within the current batch
    (local `seen` set) before inserting `ProspectContact` rows — no check against
    the vendor's existing prospect/real contacts, so re-runs duplicate suggestions.
  - Customer Contacts tab suggested-contacts: `_run_contact_discovery`
    (`app/routers/htmx/companies/contacts.py:756`) calls the provider waterfall
    with no DB access, and the `_suggested_contacts.html` panel renders results
    unfiltered against the company's existing `Contact` rows.
- **Fix shape:** before persisting (vendor) / rendering (customer), drop any
  suggestion whose email matches an existing contact case-insensitively, with a
  name-based fallback for suggestions lacking an email (use `fuzzy_score_vendor()`-style
  normalization, not inline fuzzy). Applies to both ProspectContact and real
  contact tables for the entity.

### ISS-026: Archive-only data policy — eliminate hard deletes for non-admins
- **Reported:** 2026-07-21 (user note — CRM walkthrough)
- **Area:** platform-wide (data integrity / authz)
- **Severity:** P1 (irrecoverable data loss is possible today)
- **Symptom:** users can permanently destroy CRM data (emails, names, accounts,
  phone numbers, contacts, etc.). Survey (verified: yes): **51 `db.delete()` call
  sites** across `app/routers/` + `app/services/` — companies core/sites/tags,
  vendor contacts, vendors, requirements, quotes, offers, quality plans, spec
  codes, and more. Archive columns exist only in `app/models/crm.py`; vendors
  already support `include_archived` filtering.
- **Decision (user, 2026-07-21):** archive-only for all users — archiving removes
  from view but is recoverable. Admins alone keep a separate, audited hard-delete
  (for junk/test records and privacy-removal requests).
- **Fix shape:**
  - Add `archived_at` / `archived_by_id` (nullable) to entities that lack them
    (Alembic migration with rollback).
  - Convert every non-admin delete endpoint to archive; default all list/detail
    queries to exclude archived rows; add an "Archived" recovery view per module
    with un-archive.
  - Admin hard-delete: `require_admin`, confirmation, and a `UserAdminAudit`-style
    audit row.
  - Guardrail test: assert no router outside `app/routers/admin/` calls
    `db.delete()` on protected entities (allow-list for genuinely ephemeral rows,
    e.g. drafts/join rows, decided at implementation).
- **Note:** interacts with ISS-022 (same anti-data-loss/theft theme) and the
  Safety rule (backup exists via db-backup service; recovery path documented).

### ISS-027: CRM account/contacts UI de-clutter (user-approved redesign pass)
- **Reported:** 2026-07-21 (user note — CRM walkthrough; explicit approval for all
  sub-items, satisfies the UI-guardrail rule)
- **Area:** CRM / frontend
- **Severity:** P2 (UX)
- **Symptom:** account detail page feels busy: ~18 top-level buttons/actions in the
  header, contact rows stack 3+ pills (role, DNC, completeness %), a header metric
  strip, and heavy per-site section chrome.
- **Approved sub-items:**
  1. **Consolidate header actions** — keep 2–3 primary buttons (e.g. Enrich, Add
     Contact); move the rest into the existing kebab menu
     (`app/templates/htmx/partials/customers/detail.html`, ~18 button sites today).
  2. **Quiet the badges/pills** — contact rows show role + DNC only. The per-contact
     "NN%" text pill after the name (`_contact_macros.html:252-257`) is REMOVED and
     replaced by a small segmented hashmark bar at the FAR RIGHT of the row, colored
     on a yellow→green scale by completeness (refined by user 2026-07-21; tooltip
     keeps the % + missing-fields detail). The account-level completeness badge
     (`detail.html:179-185`) remains the single summary signal.
  3. **Collapse the metric strip** — header stats to one compact row or behind a
     toggle, freeing above-the-fold space for the contact list.
  4. **Lighter site sections** — section headers slim to name + city; cadence dot
     moves to hover (`_contacts_grouped_list.html`); builds on ISS-024's
     "+ add here" removal.
  5. **Aesthetic look-and-feel pass** — overall visual cleanup with clear
     separation of data groups (spacing/dividers/hierarchy), consistent with
     existing Tailwind patterns; no new UI conventions.
- **Fix shape:** frontend-only template work (`frontend-design` + `htmx` skills);
  no route or data changes; before/after review per screen with the user.

### ISS-028: Relocate bulk data export to an admin-only Settings section
- **Reported:** 2026-07-21 (user note — revises shipped ISS-022 after review)
- **Area:** CRM / backend + frontend (authz, settings)
- **Severity:** P1 (tightens the data-exfiltration control)
- **Decision (user):** all bulk data export moves to a separate admin-only menu in
  Settings; export controls must NOT appear anywhere else in the app. Exception:
  quote building and other customer-facing document generation stay in the app
  (unchanged, `EXPORT_DATA`).
- **Fix shape:**
  - Remove `EXPORT_BULK_DATA` from MANAGER defaults (admin-only; per-user override
    remains possible via the admin users panel, mirroring `manage_connectors`).
  - Delete the export buttons from the vendors/requisitions/sightings/customers
    list toolbars (added in ISS-022) — no export UI outside Settings.
  - New Settings "Data export" page (admin-gated, existing settings patterns)
    linking the five dataset exports; exports from there are full-dataset
    (default params), no longer tied to on-screen filters.
  - Routes keep their URLs and `require_access(EXPORT_BULK_DATA)` gates.
  - Update role-matrix/toolbar tests; add settings-page tests.

### ISS-029: Contact role field — more options + write-in
- **Reported:** 2026-07-21 (user note — CRM walkthrough)
- **Area:** CRM / frontend + backend
- **Severity:** P2
- **Symptom:** the contact form's role dropdown offers only 5 canonical values
  (`ContactRole`: buyer/manager/engineer/planner/other, `app/constants.py:888`),
  while legacy rows hold 8 richer roles (buyer_po, specifier, ap_payer, logistics,
  exec, technical, decision_maker, operations) that display but cannot be re-saved.
- **Fix shape:** promote the legacy roles into `ContactRole` as first-class options
  (matching their existing display labels); selecting "Other" reveals a free-text
  write-in (Alpine show/hide) stored in the same `site_contacts.contact_role`
  String(50) column; write path accepts canonical values or the custom string;
  role pill renders custom values verbatim. Update the roles Jinja global /
  CANONICAL_ROLES plumbing and tests.

### ISS-030: Customer Activity feed shows notes from ALL user emails (scoping leak)
- **Reported:** 2026-07-21 (user note — CRM walkthrough; "deep dive this")
- **Area:** CRM (activity timeline / email mining)
- **Severity:** P1 (wrong data on customer pages; privacy/noise)
- **Symptom:** the Activity section on a customer surfaces notes for the user's
  emails broadly, not just correspondence with that customer.
- **Required behavior (user):** activity must show ONLY real interactions to/from
  the customer — logged calls, emails with the customer's contacts/domain, and
  other real-life customer touchpoints. "Really tight and stable."
- **Evidence (user-supplied feed screenshot text, 2026-07-21):** a single customer
  account's feed contained, alongside real customer threads: internal task-assignment
  notifications, calendar-invite acceptances for internal 1:1s, an out-of-office
  auto-reply, bounce/system messages, blank-subject "insufficient content" items,
  internal ops correspondence (inventory field exam, back-order reports), and test
  notes ("test"/"testttt") — i.e. the user's whole mailbox is being attributed to
  the account.
- **Filtering rules implied:** (a) include an email only when a counterparty
  address matches the customer's contact emails or company domain(s); (b) exclude
  auto-generated classes even when the sender matches: calendar responses, OOO,
  bounces/DSNs, system notifications; (c) internal-colleague mail (own org domain)
  is never customer activity; (d) presentation must be "clean readable and useful"
  — collapse noise, meaningful summaries, no "insufficient content" filler rows.
- **Root cause (deep dive, verified 2026-07-21):**
  1. **Read-time OR-leak:** the company Activity tab query
     (`app/routers/htmx/_shared_tabs.py:346-360`) ORs
     `ActivityLog.company_id == id` with `requisition_id.in_(company's reqs)` and
     applies NO `is_meaningful` filter — pulling in internal colleagues' replies on
     RFQ threads, vendor replies, sent-folder rows, and system events. It is a third
     divergent reimplementation; `get_company_activities()` and
     `get_account_timeline()` (`app/services/activity_service.py:781,861`) are both
     correctly scoped.
  2. **Quality gate unenforced:** the AI quality pass
     (`activity_quality_service.py`) already scores OOO/bounces/blank rows
     `is_meaningful=False` with `quality_classification`, but the tab renders them
     anyway ("No interaction details available" filler).
  3. **Write-time gaps:** `log_email_activity()` lacks the `own_domains`/junk
     pre-filter that `log_meeting_activity()` has; `_is_auto_reply()` exists but only
     gates resell progression, not logging; Exchange NDR/bounce senders evade
     `_is_noise_email()`; `scan_sent_folder()` writes rows with requisition_id but
     never resolves company/vendor attribution.
- **Fix plan:** (a) replace the tab query with
  `get_company_activities(meaningful_only=True)` — kills the OR-leak and enforces
  the gate in one change (RFQ-contact merge logic stays); (b) write-time: own-domain
  + auto-reply + NDR filters in `log_email_activity`/`poll_inbox`, entity resolution
  in `scan_sent_folder`; (c) management backfill to re-attribute/flag existing
  misattributed rows; (d) close the test gap (leak-scenario row must be excluded).
  Blast radius: vendor tab shares only the missing meaningful filter; digest + JSON
  API already correct.
- **IMPLEMENTED on this branch (2026-07-21):** company tab now uses
  `get_company_activities(meaningful_only=True, exclude_types={RFQ_SENT})`; vendor
  tab gained the meaningful filter; write-time own-domain/junk gate in
  `log_email_activity`; auto-reply skip + Exchange-NDR detection in `poll_inbox`;
  `scan_sent_folder` resolves entity attribution; backfill command
  `python -m app.management.reattribute_activity` (dry-run default, `--apply`).
  Run the backfill once post-deploy to clean existing misattributed rows.

### ISS-031: Remove "Add primary contact" header affordance + payment terms from accounts
- **Reported:** 2026-07-21 (user notes — CRM walkthrough; explicit removal approval)
- **Area:** CRM / frontend
- **Severity:** P3 (UI cleanup)
- **Done (this branch):**
  - "+ Add primary contact" placeholder removed from the account header meta line
    (`customers/detail.html:101-108`); "Primary: <name>" still shows when set.
  - Payment terms removed from the account Sites surface: card display
    (`site_card.html`), edit input (`site_edit_modal.html`), AND the write
    assignment in `app/routers/htmx/companies/sites.py:393` — removing only the
    input would have NULLed stored values on every site save. Column + data
    preserved; ERP is the source of truth. Quote payment terms (customer-facing
    documents) untouched.

### NOTE-A: "Park in prospecting" ownership semantics (confirmed correct, no change)
- **Verified 2026-07-21:** manual park (`prospect_reclamation.py:33` →
  send-to-prospecting, `companies/core.py:260`) clears `account_owner_id`, stamps
  `ownership_cleared_at`, pools the account as claimable with NO cooldown — any
  sales rep may pick it up immediately. Rep can park own accounts; manager/admin
  any. Matches intended design per user.

---

## Phased Plan

*(Drafted after collection closes.)*

- **Phase 0 — Data loss / correctness / security:** TBD
- **Phase 1 — Broken workflows:** TBD
- **Phase 2 — Degraded UX / performance:** TBD
- **Phase 3 — Cleanup / hardening:** TBD
