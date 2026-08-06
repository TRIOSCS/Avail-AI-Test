# PACKET 1 — Wave 1 (quiet + honest) review

One message, everything Wave 1 changed, and every call you can reverse.
Three terms used throughout, defined once: the **kernel** is the minimal
launch surface from spec §3 — one deal end to end plus the resell walk,
runnable with zero paid API keys. **Keys-off** means the parallel
instance runs with every outbound credential blanked (Azure/Graph,
Anthropic, connectors, 8x8). **Park** = job turned off behind an
existing flag with a named comeback trigger; **delete** = removed from
code, restorable from git history. Every decision below was executed as
recommended; one word from you flips it.

## 1. The numbers — baseline → now

Numbers below are as of packet delivery (2026-08-06, after Waves 2-3
also landed — Packets 2 and 3 carry their own detail):

| Metric | Baseline (2026-08-04) | Current |
|---|---|---|
| Routes (runtime, flattened) | 809 (753 unique paths) | **675** (167 /api, 483 /v2, 25 other; 627 unique) — W2 orphan sweep + badge consolidation + W2/W3 surface deletes |
| Scheduled jobs (in-app) | 59 | **7 kernel** (6 run live keys-off; the 7th, token refresh, registers only when Azure creds exist) |
| Host cron jobs (prod box) | 5 | 5 (prod untouched until cutover, by design) |
| LOC sightings.py | 3,812 | 3,812 (Wave 4 work) |
| LOC htmx_app.js | 3,654 | 3,654 (Wave 4) |
| LOC search_service.py | 3,604 | 3,604 (Wave 4) |
| Status values, all entities | 114 | **92** stored (W1 −10 dead + Task 3→2; W3 resell/buy-plan/requisition collapses) |
| Test files | 1,113 | 1,107 |
| E2E spec files | 12 | 12 |
| Nav tabs | 10 + Settings | **5 + gear** (Wave 2) |

## 2. Jobs shut off — the full 59-job disposition

Every claim below was checked against a restored copy of the production
database; an independent second pass confirmed 10 of 10 spot-checks.

### KEEP — 7 (the kernel scheduler, exactly)

- **approval_outbox_drain** — drains queued approval emails every 60s; all 6 rows ever queued were sent. Working.
- **batch_results** — applies AI inbox-parse results to vendor responses; 2,074 batches processed, latest today. Active pipeline.
- **cadence_materialize** — nightly recompute of follow-up clocks on every company (13 in prod). Named on the kernel list.
- **inbox_scan** — polls your inbox for RFQ replies feeding the Responses tab; last sync today 12:50. The heart of the kernel.
- **scan_sent_folders** — reconciles your sent mail so reply-matching works; a required component of the scan above.
- **token_refresh** — keeps the Microsoft 365 token fresh for the two Graph jobs above; 1 connected user, scans running on it today.
- **worker_liveness_check** — watchdog that alerts when a background worker goes silent; alert-only, writes nothing to business tables.

### PARK — 12 (off behind existing flags; comeback trigger named)

- **account_sweep** — reclaims dormant accounts from reps into a shared pool; multi-rep governance over a 13-company book. Comeback: a team exists.
- **auto_surface_reactivation** — auto-feeds past customers into the prospecting pool; replaced by manual prospect intake. Comeback: team exists.
- **buyplan_nudge** — nudges buyers about overdue plan lines; 2 lines ever nudged, and solo it nudges you about your own lines. Comeback: second buyer/ops user.
- **calendar_scan** — imports calendar meetings into the activity log; 39 rows, latest today — live yield, so parked not deleted. Comeback: team exists.
- **eight_by_eight_poll** — 8x8 call-record poller; zero call records ever captured despite the watermark advancing — needs an integration rebuild, not a restart. Comeback: Data Capture Initiative (first post-launch build).
- **ownership_sweep** — warning emails about accounts nearing reclamation; pure multi-rep territory governance. Comeback: team exists.
- **performance_tracking** — vendor scorecards + buyer leaderboards every 12h; 99,546 snapshot rows for an audience of one. Comeback: team exists.
- **proactive_matching** — scans offers/sightings against archived requirements; 18 matches ever. Parks with the whole Proactive workspace. Comeback: Proactive revival / Deals-badge wiring.
- **proactive_teams_push** — Teams digest of new proactive matches; its flag already defaults off. Comeback: Proactive revival.
- **quality_score_activities** — AI-scores activity notes every 15 min for the Activity Scorecard, its only consumer. Comeback: team exists.
- **recompute_buyer_scores** — nightly trader scorecards; 6 rows, consumer is the parked trader lane. Comeback: second trader user.
- **site_ownership_sweep** — clears site owners after 30 days idle; meaningless with one operator. Comeback: team exists.

