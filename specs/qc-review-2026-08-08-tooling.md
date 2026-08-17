# QC Review 2026-08-08 — Tooling Wave (Instrumented-Evidence Appendix)

Appendix to the main code-reading QC report. Every claim here is backed by an
actual tool run, live-system probe, or skill-checklist audit executed 2026-08-08
against /root/availai. Ten instruments: bandit, ruff (extended rules), pip-audit +
npm audit, full-suite coverage, Sentry (live, org trio-scs / project availai),
Playwright e2e, a silent-failure sweep of every GraphClient caller, and skill
audits (htmx, jinja2, sqlalchemy, fastapi, redis+pytest). No repo files were
modified by the audits; production DB untouched. Deduped total: **64 findings —
4 critical, 17 high, 43 medium** — plus three clean bills of health.

---

## 1. Instrumented-evidence summary — what the tools proved

- **Test suite**: 24,148 passed / 1 failed / 27 skipped in 16:57 (CI-mirror:
  TESTING=1, xdist, --cov=app). The one failure
  (tests/test_datasheet_capture_nightly.py::TestPdfContainsMpn::test_parse_failure_returns_false)
  is unrelated to money paths. Overall coverage **96%** (66,213 stmts, 2,519
  missed). Caveat handled: xdist worker gw2 dropped its coverage data; every
  reported gap was re-verified with targeted single-process runs and only ranges
  missing in BOTH runs are reported (two apparent worst offenders were gw2
  artifacts, not real gaps).
- **Playwright e2e**: the full configured suite is **139/139 green** (dead-ends +
  api 86/86; auth/smoke/data-validation/workflows 49/49; accessibility + visual
  4/4). However 2 spec files (materials-ui, sales-hub-ui) are orphaned by
  playwright.config.ts and unrunnable even by explicit filename; harnessed
  identically they fail **8/18 with 401s** — that UI coverage silently rotted.
  Two config projects (requisitions2-resize/-visuals) match zero files.
