# CRM Split-Screen Workspace — Design Spec

**Created:** 2026-04-30
**Status:** Approved
**Parent:** [CRM Master Roadmap](2026-03-29-crm-master-roadmap.md)
**Sibling specs:** [Phase 1](2026-03-29-crm-phase1-design.md) (shipped — staleness dots, CRM shell), [Phase 2a](2026-03-29-crm-phase2a-design.md), [Phase 2b](2026-03-29-crm-phase2b-design.md)
**Sequencing:** Stacked PR off `fix/ci-unblock-alembic-and-audit` (the migration 001 rewrite). Merges only after 001 lands on green main.

## Goal

Restore the split-screen master-detail pattern in CRM: a persistent left rail of accounts + right detail pane that swaps on click. Wire full integrations for click-to-call (8x8 Work API) and click-to-email (Microsoft Graph sendMail) so every interaction is automatically tracked into `ActivityLog`. Apply the pattern to both the Customers and Vendors tabs.

## Non-Goals

- Performance tab is untouched. It remains the third tab in the CRM shell, unchanged.
- Phase 2b's AI quality scoring of interactions is out of scope here; this spec produces the raw `ActivityLog` rows that Phase 2b consumes.
- Vendor discovery rethink (Phase 3) is out of scope.
- No new email-template model. Composer pre-fills the user's `email_signature` only; body is empty.

## Architecture

```
/v2/crm  → CRM shell (existing, unchanged routes; tab content swapped)
   └── #crm-tab-content
        ├── (Customers tab)  /v2/partials/crm/customers/workspace
        │   └── #crm-rail (left)   ⇄   #crm-pane (right)
        │
        └── (Vendors tab)    /v2/partials/crm/vendors/workspace
            └── #crm-rail (left)   ⇄   #crm-pane (right)
```

**Replacement, not addition.** The existing `/v2/partials/customers` and `/v2/partials/vendors` endpoints are repurposed to return the workspace layout. The flat list templates are deleted (single source of truth — see Approach A in the brainstorm). External links to `/v2/customers/{id}` continue to work: the route loads the workspace with `?account_id={id}` so the rail auto-focuses and the pane auto-loads.

```
Click rail row
  └─ hx-get /v2/partials/customers/{id}, hx-target=#crm-pane
        └─ companies_detail_partial (existing) returns customers/detail.html
              └─ pane swaps; rail row gains aria-current=true
              └─ URL pushed: /v2/crm/customers?account_id={id}

Click [Call] in detail pane
  └─ POST /v2/crm/contacts/{contact_id}/dial
        └─ eightxeight_service.click_to_dial(user, contact)
              └─ POST 8x8 click-to-dial REST endpoint
              └─ ActivityLog(event_type=call, direction=outbound, auto_logged=False,
                             external_id=<8x8 dial id>, occurred_at=now)
              └─ Company.last_activity_at = now
        └─ HX-Trigger: toast 'Calling Jane Doe — your phone will ring'

8x8 webhook (call completed)
  └─ POST /v2/crm/8x8/webhook (HMAC-verified)
        └─ eightxeight_service.handle_call_event(payload)
              └─ UPDATE ActivityLog WHERE external_id=<8x8 call id>
                 SET duration_seconds=N, details={recording_url, disposition},
                     auto_logged=True
              └─ Company.last_activity_at = call_end_ts

Click [Email] in detail pane
  └─ hx-get /v2/crm/contacts/{contact_id}/email-composer (modal partial)
        └─ Modal opens with To, Subject (blank), Body (user.email_signature only)
        └─ User fills, clicks [Send]
        └─ POST /v2/crm/contacts/{contact_id}/send-email
              └─ graph_send_service.send(user, contact, subject, body)
              └─ Microsoft Graph /me/sendMail
              └─ ActivityLog(event_type=email, direction=outbound, auto_logged=False,
                             external_id=<graph message id>, subject=...,
                             occurred_at=now)
              └─ Company.last_activity_at = now
        └─ Toast 'Email sent'; modal closes
```

