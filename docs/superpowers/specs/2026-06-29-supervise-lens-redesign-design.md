# Supervise lens — redesign (presentation only)

> **Approved by the user 2026-06-29.** Presentation-layer redesign of the Approvals
> **Supervise** lens (`/v2/approvals?lens=supervise`). Backend workflow is **unchanged** —
> the approved frozen-scope workflow rework
> (`docs/superpowers/specs/2026-06-28-approvals-rework-acceptance.md`) is a **sequenced
> follow-up**, not part of this work.

## Problem

The Supervise lens reads as messy and hard to scan. Today it stacks, top to bottom:
a cramped one-line strip of **8 tiny gray stats**, then up to **6 differently-styled
triage sections** (Approvals / Needs-SO-verify / PO-awaiting-verify / Overdue / Flagged /
Halted), then the **3-column deal board**, then the archive. Two visual languages collide
(flat list-rows in triage vs. bordered card-tiles in the board), and each board card crams
a truncated canonical title `{SO#} - {Customer} - {Owner} - BP`, a value, a colored margin
badge, a middot-separated `SO·MPN·PO` fact line, a blocker line, and a progress bar. The
result is dense, noisy, and slow to act on.

## Goal

Make the supervisor's primary job — **clearing the action queue** — effortless. One screen,
three zones, **one consistent row language**:

1. **Calm header** — a single headline count + money subline, plus client-side type-filter chips.
2. **The Action Queue (hero)** — one card, one uniform row per item, every item that needs
   the supervisor, in a single risk-first priority order.
3. **Pipeline monitor (demoted)** — the all-deals board, collapsed by default behind a
   one-line summary; the archive stays collapsed below it.

## Approved decisions (locked)

| Decision | Choice |
|---|---|
| Primary job | Clear the action queue — queue is hero, board demoted |
| Restructuring latitude | Full restructure |
| Queue shape | **One flat priority list** (no lanes, no per-type sections) |
| Row signals | **Age, value, margin %, owner** — all four |
| Scope | **Presentation only** — data-driven, survives the later workflow rework |
| Priority order | **Risk-first:** Halted → Flagged → Overdue → Approve → Verify SO → Verify PO; oldest-first within each |
| Filter chips | **Yes** — client-side (Alpine), with live counts |

**Why presentation-only is safe vs. the pending rework:** the queue is data-driven. When the
rework later folds verify-SO into the single manager approval and drops auto-approve, those
rows simply stop being emitted by `supervise_overview` — the template needs no change.

## Layout

```
┌──────────────────────────────────────────────────────────────────────────────┐
│  12 items need you           $1.24M open · 24% avg margin                      │  calm header
│  [All 12] [Approve 3] [Overdue 2] [Flagged 4] [Verify 3]                       │  filter chips (Alpine, no round-trip)
├──────────────────────────────────────────────────────────────────────────────┤
│  NEEDS YOU NOW                                                                  │  ONE card, divided rows
│ ┌────────────────────────────────────────────────────────────────────────────┐│
│ │ ⬤Halted    Initech                   1d    $5,900   44%   [Open →]          ││
│ │            SO 10240 · AM Sue Lin                                             ││
│ ├────────────────────────────────────────────────────────────────────────────┤│
│ │ ⬤Flagged   Globex Corp               6d    $12,400  11%   [Open →]          ││
│ │            SO 10198 · LM317T · Vendor Z · Buyer Bob · "wrong date code"      ││
│ ├────────────────────────────────────────────────────────────────────────────┤│
│ │ ⬤Approve   Acme Aerospace            3d    $84,200  32%   [Approve] [Review] ││
│ │            SO 10231 · AM Jane Doe                                            ││
│ └────────────────────────────────────────────────────────────────────────────┘│
├──────────────────────────────────────────────────────────────────────────────┤
│  ▸ Pipeline · 38 open deals · $1.24M open   Draft 6 · Pending 9 · Active 23  [Show board] │  demoted, collapsed
└──────────────────────────────────────────────────────────────────────────────┘
   ▸ Completed (archive)                                                            unchanged, collapsed
```

## Data layer — `app/services/buyplan_hub.py :: supervise_overview`

Add a single uniform **`queue`** list to the returned dict and **remove the now-dead
`triage` key**. `overview.triage` is read **only** by `_supervise.html` (confirmed by grep),
so replacing it leaves no other caller broken; the existing `supervise_overview` unit tests
that assert on `triage` are migrated to assert on `queue` (same six source queries,
reshaped). The `strip` key is **unchanged**.

