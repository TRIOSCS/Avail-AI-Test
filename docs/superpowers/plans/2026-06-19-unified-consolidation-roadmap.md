# AVAIL — Unified Consolidation Roadmap (2026-06-19)

One coordinator (this session) owning all open work. Built from a verified repo-wide sweep
(8 agents) reconciled against live `origin/main` (advanced through PR #404-era; ~12 PRs
merged by concurrent sessions in the last hours).

## THE ROOT PROBLEM
Multiple concurrent Claude sessions are editing the same checkout + racing PRs onto `main`,
which forced repeated migration re-chains (117→118→119) and left a dirty, deploy-blocking
working tree. **Consolidation = stop the other sessions, let one coordinator land work
serially.** I cannot terminate other sessions — that is the human's first action.

## PHASE 0 — UNBLOCK (human action + cleanup; nothing else proceeds cleanly until done)

**0a. Human: stop these live sessions**
- The session editing the **`/root/availai` main checkout** (6 modified + untracked files — they DUPLICATE already-merged #394 API-keys tab; the tree is redundant WIP blocking every deploy).
- The session editing the **`worktree-comm-ledger-alerts` worktree / PR #404** (re-proposing already-merged #390 — wasted reconciliation).
- The **`sp4-reclamation`** session (1 uncommitted plan doc) — confirm it's idle, then I absorb the branch.

**0b. Coordinator cleanup (me, once sessions stop):**
- Confirm the dirty `/root/availai` tree has nothing unique vs merged #394 → discard; `git pull` to origin tip (it's ~33 behind).
- **Close PR #404** (duplicate of merged #390) and **#306/#338** (stale nightly drafts; keep #351). Delete ~11 already-merged stale branches + their worktrees.
- **Deploy from clean main** → bring merged-but-undeployed **#390 (alerts) / #396 / #397** + migrations **118/119/120/121/122** live; live-verify.

## PHASE 1 — Buy Plan Deal Hub (my in-flight; the user's stated priority)
- Rebase `worktree-buy-plan-deal-hub` onto clean main (it's **migration-free** — inherits main's migrations, adds none; the sweep's "drop 118/119/120" was a misread). No #404 dependency now (#404 is closed).
- Complete **Tasks 4–12** of `docs/superpowers/plans/2026-06-19-buy-plan-deal-hub.md` (Tasks 1–3 read-models done): nav swap (Buy Plans in / Reporting out), alert registry `reporting`→`buy-plans`, role-lens shell, My-Orders/My-Deals/Supervise partials + Playwright, urgent-vs-routine notifications, reporting-fold to contextual chips, verify.
- PR → merge → deploy.

## PHASE 2 — Prospecting SP4: account reclamation (absorb the concurrent session's branch)
- `feat/sp4-account-reclamation` has scaffolding (migration **123** on 122 ✓, config, `get_last_activity_at()`, APScheduler sweep/reactivation jobs).
- Finish the sweep logic + tests, rebase on main, PR → merge → deploy. Independent of the Deal Hub (can run after Phase 0 in parallel if desired).

## PHASE 3 — Data-integrity landmine (no current owner)
- **`chk_offer_status` drift:** migration 048 forbids `approved`/`sold` but live PG dropped the constraint (real approved/sold offers exist). A fresh-DB rebuild would break offer-approval. Fix in a migration (**claim 124**) to match the OfferStatus enum.

## PHASE 4 — Buildable now, no creds (engineering-only)
- **CRM Phase 2** (governance): tags/tier UI, buying-role taxonomy, inline edit, DNC flag, merge-dupes.
- **Free-enrichment adds:** Nexar-deep fields, eBay-title mining, Lenovo PSREF API, `has_stock`/`has_price` sidebar facets.
- **Alerts follow-ups** (deferred from #390): vendor-inbound badge on Sales Hub, vendor activity-timeline UI, non-RFQ outbound (Sent-folder) capture, the buyer "waiting-on-vendor + ETA" note (the Deal Hub's deferred migration).

## PHASE 5 — Gated on the user (decisions/credentials — NOT engineering-blocked)
- Flip `AI_SCREEN_ENABLED` (SP3 ships dark; flips on spend). Supply `DATASHEET_LIBRARY_DRIVE_ID` + SharePoint Sites.Selected (datasheet library go-live). Enable Clay HTTP API + callback (SP2 — do NOT build a competing connector; overlaps CRM Phase 4). Nexar plan upgrade / DigiKey cred refresh / BrokerBin+Nexar go-live. Sentry OAuth. DO billing.

## Migration ledger
Chain is linear to **122** on main; **123** = SP4 (parent 122 ✓). **Next free = 124** (chk_offer_status fix + new work). No duplicate-number collisions; the in-flight branches just carry inherited copies of landed migrations (drop on rebase, no conflict).

## Sequencing
Phase 0 is a hard gate (stop sessions + deploy). Then **Phase 1 (Deal Hub)** and **Phase 2 (SP4)** can run sequentially or lightly parallel; **Phase 3** anytime after 0; **Phase 4** anytime; **Phase 5** is the user's.
