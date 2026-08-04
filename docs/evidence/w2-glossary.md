# Wave 2 UI Glossary — old → new label map (brief §4.1)

Date: 2026-08-04. Scope: USER-FACING labels in
`/root/availai-worktrees/simplification/app/templates/**` only (342 templates
scanned). DB names, code identifiers, routes, hx-* attributes, JS untouched.
Apply only after Packet-2 sign-off.

**Method.** HTML-parser scan (not raw grep): visible text nodes plus
label-bearing attributes (`placeholder`, `title`, `aria-label`, `alt`,
`data-tooltip`) after stripping Jinja blocks, scripts, styles, and comments.
Alpine/JS attribute bodies excluded, so counts are genuine label sites. Nav tab
labels live inside a Jinja `{% set nav_items %}` block in
`app/templates/htmx/partials/shared/mobile_nav.html` (lines 26-44) and are
counted separately below. Seeded from the owner's vocabulary map
(memory: workflow_page_names.md) and spec v1.1 Decision B (target nav = Deals).

## Rename table (one row per term)

| Old term | Proposed new term | Occurrences | Example locations (template paths) | Rationale |
|---|---|---|---|---|
| Requisition / Requisitions | **Deal / Deals** | 55 in 34 files | `htmx/partials/requisitions/list.html:17` ("New Requisition"); `htmx/partials/dashboard.html:38` ("Create Requisition"); `htmx/partials/quotes/detail.html:36`; `htmx/partials/approvals/_sales_order_new.html:28`; `documents/rfq_summary.html:12` ("Requisition Details") | "Requisition" is procurement-ERP jargon nobody in the trade says. Spec Decision B already renames the tab to **Deals**; the entity label should match the tab. (Owner's own shorthand is "Reqs" — fallback option: keep "Req" and skip this row; recommendation is Deal, consistent with the decided nav.) |
| Req / Reqs / REQ # (abbrev.) | **Deal / Deal #** | 21 in 17 files | `htmx/partials/shared/topbar.html:23` (placeholder "Search reqs, customers, vendors..."); `htmx/partials/customers/list.html:99` ("Has open reqs"); `htmx/partials/vendors/emails_tab.html:57` ("Req #"); `htmx/partials/search/dossier_shell.html:47` ("Add to Req") | Follows row 1. Caveat: 2 of the 21 are displayed ID prefixes ("REQ-" in `offers/review_queue.html:72`, `sightings/preview_inquiry.html:105`) — flag: renaming a displayed record prefix mid-stream can confuse existing references; owner call. |
| Sighting / Sightings | **Availability** (e.g. "Recent availability", "Availability score") | 26 in 16 files (25 label sites + 1 nav tab) | nav `shared/mobile_nav.html:28`; `htmx/partials/vendors/overview_tab.html:124` ("Recent Sightings"); `htmx/partials/requisitions/tabs/parts.html:230`; `htmx/partials/parts/tabs/sourcing.html:27` ("Sighting Score (0-100)"); `htmx/partials/settings/data_export.html:54` | App-invented word for "a vendor seen offering this part". The trade word is stock **availability** ("checking availability"). "Vendor offer" would collide with the Offer entity (real RFQ responses), so Availability is the safe trade term. Nav tab disappears in Wave 4 (folded into Deals) regardless. |
| Requirement / Requirements | **Line / Lines** ("Add line", "No lines yet") | 18 in 11 files (19 hits minus 1 plain-English use) | `htmx/partials/requisitions/tabs/parts.html:15` ("Add Requirement"); `htmx/partials/sightings/table.html:135` (aria "Select all requirements"); `htmx/partials/quote_builder/modal.html:151` ("Customer Requirements"); `documents/rfq_summary.html:39` | The entity is just a line item on the deal; "line" is how RFQs/POs are spoken of in the trade. Excluded: `requisitions/unified_modal.html:317` placeholder "...customer requirements" — plain English, keep. Note `documents/rfq_summary.html` is the vendor-facing RFQ document; "Requirements" there is tolerable trade usage — rename to "Requested parts" or leave, owner call. |
| Sales Hub | **Deals** | 3 (2 headings + 1 nav tab) | nav `shared/mobile_nav.html:26`; `htmx/partials/parts/workspace.html:15` and `:30` | App-invented tab name; spec Decision B already decided the target name is Deals. |
| Materials | **Parts** | 2 (1 nav tab + 1 heading) | nav `shared/mobile_nav.html:30`; `htmx/partials/materials/workspace.html:283` (h1) | "Materials" is ERP-speak; the trade says parts/components. Nav entry leaves the tab bar in Wave 2 anyway (becomes a lookup); the page heading remains and should read Parts. |
| sourcing board | — (zero UI occurrences) | 0 | n/a | The phrase exists only in spec/docs; the actual surface is labeled "Sightings" everywhere. Covered by the Sighting → Availability row. Reported so the packet shows it was checked. |
| Proactive (tab, "Proactive Scorecard", "proactive offer") | defer — module parks behind flag (spec §5.4) | 6 in 6 files (5 label sites + 1 nav tab) | nav `shared/mobile_nav.html:40`; `htmx/partials/proactive/scorecard.html:14`; `htmx/partials/quotes/detail.html:34` (badge); `htmx/partials/requisitions/unified_modal.html:443` (tooltip) | Whole module parks behind its flag in Wave 2, so renaming is moot for launch. If/when it unparks: propose "Auto-match". Two labels survive outside the parked module (quotes badge, unified-modal tooltip) — sweep those with the park. |

## Considered and KEPT (not in the edit count)

| Term | Sites | Why kept |
|---|---|---|
| RFQ, PO, quote, prepayment, offer, bid, award | many | Real trade terms — explicitly protected by brief §4.1. |
| Buy Plan, Quality Plan, Approvals, Resell, CRM, Tasks | many | Owner's own vocabulary (page-names map) and/or decided nav names. |
| Excess List | 1 (`htmx/partials/resell/create_modal.html:8`) | Genuine trade term — brokers circulate "excess lists". |
| Prospecting / Prospect | 7 (6 labels + nav) | Owner's own word (vocabulary map lists "Prospecting"); folds into CRM as a lens in Wave 2 with the same name. |
| Enrich / Enrichment | 26 in 15 files | Data-industry rather than trade term, but it is the owner's working vocabulary and spec §7's AI-off states use it. Flag: available for rename to "Auto-fill" if the owner wants — not recommended. |
| Cadence | 5 in 4 files | Standard sales-ops term (contact cadence). Borderline; keep unless owner objects. |
| Sourcing (generic uses: "re-sourcing pool", "sourcing run", "Sourcing" tab) | 13 in 12 files | Plain trade English in a sourcing business; only the compound "sourcing board" was jargon and it has zero UI occurrences. The 2 "sourcing leads" empty-states (`vendors/detail.html:195`, `vendors/overview_tab.html:14`) vanish with the Wave-2 Sourcing Leads deletion. |
| Unavailability, Multiplier, Dossier, Parts Workspace, Watchlist | 0 visible labels | Internal/code-only names; nothing to edit. |

## Totals

- **Label sites needing edit (recommended rename set): 125**
  (Requisition 55 + Req/Reqs 21 + Sighting 26 + Requirement 18 + Sales Hub 3 + Materials 2)
- Optional if Proactive rename is pulled forward instead of parked: +6 → 131.
- Distinct template files touched by the recommended set: ~55 (rows overlap in
  requisitions/, sightings/, parts/, vendors/, approvals/, search/, quotes/,
  customers/, settings/, documents/).

## Flags for the Packet-2 sign-off

1. **Requisition → Deal vs owner's "Reqs" habit** — spec Decision B says Deals;
   owner's phone vocabulary says Reqs. One word must win before the sweep.
2. **"REQ-" displayed ID prefix** (2 sites) — rename with the rest or keep as a
   stable record prefix.
3. **Vendor-facing document** `documents/rfq_summary.html` carries two of the
   renames ("Requisition Details", "Requirements") — external-facing copy,
   worth an explicit owner glance.
4. Nav labels are data in a Jinja `{% set %}` block (`mobile_nav.html:25-44`),
   not plain markup — a test asserts on the Approvals tuple
   (`tests/test_buyplan_nav.py`), so nav renames must update that test in the
   same commit.
5. Sighting → Availability is the one proposal with no seed in the owner's
   vocabulary map — invented here from trade usage; needs his yes.