### DELETE — 40 (git history is the archive)

- **ai_tagging** — paid AI part-tagging on a schedule; it did yield (123,840 tags) so it becomes on-demand-only when AI keys are on, per spec.
- **auto_attribute_activities** — AI attribution of activity rows; 9,772 of 12,851 sat unattributed — it never kept up. Flip-able (Decision 2).
- **auto_dedup** — AI-confirmed background merges of CRM records; the manual merge path stays. Flip-able (Decision 3).
- **batch_parse_signatures** — AI email-signature extraction; its output table has 0 rows EVER.
- **bid_due_alerts** — bid-deadline task creator; 2 tasks ever created. Flip-able (Decision 1).
- **cache_cleanup** — trims a database cache table that holds 0 rows (Redis is the real cache).
- **cleanup_usage_log** — monthly trim of the API-usage log; once the health pollers stop, the table stops growing; becomes a one-line on-demand command.
- **contact_dedup** — nightly duplicate-contact merge; 0 duplicate groups exist in today's prod copy.
- **contact_scoring** — writes a relationship score displayed nowhere; 6 contacts total.
- **contact_status_compute** — computes active/quiet/inactive on contacts; no template reads it; it has moved one row ever.
- **contacts_sync** — Outlook contacts import; 0 of 1,332 vendor cards ever came from it, no sync record ever persisted.
- **discover_prospects** — Explorium prospect discovery; 5 monthly runs produced 5 prospects, all untouched. (Explorium machine, 1 of 6.)
- **email_health_update** — scores vendor email deliverability; 0 of 1,332 cards ever scored.
- **email_reverification** — a stub that logs "no provider configured" and exits; every run a no-op.
- **enrich_pool** — Explorium enrichment arm; 5-row pool, zero actioned. (2 of 6.)
- **expire_and_resurface** — Explorium pool lifecycle pass. (3 of 6.)
- **expire_resell_lists** — auto-expires resell lists past close date; the only delete with live yield (3 lists), and closing a list is a one-click manual act in the kept flow.
- **expire_strategic_vendors** — TTL sweep on a table where zero rows have ever existed.
- **find_contacts** — Explorium contact-finding arm. (4 of 6.)
- **flag_stale_offers** — marks offers older than 14 days; display-only metadata, derivable from the date at read time.
- **health_deep** — 2-hourly deep probes of connectors that have no keys; burns real calls where keys exist. Comeback: connector keys go on.
- **health_ping** — 15-minute pings of 17 keyless connectors, accumulating error noise. Comeback: connector keys go on; the keys-off state now shows statically.
- **integrity_check** — 6-hourly self-heal scan; the identical report stays available on demand from the admin endpoint.
- **internal_confidence_boost** — tag-confidence upgrader; 0 rows produced ever. (Zero-yield tagging job 1 of 2.)
- **knowledge_expire_stale** — only counts and logs expired knowledge entries; expiry actually happens at query time.
- **po_verification** — scans your sent mail to detect PO numbers; detection-only by its own docstring. The real per-line PO-verify gate is the interactive approver action, untouched.
- **poll_signature_batch** — polls the signature pipeline that has produced 0 rows ever, every 5 minutes.
- **pool_health_report** — monthly stats logging over the 5-row Explorium pool. (5 of 6.)
- **prefix_backfill** — scheduled part-number prefix tagging; becomes an on-demand command to run when a card import lands.
- **proactive_offer_expiry** — expires "sent" proactive offers; 0 rows exist and the parked workspace can't create more.
- **refresh_scores** — Explorium re-scoring arm; all 5 prospects still "suggested". (6 of 6.)
- **reset_connector_errors** — nightly zeroing of error counters that only keyless probing inflates.
- **reset_monthly_usage** — monthly quota reset for paid connectors that are off. Comeback: connector keys go on.
- **sighting_mining** — brand-tag mining from sightings; 0 tags produced ever. (Zero-yield tagging job 2 of 2.)
- **spec_enrichment** — scheduled spec-sheet structuring; real yield historically, becomes an on-demand command.
- **stock_autocomplete** — auto-completes stock-sale buy plans; zero stock-sale plans have ever existed.
- **sweep_stale_sending_outreach** — unsticks resell outreach stuck in "sending"; ~7 outreach rows ever, 0 ever stuck.
- **teams_call_records_sync** — Teams call-record poller; 0 call rows ever captured despite the watermark advancing. Rebuilds properly inside the Data Capture Initiative if wanted.
- **warn_strategic_expiring** — warns about expiring strategic vendors; zero such vendors have ever existed.
- **webhook_subs** — Graph push-subscription manager; 0 subscriptions ever created (a role filter excluded the only user).

