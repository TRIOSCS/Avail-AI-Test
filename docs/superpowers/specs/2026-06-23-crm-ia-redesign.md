# CRM Customers Workspace — Information-Architecture Redesign

## Context
The CRM "Customers" workspace is messy and disorienting as a rep moves through account → site → contact. A 4-way IA audit found the root causes:
1. **Single-site vs multi-site accounts render as two different products** — single-site → full detail with 6 tabs; multi-site → a bare header ("select a site on the left") → drill into a site for a *different* mini-view (the fork lives at `_account_list.html` site_count check + `header.html` + `site_detail.html`).
2. **The same contact is managed in 2–3 places with different powers** — the Contacts tab has role/priority/archive/DNC/cadence/Find-contacts; the Sites-tab "Load Contacts" list (`site_contacts.html`) is bare CRUD with none of those.
3. **Sites have two mental models** — a non-interactive table (Sites tab) vs a left-panel drill-down accordion (`_sites_accordion.html`).
4. **No consistent breadcrumb / "you are here."**
5. **Buried tools** — cadence/tier/disposition behind the "Account settings" collapsible; noisy contact cards (5+ micro-actions per row).

## Goal (user-approved model)
**Left panel = the account list (pick an account). Right panel = that account's CONTACTS as the primary content. Extra info (Sites, Requisitions, Activity, Quotes, Buy Plans) lives in tabs.** Scannable contact cards with actions on hover/kebab. One consistent experience for every account; one canonical place to manage people; the account→site→contact hierarchy stays visible; key features surfaced. Apply the frontend-design lens: clear hierarchy, progressive disclosure, honest empty states, tight microcopy. Stay 100% in the existing brand palette/patterns (HTMX+Alpine+Tailwind+Jinja) — layout/IA change only.

