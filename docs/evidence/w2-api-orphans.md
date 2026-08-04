# Wave 2 pre-scoped delete list — orphaned /api routes

Prepared 2026-08-04 against worktree `/root/availai-worktrees/simplification` (read-only analysis; spec §8 / Wave 2).

## Summary

| Metric | Count |
|---|---|
| /api routes total (live route table) | 268 |
| **ORPHAN — delete list** | **107** |
| KEEP-AMBIGUOUS — kept, flagged for Wave-2 re-verify | 35 |
| KEEP — reachable, out of scope | 126 |
| Orphan handler LOC (decorator → end of function) | 4,415 |
| Test files referencing ≥1 orphan route | 80 |
| — of those, referencing ONLY orphan routes and no other app route (delete whole file) | 15 (4,604 LOC) |
| — mixed files (trim orphan-route tests only) | 65 |
| Test files referencing any /api route at all | 166 |
| Orphans with zero pinned tests (fully dead) | 0 |

Spec §8 said "111+ orphaned /api routes and their ~280 pinned test files". Verified actuals: **107 orphans** (plus 35 ambiguous keep-flagged — together 142, bracketing the 111 estimate) and **80 pinned test files**, not ~280. The 280 figure does not survive verification at the /api-route level (166 test files touch any /api route at all).

## Methodology (what counts as a reference)

