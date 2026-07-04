# Master Requested-Work Backlog (durable record)

Single authoritative list of everything the user has asked for. Started 2026-07-03.
Keep this current as items land or new ones arrive.

**Standing directive (2026-07-04):** capture each idea the moment it's given → durable
record here → work through them in phases (investigate → spec → TDD build → deploy+verify).
Don't block momentum by fully implementing each before capturing the next.

---

## 🆕 NEW REQUESTS — 2026-07-04 (requisition-lifecycle theme; capture + phase)

| # | Idea | Decision / scope | Status |
|---|------|------------------|--------|
| A | **Sales Hub — no Archive on selected part-lines; Mark Won / Mark Lost instead** | Replace the bulk **Archive** in the parts-workspace selection bar (`parts/list.html` bulk bar `:66-85`; JS `:277-322`; `bulk_archive` at `routers/htmx/parts.py:1050`) with **Mark Won** + **Mark Lost**. ✅ **User-confirmed decisions:** (1) resolve at the **part-line level** — each selected `Requirement` gets `SourcingStatus.WON`/`LOST` via `requirement_status.transition_requirement` (status_machine allows open/sourcing/offered/quoted→won/lost); (2) **capture a required "why won / why lost" reason** — ONE shared reason prompt (mirror the `req_row.html` outcome popover) applied to all selected lines. NEEDS a **migration: add `Requirement.outcome_reason` (nullable Text)** (`models/sourcing.py:128`; today only `Requisition` has it at `:66`). New endpoint (e.g. `POST /v2/partials/parts/bulk-outcome` `{requirement_ids, outcome, reason}`) → 400 on blank reason. Keep the archived-view Unarchive; only the default bulk **Archive action** is removed. | ✅ Specced — ready to build |
| B | **New Requisition — "Hot List" option** | Add a **Hot List** toggle to the New-Requisition modal (`requisitions/unified_modal.html`, near hidden metadata `:422-429`). ✅ **User-confirmed: Hot List = `HOTLIST` monitor** (stored + market-data + proactive matches, NOT sourced/archived). Build: add `hotlist: bool = Form(False)` to `requisition_import_save` (`routers/htmx/requisitions.py:463`, set `status=HOTLIST` at `:544`); **also set `Requisition.company_id` from the chosen site** (create path omits it today → proactive match query needs it — `proactive_matching.py:205` filters `status==HOTLIST` + joins Company). No auto-source suppression needed (create has no kickoff; search queue is `OPEN_PIPELINE`-scoped). Add a **"Hot List" filter pill** to the requisitions list (`requisitions/list.html:144`) so created monitor deals are visible (additive UI — necessary for the feature). | ✅ Specced — ready to build |
| C | **Hover explanations on scored / historic-price columns** | A **two-tier hover** UX wherever a **score** or **historic price** is shown: (1) hover the **column header** (e.g. "Score") → a **definitional tooltip** explaining what that metric means / how it's computed; (2) hover the **value itself** → contextual detail — for a **score**, an **AI blurb explaining why *this row* scored that way** (its specific drivers); for **historic pricing**, show the **price history** (past prices — list / sparkline / mini-chart). Apply across all score + historic-price columns (buyer-ready / avail / multiplier / vendor / sighting / prospect scores; part & material-card price history). **Build-time decisions:** AI blurb **on-demand vs precomputed** (hover-triggered AI = latency + cost + rate-limit — likely cache/precompute or lazy-load with a spinner); exact column inventory; reuse the existing hover-tooltip + `record_price_snapshot`/PartHistory data. | Captured |

