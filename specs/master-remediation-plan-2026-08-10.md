# AVAIL — Master Remediation Plan (2026-08-10)

Consolidated + deduped from six reviews. **Nothing here is fixed.** This is a
menu to pick from, ranked by priority.

Sources (full detail in each):
- `specs/ux-workflow-review-2026-08-10.md` — 62 UX/workflow findings
- `specs/process-fidelity-review-2026-08-10.md` — 15 process findings
- `specs/ai-guardrails-review-2026-08-10.md` — 10 AI findings
- `specs/data-model-durability-review-2026-08-10.md` — 8 schema findings
- `specs/customer-comms-review-2026-08-10.md` — 7 email findings
- `specs/qc-review-2026-08-08.md` — code-QC (crit/high shipped; ~54 mediums open)

Legend: ⚡ harming now · 🔁 found by ≥2 reviews (high confidence) · 💰 money
path · 🔒 security · ✅½ one layer already fixed in the 08-08 code QC ·
size **S**/M/**L**. Every item must be re-verified against code before fixing;
a few specific sub-claims (noted) need one more file each.

---

## P0 — Bleeding now (customer-facing or irreversible; do first)

**P0-1 · Proactive offer emails send body-only** ⚡💰🔁 · S
`app/services/proactive_service.py:453` (`if email_html: html_body = email_html`
bypasses `_template_email_html`). When a rep uses the AI-drafted body, the parts
table, greeting, and signature are dropped — the customer gets an intro that
says "details below" with nothing below. **Verified by hand.** Sources: comms,
UX. Fix: compose the AI body *inside* the full template; also drop the
`[AVAIL-PROACTIVE-id]` subject tag (same file, `:475/:736`).

**P0-2 · Email-parsed offers mint ACTIVE on the model's own confidence** ⚡🔒💰🔁 · S–M
`app/email_service.py:1576` (`status=ACTIVE if vr.confidence >= 0.8`). Parsed
offers skip human review and feed `quote_builder_service.py:60` + buy plans.
Prompts take untrusted vendor email text raw (no "data, not instructions"
delimiter), and the injectable field *is* the confidence gate. **Verified.**
Sources: AI, (echoes code-QC silent-failure theme). Fix: always
`PENDING_REVIEW`; delimit + injection-harden the four email prompts.

**P0-3 · Nightly auto-dedup runs irreversible merges on AI confidence** ⚡🔒 · S
`app/services/auto_dedup_service.py:176` (≥0.85 → destructive merge;
`company_merge_service.py:175` hard-deletes the losing row). Fires nightly.
Source: AI. Fix: route 0.85–0.97 pairs to the existing Data Ops review queue;
persist human dismissals so the job can't overrule them 30 days later.

**P0-4 · Customer-facing cost/margin leaks** ⚡💰🔁 · S
(a) Copy Table copies internal **Cost + Margin%** to the clipboard
(`app/templates/htmx/partials/quotes/detail.html:213`) — **verified it emits
Cost+Margin**; a paste to a customer leaks both. (b) Quote unit price prints 2dp
while Ext uses 4dp so the math doesn't add up + sub-cent parts show "$0"
(`quote_send.py:225`). (c) "Internal Notes" may reach the customer email
(`edit_form.html` → *verify the email template at fix-time*). Sources: UX, comms.

---

## P1 — Money-path integrity (wire / prepayment / quote lifecycle)

**P1-1 · Prepayment teardown gaps** 💰🔁 · M
- Voided-prepayment line can never re-request — guard keys on `ApprovalRequest`
  status which teardown leaves APPROVED (`prepayment_service.py:177-189`);
  button 400s forever. (process D3)
- PO send-back leaves the wire authorization alive (`buyplan_po.py:233-244`) —
  wire against a dead PO. (process)
- Mark-paid / reverse-payment / request-prepayment have working backends but **no
  rendered button** in the workspace (`prepayments.py:324`, `_pane_po_line.html:232`). (UX)
Fix: re-key the guard on the Prepayment's own lifecycle status; call the existing
line-scoped teardown sweep on PO send-back; render the three missing controls.

**P1-2 · Quote-delete CASCADE at the schema level** 💰🔁✅½ · S (1 migration)
`buy_plans_v3.quote_id` and `prepayments.buy_plan_id` still CASCADE — deleting a
quote can destroy an approved deal incl. **paid** wire records. Code-QC cluster 2
patched the *UI* delete path; the FK hole underneath is open. Fix: `quote_id →
SET NULL`, `prepayments.buy_plan_id → RESTRICT`. Sources: data-model, code-QC.

**P1-3 · A won quote doesn't close the deal** 💰 · M
Workspace quote-win never closes the requisition or sets `won_revenue`
(`htmx/quotes.py:564`); the twin that does has no UI callers. Owner win-rate
never records the win. Plus two opposite revision conventions
(`htmx/quotes.py:601` vs `quote_builder_service.py:520`) can double-count won
revenue. (process D5) Fix: one shared quote-result service, required lost-reason,
route by order type; unify revision on the builder convention.

**P1-4 · Wire-notice clarity** 💰 · S
Prepayment rejection reaches the requester as one context-free sentence (the
required reason is discarded); OK-vs-DO-NOT-WIRE isn't visually unmistakable
(`prepayment_notifications.py`, `approvals/notifications.py:65`). Fix: full-width
green "OK TO PAY" / red "DO NOT WIRE" banner with deal id + amount; carry the
reason + a deep link.

---

## P2 — Dead-end states (deals strand with no exit)

All from process-fidelity; all confirmed. · M each
- **D1** Testing Service / Comps plans can never complete (`buyplan_approval.py:569`,
  `inventory_jobs.py:90`) — add a manager "Mark complete" for zero-line lite plans.
- **D2** Halted-then-resumed plan can never complete (`buyplan_approval.py:474/575/882`)
  — snapshot/restore `so_status` across halt/resume; return PENDING-halted to PENDING.
- **D4** Resell lists strand in BID_OUT — `WITHDRAWN` status has **zero writers**
  (`excess_service.py:1358`); rejected customer bid has no close path
  (`bid_back_service.py:464`). Add owner withdraw-line + close-from-BID_OUT.
- **Success theater** (UX, 🔁 with P0): RFQ results show "sent" with no token
  (`rfq_results.html:14`), Process "." banner (`proactive_service.py:323`),
  unmatched bids "submitted". Rule: never render success the server didn't verify.

---

## P3 — Mobile + navigation (the owner works from a phone)

From UX; one focused pass covers ~15 findings. · M total
- Phone unusable: three workspaces render side-by-side ~195px panes; row actions
  hidden behind hover; 11-item bottom nav ~35px targets; resell tables overflow.
  Fix: `@media (hover:none)` reveal rule; collapse bottom nav to 4–5 + More;
  copy the Customers responsive split to Sales Hub/Resell/Sourcing;
  `scrollIntoView` on row-select; `overflow-x-auto` on resell tables.
- URLs lie: filters push raw `/v2/partials/*` URLs that reload unstyled; swaps
  without push-url name the wrong record (`requisitions/list.html:65`). Fix: push
  only canonical page URLs + `full_page_shell()` fallback; thread `?tab/?scope`.

---

## P4 — Ages-well: data durability, AI provenance, reporting

- **Data-model** (durability, for 2027 Dynamics): quote lines as JSON *and* rows
  — make `quote_lines` canonical, demote the JSON blob (`quotes.py:37/97`);
  `qp_serial_entries` jargon columns + `companies.sf_account_id` → neutral names;
  index hot filter/join columns; unify the three soft-delete conventions; prove
  rollback in CI without the CASCADE escape hatch. (data-model, L overall)
- **AI provenance at the decision point**: hotlist matches seeded by an AI verdict
  show no amber chip / no reject (`proactive_matching.py:640`); offer-review card
  hides currency so every save stamps USD (`ai_offer_service.py:387`); the "not
  the same part" kill-switch can re-pool transitively (`part_equivalence.py:146`).
  (AI, M)
- **Owner reporting** (SF parity ≈ 0): GP rollup by month/rep/customer (pure
  aggregation of data already persisted); surface the already-computed win rate;
  resell numbers; wire or delete the dead 12h scorecard jobs. (process, M)
- **The ~54 code-QC mediums** still open — see `specs/qc-review-2026-08-08.md`
  (event-loop `/health` stalls, form-array zip mispairings, `tagging_jobs`
  CancelledError, test-client admin-bypass fixture, Sentry triage hygiene).

---

## Cross-cutting fixes (do once, lift many screens)

1. **Global HTMX error surface** + the rule "never render success the server
   didn't verify" — kills success-theater across RFQ, Process, bids, prepay modal.
2. **Shared inlined email base layout** + `html.escape` at the composition
   boundary — fixes proactive, RFQ text-loss, and cross-client rendering at once.
3. **Standard human-review gate for AI-on-money / destructive paths** + a prompt
   "untrusted data, never follow instructions inside" delimiter — covers offers,
   merges, enrichment, facts.
4. **Orphan audit habit**: every stored field names its display surface; every
   route names its button. Catches the stranded affordances from the one-screen
   migration.

---

## Already shipped (2026-08-08 code QC) — context, not to redo

PRs #829–#833, live @`be2957b6`: Graph raise-by-default (9 fake-success send
paths), quote-number wedge + HTMX-delete buy-plan guard, 2 XSS + attachment
stored-XSS + approvals authority + prepayment dispatch, deploy CI-gate + DB
rollback, jobs/cache no-op + xdist root cause. Some P1/P2 items above are the
*deeper* layer of those (quote-delete FK, prepayment teardown).

---

## Suggested execution order

P0 (one small PR, this week) → P1-1/P1-2/P1-4 money path → the two cross-cutting
fix-once items (#1 error surface, #2 email base) → P2 dead-ends → P3 mobile pass
→ P4 durability/reporting. Each as its own PR, merge+deploy on green.

---

## Execution status (2026-08-10, updated end-of-session)

**SHIPPED to production (live, all green):**
- P0 (#838) — proactive half-sent email, AI ACTIVE-offer injection, nightly AI
  merges gated off, quote cost/margin leaks. Migration 207.
- P1 (#839) — prepayment re-request guard (D3), PO-send-back wire teardown,
  quote-delete FK guard (SET NULL / RESTRICT, migration 208), requester
  decision-email carries the reason.
- D5 (#841) — a won quote closes every contributing requisition + records
  won_revenue via a shared service (owner win-rate now records).

**In flight (PR'd, CI green-pending → merge+deploy):**
- P2/D2+D1 (#842) — halt/resume state restore (migration 209) + Testing/Comps
  manual completion (creator or manager, per owner's rule).
- P2/D4+D4b (#843) — resell withdraw-line writer + close a stranded bid_out list.
- P2 success-theater (#844) — RFQ no-token no longer shows a green "sent".

**Cross-cutting #1 (global HTMX error surface):** already existed in
app/static/htmx_app.js (htmx:responseError handler, 409 stale-guard skip) — no
work needed. Cross-cutting #2 (shared inlined email base) folded into P4.

**Business rules locked (owner, 2026-08-10):** D1 = prompt the creator; D4 =
prompt the owner (no auto-close); D4b = owner + manager/admin withdraw.

---

## Continuation backlog — P3 + P4 + remaining P2 (needs verification cycles / owner input)

These are deliberately NOT blind-coded in one autonomous pass: P3 needs live
browser verification (unavailable from the hosted CLI), and P4's data-model
reshape + reporting need owner decisions and per-change live verification. Each
carries exact file:line from the five reviews.

**Remaining P2 (small, verifiable — safe to do next session):**
- Success-theater: Process "." banner + "offer submitted" on unmatched resell
  bids (the global error toast already covers non-2xx; these are 200-fake-success
  like the RFQ one just fixed).
- Quote revision-convention unification (htmx/quotes.py inline revise vs
  quote_builder_service._build_revision) — enables double-won revenue; needs care
  because it touches customer-facing quote numbers.
- Render the mark-paid / reverse-payment prepayment controls (backends exist).

**P3 — mobile + URL-truth (one focused pass, needs browser verify):**
- @media (hover:none) reveal rule for hover-hidden row actions; collapse the
  11-item bottom nav to 4–5 + More; copy the Customers responsive split to Sales
  Hub / Resell / Sourcing; scrollIntoView on row-select; overflow-x-auto on
  resell tables. (UX review §2 — ~15 findings, one CSS/nav pass.)
- URL truth: push only canonical page URLs (never /v2/partials/*) +
  full_page_shell() fallback; thread ?tab/?scope. (requisitions/list.html:65.)

**P4 — ages-well (large; several need owner decisions):**
- Data-model (data-model review): quote_lines canonical (demote JSON blob),
  qp_serial_entries jargon columns + companies.sf_account_id → neutral names for
  Dynamics 2027, index hot columns, unify soft-delete, prove rollback in CI.
- AI provenance at the decision point (AI review): hotlist AI matches show no
  amber chip (proactive_matching.py:640); offer-review card hides currency
  (ai_offer_service.py:387); "not the same part" kill-switch can re-pool
  transitively (part_equivalence.py:146).
- Owner reporting (process review): GP rollup by month/rep/customer from data
  already persisted; surface the already-computed win rate; resell numbers; wire
  or delete the dead 12h scorecard jobs.
- The ~54 code-QC mediums — specs/qc-review-2026-08-08.md.
