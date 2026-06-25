# Quality Plan + Approvals Engine — Phase 1 Design Spec

**Date:** 2026-06-25
**Status:** Approved design — Phase 1 of the multi-phase QP + Approvals program
**Source brief:** Mike's "AVAIL — Quality Plan + Approvals Engine" planning brief (2026-06-25)
**Grounded by:** Phase 0 Discovery (this session) against the live repo

---

## 1. Goal

Build the **Quality Plan (QP)** natively in AVAIL with the **Buy Plan** as a section inside it,
and a **shared Approvals Engine** that all approval gates run on. This replaces the Teams Approvals
app, the Teams Planner board, and the manual SharePoint QP spreadsheet. The QP is the hub document
per TSO; each section is gated by an approval.

Phase 1 delivers the **engine foundation + the Prepayment gate + a native QP shell**, and *bridges*
the existing buy-plan approval (already gated on the new right) into one Approvals UI. The remaining
gates and sections follow in later phases.

## 2. Decisions locked (Discovery + Mike, 2026-06-25)

| Decision | Choice |
|---|---|
| Engine vs existing approval | **Build the generic engine**; run NEW gates (Prepayment) + QP on it now; **bridge** the existing buy-plan approval; migrate buy-plan onto the engine in **Phase 1.5**. |
| QP document output | **Native AVAIL view; retire the spreadsheet.** No xlsx generation, no SharePoint render in P1. |
| Notifications v1 | **Email (existing Graph) + in-app only.** Teams Adaptive Cards deferred to Phase 4. |
| Acctivate SO/PO | **Record-only** (keep manual `sales_order_number` / `customer_po_number`); no write-back. |
| Prepayment approvers | **Myrna ≤ $1k, Mike any amount, Marcus any amount** — any-of, first-responder-wins; configured in the extended manager-approval page. |

## 3. Grounded reuse (Phase 0 Discovery)

- **Offer** (`app/models/offers.py`) already carries ~16 of the QP Buy-Plan-section fields
  (`unit_price` NUMERIC, `condition`, `packaging`, `warranty`, `country_of_origin`, `lead_time`,
  `date_code`, `moq`, `spq`, `manufacturer`, `mpn`, …). **Add 6:** `is_primary`, `sourcing_type`,
  `vendor_rating`, `terms`, `location`, `specifics`.
- **BuyPlan** (`app/models/buy_plan.py`) already has the full approval workflow (status transitions,
  `approved_by_id`/`approved_at`/`approval_notes`, `BUYPLAN_APPROVED/REJECTED` activity types). The
  in-flight `feat/buyplan-approval-review` already **wired `require_buyplan_approver` onto it** — that
  IS the Phase-1 bridge.
- **ActivityLog** (`app/models/intelligence.py`, polymorphic, has `buy_plan_id` + approval activity
  types) backs the unified timeline; the engine also keeps its own append-only `approval_event`.
- **Graph email** (`app/services/graph_app_auth.py` app-only + the delegated token path) is reused by
  the NotificationService. SharePoint write access exists (datasheet library) but is **not needed** in
  P1 (native view).
- **Authz:** `UserRole` (BUYER/SALES/TRADER/MANAGER/ADMIN/AGENT) + the new `User.can_approve_buy_plans`
  flag + `app/dependencies.py` gate pattern.
- **Migrations:** additive; current head `154_drop_dead_cols`; the P1 migration chains after the
  in-flight roles(155)/avatar(156) land.

## 4. Scope

**In Phase 1**
- The generic Approvals Engine tables + config.
- The **Prepayment** gate as the first new native gate, running on the engine.
- A native **Quality Plan** object (header) with a one-screen view; the Buy-Plan section reuses the
  existing `BuyPlan` + its (now-gated) approval surfaced through the unified Approvals UI.
- Approvals-config UI = **extension of the manager-approval page** (per-gate approvers + thresholds).
- Email + in-app notifications via an idempotent outbox.
- Completeness gate on submit; server-side per-recipient authorization.

**Out of Phase 1 (later phases, named so nothing is silently dropped)**
- Sales-Order & PO gates and the QP Sales/Purchasing sections → **Phase 2**.
- Serial/FRU section + ops auto-capture, and the Planner-parity **Approvals Board** → **Phase 3**.
- Teams Adaptive Cards → **Phase 4**.
- Acctivate write-back → deferred. xlsx/SharePoint QP render → cut (native view).
- Migrating the existing buy-plan approval to *write through* the engine → **Phase 1.5** (next step).

## 5. Data model

> NUMERIC for money; index status/type/owner/subject FKs. Native subjects use FKs; external SO/PO
> (later) use `erp_doc_number`. All columns nullable unless stated.