The six existing queries (approval_plans, so_pending_plans, halted_plans, overdue_lines,
po_pending_verify_lines, flagged_lines) are unchanged. They are mapped into one uniform row
shape and concatenated, then sorted.

**Uniform row dict** (one shape for every kind):

```python
{
    "kind":         str,        # "halted"|"flagged"|"overdue"|"approve"|"verify_so"|"verify_po"
    "label":        str,        # "Halted"|"Flagged"|"Overdue PO"|"Approve"|"Verify SO"|"Verify PO"
    "priority":     int,        # risk-first tier, 1..6 (see below)
    "plan_id":      int,
    "line_id":      int | None, # set only for line kinds (overdue/flagged/verify_po)
    "customer_name": str,
    "so_number":    str | None, # plan.sales_order_number
    "mpn":          str | None, # line kinds only (offer.mpn)
    "vendor_name":  str | None, # line kinds only (offer.vendor_name)
    "owner_name":   str,
    "owner_role":   str,        # "AM" for plan kinds, "Buyer" for line kinds
    "value":        Decimal | None,  # parent plan.total_cost (uniform "deal value")
    "margin_pct":   float | None,    # parent plan.total_margin_pct (uniform "deal margin")
    "waiting_since": datetime,       # for `timeago` + sort key (see below)
    "issue_reason": str | None,      # flagged only (reuse existing `_issue_reason`)
}
```

**Priority tiers (risk-first):** `halted=1, flagged=2, overdue=3, approve=4, verify_so=5,
verify_po=6`. Define as a module-level constant `_QUEUE_PRIORITY: dict[str, int]` in
`buyplan_hub.py`.

**`waiting_since` per kind** (uses only existing columns — no new migration):
- `approve`, `halted`, `verify_so` → `plan.created_at` (the established sort key today;
  documents "how long the plan has existed/waited").
- `overdue` → `coalesce(line.last_nudge_at, plan.approved_at)` — the exact SLA clock the
  existing overdue predicate uses (`supervise_overview` already computes this).
- `flagged`, `verify_po` → `line.created_at`.

