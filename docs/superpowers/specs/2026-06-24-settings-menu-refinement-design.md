# Settings Menu Refinement — Design Spec

**Date:** 2026-06-24
**Branch:** `feat/settings-refine`
**Status:** Approved (brainstorm complete) — ready for implementation plan
**Goal:** Make the full Settings menu optimized, refined, simple, effective, and ready for **group testing** (multiple human testers, some non-technical, logging in as **non-admin** users).

---

## 1. Context & audience (resolved)

The Settings menu today crams three audiences into one tab bar:

- **Your own stuff** — Profile (every authenticated user).
- **Admin/ops tooling** — Connectors, System, Data Ops, Ops Group (admin-gated).
- **A maintainer triage inbox** — Tickets (shown to *everyone* but admin-gated behind the scenes; leaks other users' reports).

**Decisions locked during brainstorming:**

1. **Tester access:** group testers log in as **non-admin** users. They see **only Profile** (and submit feedback via the floating "Report a Problem" button). All four admin tabs stay hidden. Tickets must be **hidden from non-admins**.
2. **Admin tabs scope:** **full polish** — safety guards *and* refinement (jargon → plain language, success/error feedback, consistent styling, System gets real typed controls).
3. **Tickets:** **stays in Settings**, gated admin-only, drill-in **kept inside the Settings shell**, broken wiring fixed.
4. **Profile:** polished **and** functional — editable display name, settable 8×8 extension, and **two notification toggles** (buy-plan update emails + new-offer alerts).
5. **System flags:** the curated toggles must **actually control behavior** (root-cause), not be cosmetic.

**Non-goals (YAGNI):** no new admin tabs; no relocation of Tickets out of Settings; no React/SPA; no redesign of the supplier-connector data model; no new notification *channels* beyond wiring an on/off onto the two existing mechanisms.

---

## 2. Architecture principle

Build **one shared feedback/safety backbone** from conventions that already exist in the codebase, then bring every tab up to that bar. **Reuse verbatim — invent no new components.**

### 2.1 The backbone (applied to every tab)

| Concern | Canonical pattern to reuse | Source |
|---|---|---|
| **Success toast** | Server attaches `response.headers["HX-Trigger"] = json.dumps({"showToast": {"message": "...", "type": "success"}})`. A body-level bridge pipes it into `$store.toast`. | `htmx_app.js:359-364`; helper at `htmx_views.py:13146-13148` (3 existing copies) |
| **Error toast** | Handler returns 4xx with JSON `{"error": "..."}`; global `htmx:responseError` handler auto-toasts it red. **No new banner markup.** | `htmx_app.js:337-352` |
| **Confirmation** | `hx-confirm="..."` (native dialog). Rose chip for destructive, brand chip for additive. Single-quote the attr + `|e` when interpolating user values. | `_macros.html:44`, `ops_group.html:51-54` |
| **Empty state** | Centered ghost icon (`h-10/12 w-10/12 text-gray-300 stroke-width=1.5`) + `text-sm text-gray-600` heading + optional `text-xs text-gray-400` hint (+ CTA when useful). | `requisitions/tabs/tasks.html:165-172`, `resell/_lists.html:88-103` |
| **Card** | `bg-white border border-brand-200 rounded-lg p-4` (simple) or `...overflow-hidden` + header band `px-5 py-4 bg-gray-50 border-b border-brand-200`. Headings `text-lg font-semibold text-gray-900`; help `text-xs text-gray-600`. | `profile.html:9`, `ops_group.html:7`, `data_ops.html:10-11` |
| **Buttons** | Component classes only: `.btn-primary/.btn-secondary/.btn-danger/.btn-ghost` + `.btn-sm/md/lg`. Inline chip buttons (Add/Remove/merge) use rose/brand chip pattern. | `styles.css:49-59` |
| **Palette** | `brand-*` scale; status = emerald (success) / rose (error) / amber (warn) / brand (info). Safelist regex already covers these families (`tailwind.config.js:9-20`). | `tailwind.config.js:34-46` |

> **Deploy check (memory):** after `./deploy.sh`, verify any new Tailwind class appears in the built CSS. Staying within the safelisted families + component classes means no safelist edit is needed.

A small server helper `_settings_toast(response, message, kind="success")` (copy of the existing 3 instances) will live once in `app/routers/htmx_views.py` (or a shared util) and be reused by every settings mutation handler.

---

## 3. Per-tab specifications

### 3.1 Profile (tester-facing — highest polish + functional)
**Files:** `app/templates/htmx/partials/settings/profile.html`, `app/templates/htmx/partials/settings/_mailbox_sync_card.html`, `app/routers/htmx_views.py` (profile view `:13793-13805`, toggle-8x8 `:13837-13850`), `app/models/auth.py`.

Changes:
1. **Remove redundancy / placeholders:** delete the duplicate "Email" field (already shown under the name) and the `Additional profile settings coming soon.` placeholder.
2. **Editable display name:** new `POST /api/user/profile` (`require_user`) updating `user.name` (1–255 chars, stripped, non-empty). Inline edit affordance (pencil → text input → Save/Cancel; Escape cancels), success toast. **Verified safe:** `user.name` is set from Azure only inside the `if not user:` create branch (`auth.py:131`); re-login never overwrites it. New users still seed name from Azure `displayName` on first login.
3. **8×8 extension:** add a text input bound to `user.eight_by_eight_extension` (`String(20)`, column exists, **no write path today**). Persist via the same `POST /api/user/profile` (or a dedicated `POST /api/user/8x8-extension`). Relabel "8×8 Click-to-Call" → "Click-to-call (8×8 phone)" with one-line help. Keep the existing enable toggle (`/api/user/toggle-8x8`).
4. **Notification toggles (two):** add a "Notifications" card with two switches mirroring the 8×8 toggle pattern:
   - **"Email me about buy-plan updates"** → gates outbound buy-plan emails (see §4.1).
   - **"Alert me about new approved offers"** → gates the new-offer FYI nav badge (see §4.2).
   Both default **on**; both fire a success toast on change; both persist via dedicated `POST` endpoints cloned from `toggle_8x8`.
5. **Mailbox-sync card:** plain-language copy; real disconnected empty state ("Mailbox not connected" + what to do) instead of bare "Not connected / never"; show `m365_error_reason` as friendly text, not a raw string; keep "Scan now" but make failure surface an error toast.
6. **Styling:** unify the (currently ad-hoc) three cards to the canonical card style; keep the `max-w-2xl` inner cap.

### 3.2 System (admin — largest transform; real controls)
**Files:** `app/templates/htmx/partials/settings/system.html`, `app/routers/admin/system.py`, `app/services/admin_service.py`, `app/startup.py` (seed `:332-347`), the flag consumers (see §4.3).

The table is replaced by a curated, typed UI driven by a **key→meta map owned in code** (there is no `type` column in `system_config`):

| key | control | label | default |
|---|---|---|---|
| `email_mining_enabled` | toggle | "Email mining" | from env |
| `proactive_matching_enabled` | toggle | "Proactive offer matching" | from env |
| `activity_tracking_enabled` | toggle | "CRM activity tracking" | from env |
| `inbox_scan_interval_min` | number (min 5) | "Inbox scan interval (minutes)" | from env |

Each control shows a plain-language **label + one-line description**. Toggles fire success toast; number input validates `>= 5` server-side (4xx `{"error":...}` → error toast).

- **Internal watermark rows hidden** from the editor: `teams_calls_last_poll`, `8x8_last_poll`, `proactive_last_scan`. Optionally surfaced **read-only** in a collapsed "Job state (read-only)" disclosure — never editable.
- **Drop the cosmetic "sensitive" masking** (it matches none of the real keys; real secrets live encrypted in `api_sources.credentials`).
- **Make toggles real (root-cause):** see §4.3.
- `set_config_value` 404s on unknown keys, so the curated UI hard-codes its 4 keys; it never invents keys.

### 3.3 Connectors (admin — already most polished; finish it)
**Files:** `app/templates/htmx/partials/settings/connectors.html`, `_connector_macros.html`, `app/routers/sources.py`, `app/routers/clay_oauth.py`, `app/connector_status.py`.

1. **Empty state** when no `ApiSource` rows exist (today: bare header + counters over blank body).
2. **Confirmations** on app-wide-destructive actions: Clay **Disconnect** (`hx-confirm`, "This disconnects Clay enrichment for everyone. Continue?") and **disabling a currently-live data source** via the enable toggle (warn that searches will return fewer results).
3. **Success toasts** on Save key / Save credentials / enable toggle (today silent JSON `{saved:true}`).
4. **"Test all" aggregate result** line ("Tested 8 · 2 failed") after the sweep, since per-card pill changes are easy to miss on a long page.
5. **Unify status vocabulary:** the header ("need attention"), group header ("need setup"), and pill labels are three lexicons for the same idea — collapse to one consistent set (recommend pill labels as canonical; header/group reuse them).
6. Dead-provider `_DEAD` set is duplicated in two functions — consolidate to one shared constant (maintainability, not user-facing).

### 3.4 Data Ops (admin)
**Files:** `app/templates/htmx/partials/settings/data_ops.html`, `_macros.html` (`merge_button`), `app/routers/admin/buy_plan_ops.py` (or wherever vendor/company merge handlers live), the dedup-scan route in `htmx_views.py`.

1. **Real error banner** when a dedup scan throws: today the scan is wrapped in try/except that logs a warning and renders the normal "No duplicates found" empty state — a failed scan is indistinguishable from clean data. Add an explicit error state distinct from empty.
2. **Disambiguate merge direction:** add a "suggested keep" hint to **vendor** rows (companies already have it); make the two `Keep "…"` buttons visually distinguish keeper (brand) vs. the row being dropped, and widen the truncation so similar names don't render identically.
3. **Consistent refresh affordance** + fix "company" vs "customer" wording within the Company card.
4. **Refresh list after merge** so stale pairs referencing a just-merged entity drop out (today they linger until manual re-entry). Company rows also get the vendor rows' `x-data` "merged" guard for consistent post-merge behavior.

### 3.5 Ops Group (admin — safety-critical)
**Files:** `app/templates/htmx/partials/settings/ops_group.html`, `app/routers/admin/buy_plan_ops.py` (`toggle_ops_member`).

1. **Backend invariant guards (root-cause):**
   - **Refuse removing the last active member** — return 4xx `{"error": "At least one active member is required to verify buy plans."}` → auto error toast. Honors the tab's own copy.
   - **Refuse self-removal** — return 4xx `{"error": "You can't remove yourself from the verification group."}`.
2. **Confirm on Add** (it grants SO/PO verification authority): `hx-confirm`.
3. **Loading/disable** on Add/Remove (`data-loading-disable`) to prevent a double-click double-toggle.
4. **Success toast** after toggle; **humanize** the Role column (title-case/labelled, not raw enum); fix the "Member since shows on a removed user" contradiction (only show for active/inactive members, with a clear status lexicon).

### 3.6 Tickets (admin — gate + fix, kept in shell)
**Files:** `app/templates/htmx/partials/settings/index.html` (button), `_macros.html` (`tab_button`), `app/templates/htmx/partials/tickets/{workspace,list,_row,detail}.html`, `app/routers/htmx_views.py` (view partials `:16149/:16161/:16211`, full-page route `:319`), `app/routers/error_reports.py`.

1. **Gate admin-only:**
   - Move the Tickets `tab_button(...)` call **inside** the `{% if is_admin %}` block in `index.html` (currently at line 23, outside the block).
   - Flip route deps `require_user → require_admin` on the **view** partials: workspace (`:16149`), list (`:16161`), detail (`:16211`), and the screenshot endpoint (`error_reports.py:267`, closes the cross-tester screenshot leak).
   - **Keep `require_user`** on the submission path: `GET /api/trouble-tickets/form`, `POST /api/trouble-tickets/submit`, `POST /api/trouble-tickets` (the floating "Report a Problem" button must stay open to all).
2. **Keep drill-in inside the Settings shell:**
   - Add an optional `target` param to the `tab_button` macro (default `#settings-content`).
   - Retarget `_row.html` and `detail.html` from `#main-content` to `#settings-content` and **drop `hx-push-url`** so drill-in stays in the settings tab.
   - The standalone full-page route `/v2/trouble-tickets` (`:319`) becomes a **redirect to the Settings page with the Tickets tab active** (single, consistent in-shell entry point; the only existing entry today is the settings tab button anyway). "Back to Tickets" re-loads the workspace/list into `#settings-content`.
3. **Fix the Analyze button (broken):** `/api/trouble-tickets/analyze` currently returns `HTMLResponse("")` + dead `HX-Trigger: ticketsUpdated` (zero listeners) into `hx-target=#ticket-list` → blanks the list. **Fix:** re-render and return the **list partial** (the freshly-grouped tickets) so the `innerHTML` swap shows results. Extract the list query/grouping from `trouble_tickets_list` into a shared helper called by both the list view and analyze; drop the `HX-Trigger` header.
4. **Fix the "Open" filter:** today maps to `status == submitted` only, hiding `in_progress` tickets entirely. Make "Open" mean `status in (submitted, in_progress)` (mirrors `analyze_tickets`' own definition) — teach the list view to accept a logical `status=open` value, change the pill to `('open', 'Open')`.
5. **Fix false-success toast:** the detail status `<select>` raw `fetch().then(...)` toasts "Status updated" on any resolved response (incl. 4xx). Gate on `r.ok`, add `.catch`, show an error toast on failure.
6. Standardize the two divergent `|fmtdate` formats (list vs detail) to one.

---

## 4. Cross-cutting wiring details

### 4.1 Buy-plan email notification toggle
- New column `User.notify_buyplan_email_enabled = Column(Boolean, nullable=False, server_default=text("true"))`.
- Guard at the top of `_send_email()` in `app/services/buyplan_notifications.py:145`: early-return (skip the Graph send) when the recipient's `notify_buyplan_email_enabled` is false. **In-app `ActivityLog` records still write** — nothing is lost, only the email is suppressed.
- Endpoint `POST /api/user/toggle-buyplan-email` cloned from `toggle_8x8` (`htmx_views.py:13837`).

### 4.2 New-offer alert toggle
- New column `User.notify_new_offer_alert_enabled = Column(Boolean, nullable=False, server_default=text("true"))`.
- Guard inside `OfferConfirmedSource.count_for_user`/`markers_for_tab` (`app/services/alerts/sources/offers.py`): return 0/empty for users with the flag off, so the FYI badge is suppressed per-user.
- Endpoint `POST /api/user/toggle-new-offer-alert` cloned from `toggle_8x8`.

### 4.3 Making System flags actually control behavior (root-cause)
Today the 4 seeded keys are **shadowed** — jobs read `settings.<flag>` (env via Pydantic), not the DB row the System tab edits, so edits are inert.

Fix:
1. Add resolver helpers in `app/services/admin_service.py`:
   - `get_effective_flag(db, key, env_default: bool) -> bool` — returns the DB row parsed (`"true"/"false"`) if the row exists, else `env_default`.
   - `get_effective_int(db, key, env_default: int) -> int` — same for the interval.
2. Switch each consumer from `settings.<flag>` to the resolver (env value passed as fallback default):
   - `email_mining_enabled` → `app/routers/sources.py:119,771`
   - `proactive_matching_enabled` → `app/jobs/offers_jobs.py:21`
   - `activity_tracking_enabled` → `app/jobs/email_jobs.py:34,42,67`, `app/jobs/core_jobs.py:42`
   - `inbox_scan_interval_min` → `app/jobs/core_jobs.py:27,169`, `app/services/activity_service.py:1582`
3. **No-surprise cutover:** seeded rows already exist (`startup.py:332-347`) with hardcoded defaults that may differ from env. Add an idempotent startup reconcile for these 4 keys: when a row's `updated_by` is NULL (never admin-edited), set its value to the current env value. Once an admin edits via the UI (`updated_by` = admin email), reconcile leaves it alone. This preserves current env-driven behavior until an admin deliberately flips a toggle, after which the DB value is authoritative. (Reconcile lives in `startup.py` runtime ops — **not DDL**.)
4. Invalidate the 5-min `admin_service` config cache on write so a toggle takes effect promptly (the writer already updates the row; ensure the cache is cleared/short-circuited under the resolver path).

---

## 5. Data model & migration

**Migration `149`** (claimed in `MIGRATION_NUMBERS_IN_FLIGHT.txt`; chains onto `148_archive_dnc`, the current head). Mirrors the `052_add_8x8_user_fields` pattern.

```python
# alembic/versions/149_user_notify_prefs.py  (revision id <= 32 chars)
def upgrade():
    op.add_column("users", sa.Column("notify_buyplan_email_enabled", sa.Boolean(),
                  nullable=False, server_default=sa.text("true")))
    op.add_column("users", sa.Column("notify_new_offer_alert_enabled", sa.Boolean(),
                  nullable=False, server_default=sa.text("true")))

def downgrade():
    op.execute("ALTER TABLE IF EXISTS users DROP COLUMN IF EXISTS notify_new_offer_alert_enabled")
    op.execute("ALTER TABLE IF EXISTS users DROP COLUMN IF EXISTS notify_buyplan_email_enabled")
```

`server_default` backfills existing rows (ORM `default=True` only covers new inserts). Run `alembic upgrade → downgrade → upgrade` and `alembic heads` (single head) before commit. Revision id must be ≤32 chars (PG `VARCHAR(32)`).

---

## 6. Testing (tests with every change — per CLAUDE.md)

- **Profile:** name update (valid/blank/too-long), 8×8 extension set/clear, both notification toggles persist + toast; re-login does not overwrite a locally-edited name (regression test asserting the create-branch-only behavior).
- **System:** curated controls render the 4 keys with labels; toggling persists; the **behavior** wiring works (a job consumer reads the DB override via the resolver; env fallback when row absent); internal watermark keys are **not** rendered as editable; interval rejects `< 5` with a 4xx `{"error":...}`.
- **Connectors:** empty state renders with no `ApiSource` rows; Disconnect/disable carry `hx-confirm`; save/toggle emit the `showToast` trigger.
- **Data Ops:** scan-failure renders the error state (not the empty state); post-merge list refresh drops stale pairs.
- **Ops Group:** removing the last active member → 4xx + error message; self-removal → 4xx; Add carries `hx-confirm`; toggle emits success toast.
- **Tickets:** non-admin gets 403 on workspace/list/detail/screenshot; admin sees them; submission endpoints stay open to non-admins; Analyze returns the grouped list partial (non-empty); "Open" filter includes `in_progress`; detail PATCH toast is gated on `response.ok`.
- Run `TESTING=1 PYTHONPATH=<worktree> pytest tests/ -n auto` from **inside the worktree**; full suite green before PR.
- **Docs:** update `docs/APP_MAP_ARCHITECTURE.md` / `_DATABASE.md` / `_INTERACTIONS.md` for the new columns, endpoints, the System resolver, and the Tickets gating.

---

## 7. Implementation sequencing (waves)

1. **Backbone + gating + safety (test-readiness blockers):** shared `_settings_toast` helper; Tickets admin-gating (button + route deps) and Analyze/filter/toast fixes + in-shell drill-in; Ops Group last-member/self-removal guards + Add confirm; Connectors Disconnect/disable confirms; System internal-row hide. *(This wave alone makes the menu safe for the group test.)*
2. **Profile** + migration `149` + the two notification toggles + 8×8 extension + editable name.
3. **System** curated UI + real-flag wiring (§4.3).
4. **Connectors / Data Ops / Ops Group** remaining consistency/polish (toasts, empty/error states, vocabulary, styling).
5. **Final pass:** simplify → review (pr-review fleet) → full-suite verify → deploy to staging → live-verify on real Postgres.

Each wave: TDD (red→green), then simplify, then review. Ships as one branch `feat/settings-refine` → PR → merge to main → `./deploy.sh`.

---

## 8. Open risks / notes

- **System cutover behavior:** §4.3's reconcile preserves current behavior until an admin flips a toggle; flag this in the PR description so the single-toggle behavior change is expected, not a surprise.
- **Tickets full-page route:** redirecting `/v2/trouble-tickets` to the in-shell tab assumes no external deep links rely on it (confirmed: only entry today is the settings tab button).
- **Staging disk** ~81% used — prune builder/image cache before the deploy build if it tightens.
- **`gh pr edit` is broken** — use `gh api -X PATCH`. **Merge, don't rebase**, on the pushed branch.
