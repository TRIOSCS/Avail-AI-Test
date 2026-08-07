# Wave 2 Screen Diet — FINAL for Packet 2 (brief §4.2, SIGN-OFF-GATED)

Status: **APPLIED 2026-08-07** — Packet-2 sign-off received; all 19 cut
items + the 7-field DEMOTE set applied against the then-current tree.
Per-item outcomes: items 1–18 applied as planned (item 11's list Score
column removal also carried the cell's Blacklisted/Archived badges — the
row tint + "Hide blacklisted" filter keep that signal); item 19 was
**already gone** (the W2.5 Proactive park removed the quotes/detail.html
badge — a comment at the site records it). DEMOTE: six spec fields
collapsed into a "Specs" disclosure in all three requirement forms
(tabs/parts.html add form, tabs/req_row.html inline edit, unified_modal.html
per-part collapsible spec row) and dropped from the requirement-row columns;
`parts/list.html` deliberately untouched (its Bid Due triage column and
spec-count chip are workflow surfaces, already a demoted presentation).
customer_name: header + list row now render the company relation only.
Drafted 2026-08-04;
**every CUT/DEMOTE row re-verified 2026-08-04 late eve**: null rates
re-queried on `availai-simp-db-1` (prod copy, SELECTs only) and render sites
re-checked against the live W2 working tree (surface deletes in flight).
Cut rule per brief §4.2: **near-100% null AND no workflow touches it. UI
only — tables never change, columns never dropped.**

## ⚠ LOW-N CAVEAT — READ FIRST

Only `vendor_cards` (**n=1336**) and `excess_line_items` (**n=82**) are
strong evidence. Everything else is pre-launch single-user data:
requisitions n=36, requirements n=68, quotes n=17, buy_plans_v3 n=17,
companies n=20, site_contacts n=17. The pending SFDC import (no date) would
fill CRM firmographics. **Every row tagged LOW-N below is flip-able, not
proven — approve them as "cut for now, trivially restorable from git."**
Counter-signal strengthening the cuts: the owner actively used the app
between draft and re-verify (requisitions 24→36, quotes 10→17, buy plans
10→17, excess lines 77→82) and **every cut field is still at 100% null**.

## Draft → final corrections (re-verification errata)