## 3. 48-hour quiet window

**RESULT — GATE PASSED.** The 48 hours ending 2026-08-06 ~23:45 UTC:
the app container logged **17 warnings/errors total**, the enrichment
worker **1**. Every one is either a boot-time notice (missing keys-off
env vars, ENCRYPTION_SALT advisory, connector status summary,
non-canonical-categories advisory — one line each per boot) or a
designed keys-off honest skip (token refresh ×2; "approval email
skipped — no token" ×7, one per approver event exercised by the kernel
walks that ran in the window). **Zero recurring warnings from any
parked or cut feature** — the spec §11 quiet gate. Note: the window
spans the W2 and W3 deploys, so the quiet evidence covers the CURRENT
build — strictly stronger than the W1-only build it was scheduled for.
For contrast, the pre-fix baseline projected ~1,282 warnings/48h from
the two spam sources alone (next paragraph).

Context while it runs: a pre-flight log clustering (docs/evidence/
w1-quiet-preflight.md) found the two spam sources that would have
failed this gate — token refresh retrying forever with Azure creds
blank (~1,150 warnings per 48h) and the worker watchdog alerting on
workers this instance doesn't run (~132 errors). Both got code-level
guards (they now skip with a single honest notice when their
credentials are unset), so the window being measured is the post-fix
build.

## 4. Kernel E2E — green

The nightly script now walks the real kernel against the deployed
parallel instance: requisition → sourcing board → RFQ composer →
offer → quote PDF → Won → buy plan → manager approval → QP sections →
PO number → per-line verify → prepayment, plus the full resell walk
(intake → post → bid → award → outcome). Latest run: **20 checks, 18
passed, 2 honest skips, 0 failures.** A red verdict pages you alone,
with a SIMPLIFICATION marker.

The 2 skips, and why they are honest rather than failures:

1. **Per-line PO verify** — the seeded E2E admin has the
   "can approve purchase orders" toggle OFF, so the walk exercises the
   buy instruction, PO-number entry, and QP purchasing section, then
   skips the final verify click it has no right to make.
2. **Prepayment request + OK-to-pay** — depends on a verified PO line
   from the step above, so it skips downstream.

Decision 5 below turns both into real passes. (Steps that genuinely
need paid keys — AI paste-parse, actual email sends, the accounting
pay-link — are annotated as skipped steps inside otherwise-passing
tests, exactly as the keys-off design intends.)

## 5. Tonight's remaining Wave-1 items

**RESULT — ALL FIVE LANDED** (commit fa00466f and companions, deployed
with the final W1 build):
- **W1.9 dead statuses** — 10 members removed, each with three-way
  evidence (zero prod-copy rows across 33 status columns queried, zero
  writers, no DB constraint encoding it). The kept-with-reasons list
  (INBOUND, RFQS_SENT, HOTLIST, …) fed Wave 3, which has since
  collapsed them — Packet 3 has that story.
- **W1.10** — both dead status-transition tables deleted
  (BUY_PLAN_TRANSITIONS + REQUISITION_TRANSITIONS); live DB tables
  untouched per the drift-gate rule.
