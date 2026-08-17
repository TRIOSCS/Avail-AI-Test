# AVAIL Simplification Spec — v1.0 (for review)

Date: 2026-08-03. Supersedes the working brief (same-day). Built on
two verified audits: product/workflow (9 agents, every journey
mapped click-by-click) and code (13 agents, 62 findings). Every
decision below is RESOLVED with a rationale; flip any during review.
Status: AWAITING OWNER REVIEW. Nothing executes until approved.

## 1. Purpose

Shrink AVAIL to a product one person can keep green and credibly
launch: fewer tabs, fewer steps, fewer statuses, fewer features —
sized to today's reality (not live, one user, paid APIs off) with
named comeback triggers for everything sized for the future.

## 2. Ground rules (unchanged by this spec)

- Acctivate stays manual; AVAIL never owns SO/PO lifecycle.
- ERP-neutral naming everywhere (Dynamics 365 BC in 2027).
- Three approval gates survive untouched: deal approval (one any-of
  manager step), per-line PO verify, prepayment OK-to-pay + pay link.
- Audit logging on every change stays.
- HTMX + Alpine, routers-thin/services-fat conventions stay.
- Never drop raw-SQL tables (drift-gate rule); grandfather instead.

## 3. The Launch Kernel (Decision A — DECIDED)

One deal, end to end, at $0 in API keys. This list is the keep-list
AND the nightly-E2E script.

Sell/buy walk:
1. Create requisition (manual lines; paste-parse returns when AI on)
2. Work the req's sourcing board; pick vendors
3. Compose + send RFQ from own M365 mailbox
4. Inbox scan matches replies → Responses tab → create offers
   (+ one manual Add-offer door)
5. Build Quote tab → assemble (margin guardrail) → send PDF
6. Mark quote Won → Build Buy Plan
7. Approvals workspace: edit lines → submit → manager approves
8. QP sales + purchasing sections completed in the workspace
9. (When needed) prepayment request → accounting pay link → confirm
10. Buy instruction to buyer → enter PO number → per-line verify

Resell walk: excess intake → post lines → outreach → log broker
bids → bid-back PDF → award → outcome.

Support surfaces: CRM spine (companies/sites, customer contacts +
DNC, vendor cards/contacts, activity log + outcome chips), My-Day
tasks, badges.

Kernel background jobs (the ONLY scheduled jobs that must stay):
inbox reply scan, approval outbox email, cadence/follow-up clocks,
db-backup container, nightly E2E, worker liveness. Everything else
is parked or deleted per §6.

Rationale: this is exactly the surface that already works with keys
off, so the kernel is fully testable pre-launch at no cost.

## 4. Target navigation (Decision B — DECIDED): 10 tabs → 5

1. **Deals** — Sales Hub + Sightings + Search merged (§5.1)
2. **Approvals** — one pipeline, tabs merged, QP absorbed (§5.2)
3. **Resell** — owner's core loop (§5.3)
4. **CRM** — load-bearing spine + prospect intake (§5.4)
5. **Tasks (My-Day)** — the single "what do I work now" surface

Plus Settings (slimmed, §5.5). Topbar global search stays; its
AI-intent mode gates behind the AI flag with an honest "AI is off"
state (§7).

Removed from nav: Materials (becomes a lookup opened from Deals/CRM
context), Search (folded into Deals), Sightings (folded into Deals),
Proactive (workspace parked; its matching engine + badge live inside
Deals), Prospecting (folded into CRM as a lens).

## 5. Target state per workflow

### 5.1 Deals (Decision C — DECIDED: merge, Wave 4, conditions met)

One deal spine — requisition lines with their sightings, RFQ
actions, and offers inline — with a **lens toggle**:
- *Sales lens*: intake, status, quote columns, Build Quote.
- *Sourcing lens*: vendor coverage, RFQ compose, offer intake.

Consolidations this locks in:
- ONE deal editor: the requisition detail page. The split-panel
  parts workspace becomes read-only triage (status, best price,
  "Open deal"); its 7 duplicate tab endpoints are deleted.
- ONE RFQ composer (currently two).
- TWO offer doors (Responses tab + one Add-offer modal with an
  optional paste box). Deleted: the two AI paste modals and the
  standalone /v2/offers/review-queue page; flagged AI offers become
  a filter inside Responses.
- Requisition pipeline status becomes derived, not a stored 9-state
  ladder; the legacy JSON create endpoint (divergent DRAFT) is cut.
- Search's part-dossier opens from a deal line (same fan-out code,
  one door). Connector fan-out stays code-complete and keyless —
  it lights up when keys go on; no work needed for that comeback.

Sequencing condition: the sightings surface is slimmed and split
(Wave 3/4) BEFORE the merge lands.

### 5.2 Approvals (Decision D — DECIDED)

- Merge the Sales Orders + Buy Plans tabs (verified identical rows/
  pane) into one **Deals** tab inside the workspace. Tabs become:
  Deals / Purchase Orders / Prepayments.
