# AVAIL Simplification Spec — v1.1 (for final read)

Date: 2026-08-03. Supersedes v1.0 (same day). Built on two verified audits: product/workflow (9 agents, every journey mapped click-by-click) and code (13 agents, 62 findings). v1.0's decisions carry forward; §0 lists exactly what changed in owner review. Status: AWAITING OWNER APPROVAL. Nothing executes until approved.

## 0. Changes v1.0 → v1.1 (owner review, 2026-08-03)

1. **Auto-approve removed.** The sub-$5K auto-approve rule is dropped. Every deal takes the one-click manager approval (§2; branch deleted in Wave 3).
2. **Resell statuses resolved.** 34 → 5, named from the kept workflow (§5.3); remap table ships in the Wave 3 migration.
3. **8x8 + email reframed.** The dead CDR poller still leaves the scheduler, but 8x8 call data + Outlook email mining are now the named FIRST post-launch build — the Data Capture Initiative (§6) — parked, not deleted.
4. **Settings placement.** Gear icon outside the tab bar; the tab bar shows exactly 5 tabs, so acceptance stays literal (§4, §11).
5. **Proactive parks whole.** Workspace + matching engine + badge behind the existing flag, one comeback trigger; the CRM prospecting lens drops the badge (§4, §5.4).
6. **Four unwaved items slotted.** Task statuses → Wave 1; trouble-ticket AI gating → Wave 1; legacy JSON create endpoint → Wave 2; derived requisition status → Wave 3 (§10).
7. **Mechanical fixes.** Sightings split pinned to Wave 4 pre-merge (§5.1 and §10 now agree); both status collapses ship remap tables for existing rows; the retired SO-verification track is explicitly distinguished from the per-line PO verify gate (§9); §8 list formatting fixed; §12 checklist now covers the Deals merge and CRM parks.

## 1. Purpose

Shrink AVAIL to a product one person can keep green and credibly launch: fewer tabs, fewer steps, fewer statuses, fewer features — sized to today's reality (not live, one user, paid APIs off) with named comeback triggers for everything sized for the future.

## 2. Ground rules (unchanged by this spec)

- Acctivate stays manual; AVAIL never owns SO/PO lifecycle.
- ERP-neutral naming everywhere (Dynamics 365 BC in 2027).
- Three approval gates survive untouched: **deal approval (one any-of manager step — every deal; the former sub-$5K auto-approve is removed)**, per-line PO verify, prepayment OK-to-pay + pay link.
- Audit logging on every change stays.
- HTMX + Alpine, routers-thin/services-fat conventions stay.
- Never drop raw-SQL tables (drift-gate rule); grandfather instead.

## 3. The Launch Kernel (Decision A — DECIDED)

One deal, end to end, at $0 in API keys. This list is the keep-list AND the nightly-E2E script.

**Sell/buy walk:**

1. Create requisition (manual lines; paste-parse returns when AI on)
2. Work the req's sourcing board; pick vendors
3. Compose + send RFQ from own M365 mailbox
4. Inbox scan matches replies → Responses tab → create offers (+ one manual Add-offer door)
5. Build Quote tab → assemble (margin guardrail) → send PDF
6. Mark quote Won → Build Buy Plan
7. Approvals workspace: edit lines → submit → manager approves
8. QP sales + purchasing sections completed in the workspace
9. (When needed) prepayment request → accounting pay link → confirm
10. Buy instruction to buyer → enter PO number → per-line verify

**Resell walk:** excess intake → post lines → outreach → log broker bids → bid-back PDF → award → outcome.

**Support surfaces:** CRM spine (companies/sites, customer contacts + DNC, vendor cards/contacts, activity log + outcome chips), My-Day tasks, badges.

**Kernel background jobs (the ONLY scheduled jobs that must stay):** inbox reply scan, approval outbox email, cadence/follow-up clocks, db-backup container, nightly E2E, worker liveness. Everything else is parked or deleted per §6.

Rationale: this is exactly the surface that already works with keys off, so the kernel is fully testable pre-launch at no cost.

## 4. Target navigation (Decision B — DECIDED): 10 tabs → 5

1. **Deals** — Sales Hub + Sightings + Search merged (§5.1)
2. **Approvals** — one pipeline, tabs merged, QP absorbed (§5.2)
3. **Resell** — owner's core loop (§5.3)
4. **CRM** — load-bearing spine + prospect intake (§5.4)
5. **Tasks (My-Day)** — the single "what do I work now" surface

**Settings moves behind a gear icon outside the tab bar** (slimmed, §5.5) — the tab bar shows exactly 5 tabs. Topbar global search stays; its AI-intent mode gates behind the AI flag with an honest "AI is off" state (§7).