- **W1.13 task statuses → open/done** — migration 205, round-tripped
  on throwaway Postgres (todo/in_progress→open; downgrade documented
  many-to-one).
- **W1.17** — logging a manual resell outreach no longer demands a
  fresh M365 token (email-only token acquisition).
- **W1.18** — quote Won/Lost buttons now render on draft quotes
  (draft→won was already legal server-side; only the UI insisted on
  "sent").

What those five are, in plain terms: removal of dead status values
(with per-status zero-use evidence from the prod copy); deletion of the
2 dead status-transition tables; task statuses collapsed to open/done;
a keys-off fix so logging a manual resell outreach no longer demands a
fresh M365 token; and showing the quote Won/Lost buttons on draft
quotes too (the server already accepts draft→won — only the UI
insisted on "sent," which needs Graph).

## 6. Cutover runbook — v0 exists

docs/CUTOVER.md v0 is written and improves every wave. In one
paragraph: cutover happens only on your explicit word, after the
real-money launch deal completes clean. It takes a fresh production
backup as the rollback anchor, merges the branch and runs the normal
deploy script (which migrates the database and auto-rolls-back on
failed health checks), then verifies health, migration head, and a
kernel smoke. The migration path is the exact one every wave-start
database refresh rehearses — by launch it will have been rehearsed
four times. Rollback commands are written for both app and schema.
Production-touching cleanups (retiring the legacy host-cron backup in
favor of the one backup container, re-pointing the backup-freshness
probe, installing the backup verify-timers) are deliberately deferred
to cutover day and listed there. SECRET_KEY must not change at
cutover — encrypted columns are keyed to it, and the app refuses to
boot on a mismatch (verified when the parallel instance was stood up).

## 7. DECISIONS — one word flips any of them

Everything below was executed as recommended (or, for 6–14, is your
final confirmation of the already-reviewed spec). To flip one, reply
with just its flip word. Silence = the recommendation stands.

**1. bid_due_alerts deleted.** Recommended: leave deleted — 2 tasks
ever created, not on the kernel list. Flip word: **BIDALERTS**
(restores it as a follow-up clock).

**2. auto_attribute_activities deleted.** Recommended: leave deleted —
AI attribution that left 9,772 of 12,851 rows unattributed; solo you
attribute at log time. Flip word: **ATTRIBUTE** (parks it behind the
AI flag instead).

**3. auto_dedup deleted.** Recommended: leave deleted — AI-confirmed
background merges of CRM records; the manual merge path stays. Flip
word: **DEDUP** (parks it behind the AI flag instead).

**4. Inbox-scan mining side-ops gated off.** The reply scan itself
stays — it is the kernel. But its email-mining side-harvest (the
source of 1,312 auto-created vendor cards) is now flag-gated off,
destined for the Data Capture Initiative rebuild. Recommended: keep
off. Flip word: **MINING** (re-enables the side-harvest now).

**5. Grant the E2E admin the purchase-order approval toggle.**
Recommended: turn it ON — one existing per-user toggle on the parallel
instance's seeded admin, letting the nightly walk exercise the
PO-verify and prepayment gates end to end and converting the 2 honest
skips into passes. Flip word: **POTOGGLE** (leaves it off; the 2 skips
remain).

The nine spec-v1.1 final-read confirmations (each was already resolved
in your 08-03 review; this is the final read the spec asks for):

**6. Auto-approve removal** — every deal goes through the one-click
manager approval; the old sub-$5K auto-approve is gone. Recommended:
confirm. Flip word: **AUTOAPPROVE**.

**7. Resell status names** — 34 statuses collapse to DRAFT → POSTED →
BIDDING → AWARDED → CLOSED plus an outcome field on close
(sold / scrapped / withdrawn / no-bids). Drafted from your flow;
rename freely. Recommended: confirm. Flip word: **RESELLNAMES**
(reply it with your preferred names).

**8. Data Capture Initiative** — the tightest Outlook email + 8x8 call
capture AVAIL can get, rebuilt properly as the FIRST post-launch
build. Recommended: confirm it matches your intent. Flip word:
**DATACAPTURE**.

**9. Proactive parks whole** — workspace, matching engine, AND badge
park together behind the existing flag. Recommended: confirm. Flip
word: **PROACTIVE** (keeps the engine running headless instead).

