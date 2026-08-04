# Wave 2 Screen Diet — field null-rate evidence (brief §4.2)

Date: 2026-08-04. Source DB: `availai-simp-db-1` (prod copy, SELECTs only).
Template cross-reference: `/root/availai-worktrees/simplification/app/templates/**` (form `name=` attributes, rendered row/pane attributes) plus the CRM inline-edit registries in `app/routers/htmx/companies/_registries.py` (`EDITABLE_ACCOUNT_FIELDS` / `EDITABLE_CONTACT_FIELDS`).

**Method.** For every column a template renders as a form field or list column: null = SQL `NULL` or blank-after-trim text; JSON counts `null`/`[]`/`{}` as empty. 163 column measurements across 11 tables. Cut rule per brief §4.2: **near-100% null AND no workflow touches it. UI only — tables never change.**

**Denominator caveat (read first).** Only `vendor_cards` (n=1332) and `excess_line_items` (n=77) are strong evidence. requisitions n=24, requirements n=49, buy_plans_v3 n=10, buy_plan_lines n=22, quotes n=10, quote_lines n=15, companies n=13, site_contacts n=17, excess_lists n=10 — pre-launch, single-user data, and the SFDC import (no date) would fill CRM firmographics. Rows below marked **LOW-N** should be treated as flip-able, not proven.

44 of 163 measured columns are at exactly 100% null.

---

## Per-entity evidence tables

Recommendation legend: **CUT** = remove from UI in Wave 2 packet · **KEEP** = workflow touches it · **DEMOTE** = keep but collapse behind a disclosure · **NO-UI** = 100% null AND rendered nowhere (nothing to cut; already invisible).

### requisitions (n=24)

| Field | Null | Where rendered | Recommendation |
|---|---|---|---|
| name | 0/24 (0%) | list + detail_header + create modal | KEEP |
| status | 0/24 (0%) | list row badge | KEEP (derived in Wave 3 anyway) |
| urgency | 0/24 (0%) | form + row badge | KEEP |
| customer_name | 21/24 (88%) | `req_row.html` (1 use), create typeahead | DEMOTE — free-text legacy; company_id link is set on 15/24 and the typeahead flow owns it. Show company relation only |
| deadline | 21/24 (88%) | form + row | KEEP (used 3×; varchar quirk noted, not a Wave 2 item) |
| opportunity_value | 18/24 (75%) | `_opportunity_value.html` inline widget | KEEP |
| win_probability | **24/24 (100%)** | `_win_probability.html` inline widget in `detail_header.html` | **CUT — never set once; nothing reads it** |
| outcome_reason | 24/24 (100%) | rendered nowhere | NO-UI |

### requirements (n=49)