**Engine**
- `approval_request` — `id`, `gate_type` (StrEnum `buy_plan|prepayment|sales_order|purchase_order`),
  `subject_quality_plan_id` FK?, `subject_prepayment_id` FK?, `erp_doc_number`?, `status`
  (StrEnum `requested|approved|rejected|cancelled|expired`), `outcome`?, `amount` NUMERIC?, `currency`
  (default `USD`), `requested_by_id` FK, `owner_id` FK, `created_at`, `decided_at`?.
- `approval_step` — `id`, `request_id` FK, `seq` (int), `rule` (StrEnum `any|all`, P1 always `any`),
  `status`. P1 creates exactly one step per request.
- `approval_step_recipient` — `id`, `step_id` FK, `user_id` FK, `status`
  (`pending|approved|rejected|reassigned`), `responded_at`?, `comment`?, `reassigned_from_id`? FK,
  **`UNIQUE(step_id, user_id)`**.
- `approval_event` — append-only audit: `id`, `request_id` FK, `actor_id` FK?, `event_type`,
  `metadata` JSONB, `created_at`. (A parallel summary row is also written to `ActivityLog` for the
  unified timeline.)
- `approval_outbox` — `id`, `request_id` FK, `event_type`, `payload` JSONB, `status`
  (`pending|sent|failed`), `attempts`, `created_at`, `sent_at`?. Idempotency key on
  `(request_id, event_type)` to prevent double-send.
- `approval_gate_config` — `id`, `gate_type`, `approver_user_id` FK, `max_amount` NUMERIC? (null =
  no cap), `active` (bool). One row per (gate, approver). Edited from the manager-approval page.

**Quality Plan**
- `quality_plan` — `id`, `tso_ref`? (manual), `customer_id` FK, `owner_id` FK, `order_type` (StrEnum
  `new|revision`), `revision_reason`?, `status` (StrEnum `draft|in_review|approved|…`), `created_at`,
  `submitted_by_id`? FK, `submitted_at`?, `buy_plan_id`? FK (links the Buy-Plan section to an existing
  BuyPlan).

**Prepayment**
- `prepayment` — `id`, `buy_plan_id` FK? (or PO ref), `vendor_card_id` FK, `payment_method` (StrEnum
  `cc|paypal|wire`), `total_incl_fees` NUMERIC, `currency`, `test_report_sent` (bool),
  `buyer_remarks`?, `status`, `created_by_id` FK, `created_at`. A `prepayment` spawns an
  `approval_request` of `gate_type='prepayment'` with `amount = total_incl_fees`.

**Offer extension** (migration adds to the existing table): `is_primary` (bool, default false),
`sourcing_type` (StrEnum `spot|contract|commodity|preferred`?), `vendor_rating` (NUMERIC(3,1)?,
0–5), `terms` (JSONB?), `location` (String?), `specifics` (Text?). Backfill `is_primary=false`.

## 6. Routing & thresholds

- On submit, `RoutingService.route(request)` reads `approval_gate_config` for the `gate_type`, selects
  eligible approvers (`active` AND (`max_amount` IS NULL OR `request.amount <= max_amount`)), and creates
  one `approval_step` (`rule='any'`) with an `approval_step_recipient` per eligible approver.
- **Prepayment seed:** Myrna `max_amount=1000`, Mike `max_amount=NULL`, Marcus `max_amount=NULL`.
  A $2,500 prepayment therefore routes to **Mike + Marcus** only; a $400 one to **all three**.
- **First-responder-wins:** the decide path takes `SELECT … FOR UPDATE` on the request, checks it is
  still `requested`, records the recipient decision, and closes the request (`approved`/`rejected`).
  Concurrent second decision sees a non-`requested` request and is a no-op (idempotent). The
  `UNIQUE(step_id,user_id)` prevents double-recipient rows.
- Reassign delegates a recipient's slot (`reassigned_from_id` set; new recipient added). Reject is
  terminal and requires a non-blank reason.

## 7. The bridge (existing buy-plan approval)

The existing buy-plan approval already works and is now gated on `can_approve_buy_plans`
(`feat/buyplan-approval-review`). Phase 1 **surfaces it in the unified Approvals UI** by reading the
BuyPlan approval state (no behavior change). **Phase 1.5** introduces a `gate_type='buy_plan'`
`approval_request` whose decide path drives the BuyPlan status transition through the engine, retiring
the bespoke approve route. No throwaway: the bridge is read-only first, then becomes the engine's
first migrated gate.

## 8. API surface (thin routers → fat services)

- Approvals: `POST /v2/approvals/requests/{id}/decision` · `…/reassign` · `…/cancel` ·
  `GET /v2/approvals/requests` (filters) · `GET /v2/approvals/requests/{id}`.
