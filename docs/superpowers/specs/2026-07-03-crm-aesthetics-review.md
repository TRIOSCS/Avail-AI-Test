# CRM Aesthetics & Readability Review — 2026-07-03

**Scope:** Customers (accounts) list + detail, Contacts (per-account tab + global list),
Companies detail, and the shared CRM cards/tables/macros.
**Goal (user's words):** make the CRM "easier to read, more pleasing to the eye, with the
important info standing out WITHOUT being noise — clean + effective."
**Nature:** READ-ONLY review → a taste/readability sweep on the existing HTMX + Alpine +
Tailwind structure. **Not** a redesign, no new components, no React. Prefer the shipped
taste-layer tokens (`app/static/styles.css`, `tailwind.config.js`, `shared/_macros.html`);
flag any genuinely-new utility for the Tailwind safelist.

Surfaces & files reviewed:
- `app/templates/htmx/partials/customers/list.html` — split workspace shell
- `app/templates/htmx/partials/customers/_account_list.html` — left account-picker rows
- `app/templates/htmx/partials/customers/detail.html` — account detail (header + tabs)
- `app/templates/htmx/partials/customers/_cadence_hero.html` — cadence badge + tier
- `app/templates/htmx/partials/customers/_contact_macros.html` — contact `<tr>` macro (call-list)
- `app/templates/htmx/partials/customers/tabs/_contacts_grouped_list.html` — per-site grouping
- `app/templates/htmx/partials/customers/tabs/contacts_tab.html` — contacts controls
- `app/templates/htmx/partials/customers/contacts_list.html` — global all-contacts table
- `app/templates/htmx/partials/customers/tabs/quotes_tab.html`, `activity_tab.html`, `site_card.html`
- `app/templates/htmx/partials/shared/_macros.html`, `shared/empty_state.html`

---

## Taste-layer tokens already available (unify onto these — don't invent new)

- **Cards:** `.card` / `.card-sm` / `.card-lg` (rounded-xl, `border-brand-200`, `shadow-card`).
- **Tables:** `.table-wrapper`, `.compact-table`, `.data-table`; cell utils `.table-cell`,
  `.table-cell--head`, `.compact-cell*`. Table `th` styling is centralized in `styles.css`.
- **Badges/chips:** `.badge` + `badge()` macro (SEMANTIC tone map), `.badge-success/warning/danger/info`,
  `.chip`, `.badge-mini`; domain macros `status_badge()`, `account_type_badge()`.
- **Type scale:** `.h1 .h2 .h3 .h4`, `.text-secondary`, `.text-tertiary`, `.form-label`.
- **Key figures:** `.figure-accent` (azure `var(--accent)` + `tabular-nums`) — the opt-in for
  hero numerics (deal value, margin, win%). **Under-used in the CRM.**
- **Inputs & focus:** `.input` / `.input-sm` / `.input-focus` — all resolve the canonical
  **accent** ring (`--accent-ring`, azure). The single interactive accent is azure (`accent-*`).
- **Borders:** `.border-line-base` / `.border-line-subtle` (hairline tokens).
- **Shared cadence render:** `cadence_clocks()` in `shared/_macros.html`.

**The one recurring theme:** the CRM predates parts of this token layer, so it leans on raw
utility stacks (`border-gray-200 rounded-lg shadow`, `focus:ring-brand-500`, inline pills,
`·`-separated micro-text). The fix is almost never "new design" — it's "adopt the token that
already exists," which is a near-visual-no-op where the page was already correct and a
readability win where it wasn't.

---

## Findings (ranked by impact)

### HIGH

#### H1 — Account-detail header is a "wall of dots"; key figures don't stand out
`detail.html:125-159` (the MIDDLE commercial/cadence strip) and `detail.html:88-121`
(the identity line).

**Problem.** The commercial strip renders Owner · Helpers · Terms · Cadence · **Win** ·
**Rev 90d** · **Last req** · contact/site counts as one long run of `text-xs text-gray-600`
spans glued by `<span class="text-gray-300">·</span>` (10+ dot separators across the header).
Everything is the same size, weight, and colour, so nothing is a focal point. The two figures
that matter most for a call — **Win rate** and **Rev 90d** — do get `.figure-accent font-semibold`,
but they're buried mid-run and easy to miss; meanwhile "Last req" uses `font-semibold
text-gray-900` (a *different* numeric treatment), so the numbers aren't even internally
consistent. This is the single biggest "important info is lost in noise" issue in the CRM.

**Fix (concrete).** Keep the one-line header, but split the middle into a small **stat cluster**
instead of a dot-run:
- Render the 3 decision figures as tiny label/value pairs: a `.h4` micro-label
  (`Win` / `Rev 90d` / `Last req`) stacked over a `.figure-accent` value (drop the
  gray-900/semibold on "Last req" — route all three through `.figure-accent` so the numeric
  column is one visual language, tabular-nums included). Separate the cluster from the meta
  with a single `border-l border-line-subtle pl-3` divider rather than a `·`.
- Demote the low-signal meta (Owner, Helpers, Terms) to `.text-secondary` and keep them in the
  identity block; they are context, not headline. Cadence stays as its dot+label chip.
- Reduce `·` separators: use them only inside the identity line (domain · industry · city),
  not between headline figures.

**Impact:** High. **Effort:** Medium (one header block; no route/JS changes).

---

#### H2 — Global all-contacts table diverges from the token system and mutes the name
`contacts_list.html` (whole file; hotspots `:36, :47-59, :125-127, :137-143, :171-179`).

**Problems.**
1. The primary identifier — the contact **name** (`:172`) — is `text-sm font-medium
   text-brand-600`. `brand-600` is a *mid-gray* (#4B5463), so the most important cell reads as
   quiet as the row's metadata. Company, role, email, phone are all `text-sm text-gray-600` →
   every cell is equal weight, so the eye has nothing to land on.
2. Card + table chrome is raw, not tokenized: `bg-white rounded-lg shadow border
   border-gray-200` (`:36, :125`) instead of `.card` / `.table-wrapper` (rounded-xl,
   `shadow-card`, `border-brand-200`). The filter `<select>`s (`:47, :53, :59`) are raw
   `px-2 py-1.5 border border-gray-200 rounded-lg` instead of `.input` — different height,
   border, and focus colour from the account-list filters.
3. "Last outbound" (`:179`) uses timeago but no `tabular-nums`, so the ages don't align.

**Fix.**
- Name → `text-sm font-semibold text-gray-900` (make it the anchor). Company → keep
  `text-gray-600` but this is the useful secondary — consider `text-gray-700`. Email/phone →
  `.text-secondary` (recede). Role → `.chip` or `.badge` (see M2), not bare text.
- Wrap the table in `.table-wrapper` and drop the ad-hoc card classes; adopt `.input` on the
  three filter selects and the search box (search already uses `.input` at `:45` — the selects
  should match).
- Add `tabular-nums` to the "Last outbound" cell (and any numeric column).

**Impact:** High. **Effort:** Medium.

---

#### H3 — Interactive-accent drift: `focus:ring-brand-*` and `red-*` instead of the one accent
Systemic. `focus:ring-brand-*` appears in **~23** customer templates (e.g.
`contacts_tab.html:49,55`, `_cadence_hero.html:25`, `site_card.html:12,79`,
`contacts_list.html:149`, `sites_tab.html` ×5). `red-*` (not the app's `rose-*`) in
`_contact_macros.html:247` (DNC badge), `:416, :422` (DNC toggles), `_collaborators.html:19`.

**Problem.** The design system declares **one** interactive accent — Trio azure (`--accent`,
`accent-*`) — and one canonical focus ring (`--accent-ring`, via `.input`/`.input-focus`). The
CRM instead rings controls in **brand gray** (`focus:ring-brand-500/400`), so focus states read
as muted gray rather than the app's azure, and diverge from the requisitions/vendors surfaces
that already use accent. Separately, DNC uses the `red-*` ramp while the entire rest of the app
expresses danger in `rose-*` (the safelist ramp is `…|amber|emerald|rose|…` — `red` isn't in
it). `red-*` here are *literal* classes so Tailwind's content scan still emits them (not a
broken-render bug) — but they're a slightly different hue than every other danger affordance,
which reads as sloppy.

**Fix.**
- Replace native-control `focus:ring-brand-500/400` with `.input` / `.input-sm` (selects,
  search) or `.input-focus` (checkboxes) so the azure `--accent-ring` applies uniformly. For
  link/button focus, use `focus:ring-accent-500` (already the pattern in `_account_list.html`).
- Swap `red-*` → `rose-*` in `_contact_macros.html` and `_collaborators.html` (DNC badge:
  `bg-rose-50 text-rose-700 border-rose-200`, matching the shipped `.badge-danger`).

**Impact:** High (whole-surface consistency, the "one accent" rule). **Effort:** Low–Medium
(mechanical find/replace across CRM templates; verify no purge surprises post-deploy).

---

### MEDIUM

#### M2 — Inline pills everywhere instead of the `.chip` / `.badge` family
`_contact_macros.html:201-260` (role label + colour maps, DNC/Archived/completeness pills, all
hand-rolled `px-1 py-0.5 text-[11px] … rounded {{ map }}`), `_account_list.html:136-137`
(tier/sites `px-1 py-0.5 rounded text-xs bg-gray-100`), `quotes_tab.html:49-53` (margin pills),
`site_card.html:20-22` (DNC/site-type).

**Problem.** The taste layer locked three badge families (`.badge`, `.chip`, `.badge-mini`) and
a SEMANTIC tone map precisely so metadata tags share one shape/size/radius. The CRM re-implements
them ad-hoc, which drifts on padding (`px-1` vs `.chip` `px-2`), radius, and the `text-[11px]`
floor, and duplicates the semantic colour vocabulary in-template.

**Fix.**
- Role chip → `.chip` (rectangular metadata tag; keep the role colour map but apply it as the
  chip's colour classes, or better, add a small `role_chip()` macro next to `account_type_badge`).
- DNC / Archived / completeness → `badge()` with tones `danger` / `muted` / `warning` (from
  `shared/_macros.html`'s SEMANTIC map) — this also fixes H3's `red-*`.
- Account-list tier/sites → `.badge-mini` (uppercase micro tag) or `.chip`.
- Quote margin pills → `badge()` tone `success/warning/danger` (they already encode ≥30/≥15/<15).

**Impact:** Medium. **Effort:** Medium.

#### M3 — Cadence-dot colour map copy-pasted in 5 files
Identical `{"new":"bg-gray-300","on_target":"bg-emerald-400","due":"bg-amber-400","overdue":"bg-rose-500"}`
in `_account_list.html:102`, `detail.html:67`, `_contact_macros.html:311`,
`contacts_list.html:83`, and `vendors/list.html`.

**Problem.** Five sources of truth for one semantic mapping; a future tweak (or a new state)
has to be made in five places, and they *will* drift.

**Fix.** Add a `cadence_dot(state, size='sm')` macro to `shared/_macros.html` (returns the
`<span class="… rounded-full {colour}">` with aria-label), and a `CADENCE_DOT` map constant.
Replace the five inline maps with the macro. No visual change — pure de-duplication that makes
future cadence-colour edits one-touch. (`_contact_macros.html` already imports from the shared
macros file, so no new import wiring.)

**Impact:** Medium (consistency insurance). **Effort:** Low.

#### M4 — Two cadence-hero implementations; neither uses the badge primitive
`_cadence_hero.html:8-18` defines its own `badge_colors` (`bg-emerald-100 text-emerald-700`, …)
with `px-2.5 py-1 rounded-full`; `shared/_macros.html:~535` has a *second* cadence-hero with its
own map. Neither routes through `.badge` / SEMANTIC.

**Problem.** Divergent shape/weight for the same "cadence state" pill, plus a duplicate macro.

**Fix.** Point `_cadence_hero.html` at `badge()` (tones: new→muted, on_target→success,
due→warning, overdue→danger) and make the tier `<select>` a `.input-sm` (fixes its
`focus:ring-brand-400`). Consolidate on one cadence-hero definition.

**Impact:** Medium. **Effort:** Low–Medium.

#### M5 — Three different table looks across the account tabs
`_contacts_grouped_list.html` uses `.compact-table` (good, tokenized); `contacts_list.html`
uses `min-w-full divide-y divide-gray-200` + hand-rolled `table-cell--head uppercase`;
`quotes_tab.html:23-33` uses `min-w-full divide-y` + inline `px-4 py-2.5 text-xs … uppercase`
headers. Three header/border/hover treatments for the same "list of records" idea.

**Problem.** Tables that sit two tabs apart don't feel like the same app.

**Fix.** Standardize the account-tab record tables on `.table-wrapper` + the shared header cell
tokens (or `.compact-table` where density matters, as the contacts tab already does). Money
columns get `tabular-nums`; right-align numerics (quotes Total/Margin already right-aligned —
just add tabular-nums).

**Impact:** Medium. **Effort:** Medium.

---

### LOW

#### L1 — Empty states: five hand-rolled variants
`empty_state.html` (shared, `max-w-sm` card, 12×12 icon) is used only by the grouped contacts
list. Meanwhile `_detail_empty.html`, `_account_list.html:181-186`, `contacts_list.html:214-219`,
`quotes_tab.html:68-77`, and `activity_tab.html:48-56` each hand-roll their own (varying icon
sizes 10/12/16, varying padding, varying copy weight).

**Fix.** Converge the list/table empties onto `shared/empty_state.html` (message + optional
CTA). Keep `_detail_empty.html` as the deliberately-different full-height panel placeholder.
Low priority, real consistency dividend.

**Impact:** Low. **Effort:** Low.

#### L2 — "Customer Contacts" page title is `.h3` (too quiet for a top-level header)
`contacts_list.html:15` renders the page title with `class="h3"` (`text-sm font-semibold
text-gray-700`). The account-detail name uses `text-lg font-bold`. A page-level header should be
`.h2` (`text-lg font-semibold text-gray-900`) so the top of the page has an anchor.

**Impact:** Low. **Effort:** Low.

#### L3 — Link-colour inconsistency for the primary call actions
`_contact_macros.html` phone/email links are `text-brand-600` (gray) (`:271, :292`), while
account-list utility links use `hover:text-accent-600`. The clickable phone/email are the call
list's primary affordances; consider `text-accent-600` (or at least keep them distinct from
plain gray body text). *Judgement call* — the gray keeps the dense table calm, so this is
opt-in, not a must. Pick one rule and apply it to both the row and the expand-drawer copies.

**Impact:** Low. **Effort:** Low.

#### L4 — `cadence_clocks()` numerics aren't tabular / accent
`shared/_macros.html:562-591` renders the day-counts as `text-lg font-bold text-gray-900` with
no `tabular-nums`. For consistency with `.figure-accent` elsewhere, add `tabular-nums` (and
optionally route the day number through the figure treatment). Cosmetic.

**Impact:** Low. **Effort:** Low.

---

## Phased pass plan (highest-impact, lowest-risk first)

Each phase is independently shippable and reviewable; ordering front-loads readability wins and
leaves the mechanical/structural sweeps for later so nothing blocks the visible payoff.

### Phase 1 — "Make the important info pop" (High impact, low blast radius)
- **H1** account-detail header stat cluster (Win / Rev 90d / Last req as `.figure-accent`
  label/value pairs; recede Owner/Helpers/Terms; cut the dot-run). `detail.html`.
- **H2** global contacts table: name → `text-gray-900 font-semibold`, recede email/phone,
  `.table-wrapper` + `.input` filters, `tabular-nums` on ages. `contacts_list.html`.
- **L2** page title `.h3`→`.h2`; **L4** tabular-nums on `cadence_clocks`.
- *Risk:* low — visual-only, no routes/JS. *Verify:* headless render of an account with
  revenue + several contacts; confirm the three figures read first.

### Phase 2 — "One accent, one danger colour" (High impact, mechanical)
- **H3** replace `focus:ring-brand-*` with `.input`/`.input-focus`/`focus:ring-accent-500`
  across the CRM templates; swap `red-*`→`rose-*` in `_contact_macros.html`,
  `_collaborators.html`.
- *Risk:* low–medium — find/replace; **post-deploy, confirm the swapped Tailwind classes are in
  the built CSS bundle** (accent-ring utilities + rose ramp are safelisted, so this is a check,
  not a worry). *Verify:* tab through the contacts filters + a contact row; ring is azure.

### Phase 3 — "Unify the badges & the dot" (Medium impact, de-duplication)
- **M2** route role/DNC/Archived/completeness/margin/site-type pills through `.chip` / `badge()`
  / `.badge-mini` (adds a `role_chip()` macro).
- **M3** extract `cadence_dot()` + `CADENCE_DOT` into `shared/_macros.html`; replace the 5 inline
  maps. **M4** fold `_cadence_hero.html` onto `badge()` + `.input-sm`; drop the duplicate macro.
- *Risk:* medium — touches the shared macro file (used beyond CRM: vendors). Keep the macro's
  output byte-equivalent to today's spans so it's a no-op for vendors. *Verify:* account list,
  contacts tab, vendor list all still render identical dots.

### Phase 4 — "Same table everywhere" + empty-state cleanup (Medium/Low, structural)
- **M5** standardize account-tab record tables on `.table-wrapper`/`.compact-table` + shared
  header cells; tabular-nums on money. `quotes_tab.html`, `contacts_list.html`.
- **L1** converge list/table empty states on `shared/empty_state.html`. **L3** settle the
  phone/email link-colour rule.
- *Risk:* low–medium — DOM structure shifts on the quotes/contacts tables; re-check column
  alignment + the row deep-link click targets after the swap.

## New-utility / safelist notes
No net-new utilities are required — every recommendation lands on an existing token or macro.
The `accent-*` ring utilities and the `rose-*` / `amber-*` / `emerald-*` ramps used by
`badge()`/`.chip` are already covered by the `tailwind.config.js` safelist pattern
(`^(bg|text|border)-(slate|gray|brand|accent|amber|emerald|rose|blue|violet|sky)-…`). If the
`role_chip()` macro (M2) introduces any shade **not** already present in a template literal,
add that exact `bg/text` pair to the safelist and verify it in the built CSS after deploy (per
CLAUDE.md's Tailwind-verification rule). The `red-*` classes being *removed* were only rendering
because they were literals — dropping them is purely subtractive.
</content>
</invoke>