- **Bandit** (repo's own CI invocation, -c pyproject.toml): **0 High, 0 Medium**
  over 128,855 LOC; all 51 Low hits hand-triaged as false positives or
  deliberate patterns. A control run without the config confirmed the config
  masks nothing.
- **Dependency audit**: pip-audit over 88 prod + 115 dev exact pins: **zero
  vulnerabilities**. npm audit: **0 vulnerabilities**. Web-facing pins
  (fastapi/sqlalchemy/httpx/jinja2/cryptography/pydantic) all == latest.
- **Sentry (live)**: 25 unresolved issues in 14 days — but only **one is
  live-recurring**: the ENABLE_PASSWORD_LOGIN auth-bypass CRITICAL firing on
  every production boot (AVAILAI-TF, last 14h ago; flags confirmed in .env).
  One NEW real bug (AVAILAI-TH, CancelledError in tagging-job shutdown). Two
  top issues (AVAILAI-TC 114 events; AVAILAI-JV) are already fixed in deployed
  code and quiet 4-5 days — unresolved only in Sentry. The remaining ~20 are a
  single 2026-07-28 test-suite leak burst predating the TESTING gate (gate
  confirmed at app/main.py:62-68; nothing recurred in 11 days).
- **Ruff extended sweep** (B,A,C4,PIE,RET,SIM,PTH,PERF,S,DTZ,ASYNC): 1,977 hits
  triaged by reading each high-signal site → **3 real defects** survived.
- **Silent-failure sweep** (the wave's dominant defect class): the Graph retry
  layer returns `{"error": status}` dicts for 401/4xx instead of raising
  (app/utils/graph_client.py:278, 304), and **nine send paths discard that dict
  and persist success state** — approval outbox, prepayment notices (both legs
  of the designed honesty gate are unreachable), proactive offers/digests,
  ownership warnings, buy-plan/stock-sale notices, sweep notifications.
- **Skill audits**: 34 findings including a reflected Alpine-expression XSS, a
  cache-invalidation no-op verified empirically by fnmatch probe, and a test
  fixture that silently bypasses require_admin.

---

## 2. Findings by severity (deduped)

Tags: [tool] that surfaced it. Two dedups applied: buy_plans ship-date (ruff +
fastapi merged), proactive prepare-send SENT persistence (silent-failures +
sqlalchemy merged).

### Critical (4)

| # | Location | Finding | Tool |
|---|---|---|---|
| C1 | app/jobs/approval_outbox.py:119 | Approval outbox permanently marks rows `sent_at` on Graph 4xx: notifications.py send_email discards post_json's error dict (graph_client never raises for 4xx), so fail_count/dead-letter retry machinery is bypassed for exactly the 401/400/403/404 class. Approval decisions recorded delivered, never retried, nobody told. Contrast quote_send.py:133-135 which checks the dict correctly. | silent-failures |
| C2 | app/services/prepayment_notifications.py:541 | Money path: _send_group_email sets sent_any=True on a failed sendMail to accounting/AP (the wire-confirm pay-link email). email_sent=True defeats the honesty gate at :277; _write_failure_alert never fires; everyone believes AP was told to send the wire. | silent-failures |
| C3 | app/services/prepayment_notifications.py:269 | Second leg of the same gate is dead whenever a Teams webhook is configured: post_teams_channel_card swallows every failure internally (teams_notifications.py:82-85 warn-only, no raise), so teams_sent=True unconditionally — the failure-alert mechanism can never fire even when both channels failed. | silent-failures |
| C4 | app/services/vendor_merge_service.py:132 | delete_vendor_cards (132-178) — the destructive both-vendors delete with FK detach across ActivityLog/EnrichmentQueue/Offer/ProspectContact/StockListHash — has **zero test coverage** (identical missing range in full-suite and targeted runs). A missed detach or over-broad delete silently destroys history. | coverage |

### High (17)

| # | Location | Finding | Tool |
|---|---|---|---|
| H1 | app/services/proactive_service.py:485 (+486, model default intelligence.py:402) | Failed prepare-page offer send persisted as SENT: error dict discarded, matches flip SENT, throttle rows written (suppressing future offers), send_succeeded=True; AND the model's raw-string default status="sent" + sent_at=now means even the raised-exception path leaves the offer 'sent' (except only flips matches to FAILED, never po.status; commit at :533; router :536 shows success banner). | silent-failures + skill-sqlalchemy |
| H2 | app/services/proactive_service.py:720 | send_draft_offer: same defect — DRAFT converted to SENT + sent_at on a failed Graph send; no path reports delivery failure to the caller; no retry possible. | silent-failures |
| H3 | app/services/proactive_digest.py:321 | send_digest marks digest SENT and stamps every line's sent_at ("the tracking clock") on a 4xx error dict — salesperson never receives it, clocks run against them, draft is gone. | silent-failures |
| H4 | app/services/ownership_service.py:329 | Ownership-loss warning failure doubly hidden: error dict discarded + "Warning email sent" logged, and the ActivityLog dedup row was committed BEFORE the send — so the warning is never re-attempted and ownership is later revoked with zero notice. | silent-failures |
| H5 | app/services/buyplan_notifications.py:179, 599 | Both Graph email helpers (_send_email, _send_stock_email) discard the error dict and log success — every buy-plan approval, re-source broadcast, and stock-sale notice can silently fail with an INFO log asserting the opposite. | silent-failures |
| H6 | /root/availai/.env:99-100 (dup 106-107); guard app/startup.py:130-141 | Password-login auth bypass ACTIVE on the production host (ENABLE_PASSWORD_LOGIN=true + ALLOW_PASSWORD_LOGIN_RISK=true), firing a CRITICAL to Sentry every boot (AVAILAI-TF, 11 events, last 14h ago) — the only live-recurring Sentry issue. Previously accepted risk for single-user testing; app.availai.net is production. | sentry |
| H7 | app/templates/htmx/partials/requisitions/list.html:9 (+:163) | Reflected Alpine x-data expression injection (XSS): unvalidated `status`/`urgency` query params HTML-autoescaped into single-quoted JS strings inside x-data — &#39; decodes back to ' and breaks out. Verified payload executes arbitrary JS on Alpine init. Fix is `|tojson` (pattern used correctly elsewhere). | skill-jinja2 |
| H8 | app/services/proactive_matching.py:63 | Picks 24h cache invalidation is a silent no-op: PICKS_CACHE_PREFIX ends with ':' and invalidate_prefix appends another → pattern 'intel:proactive_picks::*' never matches stored keys (verified: fnmatch probe False). New matches stay hidden from the picks strip up to 24h. | skill-redis |
| H9 | app/services/proactive_matching.py:830 (+842/858; proactive_service.py:484/574/747/938; routers/htmx/proactive.py:153) | No ProactiveMatch status mutation busts the picks cache — dismissed/sent/expired/converted matches remain in the cached strip even after H8 is fixed. | skill-redis |
| H10 | app/routers/htmx/companies/core.py:1202 (+437/575/1366; sites.py/contacts.py = zero calls) | HTMX company-workspace mutations never invalidate company_list/company_detail caches (JSON PUT does, at crm/companies.py:501-502) — cached /api/companies(/id) serve stale data up to 30min/1h after any workspace edit. | skill-redis |
| H11 | app/cache/intel_cache.py:144 | Under TESTING the cache is disabled only by accident: the PG fallback still runs against the SQLite app engine (no intel_cache table), fails, and is swallowed. Empirically probed: set_cached raises internally, read-back None. The entire cache layer (hits + invalidation) is structurally untestable — exactly why H8 shipped undetected. | skill-redis-pytest |
| H12 | app/templates/htmx/partials/proactive/list.html:78 (+92/102/126/128; htmx_views.py:271) | Entire Proactive module navigates tabs/scope without hx-push-url, and the shell route never threads ?tab= — refresh, back button, and shared URLs all land on Matches. | skill-htmx |
| H13 | app/routers/htmx/settings.py:858-993 | Admin danger-zone data-ops handlers (vendor/company delete-both, dedup-pair parse, bulk merge/delete/dismiss executor — the only callers of C4's untested service) fully untested; the except→rollback+toast wrapper means a broken bulk delete fails silently. | coverage |
| H14 | app/routers/htmx/offers/rfq.py:203-269 | The real (non-TESTING) RFQ send branch (token fetch, vendor groups, Graph send, sent/failed accounting) has never been executed by the suite; a token-refresh regression would silently stop all outbound RFQs while the UI reports success. | coverage |
| H15 | app/routers/admin/users.py:562-598 | PO-verify approver grant endpoint untested — a limit-parse bug could silently grant unlimited PO-verification authority (''/'unlimited' → NULL cap), an approval-routing security control. | coverage |
| H16 | app/routers/prepayments.py:338-352 | 'Mark paid' modal GET incl. _require_mark_paid_access authz gate + approved-only guard uncovered in full-suite AND all 14 dedicated prepayment test files. | coverage |
| H17 | app/routers/prepayment_confirm.py:143-147 | Public tokenized pay-link's concurrent double-submit guard (the idempotency path preventing a double wire confirmation from re-firing the paid fan-out) untested — identical 90%/'143-147 missing' in both runs. | coverage |

### Medium (43)

**Live errors / jobs** — app/jobs/tagging_jobs.py:29 `except Exception` misses
CancelledError at shutdown: rollback skipped, session closed under a
still-running to_thread worker (Sentry AVAILAI-TH, NEW, 3 events) [sentry].
Sentry hygiene: AVAILAI-TC (114 events, Teams oneOnOne — fixed by #824
08-04, quiet 4 days) and AVAILAI-JV (contacts-delta $orderby — fixed 08-03)
still unresolved, masking regressions [sentry]. ~20-issue 2026-07-28
test-leak burst needs bulk-resolve; TESTING gate confirmed in code [sentry].

**Input handling** — app/routers/htmx/buy_plans.py:837-840 PO-confirm ship date:
unparseable input silently replaced with naive datetime.now() instead of HTTP
400; UTCDateTime binds naive as UTC so a typo'd date is recorded as 'now' and
audit-logged as a legitimate edit [ruff + skill-fastapi, merged].
app/routers/htmx/offers/rfq.py:217 (+252/272) zips vendor_names/vendor_emails
with no length validation — skew mispairs every subsequent vendor name/email on
outbound external email [ruff]. app/routers/htmx/requisitions.py:1197 (+
requisitions_edit.py:481) sub-MPN/manufacturer zip silently drops or mispairs
substitutes [ruff].

**e2e / packaging** — playwright.config.ts:35 materials-ui + sales-hub-ui specs
orphaned (headers falsely claim a project runs them); harnessed: 8/18 fail with
{"error":"Not authenticated"} — accent-migration/chip/Sales-Hub UI coverage
lost [e2e ×2]. playwright.config.ts:37-38 requisitions2-resize/-visuals match
no files → 0 tests, silent [e2e]. app/main.py:562 (+559, check_dir=False :539)
cwd-relative static mounts: uvicorn started outside repo root 500s on every
/static/* request (reproduced with traceback) [e2e].

**Coverage gaps** — buyplan_scoring.py:195-220 buyer auto-routing priorities 2-4
(commodity/geography/workload) untested — wrong-buyer routing raises no error;
offers_jobs.py:90-109 + 175-187 digest/Teams-push job bodies untested (failures
log-only); stock_list_ingest.py:264-282 post-ingest vendor-enrichment trigger
untested (all guards return False silently); resell_jobs.py:116-131 stale-
'sending' recovery sweep untested (wedged outreach rows stay wedged) [coverage ×4].

**Silent failures (lower tier)** — prospect_reclamation.py:296-298 sweep
notifications count 4xx as delivered ("sent N/N" inflated) [silent-failures].
prospect_scheduler.py:260-264 discovery batch stamped COMPLETED even when BOTH
legs failed and were swallowed; reserved FAILED status never written — dead
Explorium key produces unbroken COMPLETED batches with 0 prospects
[silent-failures].

**HTMX/UI** — proactive/list.html:253 unterminated `<button>` tag (renders via
browser error-recovery); proactive.py:127/157/868/929/948 manager See-All scope
silently reset to 'mine' by five re-render actions; buy_plans/detail.html:282
(+220/256/307/337/363) modals close before checking event.detail.successful —
typed rejection/halt reasons lost on 4xx; digests.html:14 wholesale-destructive
'Generate digests' has no hx-confirm (every sibling destructive button does);
_match_row.html:68 offers drill-down refetches on every toggle incl. collapse;
proactive partial never updates `<title hx-swap-oob>` (peer partials all do)
[skill-htmx ×6].

**Jinja2** — dossier_hero.html:16 and dossier_shell.html:20: user `mpn` in JS
string context escaped with `|e`/autoescape instead of `|tojson` (same class as
H7; mitigated by .upper()) [skill-jinja2 ×2].

**SQLAlchemy/perf** — proactive_digest.py:224 ~6+ queries per NEW match
system-wide in generate_digests (batched compute_offer_rollups exists and is
used elsewhere with an explicit PERF-guard comment) — runs under a 300s
wait_for, aborts wholesale at scale; pricing_history.py:58 func.upper(mpn)
defeats ix_quote_lines_mpn (no functional index exists) → seq-scan of a 5-way
join on a hot path; proactive_digest.py:360 per-digest line query over an
unbounded digest list + full-table User/Company loads per view;
proactive_matching.py:406 (+626-628, 497-509) triple part-expansion + query-in-
set-comprehension inside the 5000-offer scan loop; part_equivalence.py:249
unguarded IntegrityError on unique ix_peq_pair — a concurrent verdict discards
the whole 25-verdict batch; proactive_service.py:1005 (+1054) raw-string
BuyPlan status list ["active","completed"] silently excludes the newer INBOUND
(PO approved, goods inbound) from the scorecard PO count [skill-sqlalchemy ×6].

**FastAPI** — sources.py:292 Test-probe persists raw str(e) to last_error and
serves it to every logged-in user via GET /api/sources; vendor_contacts.py:160
broad except returns str(e) (around db.commit + Anthropic call) in a 200;
main.py:350 500-envelope leaks internal exception class via "type" key;
main.py:456 ModuleAccessMiddleware opens SessionLocal and runs sync queries on
the event loop for every guarded request (pool exhaustion → up-to-10s global
stalls); main.py:737+745+772 /health runs sync DB execute + Redis pings in
async def — blocks the loop exactly during outages; require_fresh_token called
directly (not via Depends) at 7 sites (sources.py:551, crm/quotes.py:442,
htmx/quotes.py:526, rfq.py:206, replies.py:292, follow_ups.py:142/292) —
dependency_overrides silently don't apply; vendor_contacts.py:76-160 +
sources.py:549-604 business logic (3-tier waterfall incl. inline AI prompt;
inbox-mining merge loop) lives in routers [skill-fastapi ×7, one merged up].

**Redis/pytest** — materials.py:657 import_part_numbers commits up to 1,800 new
cards without invalidate_prefix("material_list") (2h TTL; every sibling
mutation invalidates); redis skill docs describe flush_enrichment_cache() + a
monthly APScheduler flush that exist nowhere in the codebase;
sightings.py:130 module-level 30s _cache not reset by any autouse fixture
(cross-test leak class conftest eliminates for eight other registries);
conftest.py:633-637 the default `client` fixture silently overrides
require_admin/require_buyer to the plain buyer user — admin gates pass without
ever running (real 403 coverage only via nonadmin_client); pytest skill
fixtures.md drifts from reality (removed event_loop documented, 2-of-8 autouse
fixtures listed, wrong db_session isolation description) [skill-redis-pytest ×5].

**Dependency hygiene (no vulns)** — locks not hash-pinned (optional
pip-compile --generate-hashes); requirements.in:21-22 stale comment claims
fastapi 0.136.3 while line 4 pins 0.141.1; 11 npm devDeps one patch/minor
behind caret ranges; tailwindcss deliberately held at ^3 [dep-audit].

---

## 3. Tool-by-tool rundown

**bandit — CLEAN.** Run per the repo's own CI invocation (-c pyproject.toml)
over 128,855 LOC: 0 High, 0 Medium; all 51 Low hits hand-triaged (OAuth URL
constants, sentinel-default guard, scraper jitter randomness, deliberate
best-effort excepts). A no-config control run confirmed the config masks
nothing. Zero findings — a genuine clean bill of health.

**ruff (extended rules) — 3 real defects from 1,977 hits.** B/A/C4/PIE/RET/SIM/
PTH/PERF/S/DTZ/ASYNC sweep; 1,645 hits were the FastAPI Depends idiom; every
other high-signal rule triaged by reading each site. Survivors: naive-now +
silent bad-date coercion on PO confirm, and two parallel-form-array zips with
mispair risk (outbound vendor email; sub-MPN/manufacturer).

**dep-audit — CLEAN.** pip-audit (88 prod + 115 dev exact pins) and npm audit
both report zero vulnerabilities. Pin hygiene good (proper pip-tools two-file
pattern; starlette pin is a deliberate CVE-fix line). Notes only: no hash
pinning, one stale rationale comment, 11 npm devDeps slightly behind.

**coverage — 96% overall, but gaps cluster on money/destructive paths.** 24,148
passed / 1 unrelated failure / 27 skipped in 16:57. gw2 xdist artifact handled
by double-verification. The 10 confirmed gaps are precisely the scariest code:
vendor hard-delete, admin danger zone, real RFQ send, approver-grant limits,
prepayment authz + pay-link idempotency, buyer routing, digest jobs, enrichment
trigger, resell recovery sweep.

**sentry — one live issue, one new bug, poor triage hygiene.** 25 unresolved in
14 days, but 23 are dead noise (07-28 test-leak burst) or already-fixed
(#824 Teams, contacts-delta). Real signal: the production password-login
bypass CRITICAL every boot, and a new CancelledError shutdown race in
tagging_jobs. No unfixed high-frequency production defect.

**e2e — configured suite 139/139 green; the config itself is the finding.** Two
orphaned+rotted spec files (8/18 fail on 401 when harnessed), two projects
matching zero files, and cwd-relative static mounts that 500 outside the repo
root (reproduced).

**silent-failures — the wave's headline defect class.** The Graph retry layer
returns error dicts instead of raising; 9 send paths (3 critical, 5 high, 2
medium) discard the dict and persist success — including both legs of the
prepayment failure-alert honesty gate. Verified-clean counterexamples exist
(quote_send, email_service RFQ batch, send_teams_dm, inventory_jobs, resell
finalize), proving the codebase knows the contract.

**skill-htmx — 7 findings (1 high).** Proactive module missing hx-push-url
end-to-end, malformed button markup, scope resets, modals closing before the
POST result, destructive action without hx-confirm, wasteful refetch, missing
title OOB. Global error-toast handlers and escaped `| safe` bodies checked and
cleared.

**skill-jinja2 — 3 findings, one defect class.** Unvalidated query params in
single-quoted JS strings inside Alpine x-data using HTML escaping instead of
`|tojson` — one exploitable reflected XSS (requisitions list), two
upper()-mitigated. All 13 `| safe` sites traced clean; no autoescape-off
blocks.

**skill-sqlalchemy — 7 findings (1 high, merged into H1).** Raw-string status
persistence of failed sends, two N+1 loops, an index-defeating func.upper()
filter, triple-redundant expansion in the scan loop, an unguarded unique-
constraint race, and a stale raw-string status list missing INBOUND. Legacy
Query.get(), SessionLocal-in-service, and JSON-mutation sweeps all clean.

**skill-fastapi — 8 medium findings.** Two raw-exception-to-client leaks, the
500-envelope class-name leak, sync DB on the event loop in middleware and
/health, the ship-date coercion (merged with ruff), 7 direct
require_fresh_token calls bypassing dependency_overrides, and business logic
in routers. Auth coverage, secret comparisons (hmac.compare_digest), and
pagination clamps all verified clean.

**skill-redis-pytest — 9 findings (4 high).** Empirically verified picks-cache
invalidation no-op, unbusted match mutations, HTMX company mutations never
invalidating 30min/1h caches, cache layer untestable under TESTING (probed),
plus a missing materials-import invalidation, nonexistent documented flush
function, an unreset module-level cache, the require_admin-bypassing client
fixture, and pytest-skill doc drift.

---

*Raw logs: /tmp/claude-0/-root/1fbab33d-b381-45db-a211-905f5601c782/scratchpad/
(cov_run.log, cov_table.txt). Sentry dashboard:
https://trio-scs.sentry.io/issues/?project=availai&query=is%3Aunresolved+lastSeen%3A-14d*