```
┌ Accounts (left, unchanged picker) ┬ Acme Corp ─────────────── [Enrich][+ Contact][⋯] ┐
│ [search / filters]                │ Customers › Acme   owner·terms·cadence●·win42%·$120k · 12 contacts·3 sites
│ • Acme   ◀ on-target              │ ┌ Contacts │ Sites · Requisitions · Activity · Quotes · Buy Plans ┐
│ • Globex   overdue                │ │ [search people]            [All sites ▾]                         │
│ • Initech  due                    │ │ ── HQ · Detroit ─────────────────────────── [+ add here] ──     │
│                                   │ │  ⬤ Jane Smith ★  buyer · HQ · last touch 2d    ☎ ✉ Teams   ⋯    │
│                                   │ │  ⬤ Bob Jones        HQ · 14d                   ☎ ✉        ⋯    │
│                                   │ │ ── Plant 2 · Austin ───────────────────────── [+ add here] ──   │
│                                   │ │  ⬤ Carl Ek   overdue 40d ●                     ☎ ✉        ⋯    │
│                                   │ └──────────────────────────────────────────────────────────────── │
└───────────────────────────────────┴────────────────────────────────────────────────────────────────────┘
```
NOTE: `main` recently merged an "app-wide horizontal-space / per-page width policy" — read the CURRENT templates and build ON TOP of that width work (don't fight it). Verify all line numbers against the current files before editing (they have shifted).

## The redesign

### 1. One unified account workspace (kill the single/multi-site fork)
- `company_detail_partial` (htmx_views.py) returns the SAME unified detail for EVERY account regardless of `site_count`. Remove the header-only multi-site path: retire `company_header_partial`/`header.html` as the "click a multi-site account" target, and stop the `_account_list.html` fork that routes multi-site clicks to the header-only view — every account row loads `/v2/partials/customers/{id}` → unified detail.
- Retire the left-panel `_sites_accordion.html` drill-down as a *navigation* mechanism (sites are reached via the Sites tab now). The left list becomes a clean account picker (keep its filters/cadence/sort).
- `site_detail.html` + `company_site_detail_partial`: retire the separate site-scoped right-panel view (its function — see a site's contacts — is now served by the Contacts view's per-site sections + site filter). If any deep-link to a site is kept, redirect it to the unified detail with the site filter pre-applied.

### 2. Contacts = the canonical, primary right-panel surface
- In the unified detail (`detail.html`), **Contacts is the default + primary content** under the slim header; the tab strip's first tab is Contacts, the rest (Sites · Requisitions · Activity · Quotes · Buy Plans) are the "extra info."
- The Contacts view (`contacts_tab.html` + `_contacts_grouped_list.html`):
  - **Breadcrumb** `Customers › {Account}` at the top of the right panel (reuse the existing breadcrumb treatment from the old `site_detail.html`).
  - **People search** (client-side filter over the rendered contacts, or a debounced hx-get) + a **site filter** (`All sites ▾`, shown only when the account has >1 active site).
  - Contacts grouped under **light site section headers** (site name · city · per-site cadence dot · `+ add here`), so the account→site→contact hierarchy is visible. Single-site account → one section (no filter clutter, minimal chrome).
  - This is the ONLY contact-management surface and it has the FULL feature set everywhere (role/priority/archive/DNC/cadence/edit/delete/set-primary/suggested-contacts).

### 3. Sites tab = site info + CRUD only (retire the duplicate contact list)
- `sites_tab.html` / `site_card.html`: keep site **info + CRUD** (name, type, address, terms, owner, per-site cadence; add/edit/delete site). **Remove the per-site "Load Contacts" + `site_contacts.html` contact-management list** (the duplicate, feature-poor surface). A site card may show a contact COUNT + a "View N contacts" link that switches to the Contacts tab with that site's filter pre-applied — not a second editing surface.
- Consolidate the contact CRUD endpoints' HX-Target branching: there is now ONE render target for contacts (`#contacts-tab-list`); drop the `site_contacts.html`/`#site-{id}-contacts` branch once that surface is gone (keep behavior identical for the remaining path; update tests).

### 4. Scannable contact cards (progressive disclosure) — `_contact_macros.html`
- Always visible per card: name · primary star · role · **site label** · last-touch/cadence dot · the outreach buttons (Call/Email/Teams/WeChat).
- Reveal on hover (or in the existing kebab): edit / delete / set-primary / role / priority / archive / DNC. Use the established kebab pattern. Goal: a rep scanning "who to call next" sees a calm list, not 5 competing micro-actions per row.
- Add the **site label** to the card (the audit flagged its absence in the company-wide view).

### 5. Surface what was buried + breadcrumb
- Bring the cadence dot + state onto the always-visible slim header (already there) and make the tier/disposition reachable without hunting — keep the "Account settings" collapsible but give it a visible, labeled affordance (not just a kebab item), OR inline the cadence/disposition as compact controls. Pick the cleaner of the two per frontend-design judgment.
- Breadcrumb `Customers › {Account}` on the unified detail (the audit: account level had none).

### 6. Page-route no-cache (so deploys are visible without a hard refresh)
- The `/v2/*` full-page routes (the base_page render path) go out with **no `Cache-Control`**, so browsers heuristically cache the shell + the old hashed-CSS reference. Add `Cache-Control: no-cache` (must-revalidate) to the full-HTML page responses — in Caddy for the `/v2/*` page routes, or at the `template_response`/base_page layer. Do NOT no-cache the Vite-hashed `/static/assets/*` (those are correctly immutable). Verify a normal reload then picks up a new build.

### 7. Copy / empty states (UX)
- Honest, directive empty states: no contacts at a site → "No contacts at {site} yet" + `+ Add contact`; account with no sites → "No sites yet" + `+ Add site`; account with no contacts at all → invitation + `+ Add contact` / `Find contacts`. Sentence case, plain verbs, action-first. Breadcrumb + section labels name things the rep recognizes (Account, Site, Contact), not system terms.

## Files (read CURRENT state first; lines have shifted post-merge)
- **Routes (`app/routers/htmx_views.py`):** `company_detail_partial` (unify), retire `company_header_partial` + `company_site_detail_partial` + `sites-accordion` route, contact CRUD HX-Target consolidation, Sites-tab "View contacts" → Contacts-filter link.
- **Templates (modify):** `customers/detail.html`, `tabs/contacts_tab.html`, `tabs/_contacts_grouped_list.html`, `_contact_macros.html`, `tabs/sites_tab.html`, `tabs/site_card.html`, `_account_list.html`, `list.html`.
- **Templates (retire/repurpose):** `customers/header.html`, `customers/site_detail.html`, `customers/_sites_accordion.html`, `tabs/site_contacts.html`.
- **Caddyfile** (or response layer) for the no-cache page header.

## Constraints
- HTMX+Alpine+Jinja+Tailwind only; existing palette/patterns; Alpine `|tojson` single-quoted; `:hx-vals` object literals; StrEnum constants; Loguru. No data-model/migration change (Company→CustomerSite→SiteContact unchanged). Static-analysis ratchets: use `text-gray-600`/`text-[11px]`/`text-xs`, never `text-gray-500`/`text-[10px]`; run `tests/test_static_analysis.py`.
- Don't break the legacy `CustomerSite.contact_*` fallback rendering (some sites still carry a legacy primary contact) — keep handling both, but the canonical surface is `SiteContact`.
- Keep all existing tests green; update tests that assert the retired surfaces (`site_detail`, `site_contacts`, header-only multi-site) to the new unified flow; ADD tests for: unified detail for a multi-site account (no header-only fork), Contacts default surface with site sections + filter, Sites tab has no contact-edit surface, scannable card shows site label + hover actions, no-cache header on a `/v2/*` page route.

## Build sequence (each step shippable, TDD)
1. **Unify the workspace** — every account → unified `company_detail_partial`; retire the multi-site header-only fork + sites-accordion nav; breadcrumb on the detail. (Tests: multi-site account renders the full unified detail with Contacts default.)
2. **Contacts canonical surface** — people-search + site filter + light site sections in the Contacts view; make it the primary right-panel content; one render target. (Tests: filter narrows to a site; sections render; single-site shows one section no filter.)
3. **Sites tab = CRUD-only** — strip the duplicate contact list from `site_card`/`sites_tab`; "View N contacts" link → Contacts filter; consolidate CRUD HX-Target branches; retire `site_contacts.html`. (Tests: Sites tab has no contact editor; counts + link present.)
4. **Scannable cards** — progressive disclosure in `_contact_macros.html` + site label. (Tests: always-visible vs hover/kebab actions; site label present.)
5. **No-cache page header + copy/empty states.** (Tests: `Cache-Control: no-cache` on a `/v2/*` page response; empty-state copy.)

## Verification
- Full suite green (cwd = the worktree). `ruff check app` clean. Whole-branch frontend-design + code + silent-failure review; fix findings.
- Deploy + in-container render against real data: a MULTI-SITE account renders the unified detail (no "select a site" fork), Contacts default with site sections + filter, Sites tab shows site CRUD without a contact editor, a contact card shows its site label, and a `/v2/*` page response carries `Cache-Control: no-cache`.
