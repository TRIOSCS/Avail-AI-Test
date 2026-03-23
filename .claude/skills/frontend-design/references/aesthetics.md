# Aesthetics Reference

## Contents
- Brand Palette
- Typography
- Color Semantics
- Border & Surface Treatment
- Dark Mode

---

## Brand Palette

Defined in `tailwind.config.js`. Steel blue — cool, industrial, trustworthy. Not a generic purple/blue gradient.

```javascript
brand: {
  50:  '#F4F8FC',  // page backgrounds, table headers
  100: '#E4EFF8',  // hover fills, tinted surfaces
  200: '#CADDEF',  // borders (overrides gray-200 globally)
  300: '#A3C4E0',  // placeholder text, icons
  400: '#7AAAD0',  // secondary icons
  500: '#5B8FB8',  // primary interactive — buttons, links, scrollbars
  600: '#4A7CB5',  // hover on brand-500
  700: '#3D6895',  // active state, scrollbar hover
  800: '#2E5070',  // headings on light bg
  900: '#20384E',  // darkest text on brand backgrounds
}
```

**Global border override** in `styles.css` — all `border-gray-200` resolves to brand-tinted `#c8d5e2`. This is intentional and project-wide.

---

## Typography

| Role | Font | Class | Size |
|------|------|-------|------|
| UI text | DM Sans | `font-sans` (default) | 14px base |
| Data values | JetBrains Mono | `font-data` | 12px, -0.01em tracking |
| Table headers | DM Sans | uppercase, `tracking-widest` | 11px, weight 600 |
| Body copy | DM Sans | `text-gray-700` | 14px |
| Secondary | DM Sans | `text-gray-400` | 12px |

```html
<!-- Part numbers, prices, quantities — always monospace -->
<span class="font-data">ATtiny85-20PU</span>
<span class="font-data">${{ '%.4f'|format(price) }}</span>

<!-- Table column headers — always uppercase small caps -->
<th class="text-[11px] font-semibold uppercase tracking-widest text-brand-500 bg-brand-50">MPN</th>
```

**NEVER** use Inter, Roboto, or system-ui as primary font — DM Sans is the brand voice.

---

## Color Semantics

| Signal | Palette | Use case |
|--------|---------|----------|
| Success / Active | `emerald-*` | Approved offers, active status, win rates |
| Warning / Pending | `amber-*` | Pending review, low confidence, moderate scores |
| Danger / Error | `rose-*` | Rejected, blacklisted, errors |
| Info / Brand | `brand-*` | Links, buttons, progress, info badges |
| Neutral / Draft | `gray-*` | Sold, archived, secondary actions |
| Vendor tags | `violet-*` | Brand tags, strategic claims |
| Commodity tags | `sky-*` | Commodity classifications |

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

Cards use `border-2` (not `border`). This is a deliberate design choice for visual weight.

```html
<!-- Standard card — 2px border, rounded-xl, no drop shadow -->
<div class="bg-white border-2 border-gray-200 rounded-xl shadow-sm">

<!-- Interactive card — hover lifts with border darkening -->
<div class="bg-white rounded-lg border border-brand-200
            hover:border-brand-400 hover:shadow-md transition-all cursor-pointer">
```

Table header background is `#F4F8FC` (brand-50), bottom border is `2px solid #CADDEF` (brand-200). Alternating rows use `#fafbfc` — subtle, not stark white/gray alternation.

---

## Dark Mode

Dark mode classes (`dark:`) are present in Tailwind config but not actively used in production templates. Do not add `dark:` variants to new components unless explicitly requested — it risks inconsistent styling.