- Route table dumped from the live FastAPI app (recursing fastapi's lazy `_IncludedRouter` wrapper), not from grep — 807 routes, 268 under `/api`.
- Reference corpora: `app/templates/**` (342 html), `app/static/**` js/css/json incl. built dist bundles, and all non-test `app/**/*.py`.
- Tests corpus (never counts toward reachability): `tests/**` (py+json incl. `query_api_baseline.json`) and `e2e/**/*.ts`.
- Match = full route path with `{param}` segments matched permissively (survives `{{ id }}`, `${id}`, `' + id + '` interpolation).
- Excluded as evidence: the route's own decorator strings (incl. multi-line decorators, via AST spans), python comments/docstrings, Jinja `{# #}` comments — these classify as DOC.
- A match followed by a longer path (`/api/foo` seen only inside `/api/foo/bar`) classifies as PREFIX; DOC/PREFIX-only routes went to KEEP-AMBIGUOUS, never to the delete list.
- Dynamic-construction sweep: every literal `/api/...` string in JS/templates/services was prefix-matched against orphan paths. Catches promoted to KEEP: the 8 attachment DELETE routes (`attachment_service._DELETE_BASE` + `shared/_attachment_list.html:55` builds `{{ delete_base }}/{{ it.id }}`), and `POST /api/trouble-tickets/{diagnose-bulk,bulk-status}` (`htmx_app.js:262` `ticketBulkAction` builds `'/api/trouble-tickets/' + kind`).

## Sanity check — 10 sampled 'reachable' verdicts

Each sampled KEEP was traced to its actual referencing line; the two verdict-method bugs the sample exposed (multi-line-decorator self-match, docstring-as-evidence) were fixed and the whole analysis rerun before producing this list:

1. `POST /api/admin/users/{user_id}/active` — settings/users.html:238 `hx-post` — REAL.
2. `DELETE /api/contact-attachments/{att_id}` — dynamic `_DELETE_BASE` map — REAL (override).
3. `POST /api/resell/{list_id}/import-preview` — resell/_lines.html:84 `hx-post` — REAL.
4. `GET /api/user/avatar/{filename}` — settings/profile.html:149/151 (Jinja + JS concat) — REAL.
5. `POST /api/requisitions/{req_id}/tasks/{task_id}/edit` — _task_edit_form.html:9 — REAL.
6. `POST /api/admin/users/{user_id}/manager` — settings/users.html:115 — REAL.
7. `POST /api/resell/{list_id}/bid/{bid_id}/accept` — resell/_build_bid.html:195 — REAL.
8. `POST /api/materials/add` — materials/add_modal.html:19 `hx-post` — REAL (found via fragment grep after a comment-only first hit).
9. `PATCH /api/trouble-tickets/{report_id}` — tickets/detail.html:33/87 `fetch('/api/trouble-tickets/' + id)` — REAL (dynamic).
10. `GET /api/requisitions/{req_id}/tasks/{task_id}/row` — _task_edit_form.html:18 — REAL.

False-KEEPs the sample flushed out (then fixed globally): `GET /api/requisitions/{req_id}/quote` (evidence was its own multi-line decorator → now ORPHAN); `GET /api/companies`, `GET/DELETE/PUT /api/vendors/{card_id}/contacts...` (evidence was longer sibling paths → now KEEP-AMBIGUOUS).

## Delete list — 107 orphaned /api routes

Grouped by handler file. LOC spans decorator through end of handler. Test files listed are every test referencing that route's path.

### app/routers/requisitions/requirements.py — 17 orphan routes, 1082 LOC

| Method | Route | Line | LOC | Referencing test files |
|---|---|---|---|---|
| GET | `/api/requisitions/{req_id}/requirements` | 256 | 120 | test_api_manufacturer_validation.py, test_coverage_boost_requirements.py, test_datasheet_triggers.py, test_description_service.py, test_integration_requisitions.py, test_mpn_uppercase.py, test_requirement_entry_fixes.py, test_requirements.py, test_requirements_async_coverage.py, test_requirements_router_coverage2.py, test_routers_requisitions.py, test_security_headers.py |
| POST | `/api/requisitions/{req_id}/requirements` | 378 | 123 | test_api_manufacturer_validation.py, test_coverage_boost_requirements.py, test_datasheet_triggers.py, test_description_service.py, test_integration_requisitions.py, test_mpn_uppercase.py, test_requirement_entry_fixes.py, test_requirements.py, test_requirements_async_coverage.py, test_requirements_router_coverage2.py, test_routers_requisitions.py, test_security_headers.py |
| POST | `/api/requisitions/{req_id}/upload` | 503 | 110 | test_requirements_router_coverage.py, test_requirements_router_coverage2.py, test_routers_requisitions.py |
| GET | `/api/requisitions/{req_id}/sightings` | 732 | 138 | test_coverage_boost_requirements.py, test_integration_requisitions.py, test_requirements.py, test_requirements_async_coverage.py, test_requirements_router_coverage.py, test_requirements_router_coverage2.py, test_routers_requisitions.py |
| GET | `/api/requisitions/{req_id}/leads` | 872 | 13 | test_coverage_boost_requirements.py, test_requirements.py, test_requirements_async_coverage.py, test_requirements_router_coverage.py, test_requirements_router_coverage2.py |
| GET | `/api/leads/queue` | 887 | 19 | test_requirements.py, test_requirements_router_coverage.py |
| GET | `/api/leads/{lead_id}` | 908 | 21 | test_coverage_boost_requirements.py, test_phase2_orphans.py, test_requirements.py, test_requirements_router_coverage.py, test_requirements_router_coverage2.py |
| PATCH | `/api/leads/{lead_id}/status` | 931 | 39 | test_coverage_boost_requirements.py, test_requirements.py, test_requirements_router_coverage.py |
| POST | `/api/leads/{lead_id}/feedback` | 972 | 26 | test_phase2_orphans.py, test_requirements.py, test_requirements_router_coverage.py, test_requirements_router_coverage2.py |
| PUT | `/api/sightings/{sighting_id}/unavailable` | 1001 | 16 | test_requirements.py, test_requirements_router_coverage.py, test_routers_requisitions.py |
| POST | `/api/requisitions/{req_id}/import-stock` | 1020 | 105 | test_requirements_async_coverage.py, test_requirements_router_coverage.py, test_requirements_router_coverage2.py, test_routers_requisitions.py |
| GET | `/api/requirements/{requirement_id}/sightings` | 1131 | 62 | test_coverage_boost_requirements.py, test_part_level_endpoints.py, test_requirements.py, test_requirements_router_coverage.py, test_requirements_router_coverage2.py |
| GET | `/api/requirements/{requirement_id}/offers` | 1195 | 92 | test_authz_requisition_read_idor.py, test_coverage_boost_requirements.py, test_part_level_endpoints.py, test_requirements.py, test_requirements_router_coverage.py |
| POST | `/api/offers/{offer_id}/toggle-quote-selection` | 1289 | 17 | test_requirements.py, test_requirements_router_coverage.py |
| GET | `/api/requirements/{requirement_id}/notes` | 1308 | 30 | test_authz_app_routers_requisitions_requirements.py, test_authz_requisition_read_idor.py, test_part_level_endpoints.py, test_requirements.py |
| POST | `/api/requirements/{requirement_id}/notes` | 1340 | 18 | test_authz_app_routers_requisitions_requirements.py, test_authz_requisition_read_idor.py, test_part_level_endpoints.py, test_requirements.py |
| GET | `/api/requirements/{requirement_id}/history` | 1360 | 133 | test_authz_requisition_read_idor.py, test_requirements.py, test_requirements_router_coverage.py |

### app/routers/v13_features/activity.py — 14 orphan routes, 347 LOC

| Method | Route | Line | LOC | Referencing test files |
|---|---|---|---|---|
| POST | `/api/calls/initiate` | 223 | 38 | test_activity_router_coverage2.py, test_integrations.py |
| GET | `/api/companies/{company_id}/activities` | 296 | 13 | test_activity_authz_idor.py, test_activity_router_coverage2.py, test_routers_v13.py, test_v13_activities.py, test_v13_activity_ownership.py |
| POST | `/api/companies/{company_id}/activities/call` | 311 | 29 | test_activity_authz_idor.py, test_activity_router_coverage2.py, test_routers_v13.py |
| POST | `/api/companies/{company_id}/activities/note` | 342 | 25 | test_activity_authz_idor.py, test_activity_router_coverage2.py, test_routers_v13.py |
| GET | `/api/users/{target_user_id}/activities` | 384 | 13 | test_activity_router_coverage2.py, test_routers_v13.py, test_v13_activities.py, test_v13_activity_ownership.py |
| POST | `/api/activities/email` | 399 | 25 | test_activity_router_coverage2.py, test_routers_v13.py |
| POST | `/api/activities/call` | 426 | 25 | test_activity_router_coverage2.py, test_routers_v13.py, test_v13_activities.py, test_v13_activity_ownership.py |
| POST | `/api/vendors/{vendor_id}/activities/call` | 453 | 28 | test_activity_router_coverage2.py, test_routers_v13.py |
| POST | `/api/vendors/{vendor_id}/activities/note` | 483 | 25 | test_activity_router_coverage2.py, test_routers_v13.py |
| GET | `/api/activities/unmatched` | 515 | 21 | test_activity_router_coverage2.py, test_routers_v13.py, test_unmatched_activities.py |
| POST | `/api/activities/{activity_id}/attribute` | 538 | 32 | test_activity_router_coverage2.py, test_routers_v13.py, test_unmatched_activities.py |
| POST | `/api/activities/{activity_id}/dismiss` | 572 | 15 | test_activity_router_coverage2.py, test_routers_v13.py, test_unmatched_activities.py |
| GET | `/api/vendors/{vendor_id}/activity-status` | 589 | 29 | test_activity_router_coverage2.py, test_routers_v13.py |
| GET | `/api/companies/{company_id}/activity-status` | 620 | 29 | test_activity_router_coverage2.py, test_routers_v13.py, test_v13_activities.py, test_v13_activity_ownership.py |

### app/routers/ai.py — 11 orphan routes, 380 LOC

| Method | Route | Line | LOC | Referencing test files |
|---|---|---|---|---|
| POST | `/api/ai/find-contacts` | 93 | 78 | test_ai_router_coverage.py, test_routers_ai.py |
| GET | `/api/ai/prospect-contacts` | 173 | 33 | test_activity_authz_idor.py, test_ai_router_coverage.py, test_authz_hardening.py, test_routers_ai.py |
| POST | `/api/ai/prospect-contacts/{contact_id}/save` | 208 | 29 | test_ai_router_coverage.py, test_authz_hardening.py, test_routers_ai.py |
| DELETE | `/api/ai/prospect-contacts/{contact_id}` | 239 | 14 | test_activity_authz_idor.py, test_ai_router_coverage.py, test_authz_hardening.py, test_routers_ai.py |
| POST | `/api/ai/prospect-contacts/{contact_id}/promote` | 255 | 22 | test_activity_authz_idor.py, test_ai_router_coverage.py, test_routers_ai.py |
| POST | `/api/ai/generate-description/{requirement_id}` | 417 | 34 | test_ai_router_nightly.py, test_authz_app_routers_ai.py |
| POST | `/api/ai/parse-response/{response_id}` | 456 | 73 | test_ai_router_nightly.py, test_authz_app_routers_ai.py, test_routers_ai.py |
| POST | `/api/ai/save-parsed-offers` | 531 | 20 | test_ai_router_coverage.py, test_ai_router_nightly.py, test_routers_ai.py |
| GET | `/api/ai/company-intel` | 556 | 25 | test_ai_router_coverage.py, test_routers_ai.py |
| POST | `/api/ai/intake-parse` | 586 | 34 | test_ai_router_coverage.py, test_ai_router_nightly.py |
| POST | `/api/ai/save-freeform-offers` | 625 | 18 | test_ai_router_coverage.py, test_ai_router_nightly.py, test_routers_ai.py |

### app/routers/crm/offers.py — 9 orphan routes, 723 LOC

| Method | Route | Line | LOC | Referencing test files |
|---|---|---|---|---|
| GET | `/api/requisitions/{req_id}/offers` | 114 | 230 | test_integration_crm.py, test_offer_activity_logging.py, test_offers_nightly.py, test_offers_overhaul.py, test_offers_perf4.py, test_routers_crm.py, test_vendor_unavailability.py |
| POST | `/api/requisitions/{req_id}/offers` | 346 | 259 | test_integration_crm.py, test_offer_activity_logging.py, test_offers_nightly.py, test_offers_overhaul.py, test_offers_perf4.py, test_routers_crm.py, test_vendor_unavailability.py |
| PUT | `/api/offers/{offer_id}/reconfirm` | 667 | 19 | test_authz_app_routers_crm_offers.py, test_routers_crm.py |
| PATCH | `/api/offers/{offer_id}/mark-sold` | 749 | 27 | test_authz_offers_mark_sold.py, test_load_test_fixes.py, test_offer_activity_logging.py |
| GET | `/api/changelog/{entity_type}/{entity_id}` | 778 | 46 | test_offers_idor.py, test_offers_overhaul.py |
| POST | `/api/offers/{offer_id}/attachments/onedrive` | 873 | 35 | test_authz_app_routers_crm_offers.py, test_routers_crm.py |
| GET | `/api/onedrive/browse` | 931 | 34 | test_routers_crm.py |
| GET | `/api/offers/review-queue` | 970 | 37 | test_offers_idor.py, test_offers_nightly.py |
| POST | `/api/offers/{offer_id}/promote` | 1009 | 36 | test_access_control.py, test_authz_app_routers_crm_offers.py, test_offer_activity_logging.py, test_offers_nightly.py, test_offers_overhaul.py, test_vendor_unavailability.py |

### app/routers/sources.py — 9 orphan routes, 335 LOC

| Method | Route | Line | LOC | Referencing test files |
|---|---|---|---|---|
| PUT | `/api/sources/{source_id}/toggle` | 383 | 24 | test_data_sources.py, test_routers_sources.py, test_sources_comprehensive.py |
| GET | `/api/sources/health-summary` | 429 | 27 | test_routers_sources.py, test_sources_comprehensive.py |
| GET | `/api/system/alerts` | 458 | 32 | test_api_health.py, test_sources_comprehensive.py |
| POST | `/api/email-mining/scan` | 547 | 57 | test_routers_sources.py, test_sources_comprehensive.py |
| GET | `/api/email-mining/status` | 606 | 10 | test_routers_sources.py, test_sources_comprehensive.py |
| POST | `/api/email-mining/scan-outbound` | 618 | 46 | test_routers_sources.py, test_sources_comprehensive.py |
| POST | `/api/email-mining/compute-engagement` | 666 | 15 | test_routers_sources.py |
| GET | `/api/vendors/{vendor_id}/engagement` | 683 | 38 | test_routers_sources.py, test_routers_vendors_crud.py |
| POST | `/api/email-mining/parse-response-attachments/{response_id}` | 723 | 86 | test_authz_hardening.py, test_routers_sources.py, test_sources_comprehensive.py |

### app/routers/crm/quotes.py — 8 orphan routes, 405 LOC

| Method | Route | Line | LOC | Referencing test files |
|---|---|---|---|---|
| GET | `/api/requisitions/{req_id}/quote` | 77 | 19 | test_authz_app_routers_crm_quotes.py, test_authz_hardening.py, test_load_test_fixes.py, test_part_level_endpoints.py, test_quotes_material_card.py, test_routers_crm.py |
| GET | `/api/quotes/recent-terms` | 98 | 28 | test_routers_crm.py |
| GET | `/api/requisitions/{req_id}/quotes` | 128 | 32 | test_crm_perf_wave2b.py, test_part_level_endpoints.py, test_routers_crm.py |
| POST | `/api/requisitions/{req_id}/quote` | 162 | 164 | test_authz_app_routers_crm_quotes.py, test_authz_hardening.py, test_load_test_fixes.py, test_part_level_endpoints.py, test_quotes_material_card.py, test_routers_crm.py |
| POST | `/api/quotes/{quote_id}/result` | 470 | 64 | test_authz_app_routers_crm_quotes.py, test_load_test_fixes.py, test_routers_crm.py |
| POST | `/api/quotes/{quote_id}/revise` | 536 | 11 | test_authz_app_routers_crm_quotes.py, test_routers_crm.py |
| POST | `/api/quotes/{quote_id}/reopen` | 549 | 29 | test_authz_app_routers_crm_quotes.py, test_routers_crm.py |
| GET | `/api/pricing-history/{mpn}` | 583 | 58 | test_quotes_material_card.py, test_routers_crm.py |

### app/routers/admin/system.py — 7 orphan routes, 249 LOC

| Method | Route | Line | LOC | Referencing test files |
|---|---|---|---|---|
| GET | `/api/admin/health` | 182 | 8 | test_api_versioning.py, test_routers_admin.py, test_webhook_security_integration.py |
| GET | `/api/admin/api-health/dashboard` | 226 | 62 | test_api_health.py |
| GET | `/api/admin/sources/{source_id}/credentials` | 293 | 39 | test_credential_management.py, test_data_sources.py, test_routers_admin.py |
| PUT | `/api/admin/sources/{source_id}/credentials` | 334 | 34 | test_credential_management.py, test_data_sources.py, test_routers_admin.py |
| DELETE | `/api/admin/sources/{source_id}/credentials/{var_name}` | 370 | 27 | test_credential_management.py, test_data_sources.py, test_routers_admin.py |
| GET | `/api/admin/material-audit` | 415 | 50 | test_routers_admin.py |
| GET | `/api/admin/subscription-health` | 470 | 29 | test_subscription_health.py |

### app/routers/crm/enrichment.py — 6 orphan routes, 13 LOC

| Method | Route | Line | LOC | Referencing test files |
|---|---|---|---|---|
| POST | `/api/enrich/vendor/{card_id}` | 255 | 0 | test_enrichment_authz.py, test_routers_crm.py |
| GET | `/api/suggested-contacts` | 300 | 0 | test_authz_merge_addsite_idor.py, test_enrichment_authz.py, test_routers_crm.py |
| POST | `/api/suggested-contacts/add-to-vendor` | 319 | 0 | test_routers_crm.py |
| POST | `/api/suggested-contacts/add-to-site` | 354 | 0 | test_authz_merge_addsite_idor.py, test_routers_crm.py |
| GET | `/api/users/list` | 440 | 0 | test_routers_crm.py |
| POST | `/api/customers/import` | 458 | 13 | test_routers_crm.py |

### app/routers/requisitions/core.py — 6 orphan routes, 113 LOC

| Method | Route | Line | LOC | Referencing test files |
|---|---|---|---|---|
| GET | `/api/requisitions/counts` | 65 | 19 | test_requisitions_core_coverage.py, test_routers_requisitions.py |
| GET | `/api/requisitions/{req_id}/sourcing-score` | 503 | 13 | test_requirement_entry_fixes.py, test_requisitions_core_coverage.py, test_routers_requisitions.py |
| PUT | `/api/requisitions/{req_id}/outcome` | 543 | 20 | test_authz_app_routers_requisitions_core.py, test_requisitions_core_coverage.py |
| POST | `/api/requisitions/{req_id}/dismiss-new-offers` | 610 | 12 | test_authz_app_routers_requisitions_core.py, test_requisition_cache.py, test_requisitions_core_coverage.py, test_routers_requisitions.py |
| POST | `/api/requisitions/{req_id}/claim` | 656 | 26 | test_authz_app_routers_requisitions_core.py, test_requisitions_core_coverage.py |
| DELETE | `/api/requisitions/{req_id}/claim` | 684 | 23 | test_authz_app_routers_requisitions_core.py, test_requisitions_core_coverage.py |

### app/routers/materials.py — 5 orphan routes, 215 LOC

| Method | Route | Line | LOC | Referencing test files |
|---|---|---|---|---|
| GET | `/api/materials/by-mpn/{mpn}` | 384 | 9 | e2e/api.spec.ts, test_materials_coverage.py, test_materials_router.py, test_materials_router_coverage.py, test_routers_materials.py |
| POST | `/api/materials/{card_id}/enrich` | 471 | 95 | test_materials_coverage.py, test_materials_router.py, test_materials_router_coverage.py, test_routers_materials.py |
| POST | `/api/materials/{card_id}/restore` | 594 | 21 | test_materials_coverage.py, test_materials_router.py, test_materials_router_coverage.py |
| POST | `/api/materials/merge` | 618 | 21 | e2e/api.spec.ts, test_materials_coverage.py, test_materials_router.py, test_materials_router_coverage.py, test_routers_materials.py |
| POST | `/api/materials/import-part-numbers` | 657 | 69 | test_materials_router_coverage.py, test_on_add_enrichment.py, test_part_number_import.py |

### app/routers/vendor_contacts.py — 5 orphan routes, 260 LOC

| Method | Route | Line | LOC | Referencing test files |
|---|---|---|---|---|
| POST | `/api/vendor-contact` | 75 | 86 | test_routers_vendor_contacts.py |
| GET | `/api/vendors/{card_id}/contacts/{contact_id}/timeline` | 253 | 38 | test_contact_intelligence_service.py |
| GET | `/api/vendors/{card_id}/contacts/{contact_id}/summary` | 293 | 12 | test_contact_intelligence_service.py, test_phase2_orphans.py |
| GET | `/api/vendors/{card_id}/email-metrics` | 495 | 61 | test_routers_vendor_contacts.py |
| POST | `/api/vendor-card/add-email` | 561 | 63 | test_routers_vendor_contacts.py |

### app/routers/crm/companies.py — 3 orphan routes, 60 LOC

| Method | Route | Line | LOC | Referencing test files |
|---|---|---|---|---|
| GET | `/api/companies/check-duplicate` | 204 | 17 | test_routers_crm.py |
| POST | `/api/companies/{company_id}/summarize` | 506 | 19 | test_authz_crm_companies_idor.py, test_routers_crm.py |
| POST | `/api/companies/{company_id}/analyze-tags` | 527 | 24 | test_authz_crm_companies_idor.py, test_routers_crm.py |

### app/routers/vendors_crud.py — 3 orphan routes, 49 LOC

| Method | Route | Line | LOC | Referencing test files |
|---|---|---|---|---|
| POST | `/api/vendors/{card_id}/blacklist` | 430 | 14 | test_routers_vendors_crud.py |
| POST | `/api/vendors/{card_id}/reviews` | 462 | 19 | test_routers_vendors_crud.py |
| DELETE | `/api/vendors/{card_id}/reviews/{review_id}` | 483 | 16 | test_routers_vendors_crud.py |

### app/routers/error_reports.py — 2 orphan routes, 52 LOC

| Method | Route | Line | LOC | Referencing test files |
|---|---|---|---|---|
| GET | `/api/error-reports/{report_id}` | 437 | 24 | test_error_reports.py, test_error_reports_coverage.py, test_error_reports_coverage3.py, test_routers_error_reports.py |
| PATCH | `/api/error-reports/{report_id}` | 580 | 28 | test_error_reports.py, test_error_reports_coverage.py, test_error_reports_coverage3.py, test_routers_error_reports.py |

### app/routers/crm/clone.py — 1 orphan routes, 88 LOC

| Method | Route | Line | LOC | Referencing test files |
|---|---|---|---|---|
| POST | `/api/requisitions/{req_id}/clone` | 22 | 88 | test_authz_app_routers_crm_clone.py, test_routers_crm.py, test_routers_crm_clone.py, test_routers_requisitions.py, test_vendor_unavailability.py |

### app/routers/requisitions/attachments.py — 1 orphan routes, 44 LOC

| Method | Route | Line | LOC | Referencing test files |
|---|---|---|---|---|
| POST | `/api/requisitions/{req_id}/attachments/onedrive` | 68 | 44 | test_attachments.py, test_attachments_coverage2.py, test_attachments_router_coverage.py, test_coverage_boost_attachments.py, test_routers_attachments.py |

## Test files

### Delete whole file — every route reference is an orphan /api route, zero /v2//auth//health references (15 files, 4,604 LOC)

| Test file | LOC |
|---|---|
| e2e/api.spec.ts | 48 |
| tests/test_api_manufacturer_validation.py | 71 |
| tests/test_authz_app_routers_requisitions_core.py | 87 |
| tests/test_authz_crm_companies_idor.py | 97 |
| tests/test_credential_management.py | 355 |
| tests/test_enrichment_authz.py | 173 |
| tests/test_integration_crm.py | 281 |
| tests/test_offers_perf4.py | 191 |
| tests/test_quotes_material_card.py | 510 |
| tests/test_requirements.py | 729 |
| tests/test_requirements_async_coverage.py | 178 |
| tests/test_requirements_router_coverage.py | 791 |
| tests/test_routers_crm_clone.py | 83 |
| tests/test_subscription_health.py | 627 |
| tests/test_unmatched_activities.py | 383 |

### Mixed files — trim orphan-route tests only (65 files)

These also cover reachable/ambiguous routes or non-/api surfaces (/v2 partials, auth, health); delete only the test functions that hit orphan paths. Note test_security_headers.py, test_authz_hardening.py, test_authz_*_idor.py sit here — they use orphan routes as probes for cross-cutting guarantees and must be re-pointed at surviving routes, not deleted:

- tests/test_access_control.py
- tests/test_activity_authz_idor.py
- tests/test_activity_router_coverage2.py
- tests/test_ai_router_coverage.py
- tests/test_ai_router_nightly.py
- tests/test_api_health.py
- tests/test_api_versioning.py
- tests/test_attachments.py
- tests/test_attachments_coverage2.py
- tests/test_attachments_router_coverage.py
- tests/test_authz_app_routers_ai.py
- tests/test_authz_app_routers_crm_clone.py
- tests/test_authz_app_routers_crm_offers.py
- tests/test_authz_app_routers_crm_quotes.py
- tests/test_authz_app_routers_requisitions_requirements.py
- tests/test_authz_hardening.py
- tests/test_authz_merge_addsite_idor.py
- tests/test_authz_offers_mark_sold.py
- tests/test_authz_requisition_read_idor.py
- tests/test_contact_intelligence_service.py
- tests/test_coverage_boost_attachments.py
- tests/test_coverage_boost_requirements.py
- tests/test_crm_perf_wave2b.py
- tests/test_data_sources.py
- tests/test_datasheet_triggers.py
- tests/test_description_service.py
- tests/test_error_reports.py
- tests/test_error_reports_coverage.py
- tests/test_error_reports_coverage3.py
- tests/test_integration_requisitions.py
- tests/test_integrations.py
- tests/test_load_test_fixes.py
- tests/test_materials_coverage.py
- tests/test_materials_router.py
- tests/test_materials_router_coverage.py
- tests/test_mpn_uppercase.py
- tests/test_offer_activity_logging.py
- tests/test_offers_idor.py
- tests/test_offers_nightly.py
- tests/test_offers_overhaul.py
- tests/test_on_add_enrichment.py
- tests/test_part_level_endpoints.py
- tests/test_part_number_import.py
- tests/test_phase2_orphans.py
- tests/test_requirement_entry_fixes.py
- tests/test_requirements_router_coverage2.py
- tests/test_requisition_cache.py
- tests/test_requisitions_core_coverage.py
- tests/test_routers_admin.py
- tests/test_routers_ai.py
- tests/test_routers_attachments.py
- tests/test_routers_crm.py
- tests/test_routers_error_reports.py
- tests/test_routers_materials.py
- tests/test_routers_requisitions.py
- tests/test_routers_sources.py
- tests/test_routers_v13.py
- tests/test_routers_vendor_contacts.py
- tests/test_routers_vendors_crud.py
- tests/test_security_headers.py
- tests/test_sources_comprehensive.py
- tests/test_v13_activities.py
- tests/test_v13_activity_ownership.py
- tests/test_vendor_unavailability.py
- tests/test_webhook_security_integration.py

## KEEP-AMBIGUOUS — 35 routes kept on weak evidence (re-verify during Wave 2)

Evidence is a comment/docstring mention or a prefix-only match. Per instructions these stay KEEP, but several comments explicitly say the UI moved to /v2 partials — strong delete candidates after a manual check:

| Method | Route | Handler | Evidence | Note |
|---|---|---|---|---|
| GET | `/api/admin/config` | app/routers/admin/system.py:123 | DOC app/templates/htmx/partials/settings/system.html:7 |  |
| GET | `/api/admin/connector-health` | app/routers/admin/system.py:195 | DOC app/routers/admin/system.py:236 |  |
| GET | `/api/admin/integrity` | app/routers/admin/system.py:402 | DOC app/services/integrity_service.py:239 |  |
| GET | `/api/admin/workers/status` | app/routers/admin/system.py:501 | DOC app/models/tbf_worker_status.py:10 |  |
| POST | `/api/ai/parse-email` | app/routers/ai.py:282 | DOC app/services/ai_email_parser.py:16 |  |
| POST | `/api/ai/normalize-parts` | app/routers/ai.py:318 | DOC app/services/ai_part_normalizer.py:17 |  |
| POST | `/api/activity/call-initiated` | app/routers/activity.py:123 | DOC app/schemas/activity.py:13 |  |
| GET | `/api/companies` | app/routers/crm/companies.py:89 | DOC app/templates/htmx/partials/requisitions/unified_modal.html:49 |  |
| POST | `/api/companies` | app/routers/crm/companies.py:341 | DOC app/templates/htmx/partials/requisitions/unified_modal.html:49 |  |
| POST | `/api/enrich/company/{company_id}` | app/routers/crm/enrichment.py:131 | longer-path match only | only GET .../status is referenced (customers/enrich_status.html:12); POST trigger goes through /v2 partial |
| PUT | `/api/quotes/{quote_id}` | app/routers/crm/quotes.py:328 | DOC app/services/quote_preflight.py:9 |  |
| DELETE | `/api/quotes/{quote_id}` | app/routers/crm/quotes.py:354 | DOC app/services/quote_preflight.py:9 |  |
| POST | `/api/quotes/{quote_id}/preview` | app/routers/crm/quotes.py:381 | DOC app/services/quote_preflight.py:9 |  |
| GET | `/api/quotes/{quote_id}/preflight` | app/routers/crm/quotes.py:402 | DOC app/services/quote_preflight.py:9 |  |
| POST | `/api/quotes/{quote_id}/send` | app/routers/crm/quotes.py:423 | DOC app/services/quote_send.py:6 |  |
| POST | `/api/trouble-tickets` | app/routers/error_reports.py:393 | DOC app/templates/htmx/partials/tickets/_diagnosis.html:5 | floating reporter posts /api/trouble-tickets/submit; bare-collection create looks legacy |
| POST | `/api/error-reports` | app/routers/error_reports.py:393 | DOC app/routers/error_reports.py:3 |  |
| GET | `/api/trouble-tickets` | app/routers/error_reports.py:405 | DOC app/templates/htmx/partials/tickets/_diagnosis.html:5 |  |
| GET | `/api/error-reports` | app/routers/error_reports.py:405 | DOC app/routers/error_reports.py:3 |  |
| GET | `/api/materials` | app/routers/materials.py:240 | DOC app/templates/htmx/partials/materials/workspace.html:327 |  |
| POST | `/api/quick-search` | app/routers/materials.py:359 | DOC app/search_service.py:1078 |  |
| POST | `/api/materials/import-stock` | app/routers/materials.py:731 | DOC app/routers/htmx_views.py:356 |  |
| GET | `/api/requisitions` | app/routers/requisitions/core.py:86 | PREFIX app/templates/htmx/partials/requisitions/detail_header.html:71 |  |
| POST | `/api/requisitions` | app/routers/requisitions/core.py:518 | PREFIX app/templates/htmx/partials/requisitions/detail_header.html:71 | likely the 'legacy JSON create endpoint' named in spec Wave 2; evidence is prefix-only |
| PUT | `/api/requisitions/batch-assign` | app/routers/requisitions/core.py:565 | DOC app/schemas/responses.py:209 |  |
| GET | `/api/sources` | app/routers/sources.py:208 | PREFIX app/templates/htmx/partials/settings/_connector_macros.html:108 |  |
| GET | `/api/vendors/{vendor_id}/activities` | app/routers/v13_features/activity.py:369 | DOC app/services/excess_service.py:500 |  |
| GET | `/api/vendor-contacts/bulk` | app/routers/vendor_contacts.py:166 | DOC app/templates/htmx/partials/vendors/contacts_list.html:3 | template comment explicitly says UI uses /v2/partials/vendor-contacts instead |
| GET | `/api/vendors/{card_id}/contacts` | app/routers/vendor_contacts.py:217 | PREFIX app/templates/htmx/partials/vendors/tabs/contact_row.html:45 |  |
| POST | `/api/vendors/{card_id}/contacts` | app/routers/vendor_contacts.py:364 | PREFIX app/templates/htmx/partials/vendors/tabs/contact_row.html:45 |  |
| PUT | `/api/vendors/{card_id}/contacts/{contact_id}` | app/routers/vendor_contacts.py:421 | longer-path match only | contact_row.html edit form uses hx-put /v2/partials/... — /api variant UI-superseded |
| DELETE | `/api/vendors/{card_id}/contacts/{contact_id}` | app/routers/vendor_contacts.py:472 | longer-path match only | contact_row.html delete uses hx-delete /v2/partials/... — /api variant UI-superseded |
| GET | `/api/vendors/check-duplicate` | app/routers/vendors_crud.py:37 | DOC app/templates/htmx/partials/vendors/create_form.html:30 | create_form.html comment explicitly says UI uses the /v2 partial, NOT this JSON route |
| POST | `/api/vendors` | app/routers/vendors_crud.py:53 | PREFIX app/templates/htmx/partials/shared/_attachments.html:35 |  |
| GET | `/api/vendors` | app/routers/vendors_crud.py:96 | PREFIX app/templates/htmx/partials/shared/_attachments.html:35 |  |

## Caveats / flags

- **Path-level, not method-level:** a path referenced by any verb keeps every method on it. A few extra dead methods may hide inside KEEP paths; not worth the risk to chase in Wave 2.
- **Spec's ~280 test-file figure not reproduced** — actual pinned-to-orphan test files: 80 (15 whole-file deletable). Re-baseline the Wave-2 estimate.
- Orphan count 107 vs spec '111+': within noise of the estimate; the 35 KEEP-AMBIGUOUS routes bracket it from above.
- e2e specs counted as tests; orphan routes touched by e2e specs: e2e/api.spec.ts.
- `POST /api/requisitions` (the spec's 'legacy JSON create endpoint') landed in KEEP-AMBIGUOUS, not ORPHAN — evidence is prefix-only; it can join the delete list per the spec's explicit naming.
