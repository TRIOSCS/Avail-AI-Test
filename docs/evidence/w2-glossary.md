# Wave 2 UI Glossary — FINAL for Packet 2 (brief §4.1, SIGN-OFF-GATED)

Status: **APPLIED 2026-08-07** — Packet-2 sign-off received; rows 1, 2, 4,
5, 6 swept against the then-current tree (re-scan first, per plan; the
2026-08-04 counts below are historical). Row 3 **REJECTED** by the owner
(see row). Owner also approved the vendor-facing `documents/rfq_summary.html`
rewording ("Deal Details" / "Requested parts (n)") — applied; the displayed
**REQ-** record prefix stays REQ- (owner: keep). First drafted
2026-08-04; **re-verified 2026-08-04 late eve against the live W2 working
tree** (W2 surface deletes in flight — Dashboard, Sourcing Leads,
Email-Intelligence, Knowledge already gone from the tree; counts below are
current and can only shrink further as W2 lands). Scope: USER-FACING labels
in `/root/availai-worktrees/simplification/app/templates/**` only. DB names,
code identifiers, routes, hx-* attributes, JS untouched.

**Method.** HTML-parser scan (not raw grep): visible text nodes plus
label-bearing attributes (`placeholder`, `title`, `aria-label`, `alt`,
`data-tooltip`) after stripping Jinja blocks, scripts, styles, and comments.
Nav tab labels live in the `{% set nav_items %}` block of
`app/templates/htmx/partials/shared/mobile_nav.html` and are handled by
**W2.1 (nav 10→5), which is decided/ungated** — they are NOT part of this
gated sweep. Seeded from the owner's vocabulary map and spec v1.1 Decision B
(target nav = Deals).

## The recommended mapping (one row per term — single recommendation each)

| # | Old term | New term | Sites (re-verified) | Status |
|---|---|---|---|---|
| 1 | Requisition / Requisitions | **Deal / Deals** | 54 in 33 files | **RECOMMENDED.** Spec Decision B already names the tab **Deals**; the entity label must match the tab. Alternative (noted, not recommended): keep the owner's "Req" shorthand and skip rows 1–2 — but then tab says Deals while every page says Req. One word must win; recommendation is **Deal**. |
| 2 | Req / Reqs / REQ # | **Deal / Deal #** | 21 in 17 files | **RECOMMENDED** (follows row 1). **EXCEPT 2 sites = displayed "REQ-" record prefix → KEEP-until-sign-off** (see marker section below). |
| 3 | Sighting / Sightings | ~~Availability~~ | 26 in 16 files | **REJECTED (owner, 2026-08-07 Packet-2 sign-off) — "Sighting" STAYS everywhere.** The two words name different things in the owner's vocabulary: a **Sighting** = "we saw it listed on an API trading-vendor site" — a raw listing; an **Availability** = an offer — the buyer contacted the vendor, verified stock, negotiated, and qualified it. Renaming Sighting to Availability would collapse that distinction. Do not re-propose. |
| 4 | Requirement / Requirements | **Line / Lines** ("Add line", "No lines yet") | 18 in 11 files (19 raw − 1 plain-English use kept: `requisitions/unified_modal.html` "…customer requirements") | **RECOMMENDED.** "Line" is how RFQs/POs are spoken of in the trade. **EXCEPT the vendor-facing `documents/rfq_summary.html` site → KEEP-until-sign-off** (marker section below). |
| 5 | Sales Hub | **Deals** | 2 headings in `htmx/partials/parts/workspace.html` | **RECOMMENDED.** The nav tuple rename itself ships ungated in W2.1 (Decision B); this row is only the 2 page headings. |
| 6 | Materials | **Parts** | 2 in `htmx/partials/materials/workspace.html` | **RECOMMENDED.** Nav entry leaves the bar in W2.1 anyway (contextual lookup); the page heading should read Parts. |
| 7 | Proactive ("Proactive Scorecard", badge, tooltip) | — deferred | 5 label sites in 5 files | **OUT OF THIS SWEEP.** Module parks whole behind its flag (W2.5); the 2 labels outside the module (`quotes/detail.html` badge, `requisitions/unified_modal.html` tooltip) are swept by W2.5 itself. If it ever unparks: propose "Auto-match". |

"sourcing board": still 0 UI occurrences (re-verified) — covered by row 3; checked, nothing to edit.

## KEEP-until-sign-off markers (apply NOTHING here without a line-item yes)

| Marker | Sites | Why gated separately |
|---|---|---|
| **Displayed "REQ-" record prefix** | `htmx/partials/sightings/preview_inquiry.html:105` (`REQ-{{ group.req_id }}` — the review_queue site died with the W4 deletes) | **RESOLVED 2026-08-07: owner says KEEP.** The displayed REQ- record prefix stays REQ- everywhere. |
| **Vendor-facing document `documents/rfq_summary.html`** | `:29` "Requisition Details", `:40` "Requirements (n)" | **RESOLVED 2026-08-07: owner APPROVED the suggested wording — applied.** Headings now read "Deal Details" / "Requested parts (n)". |