Removed from nav: Materials (becomes a lookup opened from Deals/CRM context), Search (folded into Deals), Sightings (folded into Deals), **Proactive (parks whole — workspace, matching engine, and badge — behind its existing flag; §5.4, §8)**, Prospecting (folded into CRM as a lens).

## 5. Target state per workflow

### 5.1 Deals (Decision C — DECIDED: merge, Wave 4, conditions met)

One deal spine — requisition lines with their sightings, RFQ actions, and offers inline — with a lens toggle:

- **Sales lens:** intake, status, quote columns, Build Quote.
- **Sourcing lens:** vendor coverage, RFQ compose, offer intake.

Consolidations this locks in:

- ONE deal editor: the requisition detail page. The split-panel parts workspace becomes read-only triage (status, best price, "Open deal"); its 7 duplicate tab endpoints are deleted.
- ONE RFQ composer (currently two).
- TWO offer doors (Responses tab + one Add-offer modal with an optional paste box). Deleted: the two AI paste modals and the standalone /v2/offers/review-queue page; flagged AI offers become a filter inside Responses.
- Requisition pipeline status becomes **derived**, not a stored 9-state ladder (lands Wave 3, before the merge); the legacy JSON create endpoint (divergent DRAFT) is cut in the Wave 2 sweep.
- Search's part-dossier opens from a deal line (same fan-out code, one door). Connector fan-out stays code-complete and keyless — it lights up when keys go on; no work needed for that comeback.

Sequencing condition: the sightings surface is slimmed and split in **Wave 4, immediately before the merge** (§10).

### 5.2 Approvals (Decision D — DECIDED)

- Merge the Sales Orders + Buy Plans tabs (verified identical rows/pane) into one **Deals** tab inside the workspace. Tabs become: Deals / Purchase Orders / Prepayments.
- Absorb the standalone QP page: sales + purchasing sections are edited only in the workspace panes (single lock matrix). The two self-stamped QP review gates are dropped; gate count goes from five ceremonies to the three real ones (§2).
- Finish the one-screen fold: Submit, draft line edits, and Request-prepayment all live in the workspace panes.
- **Auto-approve branch deleted (Wave 3, with the transition() consolidation): every deal routes through the manager approval step.**
- Cut: the parallel JSON approval-request API; per-tab CSV export.
- Plan lifecycle collapses 7 statuses → 5 (drop INBOUND and the never-fired EXPIRED path). **The Wave 3 migration ships a remap table for existing rows.**
- **QP serial/FRU (Decision E — DECIDED): relink now.** The section is currently stranded behind the retired Deal view. Wave 2 adds one link from the workspace QP pane to the existing serial/FRU page (small, restores reachability); full absorption into the workspace happens only if receiving actually uses it after launch (comeback trigger: first live TSO with serial tracking).

### 5.3 Resell (Decision F — DECIDED: solo-operator mode)

Keep: intake → anonymized posting → outreach tracker → log bids (two entry paths, not four) → bid-back PDF → award → outcome. Keep per-line vs take-all on the bid itself; drop its triage machinery.

**Status collapse (RESOLVED): 34 → 5, named from the kept flow above:**

`DRAFT → POSTED → BIDDING → AWARDED → CLOSED`

with an outcome field on close (sold / scrapped / withdrawn / no-bids). The ~4 verified-dead statuses go first; the **Wave 3 migration ships the remap table** for the remainder.

Park (comeback trigger = second trader user exists): the internal-trader offer lane ("Open to Me" lens, Submit Offer modal), the buyer-intelligence layer (BuyerScore, ranked suggestions, nudge, auto My-Day tasks, nightly recompute jobs).

Stop maintaining the resell→Sighting mirror dual-write while nothing reads it. **The mirror returns with whichever unparks first: the trader lane or Proactive matching.**

### 5.4 CRM (Decision G — DECIDED: spine only)

Keep: companies/sites, customer contacts (quote + DNC), vendor cards + contacts (RFQ targets), activity log + click-to-call outcome chips, cadence clocks, My-Day as the ONE work queue (the other two "what do I work now" surfaces fold into it). Prospecting becomes a CRM lens: **manual prospect intake + free enrichment (SAM.gov + Google News) + warm intros.** (The proactive matching badge parks with the Proactive workspace — §4, §8.)

Cut: contact-intelligence layer (computed, displayed nowhere), orphaned Email Intelligence dashboard (write-only, linked nowhere — the Data Capture Initiative in §6 rebuilds proper surfaces), dead connector rows in Settings, per-user 8x8 toggle.

