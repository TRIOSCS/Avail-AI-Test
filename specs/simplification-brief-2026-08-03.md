# AVAIL Simplification — Chat Working Brief (v2: product lens first)

Date: 2026-08-03. Built from two verified audits of the repo: a
product/workflow audit (9 agents mapping every user journey click-by-
click) and a code audit (13 agents, 62 findings). All high-impact
claims fact-checked against code and live logs. Purpose: work through
decisions in Claude chat, bring back a spec. Nothing changed yet.

## Current reality (frames every decision)

- NOT LIVE. One user (the owner), testing spotty and sporadic.
- PAID APIs ARE OFF during testing (owner's choice, cost).
- No external consumers by design (ERP is manual; Acctivate owns
  SOs/POs/inventory; AVAIL issues buy instructions only).
- Consequence: deletion risk ~zero (git is the rollback), and the
  right test for every feature is "does it belong in the launch
  kernel?", not "is anyone using it?"

## PART 1 — WORKFLOWS & FEATURES (the product lens)

### The headline: an 11-surface app for a 1-person operation

The nav has 10 workspaces — Sales Hub, Sightings, Materials, Search,
Approvals, Resell, CRM, Proactive, Prospect, Tasks — plus Settings
(8 tabs) plus an always-present topbar global/AI search. The audit
mapped each journey and found the same pattern everywhere: **a lean,
genuinely good core loop, wrapped in surfaces built for a future
multi-user org or for paid APIs that are currently off.**

### What the paid-API-off reality means (measured, not guessed)

Works fully FREE today (Graph/M365 + local data):
- The whole RFQ spine: requisition → RFQ compose → send from your
  own mailbox → inbox scan matches replies → offers → quote PDF →
  won → buy plan → approval → buy instruction → PO reference.
- Resell loop, CRM core, Tasks/My-Day, badges, SAM.gov + Google News
  prospect enrichment, proactive matching (has a no-AI fallback).

Dead or degraded with keys off:
- "Parse with AI" BOM intake **throws a 500** — the flagship intake
  looks broken instead of saying "AI is off." (Fix the message.)
- Search returns only local rows — 8 connectors silently skipped, so
  the sourcing middle-of-funnel no-ops without saying so.
- CRM Enrich button 503s even though its SAM.gov path is free (bug).
- Explorium discovery (6 monthly jobs), Lusha/Clay enrichment,
  browser-worker marketplaces (ICSource/netCOMPONENTS/Broker Forum
  queues fill and sit forever), AI email parsing, AI garnish
  (insights panels, rephrase buttons).

Strategic gift: **the free spine ≈ the launch kernel.** The part of
the app that works without paying is almost exactly the part one
deal touches end to end. You can fully test the kernel at $0.

### Verdict per workflow (each verified in code)

**Sell-side intake (10 steps, 11 screens).** Core spine is lean and
correctly designed (one create modal, order type correctly absent at
intake, quotes already consolidated). But one deal is editable in TWO
parallel editors — the Sales Hub split-panel (7 tabs per line) AND
the requisition detail page (8 tabs) = 15 tab surfaces for the same
deal. And vendor offers have FIVE entry doors (auto-scan, review
queue page, two AI paste modals, manual form) — 3 of 5 dead/degraded
with keys off. → One editor (the requisition page), panel becomes
read-only triage; two offer doors (Responses tab + one Add-offer
modal with optional paste box).

**Sourcing (spread over 4 workspaces + Sales Hub).** Search and
Sightings run the IDENTICAL connector fan-out from two different
doors (verified). Materials is a passively-built catalog whose
differentiators (enrichment, crosses, AI filter) are all dead with
keys off. Proactive needs purchase-history data that cannot exist
pre-launch (its matching engine + badge work free and are worth
keeping — the standalone workspace is what parks). → ONE sourcing
surface; Materials demoted to a lookup; Proactive folds to a badge/
queue, workspace parks.

**Buy-side execution (13 steps).** The engine is the best-designed
part of the app: one any-of approval, per-line PO verify, prepayment
pay-link. Keep all three gates. But: the Sales Orders and Buy Plans
tabs show provably identical rows and panes (a memorial to two
retired tools) → merge. The standalone QP page duplicates the
workspace editors with CONTRADICTORY locks, and its QP_SALES /
QP_PURCHASING "gates" are self-stamped timestamps, not approvals →
absorb QP into the workspace, drop the stamp ceremony. Also: the QP
serial/FRU section — receiving/ops tracking — is STRANDED behind the
retired Deal view (unreachable today; decide: rehome or park).

**Resell (11 steps, one screen — good).** A complete two-sided
marketplace built for traders who don't exist yet: internal-offer
lane (no publish notification — a trader couldn't discover postings
anyway), buyer-intelligence layer (BuyerScore, nudges, nightly
recompute), ~34 statuses across 6 entities (≥4 dead), four bid-entry
paths. → Keep the owner's core loop (intake → post → outreach → log
bids → bid-back PDF → award). Park the trader lane and buyer
intelligence until there are traders; collapse statuses.

