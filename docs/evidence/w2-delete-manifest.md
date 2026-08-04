# Wave-2 Delete Execution Manifest — orphaned /api routes

Built 2026-08-04 from `docs/evidence/w2-api-orphans.md` (verbatim route/test/LOC data; all
totals re-derived by machine parse of that doc: 107 routes / 4,415 handler LOC / 15
whole-file tests (4,604 LOC) / 65 mixed tests — all four match the evidence doc exactly).
Spec authority: SIMPLIFICATION_SPEC.md §8 (delete list) + §10 Wave 2. Worktree
`/root/availai-worktrees/simplification`; file:line spans verified against the tree at
analysis time — re-check line numbers at execution (three build agents are editing).

**Shape: 14 commits.** B0 (re-point cross-cutting probes, no deletions) → B1–B9 (low-risk
handler deletes) → B10 (POST /api/requisitions special case) → B11–B13 (drift-gate /
fresh-boot-adjacent batches, LAST). Every batch = one commit: handler spans + that batch's
whole-file test deletes + that batch's mixed-file trims, same commit (repo convention).

## Execution rules (every batch)

1. Delete decorator → end-of-function (the evidence doc's LOC spans). **Exception in B2 —
   dual-registered handlers: delete alias decorators only, never the handler** (details in B2).
2. **Path-level keep rule**: a path referenced by any verb keeps every method. Never widen a
   deletion to a sibling method not on the orphan list.
3. Batch gate, run after each commit: `python -c "import app.main"` (boot), pytest on every
   test file touched by the batch, then route-count delta check
   (`grep -c '@router\.' <file>` before/after == expected drop).
4. KEEP-AMBIGUOUS routes (§F) are NOT deleted in these batches — B10 is the single
   spec-authorized exception.
5. Orphaned imports/services exposed by a deletion: remove only same-file dead imports.
   Service/model deletions are OUT of these commits (drift-gate rule: tables never dropped,
   ORM models only via the grandfather path — separate reviewed commit if wanted).
6. Wave-2 acceptance re-run at the end: deleted surfaces 404 on the simp instance; fresh-DB
   boot green (drift gate); kernel E2E green.

## Batch order and sizing

| # | Batch | Routes | Handler LOC | Whole-file tests deleted | Why this position |
|---|---|---|---|---|---|
| B0 | Cross-cutting re-point | 0 | 0 | 0 (re-points e2e/api.spec.ts instead of deleting it) | Probes must move to surviving routes BEFORE their current targets die |
| B1 | crm/enrichment.py | 6 | 13 | test_enrichment_authz.py (173) | Tiny warm-up; proves the batch gate |
| B2 | error_reports.py aliases | 2 | ~4 real (see warning) | — | Subtle alias-only trim; do it while attention is fresh |
| B3 | companies + vendors_crud + vendor_contacts | 11 | 369 | test_authz_crm_companies_idor.py (97) | Pure handler deletes, vendor/company domain |
| B4 | v13_features/activity.py | 14 | 347 | test_unmatched_activities.py (383) | Pure handler deletes |
| B5 | ai.py | 11 | 380 | — | Pure handler deletes |
| B6 | crm/offers.py | 9 | 723 | test_integration_crm.py (281), test_offers_perf4.py (191) | Pure handler deletes |
| B7 | crm/quotes.py | 8 | 405 | test_quotes_material_card.py (510) | Pure handler deletes |
| B8 | materials.py | 5 | 215 | — (e2e/api.spec.ts re-pointed in B0, not deleted — §E) | Pure handler deletes |
| B9 | requisitions/core + attachments + clone.py | 8 | 245 | test_authz_app_routers_requisitions_core.py (87), test_routers_crm_clone.py (83) | clone.py is a whole-file delete + `app/routers/crm/__init__.py` edit → first boot-touching batch |
| B10 | POST /api/requisitions (spec-named) | +1 | ~25 | — | Special case (§G); after B9 so core.py has settled |
| B11 | requisitions/requirements.py | 17 | 1,082 | test_requirements.py (729), test_requirements_router_coverage.py (791), test_requirements_async_coverage.py (178), test_api_manufacturer_validation.py (71) | Biggest batch; leads routes sit next to Sourcing-Leads models (drift-gate adjacency) |
| B12 | sources.py | 9 | 335 | — | email-mining routes border the PARKED Data Capture pipeline; /api/system/alerts touches health plumbing |
| B13 | admin/system.py | 7 | 249 | test_credential_management.py (355), test_subscription_health.py (627) | Credentials/health/audit handlers — most model/startup adjacency → LAST |

Totals: B1–B13 = 107 orphan routes + 1 spec-named (B10), 4,415 evidence-doc LOC (+~25 B10);
14 whole-file test deletes + e2e/api.spec.ts re-pointed = the evidence doc's 15.

## A. Batch detail

Route tables are verbatim from the evidence doc (handler file:line, decorator→end LOC).
"Trim" = delete only the test functions in that file hitting the listed paths (locate with
`rg -n '<path-fragment>' tests/<file>`).

### B0 — Cross-cutting re-point (commit 1, no deletions)

Edits only: test_security_headers.py, test_access_control.py, test_offers_idor.py,
test_activity_authz_idor.py, test_authz_hardening.py, test_authz_requisition_read_idor.py,
test_authz_merge_addsite_idor.py, e2e/api.spec.ts. Full plan in §E. Gate: those files green
BEFORE any route is deleted.

### B1 — app/routers/crm/enrichment.py (6 routes, 13 LOC)

| Method | Route | Line | LOC |
|---|---|---|---|
| POST | /api/enrich/vendor/{card_id} | 255 | 0 |
| GET | /api/suggested-contacts | 300 | 0 |
| POST | /api/suggested-contacts/add-to-vendor | 319 | 0 |
| POST | /api/suggested-contacts/add-to-site | 354 | 0 |
| GET | /api/users/list | 440 | 0 |
| POST | /api/customers/import | 458 | 13 |

The 0-LOC rows are measurement artifacts of the span method — confirm each decorator+handler
extent by eye before deleting; expect thin delegation bodies.
KEEP in this file: `GET /api/enrich/company/{company_id}/status` (enrich_status.html:12) and
the ambiguous `POST /api/enrich/company/{company_id}` (§F) — do not touch.
Whole-file delete: tests/test_enrichment_authz.py (173 LOC).
Trim: test_routers_crm.py (`/api/enrich/vendor/`, `/api/suggested-contacts`,
`/api/users/list`, `/api/customers/import`); test_authz_merge_addsite_idor.py — B0 already
handled its add-to-site/suggested-contacts tests (§E.7); confirm only merge-side tests remain.

### B2 — app/routers/error_reports.py (2 routes — ALIAS-ONLY, ~4 deletable lines)

| Method | Orphan route | Evidence-doc line/LOC | Actual action |
|---|---|---|---|
| GET | /api/error-reports/{report_id} | 437 / 24 | delete ONLY decorator line 443 `@router.get("/api/error-reports/{report_id}")` |
| PATCH | /api/error-reports/{report_id} | 580 / 28 | delete ONLY decorator line 591 `@router.patch("/api/error-reports/{report_id}")` |

**WARNING — do not apply the 52-LOC spans.** Both handlers are dual-registered (verified:
decorators stacked at error_reports.py:443–444 and 591–592). The `/api/trouble-tickets/{report_id}`
aliases are LIVE — tickets/detail.html:33 and :87 `fetch('/api/trouble-tickets/' + id, {method:'PATCH'})`.
Deleting the evidence-doc span kills a reachable route. Net deletable: the two
`/api/error-reports/...` decorator lines. The bare-collection aliases at 399/411 are
KEEP-AMBIGUOUS (§F) — untouched here.
Trim (drop only assertions/requests against the `/api/error-reports/{id}` spelling; keep the
trouble-tickets coverage): test_error_reports.py, test_error_reports_coverage.py,
test_error_reports_coverage3.py, test_routers_error_reports.py.
Gate extra: `rg -n '/api/trouble-tickets/' app/routers/error_reports.py` still shows GET+PATCH
param decorators; tickets detail page status dropdown works on the simp instance.

### B3 — crm/companies.py + vendors_crud.py + vendor_contacts.py (11 routes, 369 LOC)

companies.py (3, 60 LOC): GET /api/companies/check-duplicate (204/17),
POST /api/companies/{company_id}/summarize (506/19), POST /api/companies/{company_id}/analyze-tags (527/24).
vendors_crud.py (3, 49 LOC): POST /api/vendors/{card_id}/blacklist (430/14),
POST /api/vendors/{card_id}/reviews (462/19), DELETE /api/vendors/{card_id}/reviews/{review_id} (483/16).
vendor_contacts.py (5, 260 LOC): POST /api/vendor-contact (75/86),
GET /api/vendors/{card_id}/contacts/{contact_id}/timeline (253/38),
GET /api/vendors/{card_id}/contacts/{contact_id}/summary (293/12),
GET /api/vendors/{card_id}/email-metrics (495/61), POST /api/vendor-card/add-email (561/63).

Note: contact timeline/summary belong to the contact-intelligence layer the spec cuts
(§5.4) — routes go now; the service/model layer is a separate Wave-2 line item, not this commit.
vendor_contacts.py holds 5 more KEEP-AMBIGUOUS routes (§F) — untouched; if the §F re-verify
later deletes them the file nearly empties (11 decorators total).
Whole-file delete: tests/test_authz_crm_companies_idor.py (97 LOC).
Trim: test_routers_crm.py (check-duplicate, summarize, analyze-tags);
test_routers_vendors_crud.py (blacklist, reviews ×2 — its `/engagement` test trims in B12);
test_routers_vendor_contacts.py (vendor-contact, email-metrics, add-email);
test_contact_intelligence_service.py (timeline, summary); test_phase2_orphans.py (summary).

### B4 — app/routers/v13_features/activity.py (14 routes, 347 LOC)

| Method | Route | Line | LOC |
|---|---|---|---|
| POST | /api/calls/initiate | 223 | 38 |
| GET | /api/companies/{company_id}/activities | 296 | 13 |
| POST | /api/companies/{company_id}/activities/call | 311 | 29 |
| POST | /api/companies/{company_id}/activities/note | 342 | 25 |
| GET | /api/users/{target_user_id}/activities | 384 | 13 |
| POST | /api/activities/email | 399 | 25 |
| POST | /api/activities/call | 426 | 25 |
| POST | /api/vendors/{vendor_id}/activities/call | 453 | 28 |
| POST | /api/vendors/{vendor_id}/activities/note | 483 | 25 |
| GET | /api/activities/unmatched | 515 | 21 |
| POST | /api/activities/{activity_id}/attribute | 538 | 32 |
| POST | /api/activities/{activity_id}/dismiss | 572 | 15 |
| GET | /api/vendors/{vendor_id}/activity-status | 589 | 29 |
| GET | /api/companies/{company_id}/activity-status | 620 | 29 |

KEEP-AMBIGUOUS in file: GET /api/vendors/{vendor_id}/activities (369) — untouched.
The click-to-call + outcome-chip flow the kernel keeps lives on /v2 partials and
POST /api/activity/outreach-initiated (htmx_app.js:524) — unaffected.
Whole-file delete: tests/test_unmatched_activities.py (383 LOC).
Trim: test_activity_router_coverage2.py + test_routers_v13.py (all 14 paths — these two may
end up near-empty; if nothing meaningful remains, delete the file and say so in the commit);
test_v13_activities.py + test_v13_activity_ownership.py (4 paths each);
test_integrations.py (/api/calls/initiate); test_activity_authz_idor.py — B0 already
re-pointed its company-scoped probes (§E.4); delete its 3 companies-activities tests here.

### B5 — app/routers/ai.py (11 routes, 380 LOC)

| Method | Route | Line | LOC |
|---|---|---|---|
| POST | /api/ai/find-contacts | 93 | 78 |
| GET | /api/ai/prospect-contacts | 173 | 33 |
| POST | /api/ai/prospect-contacts/{contact_id}/save | 208 | 29 |
| DELETE | /api/ai/prospect-contacts/{contact_id} | 239 | 14 |
| POST | /api/ai/prospect-contacts/{contact_id}/promote | 255 | 22 |
| POST | /api/ai/generate-description/{requirement_id} | 417 | 34 |
| POST | /api/ai/parse-response/{response_id} | 456 | 73 |
| POST | /api/ai/save-parsed-offers | 531 | 20 |
| GET | /api/ai/company-intel | 556 | 25 |
| POST | /api/ai/intake-parse | 586 | 34 |
| POST | /api/ai/save-freeform-offers | 625 | 18 |

KEEP in file: bare `POST /api/ai/generate-description` and `/api/ai/standardize-description`
(htmx_app.js fetches) — distinct paths from the orphan `/{requirement_id}` variant; plus
2 KEEP-AMBIGUOUS (§F: parse-email, normalize-parts). Do not over-delete.
Whole-file delete: none.
Trim: test_ai_router_coverage.py (9 paths), test_routers_ai.py (9), test_ai_router_nightly.py (5),
test_authz_app_routers_ai.py (2); test_authz_hardening.py + test_activity_authz_idor.py
prospect-contact probes — B0 disposition (§E.3/E.4), delete those tests here.

### B6 — app/routers/crm/offers.py (9 routes, 723 LOC)

| Method | Route | Line | LOC |
|---|---|---|---|
| GET | /api/requisitions/{req_id}/offers | 114 | 230 |
| POST | /api/requisitions/{req_id}/offers | 346 | 259 |
| PUT | /api/offers/{offer_id}/reconfirm | 667 | 19 |
| PATCH | /api/offers/{offer_id}/mark-sold | 749 | 27 |
| GET | /api/changelog/{entity_type}/{entity_id} | 778 | 46 |
| POST | /api/offers/{offer_id}/attachments/onedrive | 873 | 35 |
| GET | /api/onedrive/browse | 931 | 34 |
| GET | /api/offers/review-queue | 970 | 37 |
| POST | /api/offers/{offer_id}/promote | 1009 | 36 |

KEEP in file: PUT /api/offers/{offer_id}/approve (688) + POST .../reject (1047) — live via
offer_card.html/review_queue.html; GET/POST /api/offers/{offer_id}/attachments (dynamic map
_attachments.html:31). The reconfirm/offer_service consolidation is Wave 3 — only the orphan
route dies now, semantics work stays out.
Whole-file delete: tests/test_integration_crm.py (281), tests/test_offers_perf4.py (191).
Trim: test_routers_crm.py (offers ×2, reconfirm, attachments/onedrive, onedrive/browse);
test_offers_nightly.py + test_offers_overhaul.py (4 each); test_offer_activity_logging.py (4);
test_vendor_unavailability.py (offers ×2 + promote — its clone test trims in B9);
test_authz_app_routers_crm_offers.py (3); test_authz_offers_mark_sold.py + test_load_test_fixes.py
(mark-sold); test_access_control.py + test_offers_idor.py — B0 already re-pointed (§E.5/E.6),
delete their promote/changelog/review-queue tests here.

### B7 — app/routers/crm/quotes.py (8 routes, 405 LOC)

| Method | Route | Line | LOC |
|---|---|---|---|
| GET | /api/requisitions/{req_id}/quote | 77 | 19 |
| GET | /api/quotes/recent-terms | 98 | 28 |
| GET | /api/requisitions/{req_id}/quotes | 128 | 32 |
| POST | /api/requisitions/{req_id}/quote | 162 | 164 |
| POST | /api/quotes/{quote_id}/result | 470 | 64 |
| POST | /api/quotes/{quote_id}/revise | 536 | 11 |
| POST | /api/quotes/{quote_id}/reopen | 549 | 29 |
| GET | /api/pricing-history/{mpn} | 583 | 58 |

5 KEEP-AMBIGUOUS quote routes remain in this file (§F) — untouched.
Whole-file delete: tests/test_quotes_material_card.py (510 LOC).
Trim: test_authz_app_routers_crm_quotes.py (5 paths), test_routers_crm.py (8),
test_part_level_endpoints.py (quote ×2, quotes), test_load_test_fixes.py (quote ×2, result),
test_crm_perf_wave2b.py (quotes); test_authz_hardening.py quote probes — B0 re-pointed
(§E.3), delete the old quote tests here.

### B8 — app/routers/materials.py (5 routes, 215 LOC)

| Method | Route | Line | LOC |
|---|---|---|---|
| GET | /api/materials/by-mpn/{mpn} | 384 | 9 |
| POST | /api/materials/{card_id}/enrich | 471 | 95 |
| POST | /api/materials/{card_id}/restore | 594 | 21 |
| POST | /api/materials/merge | 618 | 21 |
| POST | /api/materials/import-part-numbers | 657 | 69 |

KEEP in file: GET/PUT /api/materials/{card_id} (reachable; e2e uses them),
POST /api/materials/add (add_modal.html:19); ambiguous GET /api/materials, quick-search,
import-stock stay (§F).
Whole-file delete: none here — e2e/api.spec.ts was mis-classed whole-file-deletable; it is
re-pointed in B0 (§E.8) because it also covers the surviving GET/PUT /api/materials/{card_id}.
Trim: test_materials_router_coverage.py (5 paths), test_materials_coverage.py +
test_materials_router.py (4 each), test_routers_materials.py (3),
test_on_add_enrichment.py + test_part_number_import.py (import-part-numbers).

### B9 — requisitions/core.py + requisitions/attachments.py + crm/clone.py (8 routes, 245 LOC)

core.py (6, 113 LOC): GET /api/requisitions/counts (65/19),
GET /api/requisitions/{req_id}/sourcing-score (503/13), PUT /api/requisitions/{req_id}/outcome (543/20),
POST /api/requisitions/{req_id}/dismiss-new-offers (610/12), POST /api/requisitions/{req_id}/claim (656/26),
DELETE /api/requisitions/{req_id}/claim (684/23).
attachments.py (1, 44 LOC): POST /api/requisitions/{req_id}/attachments/onedrive (68/44).
clone.py (1, 88 LOC): POST /api/requisitions/{req_id}/clone (22/88) — **whole file** (its only
route; UI clone goes through /v2/partials/.../action/clone, req_row.html:151). Also remove the
two clone lines in app/routers/crm/__init__.py (import at :22, include_router at :36) and its
docstring row (:8). First batch that edits router wiring → boot check is the gate here.
Whole-file delete: tests/test_authz_app_routers_requisitions_core.py (87),
tests/test_routers_crm_clone.py (83).
Trim: test_requisitions_core_coverage.py (6 paths), test_routers_requisitions.py (counts,
sourcing-score, dismiss-new-offers, clone — its requirements-side tests trim in B11),
test_requisition_cache.py (dismiss-new-offers), test_requirement_entry_fixes.py (sourcing-score;
rest in B11), the 5 attachment files (test_attachments.py, test_attachments_coverage2.py,
test_attachments_router_coverage.py, test_coverage_boost_attachments.py,
test_routers_attachments.py — each loses only its `/attachments/onedrive` tests),
test_authz_app_routers_crm_clone.py, test_routers_crm.py + test_vendor_unavailability.py (clone).

### B10 — POST /api/requisitions — spec-named special case (§G, own commit)

### B11 — app/routers/requisitions/requirements.py (17 routes, 1,082 LOC)

| Method | Route | Line | LOC |
|---|---|---|---|
| GET | /api/requisitions/{req_id}/requirements | 256 | 120 |
| POST | /api/requisitions/{req_id}/requirements | 378 | 123 |
| POST | /api/requisitions/{req_id}/upload | 503 | 110 |
| GET | /api/requisitions/{req_id}/sightings | 732 | 138 |
| GET | /api/requisitions/{req_id}/leads | 872 | 13 |
| GET | /api/leads/queue | 887 | 19 |
| GET | /api/leads/{lead_id} | 908 | 21 |
| PATCH | /api/leads/{lead_id}/status | 931 | 39 |
| POST | /api/leads/{lead_id}/feedback | 972 | 26 |
| PUT | /api/sightings/{sighting_id}/unavailable | 1001 | 16 |
| POST | /api/requisitions/{req_id}/import-stock | 1020 | 105 |
| GET | /api/requirements/{requirement_id}/sightings | 1131 | 62 |
| GET | /api/requirements/{requirement_id}/offers | 1195 | 92 |
| POST | /api/offers/{offer_id}/toggle-quote-selection | 1289 | 17 |
| GET | /api/requirements/{requirement_id}/notes | 1308 | 30 |
| POST | /api/requirements/{requirement_id}/notes | 1340 | 18 |
| GET | /api/requirements/{requirement_id}/history | 1360 | 133 |

Drift-gate adjacency (why late): the 5 leads routes are the API face of the Sourcing Leads
workspace (spec §8 delete). Route handlers go now; Lead model/tables STAY (never drop tables;
model removal only via grandfather path, separate commit). Same for sightings/notes/history
models — heavily shared, untouched.
Whole-file delete: tests/test_requirements.py (729), tests/test_requirements_router_coverage.py
(791), tests/test_requirements_async_coverage.py (178), tests/test_api_manufacturer_validation.py (71).
Trim: test_requirements_router_coverage2.py (9 paths), test_coverage_boost_requirements.py (8),
test_part_level_endpoints.py (requirement sightings/offers/notes ×2),
test_routers_requisitions.py (requirements ×2, upload, sightings, sightings-unavailable,
import-stock), test_integration_requisitions.py (3), test_datasheet_triggers.py +
test_description_service.py + test_mpn_uppercase.py (requirements ×2 each),
test_requirement_entry_fixes.py (requirements ×2), test_phase2_orphans.py (leads ×2),
test_authz_app_routers_requisitions_requirements.py (notes ×2);
test_authz_requisition_read_idor.py + test_security_headers.py — B0 re-pointed (§E.1/E.2),
delete their old orphan-path tests here.

### B12 — app/routers/sources.py (9 routes, 335 LOC)

| Method | Route | Line | LOC |
|---|---|---|---|
| PUT | /api/sources/{source_id}/toggle | 383 | 24 |
| GET | /api/sources/health-summary | 429 | 27 |
| GET | /api/system/alerts | 458 | 32 |
| POST | /api/email-mining/scan | 547 | 57 |
| GET | /api/email-mining/status | 606 | 10 |
| POST | /api/email-mining/scan-outbound | 618 | 46 |
| POST | /api/email-mining/compute-engagement | 666 | 15 |
| GET | /api/vendors/{vendor_id}/engagement | 683 | 38 |
| POST | /api/email-mining/parse-response-attachments/{response_id} | 723 | 86 |

Park-adjacency (why late): email_mining is PARKED into the Data Capture Initiative (spec §6),
not deleted — these orphan ROUTES go (the dashboard's API face, and the dashboard itself is
cut per §5.4), but the underlying mining services/flags the parked pipeline owns must NOT be
deleted from this commit even if imports go dead in sources.py. /api/system/alerts borders
health/alerting plumbing — verify /health and admin health pages don't regress.
KEEP in file: POST /api/sources/{source_id}/test (_connector_macros.html:215); ambiguous
GET /api/sources stays (§F).
Whole-file delete: none.
Trim: test_routers_sources.py (8 paths), test_sources_comprehensive.py (7),
test_data_sources.py (toggle — its credentials tests trim in B13), test_api_health.py
(system/alerts — its dashboard test trims in B13), test_routers_vendors_crud.py (engagement);
test_authz_hardening.py parse-response-attachments probes — B0 disposition (§E.3), delete here.

### B13 — app/routers/admin/system.py (7 routes, 249 LOC) — LAST

| Method | Route | Line | LOC |
|---|---|---|---|
| GET | /api/admin/health | 182 | 8 |
| GET | /api/admin/api-health/dashboard | 226 | 62 |
| GET | /api/admin/sources/{source_id}/credentials | 293 | 39 |
| PUT | /api/admin/sources/{source_id}/credentials | 334 | 34 |
| DELETE | /api/admin/sources/{source_id}/credentials/{var_name} | 370 | 27 |
| GET | /api/admin/material-audit | 415 | 50 |
| GET | /api/admin/subscription-health | 470 | 29 |

Why last: highest drift-gate/fresh-boot adjacency — credential storage, webhook-subscription
health, integrity/audit surfaces; the temptation to chase now-unused models/services here is
exactly what the drift gate punishes (grandfather, don't drop). 4 KEEP-AMBIGUOUS admin routes
remain in file (§F). /api/admin/health dies while /health (the real probe, used by the backup
freshness check) is untouched — verify /health still 200 on the simp instance after deploy.
Whole-file delete: tests/test_credential_management.py (355), tests/test_subscription_health.py (627).
Trim: test_routers_admin.py (5 paths), test_data_sources.py (credentials ×3),
test_api_health.py (api-health/dashboard), test_api_versioning.py +
test_webhook_security_integration.py (/api/admin/health → re-point these two version/webhook
checks at `/health` or `/api/trouble-tickets/{id}`, same shape).
Final gate after B13 = the full Wave-2 acceptance run (rule 6).

## B. Whole-file test deletions by batch (15 files, 4,604 LOC)

| File | LOC | Batch |
|---|---|---|
| tests/test_enrichment_authz.py | 173 | B1 |
| tests/test_authz_crm_companies_idor.py | 97 | B3 |
| tests/test_unmatched_activities.py | 383 | B4 |
| tests/test_integration_crm.py | 281 | B6 |
| tests/test_offers_perf4.py | 191 | B6 |
| tests/test_quotes_material_card.py | 510 | B7 |
| tests/test_authz_app_routers_requisitions_core.py | 87 | B9 |
| tests/test_routers_crm_clone.py | 83 | B9 |
| tests/test_requirements.py | 729 | B11 |
| tests/test_requirements_router_coverage.py | 791 | B11 |
| tests/test_requirements_async_coverage.py | 178 | B11 |
| tests/test_api_manufacturer_validation.py | 71 | B11 |
| tests/test_credential_management.py | 355 | B13 |
| tests/test_subscription_health.py | 627 | B13 |
| e2e/api.spec.ts | 48 | **B0 — RE-POINT, not delete** (evidence-doc correction: it also exercises surviving GET/PUT /api/materials/{card_id}; §E.8) |

## C. Mixed test files — per-file trim map (65 files)

Batch tag = the commit(s) in which that file gets trimmed (multi-batch files are trimmed
incrementally, each batch removing only its own routes' tests). Locate the test functions with
`rg -n '<fragment>' tests/<file>`.

- **tests/test_access_control.py** (B6) **[CROSS-CUTTING — B0 re-point first, then trim]** — trim tests hitting: `POST /api/offers/{offer_id}/promote`
- **tests/test_activity_authz_idor.py** (B4, B5) **[CROSS-CUTTING — B0 re-point first, then trim]** — trim tests hitting: `GET /api/companies/{company_id}/activities`; `POST /api/companies/{company_id}/activities/call`; `POST /api/companies/{company_id}/activities/note`; `GET /api/ai/prospect-contacts`; `DELETE /api/ai/prospect-contacts/{contact_id}`; `POST /api/ai/prospect-contacts/{contact_id}/promote`
- **tests/test_activity_router_coverage2.py** (B4) — trim tests hitting: `POST /api/calls/initiate`; `GET /api/companies/{company_id}/activities`; `POST /api/companies/{company_id}/activities/call`; `POST /api/companies/{company_id}/activities/note`; `GET /api/users/{target_user_id}/activities`; `POST /api/activities/email`; `POST /api/activities/call`; `POST /api/vendors/{vendor_id}/activities/call`; `POST /api/vendors/{vendor_id}/activities/note`; `GET /api/activities/unmatched`; `POST /api/activities/{activity_id}/attribute`; `POST /api/activities/{activity_id}/dismiss`; `GET /api/vendors/{vendor_id}/activity-status`; `GET /api/companies/{company_id}/activity-status`
- **tests/test_ai_router_coverage.py** (B5) — trim tests hitting: `POST /api/ai/find-contacts`; `GET /api/ai/prospect-contacts`; `POST /api/ai/prospect-contacts/{contact_id}/save`; `DELETE /api/ai/prospect-contacts/{contact_id}`; `POST /api/ai/prospect-contacts/{contact_id}/promote`; `POST /api/ai/save-parsed-offers`; `GET /api/ai/company-intel`; `POST /api/ai/intake-parse`; `POST /api/ai/save-freeform-offers`
- **tests/test_ai_router_nightly.py** (B5) — trim tests hitting: `POST /api/ai/generate-description/{requirement_id}`; `POST /api/ai/parse-response/{response_id}`; `POST /api/ai/save-parsed-offers`; `POST /api/ai/intake-parse`; `POST /api/ai/save-freeform-offers`
- **tests/test_api_health.py** (B12, B13) — trim tests hitting: `GET /api/system/alerts`; `GET /api/admin/api-health/dashboard`
- **tests/test_api_versioning.py** (B13) — trim tests hitting: `GET /api/admin/health`
- **tests/test_attachments.py** (B9) — trim tests hitting: `POST /api/requisitions/{req_id}/attachments/onedrive`
- **tests/test_attachments_coverage2.py** (B9) — trim tests hitting: `POST /api/requisitions/{req_id}/attachments/onedrive`
- **tests/test_attachments_router_coverage.py** (B9) — trim tests hitting: `POST /api/requisitions/{req_id}/attachments/onedrive`
- **tests/test_authz_app_routers_ai.py** (B5) — trim tests hitting: `POST /api/ai/generate-description/{requirement_id}`; `POST /api/ai/parse-response/{response_id}`
- **tests/test_authz_app_routers_crm_clone.py** (B9) — trim tests hitting: `POST /api/requisitions/{req_id}/clone`
- **tests/test_authz_app_routers_crm_offers.py** (B6) — trim tests hitting: `PUT /api/offers/{offer_id}/reconfirm`; `POST /api/offers/{offer_id}/attachments/onedrive`; `POST /api/offers/{offer_id}/promote`
- **tests/test_authz_app_routers_crm_quotes.py** (B7) — trim tests hitting: `GET /api/requisitions/{req_id}/quote`; `POST /api/requisitions/{req_id}/quote`; `POST /api/quotes/{quote_id}/result`; `POST /api/quotes/{quote_id}/revise`; `POST /api/quotes/{quote_id}/reopen`
- **tests/test_authz_app_routers_requisitions_requirements.py** (B11) — trim tests hitting: `GET /api/requirements/{requirement_id}/notes`; `POST /api/requirements/{requirement_id}/notes`
- **tests/test_authz_hardening.py** (B5, B7, B12) **[CROSS-CUTTING — B0 re-point first, then trim]** — trim tests hitting: `GET /api/ai/prospect-contacts`; `POST /api/ai/prospect-contacts/{contact_id}/save`; `DELETE /api/ai/prospect-contacts/{contact_id}`; `POST /api/email-mining/parse-response-attachments/{response_id}`; `GET /api/requisitions/{req_id}/quote`; `POST /api/requisitions/{req_id}/quote`
- **tests/test_authz_merge_addsite_idor.py** (B1) **[CROSS-CUTTING — B0 re-point first, then trim]** — trim tests hitting: `GET /api/suggested-contacts`; `POST /api/suggested-contacts/add-to-site`
- **tests/test_authz_offers_mark_sold.py** (B6) — trim tests hitting: `PATCH /api/offers/{offer_id}/mark-sold`
- **tests/test_authz_requisition_read_idor.py** (B11) **[CROSS-CUTTING — B0 re-point first, then trim]** — trim tests hitting: `GET /api/requirements/{requirement_id}/offers`; `GET /api/requirements/{requirement_id}/notes`; `POST /api/requirements/{requirement_id}/notes`; `GET /api/requirements/{requirement_id}/history`
- **tests/test_contact_intelligence_service.py** (B3) — trim tests hitting: `GET /api/vendors/{card_id}/contacts/{contact_id}/timeline`; `GET /api/vendors/{card_id}/contacts/{contact_id}/summary`
- **tests/test_coverage_boost_attachments.py** (B9) — trim tests hitting: `POST /api/requisitions/{req_id}/attachments/onedrive`
- **tests/test_coverage_boost_requirements.py** (B11) — trim tests hitting: `GET /api/requisitions/{req_id}/requirements`; `POST /api/requisitions/{req_id}/requirements`; `GET /api/requisitions/{req_id}/sightings`; `GET /api/requisitions/{req_id}/leads`; `GET /api/leads/{lead_id}`; `PATCH /api/leads/{lead_id}/status`; `GET /api/requirements/{requirement_id}/sightings`; `GET /api/requirements/{requirement_id}/offers`
- **tests/test_crm_perf_wave2b.py** (B7) — trim tests hitting: `GET /api/requisitions/{req_id}/quotes`
- **tests/test_data_sources.py** (B12, B13) — trim tests hitting: `PUT /api/sources/{source_id}/toggle`; `GET /api/admin/sources/{source_id}/credentials`; `PUT /api/admin/sources/{source_id}/credentials`; `DELETE /api/admin/sources/{source_id}/credentials/{var_name}`
- **tests/test_datasheet_triggers.py** (B11) — trim tests hitting: `GET /api/requisitions/{req_id}/requirements`; `POST /api/requisitions/{req_id}/requirements`
- **tests/test_description_service.py** (B11) — trim tests hitting: `GET /api/requisitions/{req_id}/requirements`; `POST /api/requisitions/{req_id}/requirements`
- **tests/test_error_reports.py** (B2) — trim tests hitting: `GET /api/error-reports/{report_id}`; `PATCH /api/error-reports/{report_id}`
- **tests/test_error_reports_coverage.py** (B2) — trim tests hitting: `GET /api/error-reports/{report_id}`; `PATCH /api/error-reports/{report_id}`
- **tests/test_error_reports_coverage3.py** (B2) — trim tests hitting: `GET /api/error-reports/{report_id}`; `PATCH /api/error-reports/{report_id}`
- **tests/test_integration_requisitions.py** (B11) — trim tests hitting: `GET /api/requisitions/{req_id}/requirements`; `POST /api/requisitions/{req_id}/requirements`; `GET /api/requisitions/{req_id}/sightings`
- **tests/test_integrations.py** (B4) — trim tests hitting: `POST /api/calls/initiate`
- **tests/test_load_test_fixes.py** (B6, B7) — trim tests hitting: `PATCH /api/offers/{offer_id}/mark-sold`; `GET /api/requisitions/{req_id}/quote`; `POST /api/requisitions/{req_id}/quote`; `POST /api/quotes/{quote_id}/result`
- **tests/test_materials_coverage.py** (B8) — trim tests hitting: `GET /api/materials/by-mpn/{mpn}`; `POST /api/materials/{card_id}/enrich`; `POST /api/materials/{card_id}/restore`; `POST /api/materials/merge`
- **tests/test_materials_router.py** (B8) — trim tests hitting: `GET /api/materials/by-mpn/{mpn}`; `POST /api/materials/{card_id}/enrich`; `POST /api/materials/{card_id}/restore`; `POST /api/materials/merge`
- **tests/test_materials_router_coverage.py** (B8) — trim tests hitting: `GET /api/materials/by-mpn/{mpn}`; `POST /api/materials/{card_id}/enrich`; `POST /api/materials/{card_id}/restore`; `POST /api/materials/merge`; `POST /api/materials/import-part-numbers`
- **tests/test_mpn_uppercase.py** (B11) — trim tests hitting: `GET /api/requisitions/{req_id}/requirements`; `POST /api/requisitions/{req_id}/requirements`
- **tests/test_offer_activity_logging.py** (B6) — trim tests hitting: `GET /api/requisitions/{req_id}/offers`; `POST /api/requisitions/{req_id}/offers`; `PATCH /api/offers/{offer_id}/mark-sold`; `POST /api/offers/{offer_id}/promote`
- **tests/test_offers_idor.py** (B6) **[CROSS-CUTTING — B0 re-point first, then trim]** — trim tests hitting: `GET /api/changelog/{entity_type}/{entity_id}`; `GET /api/offers/review-queue`
- **tests/test_offers_nightly.py** (B6) — trim tests hitting: `GET /api/requisitions/{req_id}/offers`; `POST /api/requisitions/{req_id}/offers`; `GET /api/offers/review-queue`; `POST /api/offers/{offer_id}/promote`
- **tests/test_offers_overhaul.py** (B6) — trim tests hitting: `GET /api/requisitions/{req_id}/offers`; `POST /api/requisitions/{req_id}/offers`; `GET /api/changelog/{entity_type}/{entity_id}`; `POST /api/offers/{offer_id}/promote`
- **tests/test_on_add_enrichment.py** (B8) — trim tests hitting: `POST /api/materials/import-part-numbers`
- **tests/test_part_level_endpoints.py** (B7, B11) — trim tests hitting: `GET /api/requirements/{requirement_id}/sightings`; `GET /api/requirements/{requirement_id}/offers`; `GET /api/requirements/{requirement_id}/notes`; `POST /api/requirements/{requirement_id}/notes`; `GET /api/requisitions/{req_id}/quote`; `GET /api/requisitions/{req_id}/quotes`; `POST /api/requisitions/{req_id}/quote`
- **tests/test_part_number_import.py** (B8) — trim tests hitting: `POST /api/materials/import-part-numbers`
- **tests/test_phase2_orphans.py** (B3, B11) — trim tests hitting: `GET /api/leads/{lead_id}`; `POST /api/leads/{lead_id}/feedback`; `GET /api/vendors/{card_id}/contacts/{contact_id}/summary`
- **tests/test_requirement_entry_fixes.py** (B9, B11) — trim tests hitting: `GET /api/requisitions/{req_id}/requirements`; `POST /api/requisitions/{req_id}/requirements`; `GET /api/requisitions/{req_id}/sourcing-score`
- **tests/test_requirements_router_coverage2.py** (B11) — trim tests hitting: `GET /api/requisitions/{req_id}/requirements`; `POST /api/requisitions/{req_id}/requirements`; `POST /api/requisitions/{req_id}/upload`; `GET /api/requisitions/{req_id}/sightings`; `GET /api/requisitions/{req_id}/leads`; `GET /api/leads/{lead_id}`; `POST /api/leads/{lead_id}/feedback`; `POST /api/requisitions/{req_id}/import-stock`; `GET /api/requirements/{requirement_id}/sightings`
- **tests/test_requisition_cache.py** (B9) — trim tests hitting: `POST /api/requisitions/{req_id}/dismiss-new-offers`
- **tests/test_requisitions_core_coverage.py** (B9) — trim tests hitting: `GET /api/requisitions/counts`; `GET /api/requisitions/{req_id}/sourcing-score`; `PUT /api/requisitions/{req_id}/outcome`; `POST /api/requisitions/{req_id}/dismiss-new-offers`; `POST /api/requisitions/{req_id}/claim`; `DELETE /api/requisitions/{req_id}/claim`
- **tests/test_routers_admin.py** (B13) — trim tests hitting: `GET /api/admin/health`; `GET /api/admin/sources/{source_id}/credentials`; `PUT /api/admin/sources/{source_id}/credentials`; `DELETE /api/admin/sources/{source_id}/credentials/{var_name}`; `GET /api/admin/material-audit`
- **tests/test_routers_ai.py** (B5) — trim tests hitting: `POST /api/ai/find-contacts`; `GET /api/ai/prospect-contacts`; `POST /api/ai/prospect-contacts/{contact_id}/save`; `DELETE /api/ai/prospect-contacts/{contact_id}`; `POST /api/ai/prospect-contacts/{contact_id}/promote`; `POST /api/ai/parse-response/{response_id}`; `POST /api/ai/save-parsed-offers`; `GET /api/ai/company-intel`; `POST /api/ai/save-freeform-offers`
- **tests/test_routers_attachments.py** (B9) — trim tests hitting: `POST /api/requisitions/{req_id}/attachments/onedrive`
- **tests/test_routers_crm.py** (B1, B3, B6, B7, B9) — trim tests hitting: `GET /api/requisitions/{req_id}/offers`; `POST /api/requisitions/{req_id}/offers`; `PUT /api/offers/{offer_id}/reconfirm`; `POST /api/offers/{offer_id}/attachments/onedrive`; `GET /api/onedrive/browse`; `GET /api/requisitions/{req_id}/quote`; `GET /api/quotes/recent-terms`; `GET /api/requisitions/{req_id}/quotes`; `POST /api/requisitions/{req_id}/quote`; `POST /api/quotes/{quote_id}/result`; `POST /api/quotes/{quote_id}/revise`; `POST /api/quotes/{quote_id}/reopen`; `GET /api/pricing-history/{mpn}`; `POST /api/enrich/vendor/{card_id}`; `GET /api/suggested-contacts`; `POST /api/suggested-contacts/add-to-vendor`; `POST /api/suggested-contacts/add-to-site`; `GET /api/users/list`; `POST /api/customers/import`; `GET /api/companies/check-duplicate`; `POST /api/companies/{company_id}/summarize`; `POST /api/companies/{company_id}/analyze-tags`; `POST /api/requisitions/{req_id}/clone`
- **tests/test_routers_error_reports.py** (B2) — trim tests hitting: `GET /api/error-reports/{report_id}`; `PATCH /api/error-reports/{report_id}`
- **tests/test_routers_materials.py** (B8) — trim tests hitting: `GET /api/materials/by-mpn/{mpn}`; `POST /api/materials/{card_id}/enrich`; `POST /api/materials/merge`
- **tests/test_routers_requisitions.py** (B9, B11) — trim tests hitting: `GET /api/requisitions/{req_id}/requirements`; `POST /api/requisitions/{req_id}/requirements`; `POST /api/requisitions/{req_id}/upload`; `GET /api/requisitions/{req_id}/sightings`; `PUT /api/sightings/{sighting_id}/unavailable`; `POST /api/requisitions/{req_id}/import-stock`; `GET /api/requisitions/counts`; `GET /api/requisitions/{req_id}/sourcing-score`; `POST /api/requisitions/{req_id}/dismiss-new-offers`; `POST /api/requisitions/{req_id}/clone`
- **tests/test_routers_sources.py** (B12) — trim tests hitting: `PUT /api/sources/{source_id}/toggle`; `GET /api/sources/health-summary`; `POST /api/email-mining/scan`; `GET /api/email-mining/status`; `POST /api/email-mining/scan-outbound`; `POST /api/email-mining/compute-engagement`; `GET /api/vendors/{vendor_id}/engagement`; `POST /api/email-mining/parse-response-attachments/{response_id}`
- **tests/test_routers_v13.py** (B4) — trim tests hitting: `GET /api/companies/{company_id}/activities`; `POST /api/companies/{company_id}/activities/call`; `POST /api/companies/{company_id}/activities/note`; `GET /api/users/{target_user_id}/activities`; `POST /api/activities/email`; `POST /api/activities/call`; `POST /api/vendors/{vendor_id}/activities/call`; `POST /api/vendors/{vendor_id}/activities/note`; `GET /api/activities/unmatched`; `POST /api/activities/{activity_id}/attribute`; `POST /api/activities/{activity_id}/dismiss`; `GET /api/vendors/{vendor_id}/activity-status`; `GET /api/companies/{company_id}/activity-status`
- **tests/test_routers_vendor_contacts.py** (B3) — trim tests hitting: `POST /api/vendor-contact`; `GET /api/vendors/{card_id}/email-metrics`; `POST /api/vendor-card/add-email`
- **tests/test_routers_vendors_crud.py** (B3, B12) — trim tests hitting: `GET /api/vendors/{vendor_id}/engagement`; `POST /api/vendors/{card_id}/blacklist`; `POST /api/vendors/{card_id}/reviews`; `DELETE /api/vendors/{card_id}/reviews/{review_id}`
- **tests/test_security_headers.py** (B11) **[CROSS-CUTTING — B0 re-point first, then trim]** — trim tests hitting: `GET /api/requisitions/{req_id}/requirements`; `POST /api/requisitions/{req_id}/requirements`
- **tests/test_sources_comprehensive.py** (B12) — trim tests hitting: `PUT /api/sources/{source_id}/toggle`; `GET /api/sources/health-summary`; `GET /api/system/alerts`; `POST /api/email-mining/scan`; `GET /api/email-mining/status`; `POST /api/email-mining/scan-outbound`; `POST /api/email-mining/parse-response-attachments/{response_id}`
- **tests/test_v13_activities.py** (B4) — trim tests hitting: `GET /api/companies/{company_id}/activities`; `GET /api/users/{target_user_id}/activities`; `POST /api/activities/call`; `GET /api/companies/{company_id}/activity-status`
- **tests/test_v13_activity_ownership.py** (B4) — trim tests hitting: `GET /api/companies/{company_id}/activities`; `GET /api/users/{target_user_id}/activities`; `POST /api/activities/call`; `GET /api/companies/{company_id}/activity-status`
- **tests/test_vendor_unavailability.py** (B6, B9) — trim tests hitting: `GET /api/requisitions/{req_id}/offers`; `POST /api/requisitions/{req_id}/offers`; `POST /api/offers/{offer_id}/promote`; `POST /api/requisitions/{req_id}/clone`
- **tests/test_webhook_security_integration.py** (B13) — trim tests hitting: `GET /api/admin/health`

## D. (folded into A/B/C above; kept for numbering parity with the tasking)

## E. Cross-cutting suite re-pointing plan (executed in B0)

Every proposed target was verified reachable this session (template/JS reference or evidence-doc
sanity trace). Principle: the cross-cutting GUARANTEE survives; only its probe route changes.
Guarantees probing a deleted-feature-specific behavior (e.g. prospect-contact ownership) die
with the feature — noted per file.

1. **tests/test_security_headers.py**
   - `test_error_response_format` (structured-404 shape) currently GETs
     `/api/requisitions/999999/requirements` (orphan, dies B11) → re-point at
     `GET /api/trouble-tickets/999999` — surviving path (dual-registered handler
     error_reports.py:443–444; path kept live by tickets/detail.html PATCH fetch), same
     404-JSON contract.
   - `test_security_headers_on_api_endpoint` + `test_json_response_is_not_no_store` use
     `GET /api/requisitions` — KEEP-AMBIGUOUS, not orphan: they keep working through Wave 2.
     To future-proof against the §F re-verify, re-point both at
     `GET /api/trouble-tickets/{id}` with a seeded ErrorReport fixture (200 JSON), or leave
     and accept a follow-up edit if GET /api/requisitions is later cut.
2. **tests/test_authz_requisition_read_idor.py** — requisition-read IDOR guarantee. Probes
   GET /api/requirements/{rid}/offers|notes|history (all die B11) → re-point the
   owner-vs-stranger 200/404 pairs at the surviving requisition-scoped reads:
   `GET /api/requisitions/{req_id}/pdf` (detail_header.html:71) and
   `GET /api/requisitions/{req_id}/tasks/{task_id}/row` (_task_edit_form.html:18).
3. **tests/test_authz_hardening.py** — auth/ownership hardening. Quote probes (lines ~432/462,
   die B7) → `GET /api/requisitions/{req_id}/pdf` for the cross-owner 403/404 shape;
   unauthenticated-401 sweeps → `POST /api/materials/add` (materials/add_modal.html:19) or
   `POST /api/sources/{source_id}/test` (_connector_macros.html:215). The prospect-contact
   (B5) and email-mining parse-response-attachments (B12) probes are feature-specific —
   delete with their routes; no surviving equivalent claims that guarantee.
4. **tests/test_activity_authz_idor.py** — POST /api/activity/call-initiated (ambiguous —
   survives Wave 2) and POST /api/activity/outreach-initiated (KEEP, htmx_app.js:524) probes
   stay as-is. Company-scoped IDOR probes on /api/companies/{id}/activities* (die B4) →
   re-point at `GET /api/enrich/company/{company_id}/status` (enrich_status.html:12 —
   company-scoped read). Prospect-contact promote probes die with B5.
5. **tests/test_offers_idor.py** — offer-scoped IDOR. Keeps its surviving probes
   (GET/POST /api/offers/{id}/attachments — _attachments.html:31). Changelog + review-queue
   probes (die B6): move the owner-vs-stranger changelog assertion onto
   `GET /api/offers/{id}/attachments`; review-queue visibility tests die with the route.
6. **tests/test_access_control.py** — capability/role matrix. Only orphan probe is
   `POST /api/offers/1/promote` (dies B6) → re-point that row of the matrix at
   `PUT /api/offers/1/approve` (crm/offers.py:688, surviving; the file already exercises it
   at lines ~711–743 with .../reject at :1047). Admin /access + /access-panel probes hit KEEP
   routes — untouched.
7. **tests/test_authz_merge_addsite_idor.py** — 13 tests; only the add-to-site/suggested-contacts
   subset hits B1 orphans. Merge-side coverage (majority) survives untouched. The add-to-site
   ownership guarantee is feature-specific — delete those tests with B1; no re-point needed.
8. **e2e/api.spec.ts** — DO NOT whole-file delete (correction to evidence doc, which itself
   flags e2e as a caveat). Keep: GET/PUT /api/materials/{card_id} tests (KEEP routes). Replace:
   by-mpn + merge tests (die B8) with a `POST /api/materials/add` smoke. Conditional: bare
   GET /api/materials and GET /api/sources tests ride the §F re-verify — if those routes are
   cut later, swap in `GET /health` + an authenticated `GET /api/trouble-tickets` fixture probe
   so the API-surface e2e project never goes empty.

## F. KEEP-AMBIGUOUS re-verify checklist (35 routes)

Not deleted in B1–B13 (B10 excepted). Verify each during Wave 2; promote to a follow-up
delete commit only on double evidence (static AND runtime negative). Define once:

```bash
V() { rg -n "$1" app e2e --glob '!**/tests/**' --glob '!*.md'; \
      docker logs availai-simp-app-1 2>&1 | grep -cF "$1"; }   # read-only
```

Reading: rg hits must be LIVE references (hx-*/fetch/src — not comments/docstrings, which is
exactly the weak evidence that parked these); log grep counts real traffic incl. the nightly
kernel walk (0 over a multi-day window = strong delete signal; the count is path-substring
based, so eyeball collisions).

| # | Method | Route (handler) | Verification command |
|---|---|---|---|
| 1 | GET | /api/admin/config (admin/system.py:123) | `V 'api/admin/config'` |
| 2 | GET | /api/admin/connector-health (admin/system.py:195) | `V 'api/admin/connector-health'` |
| 3 | GET | /api/admin/integrity (admin/system.py:402) | `V 'api/admin/integrity'` |
| 4 | GET | /api/admin/workers/status (admin/system.py:501) | `V 'api/admin/workers/status'` |
| 5 | POST | /api/ai/parse-email (ai.py:282) | `V 'api/ai/parse-email'` |
| 6 | POST | /api/ai/normalize-parts (ai.py:318) | `V 'api/ai/normalize-parts'` |
| 7 | POST | /api/activity/call-initiated (activity.py:123) | `V 'api/activity/call-initiated'` |
| 8 | GET | /api/companies (crm/companies.py:89) | `rg -n '/api/companies[^/a-zA-Z-]' app e2e --glob '!**/tests/**'; docker logs availai-simp-app-1 2>&1 \| grep -cE 'GET /api/companies[? ]'` |
| 9 | POST | /api/companies (crm/companies.py:341) | same rg as #8; log grep `'POST /api/companies[? ]'` |
| 10 | POST | /api/enrich/company/{company_id} (crm/enrichment.py:131) | `rg -n 'api/enrich/company' app e2e --glob '!**/tests/**' \| rg -v '/status'; docker logs availai-simp-app-1 2>&1 \| grep -c 'POST /api/enrich/company'` |
| 11 | PUT | /api/quotes/{quote_id} (crm/quotes.py:328) | `rg -n 'api/quotes/' app/templates app/static; docker logs availai-simp-app-1 2>&1 \| grep -cE 'PUT /api/quotes/[0-9]+ '` |
| 12 | DELETE | /api/quotes/{quote_id} (crm/quotes.py:354) | as #11 with `DELETE` |
| 13 | POST | /api/quotes/{quote_id}/preview (crm/quotes.py:381) | `V 'quotes/' ` then filter `/preview`; log grep `'/preview'` |
| 14 | GET | /api/quotes/{quote_id}/preflight (crm/quotes.py:402) | `V '/preflight'` |
| 15 | POST | /api/quotes/{quote_id}/send (crm/quotes.py:423) | `rg -n 'api/quotes/.*send\|quotes.*\/send' app/templates app/static; docker logs availai-simp-app-1 2>&1 \| grep -c 'quotes/.*send'` — Wave-3 quote-builder consolidation claims this family; defer the verdict to Wave 3 |
| 16 | POST | /api/trouble-tickets (error_reports.py:399) | `rg -n \"'/api/trouble-tickets'\|\\\"/api/trouble-tickets\\\"\" app/templates app/static; docker logs availai-simp-app-1 2>&1 \| grep -cE 'POST /api/(trouble-tickets\|error-reports) '` (floating reporter posts .../submit — bare create looks legacy) |
| 17 | POST | /api/error-reports (error_reports.py:399, alias of #16) | rides #16 — one handler, delete = both alias decorators |
| 18 | GET | /api/trouble-tickets (error_reports.py:411) | as #16 with `GET` |
| 19 | GET | /api/error-reports (error_reports.py:411, alias of #18) | rides #18 |
| 20 | GET | /api/materials (materials.py:240) | `rg -n '/api/materials[^/a-zA-Z-]' app e2e --glob '!**/tests/**'; docker logs availai-simp-app-1 2>&1 \| grep -cE 'GET /api/materials[? ]'` |
| 21 | POST | /api/quick-search (materials.py:359) | `V 'api/quick-search'` |
| 22 | POST | /api/materials/import-stock (materials.py:731) | `V 'api/materials/import-stock'` |
| 23 | GET | /api/requisitions (requisitions/core.py:86) | `rg -n '/api/requisitions[^/a-zA-Z-]' app e2e --glob '!**/tests/**'; docker logs availai-simp-app-1 2>&1 \| grep -cE 'GET /api/requisitions[? ]'` — also probe target of test_security_headers (§E.1); re-point before any delete |
| 24 | POST | /api/requisitions (requisitions/core.py:518) | **→ §G, deleted in B10 by spec name** |
| 25 | PUT | /api/requisitions/batch-assign (core.py:565) | `V 'api/requisitions/batch-assign'` (UI bulk-assign uses /v2/partials/requisitions/bulk/assign — list.html:228) |
| 26 | GET | /api/sources (sources.py:208) | `rg -n '/api/sources[^/a-zA-Z-]' app e2e --glob '!**/tests/**'; docker logs availai-simp-app-1 2>&1 \| grep -cE 'GET /api/sources[? ]'` |
| 27 | GET | /api/vendors/{vendor_id}/activities (v13 activity.py:369) | `rg -n 'vendors/.+/activities[^/]' app/templates app/static app/services; docker logs availai-simp-app-1 2>&1 \| grep -cE 'GET /api/vendors/[0-9]+/activities '` |
| 28 | GET | /api/vendor-contacts/bulk (vendor_contacts.py:166) | `V 'api/vendor-contacts/bulk'` (template comment says UI uses /v2/partials/vendor-contacts — strong delete candidate) |
| 29 | GET | /api/vendors/{card_id}/contacts (vendor_contacts.py:217) | `rg -n 'api/vendors/.+/contacts' app/templates app/static; docker logs availai-simp-app-1 2>&1 \| grep -cE 'GET /api/vendors/[0-9]+/contacts[? ]'` |
| 30 | POST | /api/vendors/{card_id}/contacts (vendor_contacts.py:364) | as #29 with `POST` |
| 31 | PUT | /api/vendors/{card_id}/contacts/{contact_id} (vendor_contacts.py:421) | as #29 with `PUT .../contacts/[0-9]+ ` (contact_row.html edit uses hx-put /v2/partials — UI-superseded) |
| 32 | DELETE | /api/vendors/{card_id}/contacts/{contact_id} (vendor_contacts.py:472) | as #31 with `DELETE` |
| 33 | GET | /api/vendors/check-duplicate (vendors_crud.py:37) | `V 'api/vendors/check-duplicate'` (create_form.html:30 comment says UI uses the /v2 partial) |
| 34 | POST | /api/vendors (vendors_crud.py:53) | `rg -n '/api/vendors[^/a-zA-Z-]' app e2e --glob '!**/tests/**'; docker logs availai-simp-app-1 2>&1 \| grep -cE 'POST /api/vendors[? ]'` |
| 35 | GET | /api/vendors (vendors_crud.py:96) | as #34 with `GET` |

## G. B10 — POST /api/requisitions (spec-named delete despite keep-ambiguous measurement)

Spec §5.1/§10 names "the legacy JSON create endpoint (divergent DRAFT)" for the Wave-2 sweep;
the measurement parked it KEEP-AMBIGUOUS only because its evidence was prefix-only (the
`/api/requisitions/{{ req.id }}/pdf` link, detail_header.html:71). STATE.md deviation log
2026-08-04 W2-prep already records: it stays on the W2 delete list. Own commit, extra checks:

1. **Unified req form no longer calls it** (the named pre-condition):
   `rg -n 'hx-post|action=' app/templates/htmx/partials/requisitions/unified_modal.html`
   → must show only `/v2/partials/requisitions/import-save` (verified today: it does, line 395).
2. **No live POSTer anywhere**: `rg -n '/api/requisitions[^/a-zA-Z-]' app/templates app/static`
   → zero live hits (verified today: zero), plus
   `docker logs availai-simp-app-1 2>&1 | grep -cE 'POST /api/requisitions[? ]'` → expect 0.
3. **Method-surgical delete**: remove ONLY the POST handler at requisitions/core.py:518
   (span ends before `PUT .../outcome`, deleted earlier in B9 at :543 — after B9 renumbering,
   re-locate by decorator string, not line). `GET /api/requisitions` (core.py:86) is NOT
   deleted — it stays on the §F checklist (#23) and test_security_headers still GETs it.
4. **Divergent-DRAFT note for the commit message**: this endpoint creates reqs with divergent
   DRAFT semantics vs the /v2 unified flow — cite spec §5.1.
5. Tests: not in the orphan tables, so find its pins directly:
   `rg -ln 'post\("/api/requisitions"|post\(f?"/api/requisitions"[^/]' tests/` and trim those
   functions in the same commit.
6. Gate: boot + trimmed files green + `POST /api/requisitions` returns 404/405 on the simp
   instance after deploy; unified-modal create still works in the kernel walk.

## H. Caveats carried from the evidence doc

- Path-level, not method-level: extra dead methods may hide inside KEEP paths — out of Wave-2
  scope (evidence-doc caveat).
- Spec's "~280 pinned test files" did not survive verification: 80 actual (15 whole-file);
  STATE.md deviation log already re-baselined. This manifest plans against the verified 80.
- Line numbers shift as B1–B13 land and as the three build agents edit — always re-locate
  handlers by decorator string at execution time.
