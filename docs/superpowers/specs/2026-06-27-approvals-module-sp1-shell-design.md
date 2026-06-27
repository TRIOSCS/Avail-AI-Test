# SP-1 — Approvals Module Shell (rename + stage-tab restructure)

**Status:** approved design (2026-06-27)
**Program:** Approvals-module program (4 sub-projects). This is **SP-1 of 4**.
**Sibling specs (future):** SP-2 Sales Order → manager gate; SP-3 PO execution + receiving; SP-4 fall-down → re-source.

---

## 1. Goal

Re-frame the existing "Buy Plans" Deal Hub as an **Approvals** module whose top-level
tabs are the **lifecycle stages**. SP-1 is the *shell only*: rename the module, restructure
the lens switcher into stage tabs, and re-home today's surfaces into those tabs. **No new
submission flows, no new approval semantics, no lifecycle-state changes** — those land in
SP-2/SP-3/SP-4.

Success = a user lands on `/v2/approvals`, sees the stage tabs, and every surface that
exists today (deal board, buyer orders, re-sourcing, the four gate queues, supervise triage)
is reachable in its new home with unchanged behavior, gating, and the just-shipped
deal-board role-scoping (PR #534) intact.

## 2. Scope

**In scope (SP-1):**
- Rename the user-facing module: nav label `Buy Plans → Approvals`; primary route
  `/v2/buy-plans → /v2/approvals` with a 302 redirect from the old path.
- Replace the 5 role-lenses with 5 **stage tabs** and re-home existing bodies into them.
- Role-adaptive in-tab layout (work surface + pinned "Pending approvals (N)" section).
- Per-role default landing tab.
- Tests + APP_MAP doc updates.

**Out of scope (deferred):**
- "New request" buttons / submission forms → SP-2 (Sales Order), SP-3 (PO/Prepayment).
- Sales-Order-as-submission-unit + manager gate reconciliation → SP-2.
- PO `inbound`/`received`/`mark-complete`/archive states → SP-3.
- Parts-rejected-in-receiving fall-down → SP-4.
- Internal identifier rename (template dir `buy_plans/`, `buyplan_hub` service, the
  `"buy-plans"` nav/alert key, helper names). These stay as-is to keep the SP-1 diff
  focused on the restructure; a later mechanical-rename cleanup can align them. Display
  names and URLs are user-facing and DO change here; internal symbols are not and do not.

## 3. Current state (relevant)

- **Nav:** `app/templates/htmx/partials/shared/mobile_nav.html` — the `('buy-plans',
  'Buy Plans', '/v2/buy-plans', '/v2/partials/buy-plans', …)` entry. The Approvals alert
  count is merged onto this single nav badge (no separate Approvals nav item).
- **Page route:** `GET /v2/buy-plans` → the full-page `v2_page` loader, which threads and
  validates `?lens=` into the lazy partial URL (added in PR #533).
- **Hub shell:** `app/templates/htmx/partials/buy_plans/hub.html` — an Alpine lens switcher
  over `[deals, orders, resource, approvals, supervise]` + a lazy `#bp-hub-body` that loads
  the active lens body. `GET /v2/partials/buy-plans?lens=` (`buy_plans_list_partial`)
  resolves the lens (falling back to `_default_lens`) and renders the shell.
- **Lens bodies (existing partial routes in `app/routers/htmx_views.py`):**
  `/v2/partials/buy-plans/board` (deal board, role-scoped via `_can_see_all_deals` +
  All/Mine toggle), `/orders` (buyer queue + team awareness), `/resource` (open re-source
  pool), `/approvals` (the four-gate queue), `/supervise` (manager triage + all-scope board).
- **Four-gate queue:** `app/services/approvals/queue.py::build_queue_view(db, user, tab)` —
  returns a `QueueView` with `tabs` (per-gate counts), `pending_rows`, `resolved_rows`.
  Gate keys: `buy_plans`, `sales_orders`, `purchase_orders`, `prepayments`. Today these are
  *sub-tabs* of the single Approvals lens. Template `approvals/_queue.html`.
- **Gating helpers (`htmx_views.py`):** `_can_supervise` (manager/admin/ops),
  `_can_resource` (PO-cutters), `_can_approve_any` / per-gate `can_approve_*` user toggles,
  `_can_see_all_deals` (PR #534), `_default_lens` (role → landing lens).

## 4. Target information architecture

**Module:** `Approvals` (was Buy Plans). **Tabs (lifecycle order):**

| # | Tab (label) | `lens=` key | Work surface (re-homed, unchanged) | Approval queue section (gate) | Visible to |
|---|---|---|---|---|---|
| 1 | Sales Orders | `sales_orders` | *(none yet — SO build flow = SP-2)* | `sales_order` gate queue | all; queue section to `can_approve_sales_orders` |
| 2 | Buy Plans | `buy_plans` | deal board (`/board`, role-scoped + All/Mine) | `buy_plan` gate queue | all; queue section to `can_approve_buy_plans` |
| 3 | Purchase Orders | `purchase_orders` | buyer Orders (`/orders`) + Needs Re-sourcing (`/resource`) | `purchase_order` gate queue | all; queue section to `can_approve_pos` |
| 4 | Vendor Prepayments | `prepayments` | *(none — approval-only stage)* | `prepayment` gate queue | all; queue section to `can_approve_prepayments` |
| 5 | Supervise | `supervise` | existing manager triage + all-scope board | — | manager/admin/ops only (`_can_supervise`) |

The standalone four-gate **Approvals lens is retired** — each gate's pending+resolved queue
now renders *inside its stage tab* as a pinned section. `build_queue_view` is reused per
tab by passing the single gate key (it already accepts a `tab` param), so no queue-building
logic is rewritten.

## 5. Detailed design

### 5.1 Nav + route
- `mobile_nav.html`: change the entry's **label** to `Approvals` and **href** to
  `/v2/approvals` (and the partial URL to `/v2/partials/approvals`). The internal nav
  **key stays `buy-plans`** (so `_base_ctx(request, user, "buy-plans")` active-nav
  highlighting and the merged alert badge keep working untouched).
- Move the full-page `v2_page` shell handler to `GET /v2/approvals` (threading `?lens=`),
  and make `GET /v2/buy-plans` a **302 → `/v2/approvals`** (preserving any `?lens=`), so
  bookmarks/old links survive. Likewise
  `GET /v2/partials/approvals` → the hub-shell partial (the renamed
  `buy_plans_list_partial`), and keep `/v2/partials/buy-plans` working (alias or 302) for
  in-flight htmx.
- The existing `GET /v2/approvals/queue` → `/v2/buy-plans?lens=approvals` redirect (PR #533)
  is repointed to `/v2/approvals?lens=buy_plans` (the retired combined-approvals URL now
  lands on the first stage tab).

### 5.2 Hub shell (`hub.html`)
- Replace the lens list with the 5 stage tabs (table §4), preserving the Alpine-reactive
  active-pill pattern and the `#bp-hub-body` lazy-load landmine guard (explicit
  `hx-target="#bp-hub-body"`).
- Tab buttons gate-render: `supervise` only when `can_supervise`. All four stage tabs are
  always shown (the *work surface* and the *queue section* inside each are what gate by role).

### 5.3 Tab bodies (new composing partials)
One partial route per tab under `/v2/partials/approvals/<tab>`, each composing existing
pieces — no new read models:
- `sales-orders` → renders the `sales_order` gate queue section only.
- `buy-plans` → renders the existing deal-board body **+** a pinned `buy_plan` queue section.
- `purchase-orders` → renders the existing orders body **+** the resource body **+** a pinned
  `purchase_order` queue section.
- `prepayments` → renders the `prepayment` gate queue section only.
- `supervise` → renders the existing supervise body unchanged.

The pinned **"Pending approvals (N)"** section is a small shared partial that takes the
per-gate `build_queue_view(...)` result and renders the pending rows (inline approve/reject
for eligible recipients, exactly as today) + a link/disclosure to recently-resolved. It is
rendered **only** when the viewer can approve that gate (`can_approve_<gate>`); otherwise the
tab shows just the work surface. Approvals remain **org-wide among approvers** (unchanged).

The previous combined Approvals lens route (`/v2/partials/buy-plans/approvals`) and its
four-sub-tab `approvals/_queue.html` are **retired** — each gate now renders in its own tab.
The **Sales Orders** and **Vendor Prepayments** tabs have no work surface in SP-1, so a
non-approver opening them sees a neutral empty state ("No sales orders yet" / "No prepayment
requests"); the SO submission flow fills the Sales Orders tab in SP-2.

### 5.4 Default landing tab (`_default_lens` rewrite)
- buyer → `purchase_orders`
- manager / admin / ops (`_can_supervise`) → `supervise`
- sales / trader / other → `buy_plans`
- (Validation: an unknown `?lens=` falls back to this role default, as today.)

### 5.5 Gating summary (all unchanged semantics, just re-homed)
- Tabs always present except Supervise (manager/ops only).
- Work surfaces keep their current gating (deal board via `_can_see_all_deals`; orders/
  resource as today).
- Each tab's queue section uses the matching `can_approve_<gate>` predicate.

## 6. Components to change

- `app/templates/htmx/partials/shared/mobile_nav.html` — label + href.
- `app/routers/htmx_views.py` — add `/v2/approvals` + `/v2/partials/approvals` routes;
  302 the old paths; rewrite `_default_lens`; add the 5 per-tab composing partial routes;
  repoint the `/v2/approvals/queue` redirect.
- `app/templates/htmx/partials/buy_plans/hub.html` — stage-tab switcher.
- New: `app/templates/htmx/partials/approvals/_pending_section.html` (shared pinned queue
  section) and 5 thin tab-body templates (or reuse existing bodies via `{% include %}`
  inside per-tab wrappers).
- `app/services/approvals/queue.py` — no logic change; called per-gate. (Confirm
  `build_queue_view` cleanly returns a single-gate view when given a fixed tab.)
- `docs/APP_MAP_ARCHITECTURE.md` + `docs/APP_MAP_INTERACTIONS.md` — update the Deal-Hub
  section to the Approvals module + stage tabs.

## 7. Request flow

```
GET /v2/approvals[?lens=]            full page → v2_page shell (threads ?lens=)
  └─ GET /v2/partials/approvals?lens=  hub shell (switcher + lazy #bp-hub-body)
       └─ GET /v2/partials/approvals/<tab>   tab body = work surface + pinned queue section
GET /v2/buy-plans[?lens=]            302 → /v2/approvals[?lens=]   (back-compat)
```

## 8. Testing

- **Routing/redirect:** `/v2/buy-plans` → 302 `/v2/approvals` (preserving `?lens=`);
  `/v2/approvals` renders the shell with the 5 stage tabs; `/v2/approvals/queue` → first tab.
- **Tab gating:** Supervise tab hidden for sales/buyer, shown for manager/ops; each tab's
  pending-approval section appears only for the matching `can_approve_<gate>` user and is
  absent otherwise.
- **Re-homing:** the deal board (with the All/Mine toggle from #534) renders under the
  Buy Plans tab; orders + re-source render under Purchase Orders; supervise unchanged.
- **Default landing:** buyer → purchase_orders, manager → supervise, sales → buy_plans.
- **No regressions:** existing buy-plan/approvals/board/supervise route tests updated for the
  new URLs; full suite green (`-n auto`).

## 9. Risks / notes

- `v2_page` authenticates via the session cookie (`get_user`), so full-page route tests need
  the `nonadmin_client` fixture (seeds a signed session), not the plain `require_user`
  override (lesson from PR #533).
- Keep `?lens=` as the query-param name (already threaded+validated by `v2_page`); renaming
  to `?tab=` is deferred to the internal-rename cleanup to avoid touching the threading.
- Don't start docstrings with a `"` (docformatter↔ruff oscillation); run `pre-commit` twice
  when docformatter rewraps.

## 10. Out of scope → which SP

| Deferred item | Lands in |
|---|---|
| New Sales Order build-from-RFQ-offers flow + SO-includes-buy-plan + manager SO gate | SP-2 |
| New Buy Plan / New PO / New Vendor Prepayment buttons + forms | SP-2 / SP-3 |
| PO `inbound` state; buyer "mark received / complete"; explicit archive action | SP-3 |
| Parts-rejected-in-receiving → fall-down → re-source | SP-4 |
| Internal identifier rename (`buy_plans/` dir, `buyplan_hub`, `"buy-plans"` key) | later cleanup |