- Absorb the standalone QP page: sales + purchasing sections are
  edited only in the workspace panes (single lock matrix). The two
  self-stamped QP review gates are dropped; gate count goes from
  five ceremonies to the three real ones (§2).
- Finish the one-screen fold: Submit, draft line edits, and
  Request-prepayment all live in the workspace panes.
- Cut: the parallel JSON approval-request API; per-tab CSV export.
- Plan lifecycle collapses 7 statuses → 5 (drop INBOUND and the
  never-fired EXPIRED path).
- **QP serial/FRU (Decision E — DECIDED):** relink now. The section
  is currently stranded behind the retired Deal view. Wave 2 adds
  one link from the workspace QP pane to the existing serial/FRU
  page (small, restores reachability); full absorption into the
  workspace happens only if receiving actually uses it after launch
  (comeback trigger: first live TSO with serial tracking).

### 5.3 Resell (Decision F — DECIDED: solo-operator mode)

Keep: intake → anonymized posting → outreach tracker → log bids
(two entry paths, not four) → bid-back PDF → award → outcome. Keep
per-line vs take-all on the bid itself; drop its triage machinery.

Park (comeback trigger = second trader user exists): the internal-
trader offer lane ("Open to Me" lens, Submit Offer modal), the
buyer-intelligence layer (BuyerScore, ranked suggestions, nudge,
auto My-Day tasks, nightly recompute jobs).

Cut: the ~4 verified-dead statuses in the 34-status inventory;
collapse the remainder per audit evidence. Stop maintaining the
resell→Sighting mirror dual-write while nothing reads it (the
matcher reads it only for trader-matching, which is parked); the
mirror returns with the trader lane.

### 5.4 CRM (Decision G — DECIDED: spine only)

Keep: companies/sites, customer contacts (quote + DNC), vendor
cards + contacts (RFQ targets), activity log + click-to-call outcome
chips, cadence clocks, My-Day as the ONE work queue (the other two
"what do I work now" surfaces fold into it). Prospecting becomes a
CRM lens: manual prospect intake + free enrichment (SAM.gov +
Google News) + warm intros + proactive matching badge.

Cut: contact-intelligence layer (computed, displayed nowhere),
orphaned Email Intelligence dashboard, dead connector rows in
Settings, per-user 8x8 toggle.

Park (comeback = team exists): vendor dossier tabs beyond 5, saved
views, segment tags, custom fields, collaborators, Activity
Scorecard, multi-rep pool governance (claim caps, assign modal,
manager digests), standalone cross-company contact list pages.

Task statuses trim to open/done.

### 5.5 Settings, admin, platform

- Connectors page lists only connectors that exist; each shows
  keys-off state honestly.
- Badge system: consolidate 6 pollers → one badge endpoint.
- Notification delivery: exactly ONE system per event (in-app rows
  are currently write-only — delete the dead table + writers, keep
  the email path via approval outbox).
- Multi-user admin (invites, roles, approver toggles, module access
  panels) STAYS — it is small, working, and launch needs it.
- Trouble-ticket flow stays but its three hidden Anthropic calls
  gate behind the AI flag (regex fallback).

## 6. Automation & jobs (Decision J folded in — DECIDED)

Scheduled jobs shrink to the kernel list (§3). Specifics:
- 8x8 CDR polling: CUT (zero CDRs ever; click-to-call + outcome
  chips already capture the workflow). Comeback = actually routing
  calls through 8x8 in production.
- Email pipelines: KEEP the reply-scan pipeline (feeds Responses —
  kernel). email_mining stays off; its orphaned dashboard is cut.
- Tagging suite: the two zero-yield jobs are deleted; prefix/spec
  jobs become on-demand management commands; AI tagging runs only
  on-demand when AI keys are on.
- Explorium discovery machine (6 monthly jobs): DELETE (git is the
  archive; rebuild is cheap if a contract ever exists).
- Backups: ONE system — the db-backup container (retention +
  checksums + off-site). Host cron scripts deleted; /health
  freshness probe re-pointed; verify-timer units either installed
  or deleted (DECIDED: installed — it is the alarm on the one
  backup system that remains).
- Nightly E2E: kernel walk (§3) becomes the script; failure pages
  ONE admin, not three.

## 7. Keys-off honesty (Decision I — DECIDED, Wave 1)

Every AI/connector-dependent control shows a real state instead of
failing: BOM "Parse with AI" says "AI is off — enter lines or paste
when enabled" (no 500); search results state "external sources off —
showing local data"; CRM Enrich runs its free SAM.gov path (fix the
503 guard) and labels paid providers as off; AI search intent
falls back to plain search with a notice.

## 8. Park vs delete mechanics (Decision H — DECIDED)

- DELETE (git restores): anything unreachable, write-only, dead, or
  duplicated — the §5/§6 cut lists, the 111+ orphaned /api routes,
  their ~280 pinned test files, Sourcing Leads workspace, Dashboard
  + Knowledge pages, backfill graveyard (app/management one-shots),
  startup.py's completed backfills, dead schemas/statuses.