**`value` / `margin_pct`:** parent plan's `total_cost` / `total_margin_pct` for **every**
kind (so line rows show the deal's value/margin uniformly — what a supervisor weighs).

**`owner`:** plan kinds → `plan.submitted_by` (label `"AM"`); line kinds → `line.buyer`
(label `"Buyer"`). Reuse the existing `_user_name` / `_customer_name` helpers.

**Assembly & sort:**

```python
queue = [...]  # map each of the 6 source lists into the uniform dict above
queue.sort(key=lambda r: (r["priority"], r["waiting_since"]))
# ascending datetime ⇒ oldest-first within each tier
```

Return `{"strip": {...unchanged...}, "queue": queue}` — the `triage` key is removed.

This is **view-model construction only** — no DB schema change, no new query, no workflow change.

## Template — `app/templates/htmx/partials/buy_plans/_supervise.html` (rewrite)

Three zones. Wrap zones 1–2 in one Alpine root for the filter: `x-data="{ qf: 'all' }"`.

### Zone 1 — calm header + filter chips
- Headline: `{{ overview.queue|length }} items need you` (or `You're all caught up` when 0).
- Subline (muted): `${{ '{:,.0f}'.format(overview.strip.open_value) }} open ·
  {{ '{:.0f}'.format(overview.strip.avg_margin) }}% avg margin`.
- Chips: `All ({{ queue|length }})` + one chip per kind that has ≥1 row, with its count
  (`queue|selectattr('kind','equalto', k)|list|length`). Each chip sets `qf` on click;
  active chip uses `bg-accent-600 text-white` (single-accent rule), inactive `bg-brand-100
  text-brand-600 hover:bg-brand-200` — mirrors the existing lens/scope pill aesthetic.

### Zone 2 — the Action Queue (hero)
- One container card: `bg-white rounded-lg shadow-sm border border-line-base`, header row
  `NEEDS YOU NOW`, body `divide-y divide-line-subtle`.
- Define a Jinja macro **`queue_row(row)`** at the top of this file (no new file). Every row:
  - **Row visibility:** `x-show="qf === 'all' || qf === '{{ row.kind }}'"`.
  - **Left / identity:** the kind **pill** (`.chip` + hue, table below) · **Customer**
    (`text-sm font-bold text-gray-900`) · muted second line built from only the fields that
    exist, middot-joined: `SO {{ so_number }}` (mono) · `{{ mpn }}` (mono) · `{{ vendor_name }}`
    · `{{ owner_role }} {{ owner_name }}` · for flagged, the `issue_reason` in `text-rose-600`.
  - **Signals (right-aligned, tabular):**
    - **age** `{{ row.waiting_since | timeago }}` — `text-gray-500`; if `waiting_since`
      older than **72h**, `text-amber-600 font-medium` (passive aging cue, display-only —
      not a reminder/SLA mechanism).
    - **value** `${{ '{:,.0f}'.format(row.value|float) if row.value else '0' }}`.
    - **margin** badge, reusing `_board.html` thresholds exactly: `>=30 badge-success,
      >=15 badge-warning, else badge-danger`.
  - **Action** (preserve today's routes/targets verbatim — see below).
- **Empty state:** when `overview.queue` is empty, render a single calm line:
  `You're all caught up — nothing needs you right now.` (`text-sm text-gray-400`).

**Pill hues** (all `.chip` + a hue pair already used in `_supervise.html`/`_board.html`):

| kind | classes |
|---|---|
| halted | `chip bg-gray-100 text-gray-600 border-gray-200` |
| flagged | `chip bg-rose-50 text-rose-700 border-rose-200` |
| overdue | `chip bg-amber-50 text-amber-700 border-amber-200` |
| approve | `chip bg-accent-50 text-accent-700 border-accent-200` |
| verify_so | `chip bg-purple-50 text-purple-700 border-purple-200` |
| verify_po | `chip bg-brand-100 text-brand-600 border-brand-200` |

> **Tailwind safelist check:** `bg-accent-50 / text-accent-700 / border-accent-200` and
> `bg-amber-50` may not yet appear in the built bundle. After `npm run build`, verify these
> classes exist in `app/static/dist/assets/*.css`; if purged, add to the Tailwind safelist
> (per the project's post-deploy Tailwind verification rule). Not a TBD — a build-time gate.

**Actions per kind** (copied unchanged from current `_supervise.html` — same routes,
`origin=supervise`, `hx-target="#bp-hub-body"`):

| kind | action block |
|---|---|
| approve | `[Approve]` (`btn btn-primary btn-sm`, POST `/v2/partials/buy-plans/{plan_id}/approve`) + `[Review]` (`btn btn-danger btn-sm`, `hx-get /v2/partials/buy-plans/{plan_id}` → `#main-content`, push-url — reject-with-reason lives there) |
| verify_so | `[Verify SO]` (POST `/v2/partials/buy-plans/{plan_id}/verify-so`) |
| verify_po | `[Verify PO]` (POST `/v2/partials/buy-plans/{plan_id}/lines/{line_id}/verify-po`) + `[Reject]` (`action=reject`, `hx-confirm`) |
| overdue / flagged / halted | whole-row is a link: `hx-get /v2/partials/buy-plans/{plan_id}` → `#main-content`, push-url `/v2/buy-plans/{plan_id}`, `preload="mouseover"`, `tabindex=0 role=link` + `@keydown.enter/space.prevent="$el.click()"`; trailing `[Open →]` affordance |

### Zone 3 — Pipeline monitor (demoted board)
- Collapsible panel, `x-data="{ open: false }"`. Summary bar (always visible): chevron +
  `Pipeline · {{ open_cards|length }} open deals · ${{ '{:,.0f}'.format(open_value) }}` +
  per-stage counts `Draft N · Pending N · Active N` + a `Show board` / `Hide board` toggle.
  Compute `open_cards = board.draft + board.pending + board.active` and `open_value` with
  the same Jinja expressions `_board.html` already uses.
- Board body: `<div x-show="open" x-cloak>` wrapping
  `{% include "htmx/partials/buy_plans/_board.html" %}` with `hide_strip = True`,
  `scope = 'all'` (current behavior). Rendered inline but hidden by default — no new route,
  no double-fetch. (If board size ever becomes a payload concern, switch to lazy `hx-get
  /v2/partials/buy-plans/board?scope=all` on first open; out of scope now.)
- The archive continues to render via `_board.html`'s existing `{% if archive is defined %}`
  block (handler still passes `archive`). No change.

## Template — `app/templates/htmx/partials/buy_plans/_board.html` (card de-noise)

The board card is the literal "card" the user called messy; `_board.html` is **shared** by
the Buy Plans tab, Sales Orders tab, and (embedded) Supervise — so this de-noise improves
all three consistently. Changes (no elements removed that carry unique data):
- Replace the mashed `card_title` with **discrete fields**: `Customer` (`font-bold`) on line
  one; a muted second line `SO {{ tso }} · {{ owner_name }}`. (`card_title` / `build_card_title`
  stay available for any caller still wanting the canonical string; the card stops rendering it.)
- Keep value + **one** margin badge. Drop the redundant visual weight: the `SO·MPN·PO`
  fact line stays but lighter (single muted line, fewer middots); PO progress bar stays but
  thinner/subtler; keep the `needs_my_action` amber ring and the `stock` chip.
- One border + hover shadow only (no stacked borders).

**Handler — `app/routers/htmx/buy_plans.py :: _render_supervise_body`:** unchanged context
(`overview` now carries `queue`; the template reads it). No route/signature change.

## Out of scope (guardrails)

- **No backend workflow change** — auto-approve, the verify-SO gate, and the rename are the
  separate frozen-scope rework, sequenced after this.
- **No SLA/reminder/escalation machinery** — the 72h age styling is display-only.
- **No new routes, no schema/migration, no new CSS tokens** beyond verifying existing
  shades are in the built bundle.
- **No removal of any action or data field** — every current action/route is preserved.

## Acceptance criteria (testable)

1. `supervise_overview(db)` returns a `queue` list; each row has the uniform shape above
   with `value`/`margin_pct` from the parent plan and the correct `owner_role` (AM for
   plan kinds, Buyer for line kinds).
2. The queue is sorted **risk-first** (halted→flagged→overdue→approve→verify_so→verify_po)
   and **oldest-first within each tier**.
3. `issue_reason` is populated only on `flagged` rows; `mpn`/`vendor_name`/`line_id` only on
   line kinds.
4. The supervise partial renders **one** queue card (not 6 sections), uniform rows with the
   kind pill, customer, age (`timeago`), value, margin badge, and the correct inline
   action(s) — with the existing routes/targets intact.
5. Filter chips show per-kind counts; selecting a chip hides non-matching rows; `All` resets.
6. An empty queue renders the "all caught up" line.
7. The pipeline board is collapsed by default and expands to the existing `_board.html`;
   the archive still renders.
8. `supervise_overview`'s `strip` is unchanged; the board/archive tests stay green.

## Tests

- **New** `tests/test_buyplan_supervise_queue.py`: unit-test `supervise_overview`'s `queue`
  — uniform shape, risk-first + oldest-first ordering across fixtures of every kind,
  value/margin/owner_role/waiting_since/issue_reason population. Reuse the buy-plan fixtures
  already in `tests/test_buyplan_hub_supervise.py`.
- **Migrate** `tests/test_buyplan_hub_supervise.py` (and any `triage` assertions in
  `tests/test_buyplan_hub_routes.py`): the old `overview["triage"]` assertions become
  `overview["queue"]` assertions (the six source queries are unchanged, only reshaped).
  `strip` assertions stay as-is.
- **Extend** the supervise render test (the route-level test in
  `tests/test_buyplan_hub_routes.py`): assert one queue card + chips + correct row content +
  empty-state, as a supervisor; and that a non-supervisor still gets the mine-scope board.
- Full suite green (`-n auto`), `pre-commit run --files <changed>` clean.

## Files touched

- `app/services/buyplan_hub.py` — add `queue` (+ `_QUEUE_PRIORITY`) to `supervise_overview`.
- `app/templates/htmx/partials/buy_plans/_supervise.html` — rewrite (header, chips, queue
  macro + rows, collapsible pipeline).
- `app/templates/htmx/partials/buy_plans/_board.html` — card de-noise (discrete fields).
- `tests/test_buyplan_supervise_queue.py` — new (queue unit tests).
- `tests/test_buyplan_hub_supervise.py` — migrate `triage` → `queue` assertions.
- `tests/test_buyplan_hub_routes.py` — migrate any `triage` assertions; extend supervise
  render assertions (queue card, chips, empty-state).
- `docs/APP_MAP_INTERACTIONS.md` — note the supervise lens now renders a unified action
  queue (per the after-code-change APP_MAP rule).

## Sequenced follow-up (not this PR)

Execute the approved frozen-scope workflow rework
(`docs/superpowers/plans/2026-06-28-approvals-frozen-scope-plan.md`): remove auto-approve,
fold verify-SO into the single manager approval, rename "Sales Order" → "buy plan". When it
lands, the `verify_so` rows disappear from the queue automatically — no template change.