**No model changes.** `ActivityLog` already has every column needed (`event_type`, `direction`, `external_id`, `duration_seconds`, `details JSON`, `subject`, `auto_logged`, polymorphic FKs). `User.email_signature` already exists. No migration in this PR.

## URL Routes

| Method | Path | Handler | Purpose |
|---|---|---|---|
| GET | `/v2/partials/customers` | `crm.views.customers_workspace` | Returns workspace HTML (rail + pane). Accepts `account_id`, `search`, `staleness`, `sort`, `my_only`, `hx_target`, `push_url_base`. Replaces today's flat list. |
| GET | `/v2/partials/customers/rail` | `crm.views.customers_rail` | Returns just the rail HTML (used for sort/filter/search re-renders). |
| GET | `/v2/partials/customers/{id}` | `htmx_views.companies_detail_partial` | Existing — returns detail HTML for the right pane. Unchanged. |
| GET | `/v2/partials/vendors` | `crm.views.vendors_workspace` | Vendor workspace. Same shape. |
| GET | `/v2/partials/vendors/rail` | `crm.views.vendors_rail` | Vendor rail re-render. |
| GET | `/v2/partials/vendors/{id}` | `htmx_views.vendor_detail_partial` | Existing — unchanged. |
| GET | `/v2/crm/contacts/{contact_id}/email-composer` | `crm.interactions.email_composer_modal` | Returns the composer modal HTML pre-filled with To + signature. |
| POST | `/v2/crm/contacts/{contact_id}/dial` | `crm.interactions.click_to_dial` | Triggers 8x8 click-to-dial; writes initial ActivityLog. Returns toast HX-Trigger. |
| POST | `/v2/crm/contacts/{contact_id}/send-email` | `crm.interactions.send_email` | Sends email via Graph; writes ActivityLog. Returns toast + close-modal HX-Trigger. |
| POST | `/v2/crm/8x8/webhook` | `crm.interactions.eightxeight_webhook` | HMAC-authenticated 8x8 call-event ingest. Updates the ActivityLog row by `external_id`. |

The "contact" addressed by `/v2/crm/contacts/{contact_id}/...` is polymorphic across `SiteContact` (customer-side) and `VendorContact` (vendor-side). The router resolves which by trying customer first, vendor second. ActivityLog FK chosen accordingly (`site_contact_id` vs `vendor_contact_id`).

## Left Rail

### Row layout (single-line ultra-dense)

```
● Acme Corp                                    12d
● BetaCo Inc.                                  41d  ← rose dot, overdue
● GammaInd Ltd                                  —   ← brand dot, never contacted
● DeltaCo                                       3d
```

- **Dot:** 8px circle, color from staleness tier (`overdue` rose-500, `due_soon` amber-400, `recent` emerald-400, `new` brand-300). Same calculation function as Phase 1 (`days = now - last_activity_at`; thresholds 30 / 14).
- **Name:** truncate with ellipsis at rail width.
- **Timeago:** right-aligned, gray-500. `|timeago` filter, `"—"` fallback for NULL.
- **No `Strategic` chip on the rail row** (saves horizontal space). The chip remains on the right-pane Overview header — the user already sees it on focus.
- **Active row:** `aria-current="true"` + `bg-brand-50` background.

### Controls (top of rail, top-down order)

1. **Search input.** Existing pattern; debounced 300ms; HTMX target = `#crm-rail`. Searches name + MPN + owner-name (existing query).
2. **Staleness filter chips.** `All • Overdue • Due Soon • Recent • New` — only one active at a time. Click re-renders rail. Default: `All`.
3. **Sort dropdown.**
   - **Customers:** `Most overdue` (default) | `Recently contacted` | `Name A–Z` | `Last created`.
   - **Vendors:** `Recently active` (default) | `Top engagement score` | `Name A–Z` | `Sighting count desc`.