1. **W1.9/W1.13 (tonight's status removals) touched NO cut-list field** —
   verified against commit fa00466f: its template edits are task/follow-up/
   outreach status chips only. The in-flight W2 surface deletes don't touch
   cut fields either (they remove whole orphan pages).
2. **Three draft rows claimed render sites that never existed** (scan error,
   verified against the evidence-pack commit): `vendor_cards.last_reply_at`,
   `vendor_cards.last_outbound_at` (the vendor card renders neither; the
   `last_reply/last_outbound` template hits are SiteContact cadence clocks in
   `customers/` — KEEP, workflow), and `excess_line_items.notes` (no notes
   field in the add/edit line modals). All three reclassify **CUT → NO-UI**
   (nothing to remove). **Cut list: 21 → 19 fields** (the draft's "21" also
   miscounted its own enumeration of 22).
3. Site corrections: vendor firmographics are on the **create** form
   (`create_form.html`) — `edit_vendor_form.html` carries only
   name/website/emails/phones; `avg_response_hours` renders once in
   `vendors/detail.html:134` (not overview_tab ×2); tag chips live only in
   `vendor_card.html` (not list.html); `so_rejection_note` has 2 display
   sites (not 4); metric displays also surface in `sightings/vendor_modal`,
   `sightings/_vendor_row`, and `proactive/_match_row` (noted per field).

## THE CUT LIST — 19 UI fields, with exact removal plan per field

Null counts re-verified 2026-08-04 late eve. Tables/columns stay; git
restores everything.

### requisitions (n=36 — LOW-N)

**1. `win_probability` — 36/36 null (100%), never set once.**
- Remove: `htmx/partials/requisitions/_win_probability.html` (whole
  partial); its include at `requisitions/detail_header.html:42`; route
  `PATCH /v2/partials/requisitions/{req_id}/win-probability`
  (`app/routers/htmx/requisitions_edit.py:241–276`).
- Tests: trim win-prob cases in `tests/test_task_snooze_badge_winprob.py`;
  check refs in `tests/test_opportunity_value_inline.py`,
  `tests/test_crm_audit_trail.py`. (`_opportunity_value.html` KEEPS — used.)

### vendor_cards (n=1336 — STRONG evidence)

**2–5. Firmographics: `industry`, `hq_city`, `hq_country`,
`employee_size` — each 1336/1336 null (100%).**
- Remove: create-form inputs `create_form.html:51–56` (industry),
  `:77–89` (hq_city + hq_country grid), `:90–103` (employee_size select);
  detail display `detail.html:52–58` (location + industry lines); card row 6
  `vendor_card.html:102–106` (country · industry); list column
  `list.html:178` ('Location' + 'Industry' col defs) and cells `:234–235`.
- Comeback: paid enrichment keys on — the shared enrichment-result panel
  `shared/_enrich_result.html` (maps industry/employee_size/hq_*) **STAYS**;
  it is the surface these fields return through.
- Tests: `tests/test_vendor_helpers.py`, `tests/test_vendor_create_delete.py`
  (grep-verified candidates; trim only removed-UI asserts).

**6–7. `brand_tags` + `commodity_tags` chips — each 1336/1336 empty.**
- Remove: chip block `vendor_card.html:64–78` (both loops + "+n more").
- Aligns with §6 tagging shrink (prefix/spec jobs already on-demand, W1.3).

**8. `avg_response_hours` — 1336/1336 null.**
- Remove: `detail.html:134` "Avg Response" span (+ its `·` separator).
- Comeback: Data Capture Initiative (§6).

**9. `response_rate` — 1331/1336 null (99.6%).**
- Remove: card stat `vendor_card.html:99` ("x% resp");
  `sightings/vendor_modal.html:50–51` ("x% response");
  `sightings/_vendor_row.html:281–284` (intel dict — this display also dies
  with the W2 contact-intel delete / W4 sightings slim; remove now, note
  overlap). Comeback: Data Capture Initiative.

**10. `overall_win_rate` — 1333/1336 null (99.8%).**
- Remove: card stat `vendor_card.html:98`; detail stat `detail.html:130`;
  list sort option `list.html:131`, 'Win Rate' col def `:178`, cell `:233`.
- Comeback: Data Capture Initiative.

**11. `vendor_score` — 1328/1336 null (8 vendors scored, ever).**
- Remove: card score bar block `vendor_card.html:39–61` (incl. the "New — no
  score yet" else-branch); detail score hero `detail.html:64–70`
  (score_breakdown call); list 'Score' col `:178` + badge `:226–227`;
  `sightings/vendor_modal.html:62–63` ("Score: n" fallback).
  (`proactive/_match_row.html:38` parks with W2.5 — no action.)
- **Flip-able flag:** §9 kills scoring generations v1/v3/v4 but keeps
  persisted v2 as truth — if v2 is meant to backfill scores pre-launch, KEEP
  the displays and cut only the dead generations. Recommendation: cut now,
  restore with v2 backfill evidence.
- Tests (metrics 9–11): grep candidates `tests/test_score_price_hover.py`,
  `tests/test_scoring.py`, `tests/test_vendor_helpers.py`,
  `tests/test_sightings_coverage.py` — trim only display asserts; scoring
  service tests stay (persisted v2 untouched).

### companies (n=20 — LOW-N, SFDC import pending)

**12. `tax_id` — 20/20 null. ERP-owned datum; AVAIL never uses it.**
- Remove: registry rows `app/routers/htmx/companies/_registries.py:67`
  and `:116`; create input `customers/create_form.html:135`; edit input
  `customers/edit_form.html:159`.

**13. `revenue_range` — 20/20 null.**
- Remove: registry rows `_registries.py:56` and `:112`; create select
  `customers/create_form.html:77–87` (label + select); edit select
  `edit_form.html:100–104`.
- NOT touched: `prospecting/detail.html:416` (that is
  `ProspectAccount.revenue_range`, a different table) and
  `shared/_enrich_result.html` (enrichment surface — comeback path).
- Tests (12–13): `tests/test_inline_field_edit.py`,
  `tests/test_company_core_idor.py`, `tests/test_crm_views.py` grep
  candidates; provenance/migration tests unaffected (columns stay).

### site_contacts (n=17 — LOW-N)

**14–16. `secondary_email`, `secondary_phone`, `wechat_id` — each 17/17
null.**
- Remove: registry rows `_registries.py:80–82` (+ the `secondary_phone`
  branch in the phone-kind check `:244`); form inputs
  `customers/tabs/_contact_form.html:130–132`, `:136–138`, `:163–165`;
  display macros `customers/_contact_macros.html:524–528` (secondary_email),
  `:531–543` (secondary_phone), `:511–517` (wechat).
- KEEP: `phone` (17/17 null but click-to-call is kernel support), `email`,
  `linkedin_url`, `contact_role`.
- Tests: `tests/test_contact_fields_144.py`, `tests/test_inline_field_edit.py`,
  `tests/test_contacts_ui_compact.py`, `tests/test_crm_views.py`,
  `tests/test_rubric_h3_validation_lists.py`,
  `tests/test_contact_outreach_buttons.py`, `tests/test_activity_outreach.py`
  (grep candidates; trim removed-UI asserts only).

### excess_line_items (n=82 — STRONG for the owner's own flow)

**17. `date_code` — 82/82 null. Real trade term, but 0 of 82 lines in the
owner's own flow ever used it (flip-able).**
- Remove: `resell/add_line_modal.html:41–44` (label + input);
  `resell/edit_line_modal.html:41–44`; line display `resell/_lines.html:131`;
  import-preview column `resell/import_preview.html:45` (+ its header cell).
  Import parser may keep writing the column (table untouched) — display only.
- Tests: `tests/test_excess_crud.py`, `tests/test_resell_draft_edit.py`,
  `tests/test_excess_service_comprehensive.py`,
  `tests/test_resell_themes_dghi.py`, `tests/test_resell_bid_sheet.py` —
  service-level date_code tests keep passing (column stays); trim only
  form/template asserts.

### buy_plans_v3 (n=17 — LOW-N)

**18. `so_rejection_note` display — 17/17 null; belongs to the retired
SO-verification track that spec §9 deletes in Wave 3.**
- Remove display only: `approvals/_pane_sales_order.html:118` (halted-banner
  suffix — file is being edited by W2.31 in flight, re-locate at apply time);
  `buy_plans/_detail_sidebar.html:130–133`. Writers/services go with the
  Wave-3 §9 SO-track deletion, not here.

### quotes (n=17 — LOW-N)

**19. `source` display — 17/17 null; informational only.**
- Its ONLY render site is the Proactive badge `quotes/detail.html:31–36` —
  the exact lines the W2.5 Proactive-park sweep removes. **Expected to be a
  no-op by apply time**; listed for completeness. If W2.5 hasn't landed:
  remove the badge block.

## DEMOTE (workflow-touched — not cuttable under §4.2; flip-able)

Re-verified n=68: `customer_pn`, `need_by_date`, `packaging`, `firmware`,
`date_codes`, `hardware_codes` each **66/68 null (97%)** — but all six flow
into `rfq_compose` / `quote_builder_service.py` payloads when present.
**Plan:** collapse the six into ONE "Specs" disclosure in the requirement
form; drop them from default row columns. Plus
`requisitions.customer_name` (26/36 null; free-text legacy, company_id link
owns the flow): show the company relation only.

## KEEP despite 100% null (kernel/workflow — unchanged)

`buy_plan_lines`: estimated_ship_date, payment_method (PO-confirm kernel
form), received_at (serial/FRU surface — relinked W2.31), po_rejection_note
(per-line PO verify gate). `buy_plans_v3`: approval_notes (manager gate),
salesperson_notes, case_report + ai_summary/ai_flags (behind AI flag).
`quotes.notes` (feeds the PDF). `site_contacts.phone` (click-to-call).
`companies.notes`, `companies.disposition` (prospecting lens flow).
`vendor_cards.domain` (99.5% null but load-bearing for reply matching).
`excess`: best_offer_unit_price rollup, close_at window, asking_price.

## Already invisible — NO-UI (no Wave-2 work; tables untouched)

requirements: sku, oem_pn, substitutes_text, description (part_description
filter wins), package_type (deal flow); requisitions.outcome_reason;
buy_plan_lines: sales_note, manager_note, assignment_reason, ai_score,
last_nudge_at; quotes: result_reason, result_notes, won_revenue;
quote_lines.description; vendor_cards: contacts json, hq_state, legal_name,
linkedin_url (vendor flow), **last_reply_at, last_outbound_at** (reclassified
— see errata); site_contacts.notes; excess_lists.source_filename;
excess_line_items: market_price, demand_score, demand_match_count,
**notes** (reclassified — see errata).

## Apply mechanics (after sign-off)

One commit per entity group (`W2.G2: vendor_cards field cuts (§4.2, signed
off)` etc.); each commit takes its template edits + route deletion (item 1
only) + test trims together; suite green + kernel E2E green after the batch;
re-grep each cut field name across `app/templates/` → zero render sites.

**SIGN-OFF ASK (Packet 2):** Approve the 19-field cut list (items 1–19; LOW-N rows flip-able as marked) + the 7-field DEMOTE-to-"Specs"-disclosure set — UI only, tables and columns untouched, every removal one `git revert` away?