| D | **Sightings detail panel — fix top whitespace + declutter vendor list** | On `/v2/sightings` the **right-hand detail panel** (selected part, e.g. ESP32-WROVER-E) is unbalanced: the **top wastes vertical space** (part header + QTY / TARGET / CUSTOMER / STATUS block is sparse/loose) while the **lower Vendors list feels busy + cluttered** (19 rows, each = vendor + "Sighting" chip + "Also on N other reqs" + qty / best-price / score / Build RFQ). Design pass (frontend-design, `templates/htmx/partials/sightings/` detail template): tighten/condense the top header to reclaim the wasted space, and declutter the vendor rows — stronger visual hierarchy, less noise, make qty/price/score scannable. Ties into idea **C** (score/price hover) on this same Vendors table. | Captured |

| E | **Material Card detail page — tighten + clean (too much wasted space)** | `/v2/materials/{id}` (e.g. 4XB7A17133 — "SSD, ThinkSystem…"): the page is mostly **empty/sparse with large vertical gaps** — Specifications (Category / Package / Pin Count / Lifecycle, many `--`), "No pricing data available", "No alternative parts discovered yet", Insights — everything spread out with wasted space. Condense: tighter section spacing, denser spec grid, compact empty states, so the page reads clean + purposeful instead of empty. (frontend-design; materials detail template under `templates/htmx/partials/` — pin exact file at build.) | Captured |

| F | **Search / part-dossier page — tighten wasted space (feels unfinished)** | `/v2/search?mpn=…` (e.g. MAEQ-23664C, MACOM): a large **empty gap in the center** between the part title + "WHAT WE KNOW" / MARKET-PRICE header and the "Live market" section; the layout "feels unfinished and inefficient." Condense the header/action zone, close the dead vertical gap, make the dossier read complete + purposeful. (frontend-design; `templates/htmx/partials/search/` dossier incl. `dossier_hero.html`.) | Captured |

| G | **Approvals · Buy Plans / Sales Orders list — add customer, Rev, Sales GP, part numbers** | On `/v2/approvals` → Buy Plans / Sales Orders list, each row shows only Plan # + subtitle + status badge + value + View. Add per row: **customer name**, **Revenue (Rev)**, **Sales GP** (gross profit — $ and/or %), and the **part numbers (MPNs) on the plan**. Source from `BuyPlan` (customer + line-item sell/cost → Rev + GP) and its line-item MPNs. (buy-plans list template + list-context query under `templates/htmx/partials/approvals|buy_plans/`.) | Captured |

| H | **Approvals · "Create Buy Plan" button** | On the Buy Plans / Sales Orders list (`/v2/approvals`) add a **Create / New Buy Plan** button to *start* the buy-plan process — there's no create affordance there today (only View on existing plans). At build: confirm whether a create-buy-plan flow already exists (e.g. spun up from a requisition / sales order) that this button should launch, vs building a net-new entry point + form. Pairs with **G** (same list). | Captured |