Park (comeback = team exists): vendor dossier tabs beyond 5, saved views, segment tags, custom fields, collaborators, Activity Scorecard, multi-rep pool governance (claim caps, assign modal, manager digests), standalone cross-company contact list pages.

Task statuses trim to **open/done (Wave 1)**.

### 5.5 Settings, admin, platform

- **Settings lives behind a gear icon outside the tab bar (§4).**
- Connectors page lists only connectors that exist; each shows keys-off state honestly.
- Badge system: consolidate 6 pollers → one badge endpoint.
- Notification delivery: exactly ONE system per event (in-app rows are currently write-only — delete the dead table + writers, keep the email path via approval outbox).
- Multi-user admin (invites, roles, approver toggles, module access panels) STAYS — it is small, working, and launch needs it.
- Trouble-ticket flow stays but its three hidden Anthropic calls **gate behind the AI flag in Wave 1** (rides the keys-off honesty pass; regex fallback).

## 6. Automation & jobs (Decision J folded in — DECIDED)

Scheduled jobs shrink to the kernel list (§3). Specifics:

- **8x8 CDR polling: PARKED, not deleted.** The poller leaves the scheduler now (zero CDRs ever captured — it needs a working integration pass, not a restart). It moves into the Data Capture Initiative below. Click-to-call + outcome chips keep capturing the workflow in the meantime.
- **Email pipelines:** KEEP the reply-scan pipeline (feeds Responses — kernel). email_mining stays off and moves into the Data Capture Initiative; its orphaned dashboard is still cut (§5.4).
- **Data Capture Initiative (named comeback — the FIRST post-launch build):** the owner's standing intent is the tightest Outlook (email) and 8x8 (call) data capture AVAIL can get. Both pipelines return together as the first post-launch initiative, rebuilt properly with real display surfaces. Trigger: kernel E2E green in production + launch declared.
- Tagging suite: the two zero-yield jobs are deleted; prefix/spec jobs become on-demand management commands; AI tagging runs only on-demand when AI keys are on.
- Explorium discovery machine (6 monthly jobs): DELETE (git is the archive; rebuild is cheap if a contract ever exists).
- Backups: ONE system — the db-backup container (retention + checksums + off-site). Host cron scripts deleted; /health freshness probe re-pointed; **verify-timer units get installed** — the alarm on the one backup system that remains.
- Nightly E2E: kernel walk (§3) becomes the script; failure pages ONE admin, not three.

## 7. Keys-off honesty (Decision I — DECIDED, Wave 1)

Every AI/connector-dependent control shows a real state instead of failing: BOM "Parse with AI" says "AI is off — enter lines or paste when enabled" (no 500); search results state "external sources off — showing local data"; CRM Enrich runs its free SAM.gov path (fix the 503 guard) and labels paid providers as off; AI search intent falls back to plain search with a notice.

## 8. Park vs delete mechanics (Decision H — DECIDED)

**DELETE (git restores):** anything unreachable, write-only, dead, or duplicated — the §5/§6 cut lists, the 111+ orphaned /api routes, their ~280 pinned test files, Sourcing Leads workspace, Dashboard/Knowledge pages, backfill graveyard (app/management one-shots), startup.py's completed backfills, dead schemas/statuses.

**PARK behind existing flags, comeback trigger named in §5/§6:** trader lane, buyer intelligence, **Proactive (workspace + matching engine + badge, as one unit)**, org-scale CRM trimmings, **Data Capture Initiative (email_mining + 8x8 CDR)**. No new flag frameworks get built.

Tables are never dropped; ORM models may go only per the drift-gate grandfather rule.

## 9. Code work the product decisions don't cover (from code audit)

- One offer_service (three drifted copies; reconfirm semantics = the TTL-resetting variant everywhere).
- One requirement-creation pipeline (UI path gains dup detection, normalization, task auto-gen).
- One quote builder (the tab; modal deleted; multi-req entry routes into the tab flow).
- One state machine per entity: single transition() for BuyPlan + ApprovalRequest (delete legacy fallback after prod-data check, **and delete the auto-approve branch — §2**); delete the retired **sales-order verification track** (fixes the halt→resume deadlock) — **distinct from §2's per-line PO verify gate, which is untouched**; route the 3 raw status writes through the enforced transition table; delete the 2 dead transition tables.
- Split the regression factories (each mechanical, re-export pattern already proven on htmx_views.py): sightings.py (3,812 lines — prerequisite for the Deals merge), htmx_app.js (3,654), search_service.py (3,604). Kill scoring generations v1/v3/v4; persisted v2 is the one truth, display reads it.
- hx-disinherit fix on the page-level target (kills the recurring page-wipe class; needs the audit pass + headless check first).
- **Tests (Decision K — DECIDED):** diff coverage replaces the 85% global gate (same PR as the coverage-farm delete); shared test engine moves to Postgres (SQLite stays as explicit local fallback); test_static_analysis.py keeps its ~8 bug-class guards, loses line-keyed allowlists and style ratchets; every deleted route takes its tests with it.
- **Defer indefinitely (explicitly OUT):** Alembic squash (252 migrations — real cutover risk, no current pain), any Acctivate integration, any new feature work until Wave 4 lands.

