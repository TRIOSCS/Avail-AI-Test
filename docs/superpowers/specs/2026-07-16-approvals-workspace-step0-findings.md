# Approvals Workspace — Step 0 codebase-verification findings (2026-07-16)

Product contract: `specs/approvals-workspace.md` (v4). This doc records what the July-2026
code sweep verified before any workspace code was written: real names vs spec names, the
QP column split, the storage gaps Mike approved migration **192** for, and the binding
constraints inherited from the 06-28/06-29 approvals rework. Read this before touching
any phase of the build.

## Mike's approvals (2026-07-16) — migration 192, ALL SIX items

1. `buy_plans_v3.order_type` — String(20) NOT NULL server_default `'new'`; new `SOOrderType`
   StrEnum: `new / revision / testing_service / comps_program / stock_sale`. Non-sourcing
   types take the lite approval path. (`QPOrderType` only has `new`; `BuyPlan.is_stock_sale`
   is a derived vendor-name flag, not a chosen type.)
2. `buy_plan_lines.payment_method` — String(20) nullable, set at confirm-PO;
   `PaymentMethod` gains `ACH='ach'` + `COD='cod'` (code-level — `Prepayment.payment_method`
   is an unconstrained String(20), so ACH there needs NO migration).
3. `buy_plan_lines.received_at` + `received_by_id` (FK users SET NULL) — backs the manual
   mark-received action. **There is no receiving event anywhere**: `BuyPlanStatus.INBOUND`
   was retired by migration 176; completion is `verify_po → check_completion` auto-complete.
4. `activity_log.buy_plan_line_id` + `activity_log.prepayment_id` (nullable FKs, SET NULL)
   + indexes + a `(buy_plan_id, created_at)` index — per-item notes threads.
5. Three attachment tables mirroring the existing 11-column per-entity family:
   `buy_plan_attachments`, `buy_plan_line_attachments`, `prepayment_attachments`
   (NOT polymorphic — `attachment_service.store_and_attach` is parameterized by
   model + fk_field + entity_label; registration points: `_DELETE_BASE`, `_KIND_MODEL`,
   `_check_serve_access`, the `_attachments.html` `_urls` map).
6. QP purchasing parity columns — `purchasing_traceability_verified` (Bool),
   `purchasing_counterfeit_risk` (Bool), `purchasing_risk_level` (String(20)),
   `purchasing_coc_available` (Bool), `purchasing_vendor_rating` (String(255)).
   These four/five AS9120B workbook fields had NO home anywhere in the codebase
   (vendor rating existed only as `sales_vendor_rating`).

## Load-bearing name corrections (spec → real)

| Spec says | Reality |
|---|---|
| halt / resume / cancel / reset-to-draft | `halt_plan` / `resume_plan` / `cancel_buy_plan` / `reset_buy_plan_to_draft` (buyplan_approval.py; routes exist for all four) |
| `po_queue` | `build_po_queue_view()` — services/approvals/po_queue.py |
| "PO send-back route gains the note parameter if it lacks one" | It does NOT lack one: `verify_po(..., rejection_note=)`; route already passes form `rejection_note` |
| "every edit already writes ActivityLog (who/field/old→new)" | **False today.** Line/plan edit functions write ZERO audit rows and have NO stale-edit guard. The field-edit trail is a separate `ChangeLog` model (entity_type/entity_id/field_name/old_value/new_value — generic strings, so buy-plan entities need no schema change). ActivityLog has no field/old/new columns. |
| four roles | Six `UserRole` values: buyer/sales/**trader**/manager/admin/**agent** (trader = sales-tier RESTRICTED_ROLES; agent = non-interactive) |
| `po_approval_limit` | `purchase_order_approval_limit` (User) |
| "in-app notification" | The `Notification` table is **write-only** (no reader UI). Real in-app signal = the nav alert-badge system (`routers/alerts.py` + `services/alerts/` AlertSource registry). |
| `verify_po_sent` display | Exists but its scheduler job discards results; UI display needs an on-demand call (display-only, never auto-verifies). |
| teal theme / 900px breakpoint | Accent is Trio azure `accent-*` (#007DBD); no 900px breakpoint exists — split panes stack at `lg` (1024px) per the `sightings/list.html` precedent. |
| Tab badges | Old hub pills were org-wide; the workspace badges are per-viewer (`services/approvals_workspace.waiting_counts`). |

## QP fold — column split (all on `QualityPlan` unless noted)

- **SALES**: sales_condition, sales_quantity, sales_fw_hw_rev (FW/HW/REV + date & week
  codes), sales_product_commodity, sales_testing_required/_option/_specifics,
  sales_test_location, sales_serial_preapproval_required, sales_authorized_ship_early,
  sales_authorized_ship_partial, sales_routing_prescreening_whs, sales_vendor_rating,
  sales_third_party_pkg_ok, sales_pkg_requirements, sales_bom_matrix_links, sales_notes.
  SO# canonical on `BuyPlan.sales_order_number`; customer PO on `BuyPlan.customer_po_number`;
  per-serial customer-approval tracking on `QpSerialEntry` (incl. `ops_received`).
- **PURCHASING**: purchasing_po_number, purchasing_condition, purchasing_fw_hw_rev,
  purchasing_product_commodity, purchasing_testing_required/_option,
  purchasing_routing_prescreening_whs, purchasing_packaging, purchasing_tpo_ship_complete,
  purchasing_tpo_notes (+ the five migration-192 parity columns). Per-PO quantity =
  `BuyPlanLine.quantity`; serial fields on `QpSerialEntry`.
- QP is **one per (buy_plan, vendor_card) pair** — a line's purchasing section is its
  vendor's QP; lines sharing a vendor share one section. Section review = Mark-Reviewed
  stamps (`toggle_section_reviewed`), never surfaced as approvals. Edit endpoints:
  `PATCH /v2/qp/{id}/sales` + `/purchasing` (typed coercion, per-section validation).

## Binding constraints (from the 06-28/29 rework + 07-02/03 designs)

- No auto-approve; every submit → PENDING + `_open_engine_request_for_plan`.
- The single manager approval IS the SO sign-off (`_run_approve_side_effects` stamps
  `so_status`); never resurrect verify-SO, the deal-level PURCHASE_ORDER gate, INBOUND
  receiving, or the QP section gates.
- Any transition out of PENDING outside `decide()` must cancel open engine requests.
- `decide()` enqueues in_app + email ApprovalOutbox rows (locked dual-channel);
  outbox drain uses per-row commit, never SAVEPOINT.
- Prepay: requested→approved→paid|void; single-use pay_token; `/p/confirm/{token}`
  public + CSRF-exempt; an APPROVED prepayment is never auto-voided; PAID never touched.
- No code path creates an SO or PO — Acctivate does. Vendor edits are offer swaps
  (`_ensure_offer_attachable`), never free text.
- Known debt to clean during the rebuild: the legacy `approve_buy_plan` fallback in
  `buy_plan_approve_partial` (was promised for removal in the C1 rework).
- `htmx_app.js` `htmx:afterSwap` Alpine re-init uses a hardcoded ID allowlist —
  new swapped regions with `x-*` directives must be added (`ws-body`/`ws-pane` were).

## Build state

- Branch `feat/approvals-workspace` (worktree), one commit per phase.
- Phase 1 (shell) shipped: `services/approvals_workspace.py`, `approvals/workspace.html`,
  `_ws_tab_*.html` ×4, `_ws_macros.html`, per-viewer badges, copy chips, split views.
- Full 9-agent recon reports (routers/services/templates/models/docs, with file:line
  references) were generated in-session; this doc is the durable distillation.