**🐞 BUGS FOUND 2026-07-04 (jump the design queue):**
- **"Enrich not working" = actually a 30-SECOND synchronous request.** Verified live: the click returns **200** (button isn't dead) but `enrich_material` (`routers/htmx/materials.py:1040`) runs `enrich_cards` + `enrich_card_specs` **inline** (~30s: web extraction + authoritative ladder + spec pass) before swapping the page → looks hung. **Fix: enqueue a background enrichment job + return immediately**; the existing `materials/enrich_status.html` self-poller already exists to show progress. Also: when a source is disabled (Explorium 401 / Nexar+BrokerBin down) it returns a "couldn't complete" toast — also reads as broken.
- **"Insights not working"** — returns **200 fast** (works server-side); the button swaps into `#insights-panel` lower on the page. Verify it visibly updates + isn't rendering an empty panel for sparse cards (may just be scrolled below the fold / thin content).
- **(pre-existing, flagged by Wave 3)** `ownership_service.check_and_claim_open_account` is uncalled despite its docstring saying activity_service auto-invokes it on every logged activity → probable missing-wiring bug. Investigate separately.

Items A/B = requisition-lifecycle. **C = cross-cutting score/price hover layer.** **D + E + F are the SAME systemic complaint** — detail/dossier views waste vertical space + feel sparse/unfinished (D = Sightings right panel, E = Material Card page, F = Search dossier). Treat as **ONE app-wide "detail-view density" design pass**: define a consistent tightened layout/spacing rhythm + compact empty-states, apply to these three, and audit sibling detail/dossier views for the same. Build in Fable, deploy+verify to staging.

---

## ⭐ PRIORITY ORDER (2026-07-03 — execute top-down; user delegated "do each item by priority")

| P | Item | Why here | Task |
|---|------|----------|------|
| **P0** | Deploy the 7 built-but-unshipped commits to staging + verify (sightings hide-closed-deals, API-search Phase 0, requisitions UI) | finish/ship what's already built | #13 ✅ built |
| **P1** | Sightings — collapsible brand/mfr groups + clean/reset | daily-driver friction on a page you work in constantly; cheap, high value | #14 |
| **P1** | Sales Hub — nested customer › requisition › lines grouping + collapse | same; **shares ONE reusable collapse/group pattern** with #14 (build once) | #15 |
| **P2** | Feature-request ticket system + rich context on BOTH ticket types + "Create Prompt" AI action (folds in trouble-ticket form cleanup) | high leverage — makes every future request you file richer + one-click into a CLI prompt | #16, #12 |
| **P3** | API-search core sprint — Phases 1-4 | the product's core; large multi-phase program (Phase 0 ships in P0), so it runs after the quick wins; deploy+verify each phase | #7 |
| **P4** | Resell rework → Prospecting rework → Tasks rework | bigger module reworks; each = brainstorm → spec → plan → build | #8, #10, #11 |
| **P5** | CRM aesthetics / readability pass | polish; larger design sweep | #9 |

Sequencing notes: P1's two items share one collapse/group component. Build all in **Fable**.
Deploy + verify each shippable increment to staging. User can reorder any time.

---

## ✅ COMPLETED + DEPLOYED (staging) this session

| # | Item | Where / notes |
|---|------|---------------|
| 1 | Merge 7 handoff PRs + the #628 coverage PR + deploy | all squash-merged |
| 2 | 2 cosmetic fix-forwards (#705/#706 conformance regressions) | quotes/materials |
| 3 | Profile-photo upload bug | root cause = cropper raw-fetch missing x-csrftoken; fixed + live-verified |
| 4 | App-wide taste-layer pass | 109 templates; accent selected-state, elevation, pills, titles, radius |
| 5 | Phase 3: per-PO sign-off + QP review toggle + 3-tab Approvals + completed-plan backorder | migrations 176/177 |
| 6 | Approvals = ONE hub, 3 sub-tabs (Buy Plans/Sales Orders · PO Approval · Prepayment), See-all/See-mine | relabeled tab 1 |
| 7 | Datasheet SharePoint admin guide + Claude-Cowork prompt for the coworker | docs/DATASHEET_LIBRARY_SETUP.md |
| 8 | Prepayment-on-PO feature (request on a PO → manager approve → notify accounting/AP) | migration 178; spec+plan in docs/superpowers/ |
| 9 | **Prepay closure** (lifecycle requested→approved→paid\|void; public tokenized confirm link; paid fan-out; void-on-teardown) | migration 179; live-verified end-to-end |
| 10 | Activate dormant features: Lusha, email-mining, ownership+account sweeps, spec-resolver | .env flags flipped + verified |
| 11 | Git hygiene cleanup: ~45 merged/lingering branches → archive/* tags, stash dropped, stale worktrees removed | only `main` remains |
| 12 | Requisitions UI: New Requisition moved left, view toggle pinned right (kill the jump) | list.html |
| 13 | Deep search for stashed/uncompleted/unstarted work → this backlog + phased plan | docs/superpowers/plans/2026-07-03-backlog-completion-phased-plan.md |

## 🔵 IN PROGRESS

| Item | Status |
|------|--------|
| **API-search core sprint** (full program, Phases 0-4) — the product's central function; user mandate: highly functional, optimized, stable | Phase 0 DONE (streaming-search aggregate deadline + telemetry, Test-all concurrency/timeout, keyless-Test real-path, Retry-After cap 300→30s); **deploying + SSE-verifying now**. Phases 1-4 queued. Audit: docs/superpowers/specs/2026-07-03-api-search-core-audit.md. **Cadence: deploy+verify EACH phase to staging.** |

## ⭐ QUEUED — user-requested module review/rework programs (after the API-core sprint)

Each = deep review → prioritized findings → optimization + rework. Treat like the
prepayment + API-search reviews (Fable multi-lens audit workflow → action plan → build).

| Item | Scope |
|------|-------|
| **Resell module** — workflow / function / process review + optimization + improvement + rework | /v2/resell, migrations 127-133; end-to-end resell flow (offer→buyers→award→…) |
| **Prospecting module** — same treatment as Resell | prospecting workflow/function/process review + optimize + rework |
| **Tasks module** — same treatment as Resell | tasks workflow/function/process review + optimize + rework |
| **CRM aesthetics / readability** pass | Customers/Contacts/Companies + detail panels: easier to read, more pleasing, important info stands out WITHOUT being noise — clean + effective. Visual hierarchy, signal-vs-noise. (frontend-design; targeted taste sweep) |
| **Trouble-ticket "Report a Problem" data entry** — cleanup + simplification + optimization | the Report-a-Problem modal (app/templates/htmx/partials/shared/trouble_report_form.html): lots of empty space (big blank auto-screenshot box), tighten/simplify the form |
| **Sightings page — hide won/lost/archived deals** (quick fix) | The sightings page is for buyers to ACTIVELY source + find offers for OPEN requirements. Won/lost/archived deals (closed requirements) must NOT appear. Filter them out of the sightings query. |

### 🧩 "Workspace grouping & collapse UX" theme (related — do together / shared pattern)
Buyers/salespeople need to group + collapse/expand + reset their working lists so they can focus on one group at a time. Build ONE reusable collapse/group pattern and apply across:
| Surface | Ask |
|---------|-----|
| **Sightings** — collapsible brand/manufacturer groups + clean/reset | Collapse groups they don't want to work on now, open the ones they do; a "clean & reset" function to reset view/groupings. (grouping already exists in the query — add collapse + reset) |
| **Sales Hub** — nested grouping customer › requisition › requirement lines | Requirement lines grouped by requisition, requisitions grouped by customer (2-level). Easy expand/collapse (and/or filter) — cleanest, most efficient structure. |

## 🧑 USER-SIDE (yours, not code — whenever)

- SFDC data import (no near-term date); March enrichment recovery (SFDC Weekly Export).
- Launch config: disable password login after Microsoft sign-in; DO Spaces backup creds;
  set the 3 prepayment notification config keys; the datasheet SharePoint site + Sites.Selected.
- Explorium API key + eBay client id/secret (deferred — those 2 dormant features await keys).

## 📌 STANDING DECISIONS / DIRECTIVES (apply to all work)

- **Leave Approvals / separation-of-duties UNCHANGED** (explicit).
- **Future ERP = Microsoft Dynamics 365** (round-2 project post-go-live) — NOT QuickBooks
  (Desktop-in-Azure, un-connectable). Reconciliation targets Dynamics. See
  [[reference_quickbooks_desktop_azure]].
- **Deploy + verify EACH phase** to staging as we go; **clean up afterwards** (git/artifacts).
- **Use Fable** for build subagents.
- **Ask clarifying questions ONE AT A TIME** with a Recommended option (codified in CLAUDE.md).
- Money-governing + core code: TDD, migrations round-tripped on throwaway PG, full suite
  (`SENTRY_DSN=""`), live-verify (curl ≠ htmx — headless drive the real surface).
