# Buy-Plan Epic (G–K) — Design Spec (2026-07-05)

**Goal:** finish the buy-plan / sales-order workflow the owner walked through — list fields (G),
Create button (H), role-gated line editing (I), SO-number field (J), Cancel/Halt (K).

**Confirmed decisions (from the 2026-07-05 planning Q&A):**
- **Edit gate (I):** BEFORE approval (Draft/Pending) sales OR manager can edit; AFTER approval, **manager-only**. No re-approval trigger — the manager's edit authority IS the control (respects "leave Approvals unchanged").
- **Cancel/Halt (K):** Cancel (sales) → `cancelled` + reason required. Halt (manager) → `halted` + reason required + **manager can Resume** → `active`. Completed/Cancelled locked.

## What ALREADY exists (extend/expose — do NOT rebuild)
- `BuyPlan.status` enum: draft / pending / active / inbound / halted / completed / cancelled (`constants.py:443`).
- `BuyPlan.sales_order_number` (String(100)) — J is exposing this editable (`models/buy_plan.py:82`).
- **Cancel fields:** `cancelled_at`, `cancelled_by_id`, `cancellation_reason` (`buy_plan.py:123-125`).
- **Halt fields:** `halted_at`, `halted_by_id` — **NO reason column** (`buy_plan.py:126-127`).
- Router `app/routers/htmx/buy_plans.py`: create flow (`/sales-orders/new` + `/sales-orders/create`), detail (`/{plan_id}`), tabs, `/submit`, `/approve`, **`/halt`**, line ops (`/lines/{id}/confirm-po`, `/resource`, `/claim`). Cancel handling referenced in the file header ("issue, cancel, reset").
- List templates: `approvals/_tab_buy_plan.html` (Approvals decide console) + `buy_plans/hub.html` + `buy_plans/detail.html`.
- `BuyPlanLine` (`buy_plan.py:175`) with per-line status — the unit for add/remove/vendor/qty/price editing.

## To BUILD

### Migration 186 (additive, reversible)
- Add `buy_plans.halt_reason` (Text, nullable) — Halt reason (Cancel reason already exists). Round-trip on throwaway PG; claim in MIGRATION_NUMBERS_IN_FLIGHT.

### G — list fields
In `approvals/_tab_buy_plan.html` (+ the `buy_plan_tracking_rows` context builder), each row shows **Customer, Revenue (Σ line sell), Sales GP (Σ sell − Σ cost, $ and %), and the part numbers (line MPNs)** alongside the existing Plan #/status/value. Derive from BuyPlan + its lines (batch, no N+1).

### H — Create Buy Plan button
Surface a clear **"Create Buy Plan"** button on the Approvals buy-plan list → launches the existing `/v2/partials/buy-plans/sales-orders/new` create flow. Confirm at build whether that flow seeds a blank plan the user fills vs requires a source SO; relabel/adjust so it reads as "Create Buy Plan."

### I — role-gated line editing
An editable buy-plan **line UI** (add/remove lines, per-line **vendor**, **qty**, **price**) + save endpoint(s), reusing the existing line ops where possible. **Gate:** status ∈ {draft, pending} → sales OR manager may edit; status ∈ {active, inbound, halted} → **manager-only**; {completed, cancelled} → locked. Enforce server-side (not just UI hiding).

### J — SO-number field
Expose `sales_order_number` as an editable field on the plan detail; **sales** can set/edit it at any non-terminal stage.

### K — Cancel / Halt / Resume
- **Cancel** (sales, +manager): status → `cancelled`, `cancellation_reason` **required** (400 if blank), stamp `cancelled_by/at`. Only from a non-terminal status.
- **Halt** (manager-only): status → `halted`, **`halt_reason` required**, stamp `halted_by/at`. Extend the existing `/halt` endpoint to require the reason + write the new column.
- **Resume** (manager-only): `halted` → `active`. New endpoint/action; keep the halt audit fields for history.
- Role-gate all three server-side. Cancel/Halt/Resume buttons rendered per role + status on the plan detail.

### Authz
Define the sales-vs-manager check once (reuse the app's role/manager gate — `require_manager` / role == MANAGER/ADMIN, and `reports_to` where relevant). Apply to: post-approval edits (mgr), Halt/Resume (mgr), Cancel (sales+mgr), SO-number (sales+mgr).

## Tests (TDD, per feature)
List shows customer/Rev/GP/parts; Create launches the flow; pre-approval sales edit OK, post-approval sales edit → 403 but manager OK; SO-number editable + persists; Cancel requires reason (400 blank) + sets cancelled; Halt requires reason + manager-only; Resume manager-only halted→active; terminal statuses reject edits. Migration 186 round-tripped.

## Open confirmations for build
- The exact current Create flow behavior (blank vs SO-seeded) — adjust H accordingly.
- Whether a Cancel endpoint already exists (header implies yes) — wire/confirm reason-required.
- Sales-GP definition: `Σ(sell − cost)` per line; confirm the line has both sell + cost fields (or where cost lives).