- QP: `POST /v2/qp` (create, auto-fill header from requisition/quote) · `GET /v2/qp/{id}` ·
  `PATCH /v2/qp/{id}` · `POST /v2/qp/{id}/submit` (completeness gate → routes gates).
- Prepayment: `POST /v2/prepayments` (create → spawns the prepayment gate) · `GET /v2/prepayments/{id}`.
- Config: `GET/POST` the gate-config rows from the extended manager-approval admin page.

## 9. Services

- `QualityPlanService` — create/auto-fill, completeness validation, submit → spawn gate requests.
- `ApprovalService` — create / decide / reassign / cancel (transaction + outbox enqueue + event).
- `RoutingService` — resolve step + recipients from `approval_gate_config` (amount thresholds).
- `ApprovalEventService` — append-only audit + the summary `ActivityLog` row.
- `NotificationService` — reuse Graph email + in-app `Notification`; dispatched from the outbox.
- `OutboxDispatcher` — scheduler job draining `approval_outbox` idempotently; terminal-outcome subject
  hooks (mark section approved / unlock prepayment) run here, not inline.

## 10. UI (HTMX + Alpine + Jinja, dense, native)

- **QP one-screen:** collapsible sections (Sales / Purchasing / Buy Plan / Serial — only Buy Plan is
  populated in P1; the others are present, collapsed, "Phase 2"), each with an inline approval-status
  chip. The Buy-Plan section is a dense offers grid (primary + back-ups) reusing the existing
  buy-plan detail rendering.
- **Approvals queue:** a manager/approver lens listing pending requests (filter by gate/owner), with
  Approve / Reject (reason) / Reassign + comment — gated so only eligible recipients act.
- **Config:** the manager-approval page gains a per-gate approver+threshold table (seed Prepayment).
- Read-first: a glance shows all gate chips without clicking. No modal sprawl, no wizard.

## 11. Completeness gate & authorization

- `QualityPlanService.validate_complete(qp)` blocks `submit` if required fields are blank, returning a
  field-level error list rendered inline (directly kills the recurring blank-field approval errors).
- `require_approval_gatekeeper(request, db)` resolves the request + asserts the acting user is a
  pending recipient (or their delegate) — server-side, never UI-only. Admins are not auto-eligible
  unless configured.

## 12. Migration

One additive migration (chained after roles `155` + avatar `156`): create the 6 engine tables +
`quality_plan` + `prepayment`; `op.add_column` the 6 Offer columns (backfill `is_primary=false`); seed
`approval_gate_config` for Prepayment (Myrna/Mike/Marcus). Fully reversible downgrade. No destructive
ops. Revision id ≤32 chars; claimed in `MIGRATION_NUMBERS_IN_FLIGHT.txt`.

## 13. Testing (write alongside)

- Routing: any-of first-responder closes; amount threshold includes/excludes correctly (Myrna gated
  at $1k); eligible-set from config.
- Concurrency: two simultaneous decisions → one wins, idempotent (row lock + `UNIQUE(step,user)`).
- State machine: cannot decide a terminal/cancelled request; reject requires reason + is terminal;
  reassign delegates.
- Completeness gate: a QP with a blank required field is blocked from submit (field errors surfaced).
- Outbox: a terminal decision enqueues exactly one event per `(request,event_type)`; no double-send;
  subject hook runs once.
- Authz: only assigned recipients/delegates can decide (cross-user → 403, on real Postgres).
- API: each endpoint happy + auth + error path.

## 14. Risks & mitigations

| Risk | Mitigation |
|---|---|
| Concurrency on decision | `SELECT … FOR UPDATE` + `UNIQUE(step,user)`; idempotent decide. |
| N+1 on queue/QP reads | eager-load step→recipients; index `owner_id`, `status`, `gate_type`, subject FKs. |
| Offer table widening | partial index on `(is_primary, sourcing_type)`; the 6 columns are nullable. |
| Outbox double-send | idempotency key `(request_id, event_type)`; dispatcher is at-least-once + dedup. |
| Money precision | NUMERIC + `currency`, never float. |
| Migration safety | additive only; reversible; never touches data-bearing drops. |

## 15. Deferred / needs Mike (later phases)

- PO + Sales-Order approvers and $ thresholds (Phase 2). Full QP Sales/Purchasing/Serial field sets.
- The real **user roster** to seat all approvers (P1 seeds Myrna/Mike/Marcus only).
- Whether an xlsx export is ever wanted back (currently cut).
- Acctivate write-back (deferred). Teams Adaptive Cards (Phase 4).

---

*Phase 1.5 (immediate follow-on): migrate the existing buy-plan approval to decide through the engine
(`gate_type='buy_plan'`), retiring the bespoke approve route.*