| Field | Null | Where rendered | Recommendation |
|---|---|---|---|
| primary_mpn | 5/49 (10%) | everywhere (row, modals, RFQ) | KEEP |
| target_qty | 0/49 (0%) | form + row | KEEP |
| target_price | 16/49 (33%) | form + row | KEEP |
| condition | 18/49 (37%) | form + row + RFQ | KEEP |
| notes | 22/49 (45%) | form + row | KEEP |
| substitutes | 44/49 (90%) | row expand | KEEP (trade-relevant) |
| manufacturer | 46/49 (94%) | form + row + quote-builder payload | KEEP |
| brand | 47/49 (96%) | form + row + `rfq_compose.html` body | KEEP — RFQ includes it |
| customer_pn | 47/49 (96%) | form + row + quote-builder payload | DEMOTE |
| need_by_date | 47/49 (96%) | form + quote-builder payload | DEMOTE |
| packaging | 47/49 (96%) | form + row + quote-builder payload | DEMOTE |
| firmware | 47/49 (96%) | form + row + quote-builder payload | DEMOTE |
| date_codes | 47/49 (96%) | form + row + quote-builder payload | DEMOTE |
| hardware_codes | 47/49 (96%) | form + row + quote-builder payload | DEMOTE |
| sale_notes | 47/49 (96%) | unified_modal per-line note + quote-builder modal | KEEP (salesperson→pricing handoff) |
| description | 48/49 (98%) | only as fallback behind `part_description` filter (MaterialCard wins) | NO-UI (display keeps working via MaterialCard) |
| sourcing_status | 0/49 (0%) | row | KEEP (derived in Wave 3) |
| sku | 49/49 (100%) | rendered nowhere (search/materials `sku` hits are MaterialCard's) | NO-UI |
| oem_pn | 49/49 (100%) | rendered nowhere | NO-UI |
| substitutes_text | 49/49 (100%) | rendered nowhere | NO-UI |
| package_type | 49/49 (100%) | hidden parse-carry input in `unified_modal.html`; visible only on search dossier/materials (MaterialCard surface) | NO-UI in deal flow — rides the AI park, no Wave 2 action |

DEMOTE note (flip-able): the six ~96%-null spec fields all flow into `rfq_compose` / `quote_builder_service.py` payloads when present, so they are workflow-touched and cannot be cut under the §4.2 rule. Recommendation: collapse them behind one "Specs" disclosure in the requirement form and drop them from default row columns.

### buy_plans_v3 (n=10) — LOW-N

| Field | Null | Where rendered | Recommendation |
|---|---|---|---|
| status / order_type / so_status | 0/10 | panes | KEEP |
| sales_order_number | 5/10 (50%) | panes | KEEP (kernel) |
| customer_po_number | 6/10 (60%) | panes | KEEP |
| total_cost / revenue / margin | 2/10 (20%) | panes | KEEP |
| ai_summary / ai_flags | 8/10 (80%) | panes | KEEP behind AI flag (keys-off honesty, Wave 1) |
| cancellation_reason | 9/10 (90%) | cancel flow | KEEP (workflow) |
| approval_notes | 10/10 (100%) | `_pane_sales_order.html` | KEEP — the manager-gate note field (kernel gate; LOW-N) |
| salesperson_notes | 10/10 (100%) | pane | KEEP (handoff note; LOW-N) |
| case_report | 10/10 (100%) | detail | KEEP behind AI flag (generated on completion, AI off) |
| so_rejection_note | 10/10 (100%) | pane (4 uses) | **CUT — belongs to the retired SO-verification track that spec §9 already deletes in Wave 3; confirmed dead in data** |

### buy_plan_lines (n=22) — LOW-N

| Field | Null | Where rendered | Recommendation |
|---|---|---|---|
| quantity / unit_cost / unit_sell / margin_pct / status / buyer_id | 0/22 | pane + lines | KEEP |
| po_number | 16/22 (73%) | PO verify flow | KEEP (kernel step 10) |
| issue_type / issue_note | 21/22 (95%) | PO issue flow | KEEP (workflow) |
| assignment_reason / ai_score | 12/22 (55%) | rendered nowhere | NO-UI |
| estimated_ship_date | 22/22 (100%) | PO-confirm form field ×2 | KEEP — part of the buy-instruction/PO-confirm kernel form; flag: zero usage to date |
| payment_method | 22/22 (100%) | PO-confirm form field ×2 | KEEP — prepayment-gate adjacent (kernel); flag: zero usage |
| received_at | 22/22 (100%) | receiving/serial-FRU surface | KEEP — that surface is being relinked in Wave 2 (spec §5.2 Decision E) |
| po_rejection_note | 22/22 (100%) | written by `rejection_note` form; not displayed back | KEEP (per-line PO verify gate, kernel) |
| sales_note | 22/22 (100%) | rendered nowhere (template `sales_note(s)` hits are QP fields) | NO-UI |
| manager_note | 22/22 (100%) | rendered nowhere | NO-UI |
| last_nudge_at | 20/22 (91%) | rendered nowhere (system) | NO-UI |

### quotes (n=10) — LOW-N

| Field | Null | Where rendered | Recommendation |
|---|---|---|---|
| quote_number / revision / status / validity_days | 0/10 | detail, PDF | KEEP |
| payment_terms / shipping_terms | 5/10 (50%) | `edit_form.html` + PDF | KEEP |
| subtotal / total_margin_pct / sent_at | 5/10 (50%) | detail/list | KEEP |
| result | 7/10 (70%) | won/lost buttons in `detail.html` | KEEP (kernel step 6) |
| result_reason | 8/10 (80%) | no current capture UI in templates | NO-UI (2 legacy values exist) |
| result_notes / won_revenue | 10/10 & 7/10 | rendered nowhere | NO-UI |
| notes | 10/10 (100%) | `edit_form.html` textarea + PDF | KEEP — standard quote field feeding the PDF; LOW-N |
| source | 10/10 (100%) | `detail.html` | **CUT display — never populated, informational only** |

### quote_lines (n=15) — LOW-N

All pricing columns (mpn, qty, cost_price, sell_price, margin_pct, currency) 0/15 null — KEEP. `manufacturer` 5/15 — KEEP. `description` **15/15 (100%)** but the UI reads `part_description` (MaterialCard first, own column as fallback; material_card_id set on 10/15) → NO-UI action; display keeps working.

### vendor_cards (n=1332) — the strong denominator

| Field | Null | Where rendered | Recommendation |
|---|---|---|---|
| display_name | 0/1332 (0%) | everywhere | KEEP |
| emails | 20/1332 (2%) | detail + RFQ targeting | KEEP |
| website | 51/1332 (4%) | detail + edit form | KEEP |
| phones | 292/1332 (22%) | detail + edit form | KEEP |
| domain | 1326/1332 (99.5%) | detail/list | KEEP — load-bearing for reply matching despite nulls |
| sighting_count / total_pos | 0/1332 | list/detail | KEEP |
| industry | **1332/1332 (100%)** | `edit_vendor_form.html` + `list.html` | **CUT from form + list** |
| hq_city | **1332/1332 (100%)** | edit form + detail | **CUT** |
| hq_country | **1332/1332 (100%)** | edit form + list + detail | **CUT** |
| employee_size | **1332/1332 (100%)** | edit form | **CUT** |
| hq_state / legal_name | 1332/1332 (100%) | rendered nowhere in vendors/ | NO-UI |
| linkedin_url | 1332/1332 (100%) | only `prospect_card.html` (prospecting lens) | NO-UI in vendor flow |
| contacts (json) | 1332/1332 (100%) | rendered nowhere (vendor_contacts table superseded it) | NO-UI |
| brand_tags | **1332/1332 (100%)** | chips ×4 in `list.html`/`vendor_card.html` | **CUT chips — tagging jobs die/go on-demand in Wave 1 (§6)** |
| commodity_tags | **1332/1332 (100%)** | chips ×4 | **CUT chips (same)** |
| avg_response_hours | **1332/1332 (100%)** | `overview_tab.html` ×2 | **CUT — comeback = Data Capture Initiative** |
| last_reply_at | **1332/1332 (100%)** | `vendor_card.html` ×2 | **CUT — same comeback** |
| last_outbound_at | 1331/1332 (99.9%) | `vendor_card.html` ×2 | **CUT — same comeback** |
| response_rate | 1327/1332 (99.6%) | list | **CUT — same comeback** |
| overall_win_rate | 1329/1332 (99.8%) | list + detail | **CUT — same comeback** |
| vendor_score | 1324/1332 (99.4%) | ×9 in `vendor_card.html`/list | **CUT display — 8 vendors scored, ever; align with §9 scoring-generation kill (persisted v2 only). Flip-able if v2 is meant to backfill it pre-launch** |

### companies (n=13) — LOW-N, SFDC import pending

| Field | Null | Where rendered (all via `_registries.py` inline-edit + create form) | Recommendation |
|---|---|---|---|
| name / source / account_type | 0–1/13 | list + detail | KEEP |
| domain | 5/13 (38%) | detail | KEEP |
| industry | 6/13 (46%) | detail | KEEP |
| account_owner_id | 9/13 (69%) | list/detail | KEEP |
| tier | 10/13 (77%) | list chip | KEEP |
| website / phone / employee_size / credit_terms / legal_name / hq_city / hq_state / hq_country / linkedin_url | 11–12/13 (85–92%) | detail field grid | KEEP pending SFDC import (LOW-N) |
| revenue_range | **13/13 (100%)** | detail field grid | **CUT from registry — no workflow reads it (LOW-N flag)** |
| tax_id | **13/13 (100%)** | detail field grid | **CUT — ERP-owned datum; AVAIL never uses it (LOW-N flag)** |
| notes | 13/13 (100%) | registry field | KEEP — activity-log notes carry the load, but a notes field is standard; flip-able |
| disposition | 13/13 (100%) | `_disposition_control.html` | KEEP — part of the prospecting→CRM lens flow (§5.4) |

### site_contacts (n=17) — LOW-N

| Field | Null | Where rendered | Recommendation |
|---|---|---|---|
| full_name / first_name / last_name / title | 0–8/17 | list + inline edit | KEEP |
| email | 13/17 (76%) | list + inline edit | KEEP (RFQ/quote target) |
| linkedin_url | 9/17 (53%) | inline edit | KEEP |
| contact_role | 15/17 (88%) | role select (canonical roles) | KEEP |
| phone | 17/17 (100%) | list + inline edit | KEEP — click-to-call is a kernel support surface |
| secondary_email | **17/17 (100%)** | inline-edit registry + macros | **CUT from registry/display** |
| secondary_phone | **17/17 (100%)** | inline-edit registry + macros | **CUT** |
| wechat_id | **17/17 (100%)** | inline-edit registry + macros | **CUT** |
| notes | 17/17 (100%) | rendered nowhere (`_contact_notes_modal` shows the activity feed, not this column) | NO-UI |

### excess_lists (n=10) — LOW-N

title/company_id/status/owner_id/version 0/10 null — KEEP. notes 7/10, close_at + open_at 7/10 — KEEP (close_at drives the live/closed bidding window). source_filename 9/10 — rendered nowhere, NO-UI.

### excess_line_items (n=77)

| Field | Null | Where rendered | Recommendation |
|---|---|---|---|
| part_number / quantity / condition / status / offer_count | 0/77 | `_lines.html` + modals | KEEP |
| manufacturer | 3/77 (4%) | lines + modals | KEEP |
| asking_price | 47/77 (61%) | add/edit modal | KEEP |
| best_offer_unit_price | 71/77 (92%) | best-bid rollup in `_lines.html` | KEEP (display-only rollup is spec'd behavior) |
| date_code | **77/77 (100%)** | add/edit modal + `_lines.html` column | **CUT from modal + list column — real trade term, but 0 of 77 lines in the owner's own flow ever used it (flip-able)** |
| notes | **77/77 (100%)** | add/edit modal | **CUT from modal** |
| description | 77/77 (100%) | via `part_description` filter (MaterialCard wins; material_card_id set 64/77) | NO-UI action |
| market_price / demand_score / demand_match_count | 62–0/77 | rendered nowhere | NO-UI — buyer-intelligence layer already parked (§5.3) |

---

## The Wave 2 packet cut list (summary)

**CUT (21 UI fields — near-100% null, no workflow):**

1. `requisitions.win_probability` widget (24/24)
2. `vendor_cards`: industry, hq_city, hq_country, employee_size (4 × 1332/1332) — comeback: paid enrichment keys on
3. `vendor_cards`: brand_tags + commodity_tags chips (2 × 1332/1332) — aligns with §6 tagging shrink
4. `vendor_cards`: avg_response_hours, last_reply_at, last_outbound_at, response_rate, overall_win_rate, vendor_score displays (6, all ≥99.4% null) — comeback: Data Capture Initiative; vendor_score flip-able vs §9 scoring v2
5. `companies`: tax_id, revenue_range (2 × 13/13, LOW-N)
6. `site_contacts`: secondary_email, secondary_phone, wechat_id (3 × 17/17, LOW-N)
7. `excess_line_items`: date_code, notes (2 × 77/77)
8. `buy_plans_v3.so_rejection_note` display (10/10) — rides the Wave 3 §9 SO-track deletion
9. `quotes.source` display (10/10, LOW-N)

**DEMOTE (flip-able, workflow-touched so not cuttable under §4.2):** requirement spec long-tail — customer_pn, need_by_date, packaging, firmware, date_codes, hardware_codes (~96% null each, but all feed RFQ compose / quote-builder payloads) → one collapsed "Specs" disclosure; `requisitions.customer_name` free-text → show company link only.

**KEEP despite 100% null (kernel/workflow):** buy_plan_lines estimated_ship_date, payment_method, received_at, po_rejection_note; buy_plans_v3 approval_notes, salesperson_notes; quotes.notes; site_contacts.phone.

**Already invisible (no Wave 2 work; candidate notes for later model cleanup, tables untouched):** requirements oem_pn / sku / substitutes_text / outcome_reason / package_type(deal flow); requisitions.outcome_reason; buy_plan_lines sales_note / manager_note / assignment_reason / ai_score / last_nudge_at; quotes result_reason / result_notes / won_revenue (no capture UI); quote_lines.description; vendor_cards contacts / hq_state / legal_name; site_contacts.notes; excess_lists.source_filename; excess_line_items market_price / demand_score / demand_match_count.