**10. Kernel walk** — the §3 script (section 4 above) matches how you
actually run a deal. Recommended: confirm. Flip word: **KERNEL**
(reply it with what's wrong).

**11. Deals merge (Wave 4)** — one deal editor with a sales/sourcing
lens toggle; the split-panel duplicate edit endpoints get deleted.
Recommended: confirm. Flip word: **DEALSMERGE**.

**12. CRM parks** — org-scale trimmings (saved views, segment tags,
custom fields, collaborators, scorecard, pool governance) park until a
team exists. Recommended: confirm. Flip word: **CRMPARKS**.

**13. Resell solo-mode** — the internal-trader offer lane parks until
a second trader exists. Recommended: confirm. Flip word: **TRADERLANE**.

**14. The delete lists** — anything in section 2's 40 deletes you want
parked instead of deleted? Recommended: no changes. Flip: reply the
**job name** and it moves delete → park.

## 8. Deviation log — verbatim from STATE.md

(W-numbers are Wave-1 checklist items in docs/STATE.md; §-numbers are
spec sections.)

- 2026-08-04 S1 (RESOLVED same day): Session started on spec v1.0 (only version on disk); owner supplied v1.1 mid-session. docs/SIMPLIFICATION_SPEC.md replaced with v1.1 verbatim; deltas folded in: 8x8 poll delete→park (Data Capture Initiative), Proactive parks whole, task statuses + trouble-ticket gating added to Wave 1 (W1.13/W1.14), Settings gear outside a literal 5-tab bar, resell 34→5 named statuses with Wave-3 remap. Disposition table reconciled (keep 7 / park 12 / delete 40).
- 2026-08-04 S1: Login visual-regression baseline was stale on main (snapshot from the original frontend-testing commit; login.html restyled twice since — password form + light theme are deliberate). Re-baselined on this branch after image inspection; note: PRODUCTION's own nightly is red on this same test and stays red until main gets the same re-baseline (owner's call — outside this branch's scope).
- 2026-08-04 S1: Nightly E2E v0 = deployed-instance smoke + branch Playwright suite (with Vite build step — missing dist/ renders every page unstyled). The deployed-app kernel-walk script replaces the smoke in W1.11 per spec §10.
- 2026-08-04 S1: Host-level cron jobs (backup_postgres.sh 6-hourly, daily_coverage_report.sh, check_fru_matrix_refresh.sh, weekly_cleanup.sh, nightly_tests.sh) operate on PRODUCTION; spec §6 removals of host-side duplicates are deferred to cutover and recorded in CUTOVER.md instead of being executed during Wave 1. In-app scheduler shrink proceeds normally on the branch.
- 2026-08-04 W2-prep: measured orphan inventory re-baselines two spec §8 estimates — orphaned /api routes = 107 firm (+35 keep-ambiguous; spec said "111+"), pinned test files = 80 (spec said "~280"). Full route-by-route list in docs/evidence/w2-api-orphans.md; POST /api/requisitions (the legacy JSON create) measured keep-ambiguous but stays on the W2 delete list because the spec names it explicitly.
- 2026-08-04 W1: simp-DB worker singletons (ics/nc/tbf_worker_status.is_running) flipped false on the copy so the watchdog stops firing pre-W1.16; the durable fix is W1.16 since every wave-start refresh re-imports prod's true.
- 2026-08-04 W1: rule 3.5 relaxed for the shrink batch — W1.1–W1.16 landed as 14 module-scoped commits (not one per checklist item): parallel agent execution interleaved items inside shared files; each commit message enumerates its items, so packet assembly and surgical rollback hold at module granularity.
- 2026-08-04 W1: suite gate — 23,740 passed / 19 failed on first full run; all 19 root-caused and fixed same-session (14 = W1.15 guard needed Azure-configured test fixtures; 3 = tests pinned the pre-fix CRM-enrich 503; 2 = tests pinned the six-poller nav markup). Zero failures on the affected files after fix; confirmation full-suite run recorded in the packet.

— End of Packet 1. Reply with flip words (any, in one message) or
"all stands." Nothing else in Wave 1 needs you.