4. **My accounts toggle (customers only).** Hidden for non-managers (it's the only mode they have). For managers (`role in ("manager","admin")`), default = off (show all); on = filter to `account_owner_id == user.id`. Vendors do not show this toggle (vendors are open).

### "Needs Attention" band

A static rose-tinted band sits between the controls and the row list. Visual chrome only — no expansion (the rail is already sorted overdue-first by default, so duplicating those rows inside the band would be noise):

```
┌──────────────────────────────────────────┐
│ ⚠ Needs Attention            3 overdue   │   bg-rose-50 border-rose-200
│   [Show overdue only]                    │
└──────────────────────────────────────────┘
```

- Renders only when `count(overdue accounts visible to user) > 0`.
- The count is a server-rendered number from the same query that drives the rail (no second query).
- The `Show overdue only` button activates the `Overdue` staleness chip (same effect as clicking the chip directly). Once active, the band hides itself (filter is now applied; the band would be tautological).
- Re-renders when the rail re-renders (search, filter, sort changes).

### Account-type scope

The customer rail includes **all `Company` rows regardless of `account_type`** — Customer, Prospect, Partner, Competitor. Today's customer list already does this; staleness still applies. Reasoning: a salesperson cycling contacts wants to see prospects too, and the dot color already differentiates relationship age.

### Ownership scoping (auth-enforced, not just filter)

Centralized helper added to `app/routers/crm/_helpers.py`:

```python
def scope_companies_to_user(query, user):
    """Apply ownership scoping for the customer rail.

    Managers/admins see everything. Everyone else sees only their own.
    """
    if user.role in ("manager", "admin"):
        return query
    return query.filter(Company.account_owner_id == user.id)
```

Called from `customers_workspace` and `customers_rail`. Vendors do **not** use this helper — vendors are open to all CRM users.

The detail-pane endpoint `/v2/partials/customers/{id}` also enforces scoping: a non-manager loading another rep's account by URL gets a 403. (Today's endpoint does not enforce this — added as part of this work.)

### Selection persistence

Querystring drives the layout:
- `?account_id=N` → rail focuses + pane loads detail.
- `?search=X&staleness=overdue&sort=name&my_only=1` → rail filters/sorts.

URL is pushed via `hx-push-url` on the workspace endpoint. Browser back/forward works. Bookmarks work.

### Keyboard navigation

Bound by an Alpine `x-on:keydown.window` handler on the rail container:

| Key | Action |
|---|---|
| `↓` / `j` | Move focus to next visible row |
| `↑` / `k` | Move focus to previous visible row |
| `Enter` | Open focused row in pane (HTMX trigger same as click) |
| `Esc` | (Mobile-only, when in detail mode) collapse pane → return to rail |
| `/` | Focus the search input |

