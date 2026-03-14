# AVAIL AI — Full Router Code Review

**Reviewed:** All 28 router files in `app/routers/`
**Date:** 2026-03-14
**Branch:** `cursor/full-code-review-25bb`

---

## Table of Contents

1. [Executive Summary](#executive-summary)
2. [CRITICAL Issues (Must Fix)](#critical-issues)
3. [HIGH Issues (Should Fix)](#high-issues)
4. [MEDIUM Issues (Recommended)](#medium-issues)
5. [LOW Issues (Nice to Have)](#low-issues)
6. [Per-File Analysis](#per-file-analysis)

---

## Executive Summary

| Metric | Count |
|--------|-------|
| Total router files reviewed | 28 (excluding `__init__.py`) |
| CRITICAL issues | 7 |
| HIGH issues | 18 |
| MEDIUM issues | 24 |
| LOW issues | 15 |

**Overall assessment:** The codebase has a mix of well-structured thin routers (e.g., `prospect_pool.py`, `emails.py`, `documents.py`, `strategic.py`) and several **fat routers** that violate the project's own architectural rule of "routers are thin, services are fat." The worst offenders are `views.py` (1,579 lines), `rfq.py` (1,001 lines), `sources.py` (867 lines), `materials.py` (763 lines), and `ai.py` (772 lines), which contain substantial business logic, raw DB queries, and data transformation code that belongs in services.

---

## CRITICAL Issues

### C1. Raw SQL Injection Vector — `vendor_analytics.py` lines 184-238
**File:** `app/routers/vendor_analytics.py`
**Lines:** 184-238

The `_vendor_parts_summary_query` function uses f-string interpolation inside `sqltext()`:
```python
mpn_filter = "AND LOWER(mpn) LIKE :mpn_pattern ESCAPE '\\'"
# ...
rows = db.execute(
    sqltext(f"""
    SELECT ... WHERE mpn != '' {mpn_filter}
    ...
    """),
    params,
).fetchall()
```
While `mpn_filter` is a static string chosen by the code (not user input), this pattern of building SQL with f-strings inside `sqltext()` is fragile and dangerous. If any future developer changes the logic to include user input in the f-string, it becomes a direct SQL injection. The `escape_like()` helper correctly sanitizes the LIKE parameter, but the SQL construction pattern itself is risky.

**Fix:** Use SQLAlchemy ORM queries or build the query conditionally without f-string interpolation in `sqltext()`.

---

### C2. Missing Auth on Admin Endpoints — `tagging_admin.py` (all endpoints)
**File:** `app/routers/tagging_admin.py`
**Lines:** 27, 113, 135, 206, 219, 241, 267, 289, 311, 336, 358, 409, 431, 485, 507, 521, 559, 581

All endpoints in `tagging_admin.py` only use `require_user` — **any authenticated user** can trigger AI backfills, Nexar bulk validation, batch enrichment, purge operations, and other admin-only actions. These should use `require_admin`.

**Fix:** Replace `_user=Depends(require_user)` with `_user=Depends(require_admin)` on all endpoints.

---

### C3. Missing Auth on Admin Endpoints — `nc_admin.py` lines 66-95
**File:** `app/routers/nc_admin.py`
**Lines:** 66-95

The `nc_force_search` and `nc_skip` endpoints allow **any authenticated user** to force-search or skip queue items. These are admin operations.

**Fix:** Use `require_admin` instead of `require_user`.

---

### C4. Missing Auth on Admin Endpoints — `ics_admin.py` lines 66-95
**File:** `app/routers/ics_admin.py`
**Lines:** 66-95

Same issue as C3. The `ics_force_search` and `ics_skip` endpoints allow any user to manipulate the ICS queue.

**Fix:** Use `require_admin` instead of `require_user`.

---

### C5. Inline Admin Check Bypasses Dependency System — `knowledge.py` lines 164-183
**File:** `app/routers/knowledge.py`
**Lines:** 164-183

The `update_knowledge_config` endpoint performs an inline admin check by comparing emails:
```python
if user.email not in (settings.ADMIN_EMAILS or "").split(","):
    raise HTTPException(403, "Admin only")
```
This bypasses the `require_admin` dependency, is inconsistent with the rest of the codebase, and the `settings.ADMIN_EMAILS` attribute name differs from the standard `settings.admin_emails`.

**Fix:** Use `Depends(require_admin)`.

---

### C6. Missing Input Validation — `tags.py` line 32
**File:** `app/routers/tags.py`
**Line:** 32

The search parameter `q` is used directly in an ILIKE query without `escape_like()`:
```python
if q:
    query = query.filter(Tag.name.ilike(f"%{q}%"))
```
A user could inject LIKE wildcards (`%`, `_`) to manipulate query behavior.

**Fix:** Use `escape_like(q)` before the ILIKE filter.

---

### C7. Variable Shadowing Overrides Function Parameter — `rfq.py` line 778
**File:** `app/routers/rfq.py`
**Line:** 778

The variable `payload` is reassigned to a dict, shadowing the `FollowUpEmail` function parameter:
```python
payload = {
    "message": { ... }
}
```
This prevents accessing the original `payload` object after this line.

**Fix:** Rename to `mail_payload` or `graph_payload`.

---

## HIGH Issues

### H1. Fat Router: `views.py` — 1,579 lines of DB queries and business logic
**File:** `app/routers/views.py`
**Lines:** 75-140, 361-378, 526-579, 770-829, 987-1036, 1195-1259, 1367-1435

Contains 7 major private query-builder functions (`_query_requisitions`, `_filter_results`, `_sort_results`, `_query_companies`, `_query_quotes`, `_query_vendors`, `_query_buy_plans`, `_query_prospects`) with complex filtering, sorting, pagination, and data transformation. Also has inline HTML building (lines 196-216) and inline data mutation (lines 1563-1568 for prospect claiming).

**Fix:** Extract all `_query_*` functions to corresponding service files. The prospect claim at line 1551 should delegate to a service.

---

### H2. Fat Router: `rfq.py` — 1,001 lines with massive `get_activity` endpoint
**File:** `app/routers/rfq.py`
**Lines:** 277-502

The `get_activity` endpoint is ~225 lines of complex business logic: grouping contacts/responses/activities by vendor, computing vendor-level status from contact statuses, resolving vendor card IDs, collecting phone numbers, and building a complex result structure. This is pure business logic that belongs in a service.

**Fix:** Extract to `services/activity_feed_service.py`.

---

### H3. Fat Router: `rfq.py` — `rfq_prepare` endpoint
**File:** `app/routers/rfq.py`
**Lines:** 506-667

The `rfq_prepare` endpoint is ~160 lines of exhaustion mapping, batch vendor card fetching, past RFQ email lookup, and async contact enrichment. All business logic.

**Fix:** Extract to `services/rfq_prepare_service.py`.

---

### H4. Fat Router: `rfq.py` — `_enrich_with_vendor_cards` function
**File:** `app/routers/rfq.py`
**Lines:** 860-1001

The `_enrich_with_vendor_cards` function is ~140 lines of vendor card auto-creation, review fetching, sighting counting, email/phone merging, and blacklist filtering. This is a full service-layer operation living in a router file.

**Fix:** Move to `services/search_enrichment_service.py`.

---

### H5. Fat Router: `materials.py` — business logic in router
**File:** `app/routers/materials.py`
**Lines:** 45-77, 82-195, 472-610, 626-762

Contains manufacturer inference logic (`_infer_manufacturer_from_prefix`, `backfill_missing_manufacturers`), complex serialization (`material_card_to_dict`), the entire merge operation (`merge_material_cards`), and the stock import pipeline (`import_stock_list_standalone`). All of these are service-layer operations.

**Fix:** Extract inference/backfill to a service. Move `material_card_to_dict` to `utils/` or a serializer module. Move merge to `services/material_merge_service.py`. Move stock import to `services/stock_import_service.py`.

---

### H6. Fat Router: `sources.py` — 9 test connector classes in router
**File:** `app/routers/sources.py`
**Lines:** 123-278

Nine full test connector classes (`_EmailMiningTestConnector`, `_AnthropicTestConnector`, `_TeamsTestConnector`, etc.) are defined inside the router file, along with the `_get_connector_for_source` factory function and the `_create_sightings_from_attachment` function.

**Fix:** Move all test connectors and the factory to `services/source_test_service.py`. Move `_create_sightings_from_attachment` to `services/attachment_parser.py` or a sighting creation service.

---

### H7. Fat Router: `vendor_contacts.py` — AI prompt + 3-tier waterfall in router
**File:** `app/routers/vendor_contacts.py`
**Lines:** 45-186

The `lookup_vendor_contact` endpoint contains the entire 3-tier waterfall logic (cache check, website scrape, AI lookup) with a full Claude AI prompt embedded in the router. This is complex business logic.

**Fix:** Extract to `services/vendor_contact_lookup_service.py`.

---

### H8. Fat Router: `vendors_crud.py` — `list_vendors` contains ~225 lines of query logic
**File:** `app/routers/vendors_crud.py`
**Lines:** 82-319

The `list_vendors` endpoint's inner `_fetch` function contains tier filtering, full-text search, batch review stats, strategic claim info, top contact resolution, auto-calculated star ratings, and location string building — all in a 235-line nested function.

**Fix:** Extract to `services/vendor_list_service.py`.

---

### H9. Fat Router: `ai.py` — business logic for offer creation
**File:** `app/routers/ai.py`
**Lines:** 475-533, 644-771

The `save_parsed_offers` and `ai_save_freeform_offers` endpoints contain MPN fuzzy matching, material card resolution, vendor card creation, and offer construction logic. The `ai_apply_freeform_rfq` endpoint creates requisitions and requirements inline.

**Fix:** Extract offer saving to `services/offer_import_service.py`.

---

### H10. Fat Router: `prospect_suggested.py` — serialization + sorting logic
**File:** `app/routers/prospect_suggested.py`
**Lines:** 421-523

The `_serialize_prospect` function (75 lines) and `_sort_serialized_prospects` function (25 lines) contain business logic for readiness tier calculation, signal tag building, buyer-ready scoring, and warm intro detection.

**Fix:** Move to `services/prospect_serializer.py`.

---

### H11. Missing `require_admin` on Vendor Delete — `vendors_crud.py` line 409-422
**File:** `app/routers/vendors_crud.py`
**Lines:** 409-422

The `toggle_blacklist` endpoint uses `require_user` — any authenticated user can blacklist/unblacklist vendors. This should probably require buyer or admin role.

**Fix:** Consider using `require_buyer` or `require_admin`.

---

### H12. Untyped `body: dict` parameter — `rfq.py` line 232
**File:** `app/routers/rfq.py`
**Line:** 232

The `update_vendor_response_status` endpoint accepts `body: dict` instead of a Pydantic schema:
```python
async def update_vendor_response_status(vr_id: int, body: dict, ...):
```
This bypasses FastAPI's input validation entirely.

**Fix:** Create a Pydantic schema `VendorResponseStatusUpdate`.

---

### H13. Untyped `payload: dict` — `prospect_suggested.py` line 272
**File:** `app/routers/prospect_suggested.py`
**Lines:** 272, 358

The `dismiss_suggested` and `add_prospect` endpoints accept `payload: dict` instead of Pydantic schemas.

**Fix:** Create proper Pydantic schemas for these.

---

### H14. Missing Rate Limiting on Email-Sending Endpoints — `outreach.py`
**File:** `app/routers/outreach.py`
**Lines:** 40-100

The `send_outreach` endpoint has no rate limiter but sends up to 50 emails per request. It could be abused.

**Fix:** Add `@limiter.limit("5/minute")`.

---

### H15. `follow_up_send_batch` — No Pydantic validation on request body
**File:** `app/routers/rfq.py`
**Lines:** 798-856

The `send_follow_up_batch` endpoint uses `raw = await request.json()` to parse the body instead of a Pydantic schema:
```python
raw = await request.json()
contact_ids = raw.get("contact_ids", [])
```
This bypasses input validation.

**Fix:** Create a Pydantic schema `BatchFollowUpRequest`.

---

### H16. Inconsistent 403 vs 404 for access control — `views.py` lines 245-246
**File:** `app/routers/views.py`
**Lines:** 245-246, 284-285

When a sales user tries to access another user's requisition, the router returns 404 instead of 403:
```python
if user.role == "sales" and req.created_by != user.id:
    raise HTTPException(status_code=404, detail="Requisition not found")
```
While returning 404 to hide resource existence is a valid security pattern (IDOR prevention), this should be documented/consistent.

**Assessment:** This is actually a **valid security pattern** (prevents resource enumeration). Not a bug, but should be consistently applied.

---

### H17. Missing ownership validation on dismiss_matches — `proactive.py` lines 88-96
**File:** `app/routers/proactive.py`
**Lines:** 88-98

The `dismiss_matches` endpoint correctly filters by `salesperson_id == user.id`, but the `add_do_not_offer` endpoint (lines 101-146) does not verify the user has access to the specified `company_id`. Any user can suppress MPNs for any company.

**Fix:** Add ownership/role validation for `add_do_not_offer`.

---

### H18. `request.json()` used instead of Pydantic — `materials.py` lines 323, 399, 486
**File:** `app/routers/materials.py`
**Lines:** 323, 399, 486

Multiple endpoints use `body = await request.json()` instead of Pydantic schemas:
- `quick_search` (line 323)
- `enrich_material` (line 399)
- `merge_material_cards` (line 486)

**Fix:** Create Pydantic schemas for each.

---

## MEDIUM Issues

### M1. Inline `from fastapi.responses import HTMLResponse` — `views.py`
**File:** `app/routers/views.py`
**Lines:** 194, 628

`HTMLResponse` is imported inside function bodies instead of at the top of the file.

---

### M2. `type("Req", (), {...})` pattern — `views.py`
**File:** `app/routers/views.py`
**Lines:** 130, 570, 819, 1027, 1247

Dynamic namespace objects created with `type()` are used throughout `views.py` for template data. This is an anti-pattern — use dataclasses, Pydantic models, or plain dicts.

---

### M3. N+1 Query in `_query_quotes` — `views.py` lines 813-817
**File:** `app/routers/views.py`
**Lines:** 813-817

Each quote fetches its line count individually inside a loop:
```python
for qobj, site_name in rows:
    line_count = db.query(sqlfunc.count(QuoteLine.id)).filter(QuoteLine.quote_id == qobj.id).scalar()
```

**Fix:** Use a subquery or window function to fetch all counts in one query.

---

### M4. `suggested_stats` loads all prospects into memory — `prospect_suggested.py` line 190
**File:** `app/routers/prospect_suggested.py`
**Line:** 190

```python
buyer_ready = sum(1 for p in base.all() if build_priority_snapshot(p)["is_buyer_ready"])
```
This loads ALL suggested prospects into memory to count buyer-ready ones. Could be thousands of records.

**Fix:** Implement `build_priority_snapshot` logic as a SQL query or cache the result.

---

### M5. `list_suggested` loads all prospects for certain sorts — `prospect_suggested.py` lines 112-113
**File:** `app/routers/prospect_suggested.py`
**Lines:** 112-118

When `sort == "buyer_ready_desc"` or `buyer_ready_only == True`, the endpoint loads ALL matching prospects into Python memory for sorting:
```python
items = [_serialize_prospect(p) for p in query.all()]
```

**Fix:** Pre-compute buyer_ready_score in DB or use a materialized column.

---

### M6. Missing response_model on most endpoints
**Files:** Most router files

Only a handful of endpoints use FastAPI's `response_model` parameter (e.g., `sources.py` line 378, `vendor_analytics.py` line 138-139, `vendors_crud.py` lines 82, 376). The vast majority return raw dicts without response model validation.

**Impact:** No automatic response validation, no auto-generated OpenAPI schemas, no field filtering.

---

### M7. Sync endpoints used where async would be appropriate — `strategic.py`, `task.py`, `knowledge.py`
**Files:** `app/routers/strategic.py`, `app/routers/task.py`, `app/routers/knowledge.py`

These files use `def` (sync) endpoints instead of `async def`. While FastAPI handles this correctly by running them in a thread pool, they consume a thread instead of yielding the event loop.

---

### M8. `check_vendor_duplicate` fetches 500 vendors for fuzzy matching — `vendors_crud.py` lines 60-77
**File:** `app/routers/vendors_crud.py`
**Lines:** 60-77

```python
existing = db.query(VendorCard.id, VendorCard.normalized_name, VendorCard.display_name).limit(500).all()
```
This loads 500 vendors into memory for Python-side fuzzy matching. At scale, this will become slow and miss vendors beyond the first 500.

**Fix:** Use PostgreSQL's `pg_trgm` extension for database-side fuzzy matching.

---

### M9. Hardcoded batch_id default — `tagging_admin.py` line 206
**File:** `app/routers/tagging_admin.py`
**Line:** 206

```python
async def apply_batch_results(batch_id: str = "msgbatch_01M2nTyzQ141rLBb6SJte9fi", ...):
```
A specific batch ID is hardcoded as the default value.

**Fix:** Make `batch_id` required (no default).

---

### M10. Missing `status_code=201` on creation endpoints
**Files:** Multiple

Several POST endpoints that create resources return 200 instead of 201:
- `app/routers/error_reports.py` line 41 (creates ticket, returns 200)
- `app/routers/vendor_contacts.py` line 382 (creates contact, returns 200)
- `app/routers/knowledge.py` line 89 (creates entry, returns 200)
- `app/routers/ai.py` line 475 (creates offers, returns 200)

---

### M11. Global mutable state — `tagging_admin.py` line 21
**File:** `app/routers/tagging_admin.py`
**Line:** 21

```python
_enrichment_status: dict = {"running": False, "started_at": None, "result": None}
```
Module-level mutable state is not safe in multi-worker deployments. Use Redis or DB for shared state.

---

### M12. `htmx_views.py` — Inline HTML for requirement rows
**File:** `app/routers/htmx_views.py`
**Lines:** 251-266

The `add_requirement` endpoint builds HTML inline using f-strings:
```python
html = f"""<tr class="hover:bg-gray-50">..."""
```
This is fragile, hard to maintain, and not XSS-safe if user input (like `primary_mpn`) contains HTML.

**Fix:** Use a Jinja2 template partial.

---

### M13. `htmx_views.py` — `requisition_create` contains business logic
**File:** `app/routers/htmx_views.py`
**Lines:** 170-220

The `requisition_create` endpoint parses parts text, creates `Requirement` objects, and handles CSV-like parsing — all in the router.

**Fix:** Move to a service function.

---

### M14. `error_reports.py` — `_next_ticket_number` is not race-safe
**File:** `app/routers/error_reports.py`
**Lines:** 36-38

```python
def _next_ticket_number(db: Session) -> str:
    last = db.query(func.max(TroubleTicket.id)).scalar() or 0
    return f"TT-{last + 1:04d}"
```
Two concurrent requests could generate the same ticket number.

**Fix:** Use a database sequence or let the DB auto-increment handle this.

---

### M15. `command_center.py` — All DB queries in router
**File:** `app/routers/command_center.py`
**Lines:** 27-115

The entire endpoint is 4 raw DB queries with inline serialization. No service layer.

**Fix:** Extract to `services/command_center_service.py`.

---

### M16. `views.py` — Prospect claim mutates DB directly in router
**File:** `app/routers/views.py`
**Lines:** 1551-1578

The `prospect_claim` endpoint directly mutates DB fields:
```python
prospect.status = "claimed"
prospect.claimed_by = user.id
prospect.claimed_at = datetime.now(timezone.utc)
db.commit()
```

**Fix:** Delegate to `services/prospect_pool_service.py`.

---

### M17. `sources.py` — `list_api_sources` mutates DB during GET
**File:** `app/routers/sources.py`
**Lines:** 378-435

The GET endpoint for listing sources modifies source status and commits:
```python
for src in sources:
    if not any_set and src.status not in ("disabled", "error"):
        src.status = "pending"
db.commit()
```
GET endpoints should be idempotent and not mutate state.

**Fix:** Move status auto-detection to a separate background task or POST endpoint.

---

### M18. Missing error handling for `int()` conversion — `vendor_analytics.py` lines 43-44
**File:** `app/routers/vendor_analytics.py`
**Lines:** 43-44

```python
limit = min(int(request.query_params.get("limit", "100")), 500)
offset = max(int(request.query_params.get("offset", "0")), 0)
```
If a user sends `?limit=abc`, this will raise an unhandled `ValueError`.

**Fix:** Use FastAPI `Query()` parameters with proper types.

---

### M19. Same issue in `vendors_crud.py` — `autocomplete_names` lines 329-332
**File:** `app/routers/vendors_crud.py`
**Lines:** 329-332

```python
q = request.query_params.get("q", "").strip().lower()
limit = min(int(request.query_params.get("limit", "8")), 20)
```

**Fix:** Use FastAPI `Query()` parameters.

---

### M20. Same issue in `materials.py` — `list_materials` lines 203-205
**File:** `app/routers/materials.py`
**Lines:** 203-205

```python
q = request.query_params.get("q", "").strip().lower()
limit = min(int(request.query_params.get("limit", "200")), 1000)
offset = max(int(request.query_params.get("offset", "0")), 0)
```

**Fix:** Use FastAPI `Query()` parameters.

---

### M21. `sources.py` — Scan inbox/outbound have duplicated options parsing
**File:** `app/routers/sources.py`
**Lines:** 600-610, 688-697

The mining options parsing logic is duplicated between `scan_inbox_for_vendors` and `email_mining_scan_outbound`.

**Fix:** Extract to a shared helper.

---

### M22. `requisitions2.py` — Inline business logic in `inline_save`
**File:** `app/routers/requisitions2.py`
**Lines:** 208-264

The `inline_save` endpoint contains status transition logic, urgency validation, deadline parsing, and owner reassignment — all inline instead of delegating to a service.

**Fix:** Extract field update logic to a service function.

---

### M23. `requisitions2.py` — Inline business logic in `row_action`
**File:** `app/routers/requisitions2.py`
**Lines:** 270-344

The `row_action` endpoint has a long if/elif chain for 7 different actions (archive, activate, claim, unclaim, won, lost, assign), each with service calls and error handling.

**Fix:** Extract to a service dispatch function.

---

### M24. `requisitions2.py` — SSE stream missing auth
**File:** `app/routers/requisitions2.py`
**Lines:** 97-125

The `/stream` SSE endpoint does not use `require_user` — any client can connect without authentication:
```python
async def requisitions_stream(request: Request):
```

**Fix:** Add `user: User = Depends(require_user)`.

---

## LOW Issues

### L1. Inconsistent import style — some files use relative imports, others absolute
Multiple files mix `from ..database import get_db` with `from app.database import get_db`.
**Files:** `nc_admin.py`, `ics_admin.py`, `strategic.py`, `task.py`, `knowledge.py`, `tagging_admin.py`

### L2. Inline imports inside functions
**Files:** Many files have imports inside function bodies (e.g., `rfq.py` lines 179, 196, 287, `materials.py` line 237). While sometimes needed for circular import avoidance, many could be moved to the top.

### L3. `views.py` line 64 — Placeholder TODO comment
```python
results = []  # TODO: aggregate search across requisitions, companies, vendors
```

### L4. Unused `wants_html` import — `views.py` line 18
```python
from app.dependencies import require_user, wants_html
```
`wants_html` is imported but never used.

### L5. Duplicate `from fastapi.responses import HTMLResponse` — `views.py`
Imported inline in 4 different functions instead of once at top.

### L6. `proactive.py` — Potential lazy-load N+1 on `m.offer`
**File:** `app/routers/proactive.py`
**Line:** 189

```python
offer = m.offer  # Potential N+1
```
Each match lazy-loads its offer.

### L7. `htmx_views.py` — `get_user` used instead of `require_user`
**File:** `app/routers/htmx_views.py`
**Line:** 68

The main page handler uses `get_user(request, db)` which returns `None` for unauthenticated users instead of raising 401. This is intentional (shows login page), but differs from other routers.

### L8. `auth.py` — f-string in logger calls
**File:** `app/routers/auth.py`
**Lines:** 117, 119, 124, 141, 158

Uses f-strings instead of loguru's lazy formatting:
```python
logger.error(f"Azure token exchange failed: {e}")
```
Should be:
```python
logger.error("Azure token exchange failed: {}", e)
```

### L9. `outreach.py` — f-string in logger calls
**File:** `app/routers/outreach.py`
**Lines:** 83-86

Same issue as L8.

### L10. `vendor_contacts.py` — f-string in logger calls
**File:** `app/routers/vendor_contacts.py`
**Lines:** 79, 96, 111, 176

Same issue as L8.

### L11. Missing `status_code=204` on delete that returns no content
The `task.py` line 95 correctly uses `status_code=204`, but other delete endpoints return `{"ok": True}` with 200 (e.g., `knowledge.py` line 132, `vendor_contacts.py` line 472). Should be consistent.

### L12. `strategic.py` — sync endpoints
**File:** `app/routers/strategic.py`
All endpoints use `def` instead of `async def`, consuming thread pool threads.

### L13. `emails.py` — Defensive `model_dump()` usage
**File:** `app/routers/emails.py`
Lines 52, 56, 76, 79, 102, 106

Uses `.model_dump()` on response models instead of returning the model directly (FastAPI handles serialization).

### L14. `prospect_suggested.py` — `list_suggested` has 15 query parameters
**File:** `app/routers/prospect_suggested.py`
**Lines:** 33-49

Endpoint signature has 15 parameters. Consider grouping into a Pydantic query model.

### L15. `proactive.py` — `get_site_contacts` is a pure DB query in router
**File:** `app/routers/proactive.py`
**Lines:** 314-338

Simple query, but could be in a service for consistency.

---

## Per-File Analysis

### `app/routers/__init__.py` ✅
Clean module docstring. No issues.

### `app/routers/activity.py` ✅ GOOD
- **Thin?** Mostly yes. `call_initiated` has some resolution logic but delegates to service for timeline.
- **Auth:** `require_user` used correctly.
- **Error handling:** Good — swallows errors for fire-and-forget endpoint.
- **Rate limiting:** Custom in-memory rate limiter (acceptable for this use case).

### `app/routers/ai.py` ⚠️ MEDIUM
- **Thin?** No. Contains `_build_vendor_history` helper with DB queries, offer creation logic in `save_parsed_offers` and `ai_save_freeform_offers`.
- **Auth:** `require_user` used; AI gate check via `_ai_enabled()`.
- **Rate limiting:** Good — `@limiter.limit("10/minute")` on AI endpoints.
- **Issues:** H9, H13 (no Pydantic on some endpoints).

### `app/routers/auth.py` ✅ ACCEPTABLE
- **Thin?** Yes for most endpoints. OAuth callback has necessary inline logic.
- **Auth:** Correctly uses `get_user` for public pages, proper CSRF state validation.
- **Security:** Password hashing uses PBKDF2-HMAC-SHA256 with 200K iterations (good). Login form gated by env var.
- **Issues:** L8 (f-string logger).

### `app/routers/command_center.py` ⚠️ MEDIUM
- **Thin?** No — all DB queries inline.
- **Auth:** `require_user` used.
- **Issues:** M15.

### `app/routers/documents.py` ✅ EXCELLENT
- **Thin?** Yes. Delegates to `document_service`.
- **Auth:** `require_user`.
- **Rate limiting:** Yes.
- **Error handling:** Good — catches ValueError (404) and Exception (500).

### `app/routers/emails.py` ✅ GOOD
- **Thin?** Yes. Delegates to `email_threads` and `email_intelligence_service`.
- **Auth:** `require_user` + `require_fresh_token`.
- **Response models:** Uses Pydantic response schemas.
- **Error handling:** Good — graceful degradation on token expiry.

### `app/routers/error_reports.py` ✅ ACCEPTABLE
- **Thin?** Mostly. Inline schema `ErrorReportCreate` is fine.
- **Auth:** `require_user`.
- **Issues:** M14 (race condition on ticket number), M10 (missing 201 status).

### `app/routers/htmx_views.py` ⚠️ MEDIUM
- **Thin?** No. `requisition_create` has inline business logic.
- **Auth:** `require_user` on partials, `get_user` on page entry.
- **Issues:** M12 (inline HTML), M13 (business logic in router), L7.

### `app/routers/ics_admin.py` 🔴 CRITICAL
- **Thin?** Yes — delegates to queue_manager.
- **Auth:** `require_user` but should be `require_admin` for mutation endpoints.
- **Issues:** C4.

### `app/routers/knowledge.py` ⚠️ MEDIUM
- **Thin?** Yes — delegates to knowledge_service.
- **Auth:** `require_user`.
- **Issues:** C5 (inline admin check), M7 (sync endpoints).

### `app/routers/materials.py` 🔴 HIGH
- **Thin?** No. Contains inference logic, serialization, merge operation, stock import.
- **Auth:** `require_user` for reads, `require_admin` for delete/restore, `require_buyer` for import. Good.
- **Issues:** H5, H18, M20.

### `app/routers/nc_admin.py` 🔴 CRITICAL
- **Thin?** Yes — delegates to queue_manager.
- **Auth:** `require_user` but should be `require_admin` for mutation endpoints.
- **Issues:** C3.

### `app/routers/outreach.py` ✅ ACCEPTABLE
- **Thin?** Mostly. Has greeting personalization inline (acceptable).
- **Auth:** `require_user` + `require_fresh_token`.
- **Response model:** Uses `OutreachResult`.
- **Issues:** H14 (missing rate limit).

### `app/routers/proactive.py` ✅ ACCEPTABLE
- **Thin?** Mostly. `draft_proactive_email` has some context building.
- **Auth:** `require_user`.
- **Issues:** H17 (missing ownership on do_not_offer), L6, L15.

### `app/routers/prospect_pool.py` ✅ EXCELLENT
- **Thin?** Yes. All 5 endpoints delegate to service functions.
- **Auth:** `require_user`.
- **Input validation:** Uses Pydantic schemas.

### `app/routers/prospect_suggested.py` ⚠️ MEDIUM
- **Thin?** No. Contains serialization and sorting logic.
- **Auth:** `require_user`.
- **Issues:** H10, H13, M4, M5.

### `app/routers/requisitions2.py` ⚠️ MEDIUM
- **Thin?** Mostly. Delegates to services but has inline field update logic.
- **Auth:** `require_user`.
- **Issues:** M22, M23, M24 (SSE missing auth).

### `app/routers/rfq.py` 🔴 HIGH
- **Thin?** No. Contains ~500 lines of business logic.
- **Auth:** `require_user` for reads, `require_buyer` for sends. Good.
- **Rate limiting:** Good on send endpoints.
- **Issues:** H2, H3, H4, H12, H15, C7.

### `app/routers/sources.py` 🔴 HIGH
- **Thin?** No. Contains 9 test connector classes and 2 helper functions.
- **Auth:** `require_user`, `require_settings_access` for toggle.
- **Rate limiting:** Good — 2/min on scan, 5/min on test.
- **Issues:** H6, M17, M21.

### `app/routers/strategic.py` ✅ EXCELLENT
- **Thin?** Yes. All endpoints delegate to `strategic_vendor_service`.
- **Auth:** `require_user` for reads, `require_buyer` for mutations.
- **Issues:** L12 (sync endpoints), but minor.

### `app/routers/tagging_admin.py` 🔴 CRITICAL
- **Thin?** Mostly (delegates to services).
- **Auth:** Only `require_user` — needs `require_admin`.
- **Issues:** C2, M9, M11.

### `app/routers/tags.py` 🔴 CRITICAL
- **Thin?** Yes — simple DB queries (acceptable for read-only).
- **Auth:** `require_user`.
- **Response models:** Uses Pydantic response schemas. Good.
- **Issues:** C6 (missing escape_like on search).

### `app/routers/task.py` ✅ GOOD
- **Thin?** Yes. Delegates to `task_service`.
- **Auth:** `require_user`.
- **Status codes:** Correct 201 on create, 204 on delete.
- **Issues:** M7 (sync endpoints).

### `app/routers/vendor_analytics.py` 🔴 CRITICAL
- **Thin?** Mostly — but contains raw SQL.
- **Auth:** `require_user` for reads, `require_buyer` for AI analysis.
- **Issues:** C1 (SQL construction pattern), M18.

### `app/routers/vendor_contacts.py` ⚠️ HIGH
- **Thin?** No. Contains 3-tier waterfall with AI prompt.
- **Auth:** `require_user` for reads, `require_buyer` for mutations. Good.
- **Issues:** H7, L10.

### `app/routers/vendor_inquiry.py` ✅ GOOD
- **Thin?** Yes. Delegates to `vendor_email_lookup` service.
- **Auth:** `require_user` for lookup, `require_buyer` for sending.
- **Rate limiting:** Good — 3/min on send.
- **Input validation:** Pydantic schemas with field constraints.

### `app/routers/vendors_crud.py` 🔴 HIGH
- **Thin?** No. `list_vendors` has 235 lines of query/transform logic.
- **Auth:** `require_user` for most, `require_admin` for delete. Good.
- **Issues:** H8, H11, M8, M19.

### `app/routers/views.py` 🔴 HIGH
- **Thin?** No. 1,579 lines — the worst offender.
- **Auth:** `require_user` on all endpoints. Good.
- **Issues:** H1, M1, M2, M3, M16, L3, L4, L5.

---

## Summary of Recommended Actions (Priority Order)

### Immediate (Security)
1. **C2:** Add `require_admin` to all `tagging_admin.py` endpoints
2. **C3/C4:** Add `require_admin` to `nc_admin.py` and `ics_admin.py` mutation endpoints
3. **C5:** Replace inline admin check in `knowledge.py` with `require_admin`
4. **C6:** Add `escape_like()` to `tags.py` search
5. **M24:** Add auth to `requisitions2.py` SSE stream

### Short-term (Architecture)
6. **H1-H10:** Extract business logic from fat routers to services (views, rfq, materials, sources, vendor_contacts, vendors_crud, ai, prospect_suggested)
7. **H12, H13, H15, H18:** Replace `dict` and `request.json()` with Pydantic schemas
8. **C1:** Refactor raw SQL in `vendor_analytics.py` to use ORM

### Medium-term (Quality)
9. **M6:** Add `response_model` to all endpoints
10. **M10:** Use correct HTTP status codes (201 for creation)
11. **M18-M20:** Use FastAPI `Query()` instead of manual `request.query_params` parsing
12. **M3:** Fix N+1 query in quotes listing
13. **M4/M5:** Optimize prospect queries to avoid loading all records into memory
