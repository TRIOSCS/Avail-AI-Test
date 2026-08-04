# AVAIL Simplification — CLI Execution Brief v2

Date: 2026-08-04. Supersedes Brief v1 (same scope; adds execution discipline). Companion to the **AVAIL Simplification Spec v1.1**, which remains the detailed work order. Where this brief and v1.1 conflict, **this brief wins**. Owner: Mike. Executor: Claude Code.

## 1. Mission

Simplify what is already built so it can go live. This is not a rebuild and not a redesign. The product that launches is the product that exists today, minus bloat, minus duplicates, minus dead weight — easier to use, easier to troubleshoot, easier to keep green. Important functions get rearranged or consolidated, never lost.

## 2. Hard rules

1. **Simplify, don't rebuild.** Every change must delete or merge something that already exists. If any task turns out to require net-new UI or behavior that exists nowhere in the codebase: STOP. Write it as a one-line backlog item in STATE.md and move on. Never build it.
2. **Parallel instance.** All work lands on a separate branch, deployed to a separate instance with a copy of the production database. Production AVAIL is never touched, restarted, or migrated until cutover. Cutover is one explicit event, on Mike's word only.
3. **v1.1 ground rules carry unchanged:** Acctivate stays manual; ERP-neutral naming; three approval gates — deal approval on EVERY deal (the sub-$5K auto-approve is removed), per-line PO verify, prepayment OK-to-pay + pay link; audit logging on every change; HTMX + Alpine; routers thin, services fat; tables are never dropped (drift-gate grandfather rule).
4. **Existing coding rules apply** (project playbook): tests alongside code, exact file paths, Loguru not print, header docstrings, warn before destructive operations, session-end checklist.

## 3. Execution discipline

1. **Working files live in the repo.** This brief and the v1.1 spec get committed to `docs/` on the simplification branch in Session 1. A `docs/STATE.md` is created alongside them and becomes the single source of truth for progress. **Every session starts by reading STATE.md; every session ends by updating it.** No decision already recorded there gets re-litigated in a later session.
2. **STATE.md contains exactly:** current wave and its checklist, the baseline metrics table, the deviation log, and the backlog list (every "gains"/net-new item stopped by Rule 2.1).
3. **Waves become checklists.** At each wave start, decompose that wave's prose from v1.1 §10 (plus §4 of this brief) into a checkbox list in STATE.md, then burn it down. A wave is done when its boxes are checked, the suite is green, and the nightly E2E passes.
4. **Deviation log.** Anything done differently than the spec says gets one logged line with the reason — surfaced in that wave's review packet, never silent.
5. **One checklist item = one commit,** message tagged to its spec section: `W2: delete Sourcing Leads (§8)`. Rollback stays surgical, and review packets assemble from the commit log plus the deviation log — never from memory.
6. **Baselines before Wave 1.** Record in STATE.md before any Wave 1 work: route count, scheduled job count, LOC of sightings.py / htmx_app.js / search_service.py, status count per entity, test file count, nav tab count. **Every review packet opens with the baseline → current numbers table.** The shrink shows in numbers, not adjectives.
7. **DB refresh every wave start.** Refresh the parallel instance's database from the latest production backup and re-run all migrations against it. By launch, the cutover migration path has been rehearsed against a fresh prod copy four times.
8. **No session ends red.** A fast kernel smoke check must pass before any session closes. If it's red, fix or revert before stopping — the next session never opens on a broken instance.
9. **The blocker ping.** One interruption is allowed outside the four review packets: when something discovered mid-wave contradicts the spec. One message — the conflict, a recommended resolution, and flip-able options. Everything else waits for the packet.

## 4. Amendments to v1.1 (owner Q&A, 2026-08-03)

