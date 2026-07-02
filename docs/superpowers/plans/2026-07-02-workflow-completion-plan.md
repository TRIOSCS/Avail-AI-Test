# AvailAI Workflow-Completion Program — Phased Plan (2026-07-02)

Follow-on to the production-polish program (35 PRs merged + deployed, build `48b49571`).
These are the **design-fork decisions** the user made on 2026-07-02 — features/policy to
BUILD, not just polish. Durable tracker so nothing falls behind.

**Standing execution rules:** TDD + tests with every change · parallel worktree
subagents for disjoint work · adversarial review of every fix (the PERF-4 catch proved
its worth) · CI-gated PRs, merge on green, verify main green before stacking · deploy via
`./deploy.sh` + live-verify after each phase · HTMX+Alpine (not React) · no band-aids.

---

## Phase 1 — Contained wiring (low-risk, existing backends → UI)
Parallel worktree agents, adversarial review, one themed PR each. No new design needed.

1. **Reclaim/reassign prospects UI** — wire the existing reclaim/reassign endpoints to buttons on the Prospecting tab (the sweep email already promises them). [DC-02]
2. **CRM create/edit-account buttons** — surface the existing create-account + whole-form edit-account forms with buttons in the CRM (today accounts arrive only via CSV import). [F7]
3. **Quote Delete-draft button** — wire the existing `DELETE /v2/partials/quotes/{id}` to a Delete button on a draft quote's detail. [OQ-10 part]
4. **Manage-connectors capability gating** — make the advertised `MANAGE_CONNECTORS` grant actually let a non-admin reach the Connectors settings tab (settings_partial gate + tab visibility). [SET-06]
5. **Sales-Hub relabel + view toggle** — workspace stays canonical "Sales Hub"; relabel the flat requisitions list to "Requisitions list"; add a discoverable toggle between the two views. [REQ-12]
6. **Proactive inline add-contact** — on the Proactive Prepare page, add an inline "add contact" affordance when the site has no emailable contact (unblocks Send). [PROACTIVE-04]
7. **Watchdog false-alarm fix** — enrichment-worker emits a heartbeat during its 1h daily-cap sleep (or the liveness watchdog exempts capped-sleep), so it stops false-alarming "heartbeat stale" every cap window. [ops]

**Acceptance:** each wired action works end-to-end with a render/behavioral test; targets exist in served DOM (deep-link + workspace contexts); CI green; deployed + smoke-verified.

---

## Phase 2 — Standalone features (short design each → build)
Each gets a one-paragraph design decision resolved below (no TBDs), then TDD build.

8. **"Send All" follow-ups actually sends** — mirror the single-follow-up Microsoft Graph send path per stale contact (same DNC hard-block + swallow-and-report error handling); return an honest summary card ("N sent, M skipped/failed"), NOT a blanket "responded" mark. [F-followups]
9. **Quote terms editor + Preview** — on quote detail: an inline/modal editor for payment_terms/shipping_terms/notes/valid_until posting to the existing `/quotes/{id}/edit` with the `recent-terms` datalist; a "Preview" button opening the existing `/quotes/{id}/preview` render in the modal. [OQ-08 + OQ-10 preview]
10. **Resell award-winner picker** — owner-only "Award" action on the Offers tab that calls the existing award-offer endpoint. **Design decision:** award is **per-offer** (mark the winning offer); the list moves to `awarded` when the owner awards (matches the existing award endpoint granularity — CONFIRM against the endpoint before building). [RS-3]
11. **Resell outreach reply-tracking** — wire `record_response` (the reply adapter) into the inbound reply/email-mining pipeline so outreach tracker statuses advance past "sent". [RS-4]
12. **Bulk cross-req quote builder** — fix `/quote-builder/multi` route ordering (register before `/{req_id}`) + restore the `#quote-builder-content` modal shell + `open-quote-builder` listener. **Design decision:** 2+ selected requisitions build **ONE combined quote** containing lines from all selected reqs (cross-req = combined). [OQ-02/REQ-04]

**Acceptance:** feature works end-to-end; behavioral + query-count/CSRF tests; CI green; deployed + verified.

---

## Phase 3 — Buy-plan approval + Quality-Plan workflow (design session → build)
The big, entangled, policy+architecture change. **A dedicated design spec is written and
user-reviewed BEFORE building** (this phase does not start with code).

13. **Remove auto-approve threshold** — delete `po_auto_approve_threshold`; **every** buy plan routes through a manager-approval gate (no dollar-based auto-approve). [BP-2 policy]
14. **PO-gate decide UI** — build the approve/reject decide surface (My-Queue entry + Approve banner on the plan) so approved plans reach ACTIVE→INBOUND→receiving; PO approvers' nav badge points at real work. [BP-2/BP-3]
15. **Merge Quality Plan into the buy plan** — fold the QP (currently a separate /v2/qp/* module with its own section gates) INTO the buy-plan detail surface as embedded sections, not a tacked-on separate function. Remove the dead top-level "Submit for Review" absorbing state as part of the merge. [QP-1 + user direction]

**Gate:** Phase 3 begins only after its design spec (`docs/superpowers/specs/2026-07-<dd>-buyplan-quality-workflow-design.md`) is written and approved.

---

## Sequencing
Phase 1 (parallel, now) → deploy · Phase 2 (per-feature) → deploy · Phase 3 (design → approve → build) → deploy. Each phase is independently shippable; nothing blocks Phase 1.