## Considered and KEPT (unchanged from draft; re-confirmed)

- **RFQ, PO, quote, prepayment, offer, bid, award** — real trade terms, protected by brief §4.1.
- **Buy Plan, Quality Plan, Approvals, Resell, CRM, Tasks, Excess List, Prospecting** — owner vocabulary / decided nav names.
- **Enrich/Enrichment** (26 sites), **Cadence** (5) — owner's working vocabulary; rename available on request, not recommended.
- **Sourcing** generic uses — plain English in a sourcing business. The 2 "sourcing leads" empty-states die with the W2 Sourcing-Leads delete (in flight tonight).
- Unavailability, Multiplier, Dossier, Watchlist — no visible labels; nothing to edit.

## Totals (re-verified 2026-08-04 late eve)

- **Recommended rename set (rows 1–6): 123 label sites across 59 distinct template files** (124 raw − 1 plain-English keep). Of these, **4 sites are KEEP-until-sign-off** (2 REQ- prefix in row 2 + 2 rfq_summary in rows 1/4). Arithmetic: rows 1+2+4+5+6 = 97 sites → **93 apply on packet sign-off** (97 − 4 gated); row 3 (Sighting→Availability, 26 sites) adds only with its own explicit yes → **119 with row 3 approved**.
- Draft→final deltas: Requisition 55→54 (dashboard.html deleted by W2 tonight); Requirement file split corrected; totals restated. Counts will shrink further as W2/W4 deletes land — **the apply step re-runs the scan first** (see plan).

## Apply plan — one command after sign-off

**Standing scope rule for every edit:** visible label text and the 5
label-bearing attributes ONLY. Never touch: routes/URLs (`/v2/requisitions`
stays), `hx-*` attributes, Alpine/JS expressions, Jinja variable and macro
names, template file/dir names, DB or code identifiers, test fixture data
that mirrors DB values.

**The one command (executor prompt, verbatim):**
`Apply docs/evidence/w2-glossary.md rows [list the approved row numbers] — re-run the scan, sweep, update pinned tests, verify.`

That command executes, in order:

1. **Re-run the scan** (method section above) on the then-current branch to
   regenerate the exact site list — W2/W4 deletes will have shrunk it; never
   sweep from this doc's frozen file list.
2. **Sweep, one commit per approved row** (`W2.G1: Requisition→Deal labels
   (§4.1, signed off)` etc.), touching only the scanned sites, minus any
   KEEP-until-sign-off sites not explicitly approved. Grammar pass in the
   same commit: "a Deal", "no lines yet", pluralization.
3. **Pinned-test updates, same commit as their row.** Known pin sites
   (grep-verified tonight; re-grep at apply time):
   - `tests/test_saleshub_view_toggle.py`, `tests/test_htmx_views.py`,
     `tests/test_sightings_router.py`, `tests/test_req_import.py`,
     `tests/test_requisition_hotlist.py`, `tests/test_authz_ownership.py`,
     `tests/test_alerts_spotlight_render.py`, `tests/test_nav_badges.py`,
     `tests/test_requisitions2_redirect.py`, `tests/e2e/test_app_deep.py`
     (assert on "New Requisition"/"Sales Hub"/"Recent Sightings"/
     "Add Requirement"/"Search reqs" strings)
   - Playwright: `e2e/sales-hub-ui.spec.ts`, `e2e/kernel-walk.spec.ts`
   - Re-grep command: `grep -rl 'Requisition\|Sales Hub\|Sighting\|Requirement' tests/ e2e/` then trim only label-string asserts.
4. **Nav-tuple test rule:** any commit that edits `mobile_nav.html` labels
   (W2.1 owns the nav rename; this sweep normally does not touch nav) MUST
   update `tests/test_buyplan_nav.py` in the same commit — it asserts the
   literal `('buy-plans', 'Approvals', '/v2/approvals',` tuple plus
   `urlToNav` aliases against the template source.
5. **Verify:** re-run the scan → zero remaining old-term label sites in the
   approved rows (KEEP-until-sign-off sites excepted); suite green; kernel
   E2E green; headless render of Deals list + workspace shows new labels.

**SIGN-OFF ASK (Packet 2):** Approve glossary rows 1, 2, 4, 5, 6 as recommended (Deal / Deal # / Line / Deals / Parts — 93 sites)? And separately: (a) row 3 Sighting→**Availability** (26 sites) yes/no, (b) rename the displayed **REQ-** prefix (2 sites) yes/no, (c) reword vendor-facing **rfq_summary.html** (2 sites) yes/no.