1. **UI glossary — Wave 2.** Rename app-invented jargon in UI labels only (Requisition, Sighting, Requirement, sourcing board, and peers). Real trade terms stay (RFQ, PO, quote, prepayment). Database and code names untouched. The old→new table ships in the Wave 2 review packet; apply only after sign-off.
2. **Screen diet — Wave 2.** Query the production copy for form fields with near-100% null rates and list columns no workflow touches. Ship the evidence-backed cut list in the Wave 2 packet; remove from the UI only after sign-off. Tables never change.
3. **Deals merge discipline (Wave 4):** extend the existing requisition detail page only. The lens toggle shows/hides panes that already exist. No new page gets built.
4. **The "gains" clause:** when consolidating duplicates (offer_service, requirement pipeline, quote builder, RFQ composer), the survivor inherits only behavior that already exists in at least one of the merged paths. Anything not implemented anywhere today is a backlog line in STATE.md, not a build.
5. **Trouble-ticket fallback:** wire the existing regex path if one exists; if none does, smallest viable stub or defer to backlog.
6. **Schedule insurance:** if Wave 4 runs long, the Postgres test-engine migration defers first. Everything else ships.
7. **Explicitly rejected — do not build, do not re-propose:** a separate rep-only deal page, a phase bar / primary-action stepper, a My-Day home screen, any new landing page. Considered and dropped as rebuild-shaped.

## 5. Launch definition and cutover

- **Live means:** one salesperson, one buyer, and Mike as approver run ONE real deal end to end on the new instance — the v1.1 §3 kernel walk, with real money.
- **All four waves complete before launch.** No phased go-live.
- **Go signal:** the launch deal starts as soon as Wave 4 lands. Wave 4's own acceptance already requires the nightly kernel E2E green, so "lands" means lands green — no additional waiting window after that.
- **Cutover runbook:** written during Wave 1 (`docs/CUTOVER.md`) — exact commands, verification steps, and rollback. Every wave-start DB refresh (Rule 3.7) is a rehearsal of it. Launch day runs a rehearsed script, not a first attempt.
- **Cutover trigger:** the launch deal completing clean. Then, on Mike's explicit word: execute CUTOVER.md against production, switch instances. One event.

## 6. Review packets — the only four scheduled times Mike is needed

One packet per wave, delivered at wave end as a single message, assembled from the commit log, STATE.md deviation log, and the baseline table. Nothing inside a wave waits on Mike (blocker pings excepted, Rule 3.9). Each packet presents its decisions as flip-able choices, each with a recommended answer, and opens with the baseline → current numbers.

- **Packet 1 (end of Wave 1):** jobs shut off (list), dead statuses removed (list), 48h log summary, kernel E2E green confirmation, CUTOVER.md v0.
- **Packet 2 (end of Wave 2):** glossary old→new table, field/column cut list with usage evidence, delete-list confirmation (what now 404s), 5-tab + Settings-gear nav check.
- **Packet 3 (end of Wave 3):** resell status remap table (34 → DRAFT / POSTED / BIDDING / AWARDED / CLOSED + outcome field), buy-plan remap table (7→5), canonical-semantics confirmations (reconfirm TTL, every-deal approval, UI dup detection).
- **Packet 4 (end of Wave 4):** kernel-walk demo on the parallel instance, router-size report, final numbers table, launch-deal scheduling.

## 7. Execution order

Waves 1–4 exactly as v1.1 §10, with §4 of this brief folded in. **Wave-start ritual:** refresh the parallel DB from the latest backup, re-run migrations, decompose the wave into its STATE.md checklist. **Wave-close ritual:** suite green, nightly E2E green, packet delivered. Estimates stay estimates, not promises.

## 8. Session 1 — do this first

1. Create the simplification branch.
2. Commit this brief + the v1.1 spec to `docs/`; create `docs/STATE.md` with the baseline metrics table (Rule 3.6).
3. Stand up the parallel instance: separate deployment, database restored from the latest db-backup-container backup. Confirm it boots clean and is reachable.
4. Write `docs/CUTOVER.md` v0.
5. Point a nightly E2E run at the parallel instance.
6. Only then begin Wave 1.
7. End of every session: kernel smoke green, STATE.md updated, session-end checklist per the playbook, one line stating exactly where the current wave stands.
