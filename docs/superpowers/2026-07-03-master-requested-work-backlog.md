# Master Requested-Work Backlog (durable record)

Single authoritative list of everything the user has asked for. Started 2026-07-03.
Keep this current as items land or new ones arrive.

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
