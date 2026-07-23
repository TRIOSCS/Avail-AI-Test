# Aesthetics Reference

## Contents
- Brand Palette
- Typography
- Color Semantics
- Border & Surface Treatment
- Dark Mode

---

## Brand Palette

Defined in `tailwind.config.js`. `brand-*` is a **neutral gray ramp** — text, surfaces,
hairlines. It is NOT blue and it is NOT the interactive color. The single interactive
accent is `accent-*`, the Trio brand azure (trioscs.com theme primary).

```javascript
brand: {
  50:  '#F8F9FA',  // page backgrounds, subtle fills
  100: '#F0F1F4',  // hover fills, tinted surfaces
  200: '#D8DBE2',  // card/table borders
  300: '#ADB3BF',  // placeholder text, disabled icons, scrollbar thumb
  400: '#838B9B',  // secondary icons, tertiary text
  500: '#5F6878',  // mid gray — NOT a primary-action color
  600: '#4B5463',  // secondary text, h4 labels
  700: '#3A4252',  // headings on light bg
  800: '#2A3040',  // dark surfaces
  900: '#1C2130',  // darkest text
}

// The single interactive accent — mirrors --accent* in styles.css.
// 500 = --accent, 600 = --accent-hover. Drives primary buttons, active
// nav/tab states, focus rings, links, and key-figure numerics.
accent: {
  50:  '#E7F3FA',
  100: '#C9E6F4',
  200: '#9BD0EC',
  300: '#66B5E0',
  400: '#2E97D2',
  500: '#007DBD',  // = --accent
  600: '#0B6699',  // = --accent-hover
  700: '#095A85',
  800: '#094C6E',
  900: '#0A3F5A',
}
```

**Global border override** in `styles.css`:
```css
.border-gray-200 { border-color: #BFC4CE !important; } /* = --line */
.border-gray-100 { border-color: #D5D8DF !important; } /* = --line-subtle */
```
Every `border-gray-200` / `border-gray-100` in a template resolves to the neutral
hairline tokens, not literal gray. The canonical hairline classes are
`.border-line-base` (`var(--line)`, `#BFC4CE`) and `.border-line-subtle`
(`var(--line-subtle)`, `#D5D8DF`) — new code should reach for these directly instead of
relying on the `border-gray-*` override, which exists only for legacy migration.

---

## Typography

| Role | Font | Class | Size |
|------|------|-------|------|
| UI text | Aptos → Inter → Segoe UI → system-ui | `font-sans` (default) | 14px base |
| Data values | JetBrains Mono | `font-data` | 12px, -0.01em tracking |
| Table headers | inherited sans | uppercase, `tracking-wide`/`tracking-widest` | 11px, weight 600 |
| Body copy | inherited sans | `text-gray-900` | 14px |
| Secondary | inherited sans | `.text-secondary` (`text-gray-600`), 11px floor | 12px |

```html
<!-- Part numbers, prices, quantities — always monospace -->
<span class="font-data">ATtiny85-20PU</span>
<span class="font-data">${{ '%.4f'|format(price) }}</span>

<!-- Table column headers — always uppercase small caps -->
<th class="text-[11px] font-semibold uppercase tracking-wide text-gray-500">MPN</th>
```

**Font stack, in order:** Aptos (Microsoft system font on Win11/Office-enabled
machines — cannot be web-loaded, so it wins on machines that have it) → Inter (a real,
loaded web font via a Google Fonts `<link>` in `base.html`/`login.html`, the
cross-platform fallback) → Segoe UI → system-ui. **Inter is not banned** — it is the
deliberate fallback that renders correctly everywhere Aptos isn't installed. Do not
introduce DM Sans; it is not part of this stack.

---

## Color Semantics

| Signal | Palette | Use case |
|--------|---------|----------|
| Interactive accent | `accent-*` | Primary buttons, links, active nav/tab state, focus rings (`--accent-ring`), key figures |
| Success / Active | `emerald-*` | Approved offers, active status, win rates |
| Warning / Pending | `amber-*` | Pending review, low confidence, moderate scores |
| Danger / Error | `rose-*` | Rejected, blacklisted, errors |
| Info (badge only) | `brand-100/700/200` | `.badge-info` — a neutral-gray info pill, not a link/button color |
| Neutral / Draft | `gray-*` / `brand-*` | Sold, archived, secondary text, secondary actions |
| Vendor tags | `violet-*` | Brand/manufacturer tags |
| Commodity tags | `sky-*` | Commodity classifications |
| Status-map variety | `sky-*`, `violet-*`, `slate-*`, `orange-*` | Legitimate per-status distinction (e.g. requisition status dots) — this variety is intentional, not a violation of the "one accent" rule |

`brand-*` is reserved for neutral chrome (text, borders, subtle surfaces). Never use
`brand-*` for anything a user clicks, focuses, or hovers as an action — that's `accent-*`
territory. A number of older templates still write `focus:ring-brand-500` on inputs;
that is legacy drift being migrated off, not the pattern to copy into new code.

```html
<!-- Score threshold coloring — consistent 66/33 breakpoints everywhere -->
<span class="
  {% if score >= 66 %}text-emerald-700
  {% elif score >= 33 %}text-brand-600
  {% else %}text-amber-700{% endif %}">
  {{ score|round|int }}
</span>
```

---

## Border & Surface Treatment

Cards use a **1px** border (`border`, not `border-2`) plus the `shadow-card` tier —
`.card` in `styles.css` is `bg-white rounded-xl border border-brand-200 shadow-card
p-3.5`. There is no heavier "deliberate visual weight" 2px card border anywhere in the
current system.

```html
<!-- Standard card — use the .card component class, not raw utilities -->
<div class="card">...</div>

<!-- Interactive card (navigates on click) — accent on hover, not brand -->
<div class="bg-white rounded-xl border border-brand-200
            hover:border-accent-300 hover:shadow-float transition-all cursor-pointer p-3.5">
  <!-- content -->
</div>
```

Only two shadow tiers exist, both defined in `tailwind.config.js`:
- `shadow-card` — resting surfaces (cards, table wrappers)
- `shadow-float` — modals, dropdowns, action rails

There is no `shadow-sm`/`shadow-md` ladder for these components; reach for `shadow-card`
or `shadow-float` and nothing between.

Radius language (3 tiers, do not mix):
- `rounded` (4px) — badges/chips
- `rounded-lg` (8px) — buttons/inputs/modals
- `rounded-xl` (12px) — cards
Genuinely-circular pills (`.badge`, avatars) keep `rounded-full` as a deliberate shape,
not part of this ladder.

Table header background is plain white (`#fff`), bottom border is `2px solid #BFC4CE`
(the `--line` hairline), header text is `#6B7280` uppercase 11px — there is no tinted
brand-50 header fill in the current `.data-table`/`.responsive-table` styling.

---

## Dark Mode

Dark mode classes (`dark:`) are present in Tailwind config but not actively used in
production templates. Do not add `dark:` variants to new components unless explicitly
requested — it risks inconsistent styling.