## 10. Execution waves

Each wave = one or more PRs, suite green + nightly E2E pass before the next. Estimates are sessions, not promises.

**Wave 0 — DONE (08-03 eve):** acute fixes shipped @56f213a5 (Teams DM, contacts-sync, connector cooldown, Playwright, backup symlink, ownership link).

**Wave 1 — quiet + honest (1-2 sessions):** keys-off honesty states (§7) including trouble-ticket AI gating; job shrink to kernel list + tagging/Explorium removals + 8x8 poller off (§6); one backup system + verify timer; badge consolidation; dead-status + dead-transition-table cleanup; **task statuses → open/done**; E2E script = kernel walk, single-admin paging. *Acceptance: scheduler runs only kernel jobs; zero recurring warnings in a 48h log window; kernel E2E green.*

**Wave 2 — the delete/park sweep (2-3 sessions):** nav 10→5 with Settings gear (§4); park lanes flagged off — trader lane, buyer intelligence, **Proactive as one unit** (§5); delete: orphaned /api routes + tests **including the legacy JSON create endpoint**, coverage farm, Sourcing Leads, Dashboard/Knowledge, Email-Intelligence dashboard, contact-intelligence, backfill graveyard, startup.py backfills → alembic, write-only Notification table; QP serial/FRU relink; diff-coverage gate lands with the farm delete. *Acceptance: tab bar shows exactly 5 tabs + gear; deleted surfaces 404; app boots clean on fresh DB (drift gate green); kernel E2E green.*

**Wave 3 — one implementation per behavior (2-3 sessions):** offer_service; requirement pipeline; **derived requisition status (replaces the stored 9-state ladder)**; quote builder; RFQ composer; offer doors 5→2; QP single lock matrix (workspace); notification single-path; BuyPlan/ApprovalRequest single transition() + legacy fallback removal + **auto-approve branch deletion**; resell status collapse 34→5 + bid paths 4→2; **both status remap tables ship with their migrations**. *Acceptance: behavior parity on the kernel walk; the named canonical semantics (reconfirm TTL, UI dup detection, every-deal approval) verified by test.*

**Wave 4 — merges + splits, one per session (3-4 sessions):** split sightings.py → slim board; THEN the Deals merge (lens toggle); Approvals tab merge + QP absorption + one-screen fold; htmx_app.js split; hx-disinherit fix; Postgres test engine; search_service decomposition + scoring v2-only. *Acceptance: 5-tab app fully walkable per §3 script; no router over ~800 lines (new static-analysis guard); kernel E2E green on Postgres-backed suite.*

## 11. Global acceptance (definition of done)

- The §3 kernel walk passes nightly, headless, on the deployed app.
- Scheduler job list == §3 kernel list exactly.
- Nav == §4 exactly: **tab bar shows 5 tabs; Settings reachable only via the gear icon**; no orphaned links or badges.
- **No auto-approve path remains; every deal shows a manager approval event in its audit log.**
- Fresh-DB boot green (migrations + drift gate), no startup backfills.
- 48h of logs contain no recurring warnings from parked/cut features.
- Every parked feature has its comeback trigger written in this spec; every cut is recoverable from git history.

## 12. Review checklist for the owner (v1.1)

Resolved in the 2026-08-03 review — confirm on final read:

- [ ] Auto-approve removal (§2) — every deal through the manager step. Confirmed?
- [ ] Resell status names (§5.3): DRAFT / POSTED / BIDDING / AWARDED / CLOSED + outcome field — drafted from your flow; rename freely.
- [ ] Data Capture Initiative (§6) — tightest Outlook + 8x8 data as the FIRST post-launch build. Matches your intent?
- [ ] Proactive full-park (§4) — the matching ENGINE parks along with the workspace and badge. Correct, or keep the engine running headless?

Carried from v1.0:

- [ ] Kernel walk (§3) matches how you actually run a deal?
- [ ] Deals merge (§5.1) — one editor, lens toggle, split-panel edit endpoints deleted. OK?
- [ ] CRM parks (§5.4) — org-scale trimmings parked until a team exists. OK?
- [ ] Resell solo-mode (§5.3) — trader lane park OK?
- [ ] Anything on the delete lists you want parked instead?