**CRM + Tasks (13 screens).** The deal flows consume exactly:
companies/sites, customer contacts (quote + DNC), vendor cards +
contacts (RFQ targets), activity log + outcome chips, and the one
task table My-Day renders. That spine stays. Around it: a 10-tab
vendor dossier (→ 5), a never-surfaced contact-intelligence layer
(computed, shown nowhere → cut), three "what do I work now" surfaces
(→ one: My-Day), saved views/segments/custom fields/collaborators
and the Activity Scorecard (→ park until there's a team).

**Growth & data.** Keep: manual prospect intake + free enrichment +
proactive matching + badges (consolidate 6 badge pollers → one
endpoint). Park: Explorium discovery machine, multi-rep pool
governance (claim caps, assign modals, manager digests). Cut: the
orphaned Email Intelligence dashboard, dead connector rows in
Settings, per-user 8x8 toggle.

**Also surfaced (critic pass):** in-app Notification table is
write-only — rows written, no UI reads them (confirms code audit);
Dashboard and Knowledge Base pages reachable from no link; trouble-
ticket flow has three hidden Anthropic dependencies; Settings has 8
tabs incl. per-user module access panels — multi-user machinery.

### The unification question: Sales Hub + Sightings → one tab?

**Yes — and the audits independently converged on it.** Evidence:
the two tabs already manipulate the same objects through duplicate
surfaces — the parts panel re-implements the requisition page's tabs,
Sightings re-implements Search's fan-out, and both host RFQ compose.
Merging is reuniting one workflow (demand line → candidates → RFQ →
offers), not gluing two features. Conditions that keep it from being
a mistake: (1) one deal spine with a **role lens toggle** (Sales
view: intake/status/quote columns; Sourcing view: coverage/RFQ/offer
columns) — the same play the Approvals workspace already proved, not
two screens stapled together; (2) do it AFTER the deletes — the
sightings surface is the app's #1 regression factory, so slim it
before merging into it; (3) fold Search's part-dossier in at the
same time so the result replaces THREE tabs, not two.

### The target shape (proposal to pressure-test in chat)

Nav goes 10 → 5 + Settings:

1. **Deals** — Sales Hub + Sightings + Search merged; lens toggle
   (Sales / Sourcing); Materials reachable as a lookup, not a tab.
2. **Approvals** — as today, minus the SO/BP tab split, with QP
   absorbed and the one-screen fold finished.
3. **Resell** — owner's core loop only; trader lane parked.
4. **CRM** — the load-bearing spine, slimmed dossiers.
5. **Tasks (My-Day)** — the single "what do I work now" surface.

Everything else: parked behind a flag or deleted, per decisions
below. The kernel = one deal walked through tabs 1→2 (+3 for excess)
with 4 and 5 as support — and it runs at $0 in API keys.

## PART 2 — CODE (appendix; full detail in session evidence)

The code audit's six moves, now scoped by Part 1's decisions:

1. **Broken/zero-yield automation** — mostly repaired in the 08-03
   evening fix sweep (@56f213a5: Teams DM, contacts-sync, connector
   cooldown, Playwright, backup symlink). Remaining: keep-vs-park is
   now a kernel question; tagging suite still deserves demotion to
   on-demand; consolidate to ONE backup system; nightly E2E is the
   safety net for everything below.
2. **Dead surface deletion** — 111 orphaned /api routes (+~55 more
   found independently), ~280 test files pinning dead code or feeding
   a coverage number, unreachable Sourcing Leads workspace, Dashboard
   + Knowledge pages, backfill graveyard (26 modules), startup.py's
   parallel migration system, write-only Notification table.
3. **One implementation per behavior** — offer lifecycle (2-3
   copies, drifted), requirement creation (UI path skips dup
   detection!), quote builder ×3, QP editors ×2 (superseded by
   Part 1's QP absorption), email pipelines ×2, notifications ×3.
4. **One state machine per entity** — BuyPlan/ApprovalRequest dual
   machines + 8 hand sweeps; retired SO-verification track that
   deadlocks halt→resume TODAY; dead statuses (INBOUND, EXPIRED,
   auto_approved); requisition 9-state ladder → derive it.
5. **Split the regression factories** — sightings.py (3,812 lines,
   35 fix commits; prerequisite for the Deals merge), htmx_app.js
   (3,654 lines), search_service.py (3,604); hx-disinherit fix for
   the recurring page-wipe class; kill 3 of 4 scoring generations.
6. **Test suite** — diff coverage instead of the 85% farm, Postgres
   test engine (SQLite has masked real prod 500s), split the
   meta-test, delete tests with their dead routes.

## DECISIONS TO MAKE IN CHAT

A. **Launch kernel** — walk one real deal end to end; write down
   every screen/job it touches. Keep-list + E2E script. (The free
   spine above is the candidate answer — confirm or amend it.)
B. **Target nav** — approve the 5-tab shape? Which of Materials /
   Proactive / Prospect earn a comeback path, and what triggers it
   (launch? first paid key? first extra user?)
C. **Deals merge** — approve Sales Hub + Sightings + Search → one
   tab with lens toggle, sequenced after the slim-down?
D. **Approvals ceremony** — merge SO/BP tabs; absorb QP; drop the
   two self-stamp gates; keep deal-approval + PO-verify + prepayment
   as the only three gates?
E. **QP serial/FRU section** — rehome into the workspace (receiving
   needs it eventually) or park until ops actually uses it?
F. **Resell** — park trader lane + buyer intelligence until a second
   user exists?
G. **CRM scope** — approve spine-only CRM (cut contact-intelligence,
   park org-scale trimmings, vendor dossier 10→5 tabs)?
H. **Park mechanics** — parked features: delete outright (git
   restores) or flag-off? (Recommend delete for anything unreachable
   or write-only; flag-off only for things with a named comeback
   trigger.)
I. **Keys-off honesty** — approve graceful "AI is off" states for
   BOM parse / search / enrich instead of 500s and silent empties?
J. **8x8 + email pipelines** — does either belong in the kernel at
   all, or do both wait until after launch?
K. **Test strategy** — diff coverage + Postgres engine?

## SUGGESTED ORDER

Pre-launch, waves are for reviewability, not safety.

- **Wave 0 (done 08-03 eve):** acute fixes shipped @56f213a5.
- **Wave 1 (small):** keys-off honesty fixes; automation demotions;
  one backup system; badge consolidation; dead-status cleanup.
- **Wave 2 (deletes/parks):** everything in B/F/G/H — nav shrink,
  parked lanes, dead surfaces, their tests, coverage farm.
- **Wave 3 (dedupe):** one offer service, one requirement pipeline,
  one quote builder, one RFQ composer, two offer doors, one QP
  editor, one notification path.
- **Wave 4 (the merges + splits, one per session):** slim sightings
  → split it → THEN the Deals merge (C); Approvals tab merge + QP
  absorption (D); htmx_app.js split; Postgres test engine.

## WHAT THE SPEC YOU BRING BACK MUST CONTAIN

- The launch kernel (ordered screens/flows of one real deal) — this
  doubles as the nightly-E2E script.
- A / B / C / … decisions above, each resolved. No TBDs.
- Park list with comeback triggers; cut list (git is the archive).
- Wave assignment per item; what is explicitly OUT until launch.
- Acceptance per wave: app boots clean, suite green, kernel E2E
  passes, deleted surfaces 404, no orphan nav entries.