- PARK behind existing flags, comeback trigger named in §5: trader
  lane, buyer intelligence, Proactive workspace, org-scale CRM
  trimmings, email_mining. No new flag frameworks get built.
- Tables are never dropped; ORM models may go only per the
  drift-gate grandfather rule.

## 9. Code work the product decisions don't cover (from code audit)

- One offer_service (three drifted copies; reconfirm semantics =
  the TTL-resetting variant everywhere).
- One requirement-creation pipeline (UI path gains dup detection,
  normalization, task auto-gen).
- One quote builder (the tab; modal deleted; multi-req entry routes
  into the tab flow).
- One state machine per entity: single transition() for BuyPlan +
  ApprovalRequest (delete legacy fallback after prod-data check);
  delete the retired SO-verification track (fixes the halt→resume
  deadlock); route the 3 raw status writes through the enforced
  transition table; delete the 2 dead transition tables.
- Split the regression factories (each mechanical, re-export
  pattern already proven on htmx_views.py): sightings.py (3,812
  lines — prerequisite for the Deals merge), htmx_app.js (3,654),
  search_service.py (3,604). Kill scoring generations v1/v3/v4;
  persisted v2 is the one truth, display reads it.
- hx-disinherit fix on the page-level target (kills the recurring
  page-wipe class; needs the audit pass + headless check first).
- Tests (Decision K — DECIDED): diff coverage replaces the 85%
  global gate (same PR as the coverage-farm delete); shared test
  engine moves to Postgres (SQLite stays as explicit local
  fallback); test_static_analysis.py keeps its ~8 bug-class guards,
  loses line-keyed allowlists and style ratchets; every deleted
  route takes its tests with it.
- Defer indefinitely (explicitly OUT): Alembic squash (252
  migrations — real cutover risk, no current pain), any Acctivate
  integration, any new feature work until Wave 4 lands.

## 10. Execution waves

Each wave = one or more PRs, suite green + nightly E2E pass before
the next. Estimates are sessions, not promises.

**Wave 0 — DONE (08-03 eve):** acute fixes shipped @56f213a5
(Teams DM, contacts-sync, connector cooldown, Playwright, backup
symlink, ownership link).

**Wave 1 — quiet + honest (1-2 sessions):**
keys-off honesty states (§7); job shrink to kernel list + tagging/
Explorium/8x8 removals (§6); one backup system + verify timer;
badge consolidation; dead-status + dead-transition-table cleanup;
E2E script = kernel walk, single-admin paging.
Acceptance: scheduler runs only kernel jobs; zero recurring
warnings in a 48h log window; kernel E2E green.

**Wave 2 — the delete/park sweep (2-3 sessions):**
nav 10→5 (§4); park lanes flagged off (§5); delete: orphaned /api
routes + tests, coverage farm, Sourcing Leads, Dashboard/Knowledge,
Email-Intelligence dashboard, contact-intelligence, backfill
graveyard, startup.py backfills → alembic, write-only Notification
table; QP serial/FRU relink; diff-coverage gate lands with the
farm delete.
Acceptance: nav shows 5 tabs; deleted surfaces 404; app boots
clean on fresh DB (drift gate green); kernel E2E green.

**Wave 3 — one implementation per behavior (2-3 sessions):**
offer_service; requirement pipeline; quote builder; RFQ composer;
offer doors 5→2; QP single lock matrix (workspace); notification
single-path; BuyPlan/ApprovalRequest single transition() + legacy
fallback removal; resell status collapse + bid paths 4→2.
Acceptance: behavior parity on the kernel walk; the named canonical
semantics (reconfirm TTL, UI dup detection) verified by test.

**Wave 4 — merges + splits, one per session (3-4 sessions):**
split sightings.py → slim board; THEN the Deals merge (lens toggle);
Approvals tab merge + QP absorption + one-screen fold; htmx_app.js
split; hx-disinherit fix; Postgres test engine; search_service
decomposition + scoring v2-only.
Acceptance: 5-tab app fully walkable per §3 script; no router over
~800 lines (new static-analysis guard); kernel E2E green on
Postgres-backed suite.

## 11. Global acceptance (definition of done)

- The §3 kernel walk passes nightly, headless, on the deployed app.
- Scheduler job list == §3 kernel list exactly.
- Nav == §4 exactly; no orphaned links or badges.
- Fresh-DB boot green (migrations + drift gate), no startup
  backfills.
- 48h of logs contain no recurring warnings from parked/cut
  features.
- Every parked feature has its comeback trigger written in this
  spec; every cut is recoverable from git history.

## 12. Review checklist for the owner

Flip any of these and the spec absorbs it cleanly:
- [ ] Kernel walk (§3) matches how you actually run a deal?
- [ ] 5-tab nav (§4) — anything you'd keep as its own tab?
- [ ] QP self-stamp gates really droppable (§5.2)?
- [ ] Resell solo-mode (§5.3) — trader lane park OK?
- [ ] 8x8 cut / email_mining stays-off (§6) — matches your intent?
- [ ] Anything on the delete lists you want parked instead?