The handler is a no-op when focus is inside an input/textarea (so `j`/`k` don't hijack typing).

## Right Pane

### Existing detail templates reused (no new templates for tabs)

`customers/detail.html` and `vendors/detail.html` are loaded into `#crm-pane` exactly as they render today, with two additions:

1. **Click-to-call buttons** added to every contact row (in the Contacts tab + the Overview's primary-contact card). Replaces today's `tel:` link with a button that fires the dial endpoint.
2. **Click-to-email buttons** added beside every email address. Opens the composer modal.

The detail templates are parameterized to receive `pane_target="#crm-pane"` and `push_url_base="/v2/crm/customers"` (or `/v2/crm/vendors`) so any in-template HTMX (e.g. tab switches inside detail) stays scoped to the pane and updates the URL correctly. This extends the same parameterization pattern Phase 1 introduced for `hx_target`.

### Contact-row UI (inside the existing Contacts tab)

```
Jane Doe               VP Sourcing
☎  (212) 555-0142   ← click-to-dial
✉  jane@acme.com    ← click-to-email
[📞 Call] [✉ Email]  ← buttons (touch-friendly)
```

Each contact row is a self-contained Alpine island. The button click POSTs to the interaction endpoint with `hx-include` of the contact ID. Server returns:
- HX-Trigger: `{ "showToast": {"type": "success", "message": "Calling Jane Doe — your phone will ring"} }` for dial.
- HX-Trigger: `{ "showToast": {...}, "closeModal": true }` for send-email.

(Existing toast store at `$store.toast` consumes these. Pattern matches the rest of the app per CLAUDE.md.)

## 8x8 Click-to-Dial Integration

### Service module — `app/services/eightxeight_service.py`

Public functions:
- `click_to_dial(user: User, contact_phone: str, contact_label: str) -> str` — POSTs to 8x8 click-to-dial REST endpoint with the rep's user identity (looked up via `user.eightxeight_extension`, a new optional column on `User` — see Migration). Returns the 8x8 dial-id string used as `external_id`.
- `verify_webhook(headers, body) -> bool` — HMAC-SHA256 verification using `EIGHTXEIGHT_WEBHOOK_SECRET`. Returns False on bad signature; route returns 401.
- `handle_call_event(payload: dict, db: Session) -> None` — Updates ActivityLog by `external_id`; sets `duration_seconds`, `details["disposition"]`, `details["recording_url"]`, `auto_logged=True`, `occurred_at=call_end_ts`. Idempotent on repeat webhooks.

### User column — `eightxeight_extension`

`User` gains one nullable column: `eightxeight_extension VARCHAR(50)`. Maps the AvailAI user to their 8x8 extension number. Set by an admin in the user settings page (out of scope for this spec — sales reps coordinate with IT). Without it, the click-to-dial button on the detail pane is **disabled with a tooltip** ("Your 8x8 extension isn't configured — contact your admin").

This **is** a model change → one Alembic migration: `add_eightxeight_extension_to_users.py` with explicit `op.add_column()` and matching `op.drop_column()` downgrade. No `Base.metadata.create_all()`.

### Env vars (added to `.env.example`)

```
EIGHTXEIGHT_API_BASE_URL=https://api.8x8.com/...
EIGHTXEIGHT_API_KEY=...
EIGHTXEIGHT_WEBHOOK_SECRET=...
```

### Failure handling

- 8x8 API non-200 → ActivityLog **not** written; route returns HX-Trigger toast "Couldn't start call: {error}". User retries.
- 8x8 webhook arrives without a matching ActivityLog (e.g. dial that didn't go through us, or out-of-order delivery) → log a warning, no-op. We don't manufacture rows.
- 8x8 webhook arrives twice with the same `external_id` → second one is a no-op (idempotency by external_id).

## Graph Send-Email Integration

### Service module — `app/services/graph_send_service.py`

Public function: `send(user, to_email, to_name, subject, body) -> str` — calls `/me/sendMail` via existing `app/utils/graph_client.py`. Returns the Graph message-id (used as `external_id`).

### Composer modal — `app/templates/htmx/partials/crm/email_composer.html`

Loaded via HTMX into the existing `#modal` mount. Form fields:
- `To:` `{{ contact.email }}` (read-only, with `(name)` next to it)
- `Subject:` empty input
- `Body:` `<textarea>` pre-filled with `{{ user.email_signature or "" }}` at the bottom (a blank line above so the rep can type)

Submit: HTMX POST → endpoint validates non-empty subject + body, calls `graph_send_service.send`, writes ActivityLog, returns close-modal + toast.

### Failure handling

- Graph 4xx (e.g. invalid recipient, throttled) → ActivityLog not written; modal stays open with inline error.
- Graph 5xx → same; user retries.
- We **do not** retry server-side: a failed send must be visible to the rep (silent failure on outbound email is unacceptable).

## Vendor Side Parity

What's the same:
- Workspace layout, rail row format, controls, keyboard nav, mobile collapse.
- Click-to-call and click-to-email semantics + endpoints (the same router resolves vendor contacts).
- Staleness dot calculation reuses the same function against `VendorCard.last_activity_at`.

What's different:
- **No `My accounts` toggle** (vendors are open). All CRM users see all vendors.
- **No ownership scoping** in either the rail query or detail endpoint.
- **Sort default** is "Recently active". Other sort options: Top engagement score (uses `engagement_score`), Name A–Z, Sighting count desc.
- **`Strategic` chip is irrelevant** — vendor cards don't have that field.
- **Phone resolution** for click-to-dial: `VendorContact.phone` → fallback to first item in `VendorCard.phones` JSON list. Disabled (greyed) if neither available.

## Mobile Collapse

Tailwind `lg:` breakpoint = 1024px. Below it:

```
< 1024px:                ≥ 1024px:
┌──────────────┐         ┌──────────┬──────────────┐
│   #crm-rail  │         │  rail    │     pane     │
│  (full vw)   │         │ (320px)  │  (flex-1)    │
└──────────────┘         └──────────┴──────────────┘
   tap account →
┌──────────────┐
│ < back  pane │
│  (full vw)   │
└──────────────┘
```

CSS (no JS). Both `#crm-rail` and `#crm-pane` are present in the DOM at all viewports. Below `lg`:
- `#crm-rail` is `block` when `account_id` is absent; `hidden` when `account_id` is present.
- `#crm-pane` is the inverse.
- A back button rendered above the pane (visible only at `< lg`) clears `account_id` from the URL via `hx-get` to the workspace endpoint with rail-only context.

Above `lg`: both panels visible side-by-side; the back button is hidden.

## Performance Tab

Untouched. The third tab in the CRM shell still calls `/v2/partials/crm/performance`. Not part of this redesign.

## File Plan

### New files

| File | Responsibility |
|---|---|
| `app/services/eightxeight_service.py` | 8x8 click-to-dial + webhook handler |
| `app/services/graph_send_service.py` | Graph sendMail wrapper |
| `app/routers/crm/interactions.py` | The 4 new endpoints (composer, dial, send, webhook) |
| `app/templates/htmx/partials/crm/customers_workspace.html` | Rail + pane shell, customers tab |
| `app/templates/htmx/partials/crm/vendors_workspace.html` | Rail + pane shell, vendors tab |
| `app/templates/htmx/partials/crm/_rail.html` | Shared rail body (rows + Needs Attention band). Receives `accounts`, `kind` ("customer" / "vendor"). |
| `app/templates/htmx/partials/crm/_rail_controls.html` | Search + chips + sort + (optional) My toggle |
| `app/templates/htmx/partials/crm/email_composer.html` | Modal contents |
| `alembic/versions/<next>_add_eightxeight_extension_to_users.py` | Model change (one column) |
| `tests/test_crm_workspace.py` | Workspace + rail + scoping tests |
| `tests/test_crm_interactions.py` | Click-to-dial, click-to-email, webhook tests |
| `tests/services/test_eightxeight_service.py` | Unit tests with mocked 8x8 client |
| `tests/services/test_graph_send_service.py` | Unit tests with mocked Graph client |

### Modified files

| File | Change |
|---|---|
| `app/routers/crm/views.py` | Add `customers_workspace`, `customers_rail`, `vendors_workspace`, `vendors_rail` routes. Mount `interactions` sub-router. |
| `app/routers/crm/_helpers.py` | Add `scope_companies_to_user`. |
| `app/routers/crm/__init__.py` | Register `interactions` sub-router. |
| `app/routers/htmx_views.py::companies_detail_partial` | Enforce ownership scope (403 for non-owners non-managers). Accept `pane_target` / `push_url_base` context. |
| `app/routers/htmx_views.py::vendor_detail_partial` | Accept `pane_target` / `push_url_base` context (no scoping change). |
| `app/templates/htmx/partials/customers/detail.html` | Add click-to-call + click-to-email buttons on contact rows; honor `pane_target`. |
| `app/templates/htmx/partials/vendors/detail.html` | Same. |
| `app/templates/htmx/partials/customers/tabs/site_contacts.html` | Replace `tel:`/`mailto:` with the new buttons. |
| `app/templates/htmx/partials/vendors/tabs/contacts.html` | Same. |
| `app/templates/htmx/partials/crm/shell.html` | Default-tab loading already correct; verify it lazy-loads workspace not the legacy list. |
| `app/models/auth.py::User` | Add `eightxeight_extension = Column(String(50))`. |
| `.env.example` | Add three EIGHTXEIGHT_* env vars. |

### Deleted files

| File | Why |
|---|---|
| `app/templates/htmx/partials/customers/list.html` | Replaced by `customers_workspace.html` + extracted row markup in `_rail.html`. Single source of truth. |
| `app/templates/htmx/partials/vendors/list.html` | Replaced by `vendors_workspace.html` + extracted row markup in `_rail.html`. Same pattern, parity with customer side. |

## Permissions

- **Customers tab** is visible to all CRM users (anyone with `require_user`). Data is scoped by `scope_companies_to_user`. Non-managers see their own; managers/admins see all.
- **Vendors tab** is visible to all CRM users; no scoping.
- **Detail endpoint scoping (customers)** is enforced server-side. A direct GET to `/v2/partials/customers/{other_reps_account_id}` returns 403 for non-managers. The `My accounts` toggle on the rail is a UX convenience only.
- **`POST /v2/crm/contacts/{id}/dial`** requires the contact's company/vendor be visible to the user (same scope check); otherwise 403.
- **`POST /v2/crm/contacts/{id}/send-email`** same.
- **`POST /v2/crm/8x8/webhook`** is unauthenticated by session but HMAC-verified via `EIGHTXEIGHT_WEBHOOK_SECRET`. Bad signature → 401.

## Testing

| Suite | Coverage |
|---|---|
| `tests/test_crm_workspace.py` | Workspace renders rail + pane skeleton; rail filters/sorts produce expected row order; `account_id` query param auto-loads pane; non-manager can't load other rep's account (403); manager sees all; vendors tab renders for all; `My accounts` toggle hidden for non-managers. |
| `tests/test_crm_interactions.py` | Dial endpoint creates ActivityLog with `external_id`; missing `eightxeight_extension` returns 400 (not 500); 8x8 webhook with valid HMAC updates the log; bad HMAC → 401; duplicate webhook → idempotent; send-email endpoint creates log + closes modal; Graph 4xx surfaces as inline error not silent failure. |
| `tests/services/test_eightxeight_service.py` | `click_to_dial` posts the right body; `verify_webhook` accepts good HMAC, rejects bad; `handle_call_event` updates duration + details. |
| `tests/services/test_graph_send_service.py` | `send` posts to `/me/sendMail`; bubbles 4xx and 5xx as exceptions. |

All run under existing SQLite test fixture. 8x8 + Graph clients mocked at module boundary (HTTP layer).

E2E (Playwright, `tests/e2e/crm-split-screen.spec.ts`): rail filter + sort + click; pane swap; mobile breakpoint collapse + back; click-to-call disabled when `eightxeight_extension` is null; composer modal opens, sends, closes, shows toast.

## Migration / Rollout Plan

This is **not** a feature-flagged rollout. The new workspace replaces the existing flat list at the same URLs. Reasoning: dual-rendering paths violate single source of truth, and the user has explicitly chosen the new pattern.

Rollout sequence:
1. Migration 001 rewrite (sibling PR) lands first → CI green.
2. This PR rebases onto green main.
3. Pre-merge checks: full pytest suite green, Playwright workspace spec green, manual smoke on staging (rail render + click + dial mock + email mock + mobile breakpoint).
4. Merge.
5. Post-merge: monitor `ActivityLog` row creation rate (should jump for `event_type=call,direction=outbound,auto_logged=False`). If 0 calls in 24h, that's a regression signal — investigate.
6. **Rollback path:** revert PR. No data migration to undo (the `eightxeight_extension` column is additive, harmless to leave).

## What This Does NOT Include

- AI quality scoring of new ActivityLog rows (Phase 2b — separate spec).
- 8x8 admin UI for assigning extensions to users (out of scope; reps coordinate with IT or an admin sets it via direct DB or Settings page extension).
- Vendor discovery rethink (Phase 3).
- Email body templates (per-user signature only).
- New CRM tabs or restructuring of the shell (Customers | Vendors | Performance unchanged).
- Changes to existing detail-page tab structure (Overview / Contacts / Activity / etc.).
- Changes to the Performance tab content.
