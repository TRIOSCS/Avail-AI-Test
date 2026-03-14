# Full Code Review: app/services/ Directory

**Date:** 2026-03-14  
**Reviewer:** Automated Deep Review  
**Scope:** All 134 service files in `app/services/`  
**Branch:** `cursor/full-code-review-25bb`

---

## Executive Summary

The services layer is well-structured and follows the project's "fat services, thin routers" architecture consistently. Business logic is properly separated from HTTP concerns. However, the review identified **47 distinct issues** across 10 categories. The most critical areas are:

1. **Missing transaction atomicity** in multi-step operations
2. **N+1 query patterns** in several hot-path services  
3. **Race conditions** in ownership claiming and proactive matching
4. **Synchronous DB sessions passed to async functions** (async/sync mismatch)

---

## Issue Legend

- **[CRITICAL]** — Could cause data corruption, security vulnerability, or production outage
- **[HIGH]** — Significant performance or correctness issue
- **[MEDIUM]** — Code quality, maintainability, or minor correctness concern
- **[LOW]** — Style, minor optimization, or defensive coding improvement

---

## 1. Transaction Handling Issues

### Issue 1.1 [CRITICAL] — Non-atomic multi-entity creation in `proactive_service.py`

**File:** `app/services/proactive_service.py`  
**Lines:** 534–680 (`convert_proactive_to_win`)

The function creates a Requisition, multiple Requirements, Offers, a Quote, and a BuyPlan with a single `db.commit()` at line 680. If any intermediate `db.flush()` succeeds but a later operation fails, the partial state could leak because the `except` handler does not exist — any exception will propagate with a dirty session.

**Risk:** Orphaned requisitions/quotes if BuyPlan creation fails.

**Fix:** Wrap in a try/except with `db.rollback()`, or use `db.begin_nested()` savepoints.

---

### Issue 1.2 [HIGH] — Commit inside loop in `ownership_service.py`

**File:** `app/services/ownership_service.py`  
**Lines:** 92–93 (`run_ownership_sweep`)

```python
if cleared or warned:
    db.commit()
```

The commit happens after the full loop, which is correct for atomicity. However, the function is `async` but uses a synchronous `Session`. If the loop takes a long time (many owned accounts), the session is held open for the entire duration without yielding, blocking other async tasks on the same event loop.

---

### Issue 1.3 [HIGH] — Mixed commit responsibility in `buyplan_notifications.py`

**File:** `app/services/buyplan_notifications.py`  
**Lines:** 192, 314, 373, 475, 560

Multiple notification functions call `db.commit()` directly. The callers (routers) may also commit. This leads to double-commit risk and violates the principle of letting the outermost caller control transaction boundaries.

---

### Issue 1.4 [MEDIUM] — `db.rollback()` in `email_intelligence_service.py` swallows context

**File:** `app/services/email_intelligence_service.py`  
**Lines:** 269–271

```python
except Exception as e:
    logger.warning("Failed to store email intelligence: %s", e)
    db.rollback()
    return None
```

Rolling back at the service layer may discard other pending changes the caller expects to be committed. The caller should decide whether to rollback.

---

## 2. N+1 Query Patterns and Database Efficiency

### Issue 2.1 [HIGH] — N+1 in `avail_score_service.py` buyer B3 metric

**File:** `app/services/avail_score_service.py`  
**Lines:** 387–403 (`_buyer_b3_vendor_followup`)

For each stale contact, a separate query checks for follow-ups:

```python
for sc in stale_contacts:
    followup = db.query(Contact.id).filter(...).first()
```

With many stale contacts, this fires one query per contact. Should use a single batch query with a subquery or `EXISTS` pattern.

---

### Issue 2.2 [HIGH] — N+1 in `avail_score_service.py` buyer B4 metric  

**File:** `app/services/avail_score_service.py`  
**Lines:** 412–433 (`_buyer_b4_pipeline_hygiene`)

Same pattern — one query per requisition to check for offers within 5 days:

```python
for req in user_reqs:
    has_offer = db.query(Offer.id).filter(...).first()
```

---

### Issue 2.3 [HIGH] — N+1 in `avail_score_service.py` sales B3 metric

**File:** `app/services/avail_score_service.py`  
**Lines:** 699–730 (`_sales_b3_quote_followup`)

One query per sent quote to check for follow-up activity.

---

### Issue 2.4 [MEDIUM] — N+1 in `ownership_service.py` site queries

**File:** `app/services/ownership_service.py`  
**Lines:** 410–412 (`get_open_pool_sites`)

Uses a `company_cache` dict to avoid N+1, which is good. But the cache is per-call — if the same function is called repeatedly, the cache is rebuilt each time.

---

### Issue 2.5 [MEDIUM] — N+1 in `buyplan_scoring.py` workload calculation

**File:** `app/services/buyplan_scoring.py`  
**Lines:** 225–236 (`assign_buyer`)

One count query per buyer to get their workload:

```python
for buyer in buyers:
    count = db.query(sqlfunc.count(BuyPlanLine.id)).filter(...).scalar()
```

Should batch into a single `GROUP BY` query.

---

### Issue 2.6 [MEDIUM] — `requisition_list_service.py` uses 22 correlated subqueries

**File:** `app/services/requisition_list_service.py`  
**Lines:** 73–261

Each subquery is individually correlated against `Requisition`. While PostgreSQL can optimize these, the sheer number (22) creates significant query planning overhead. Consider using lateral joins or a CTE-based approach for the most frequently accessed data.

---

### Issue 2.7 [LOW] — `vendor_score.py` loads up to 50,000 offer rows

**File:** `app/services/vendor_score.py`  
**Lines:** 132

```python
offer_rows = db.query(Offer.id, Offer.vendor_card_id, Offer.vendor_name).limit(50000).all()
```

Hard-coding `limit(50000)` means the scoring will silently stop being accurate once the table grows past 50K. Same issue at lines 148, 159 with `limit(10000)`.

---

## 3. Async/Await Issues

### Issue 3.1 [CRITICAL] — Sync DB session in async functions throughout

**Files affected:**
- `ownership_service.py` line 34: `async def run_ownership_sweep(db: Session)`
- `email_intelligence_service.py` line 189: `async def process_email_intelligence(db: Session, ...)`
- `email_threads.py` line 276: `async def fetch_threads_for_requirement(..., db: Session, ...)`
- `proactive_service.py` line 314: `async def send_proactive_offer(db: Session, ...)`
- `engagement_scorer.py` line 136: `async def compute_all_engagement_scores(db: Session)`
- `vendor_score.py` line 120: `async def compute_all_vendor_scores(db: Session)`

All these functions accept a synchronous `Session` but are declared `async`. SQLAlchemy sync sessions perform blocking I/O, which blocks the entire event loop during every database query. This is a fundamental architectural issue — all DB access in async functions should use `AsyncSession` or be delegated to `run_in_executor`.

---

### Issue 3.2 [HIGH] — `auto_dedup_service.py` uses `run_until_complete` inside async context

**File:** `app/services/auto_dedup_service.py`  
**Lines:** 181, 201

```python
return asyncio.get_event_loop().run_until_complete(
    _ask_claude_merge(...)
)
```

`run_until_complete()` cannot be called from within a running event loop (which is the case when called from the scheduler). This will raise `RuntimeError: This event loop is already running`. The function should be made `async` and awaited instead.

---

### Issue 3.3 [MEDIUM] — `buyplan_notifications.py` fires detached background tasks

**File:** `app/services/buyplan_notifications.py`  
**Lines:** 22–46 (`run_buyplan_bg`)

```python
asyncio.create_task(_run())
```

Background tasks created with `create_task` are fire-and-forget. If the app shuts down while tasks are pending, they'll be cancelled silently. The error handling logs but doesn't propagate failures back to the caller.

---

## 4. Error Handling Issues

### Issue 4.1 [HIGH] — Bare `except Exception` swallows important errors

**Files affected (partial list):**
- `activity_service.py` lines 100, 124: catches `Exception` during phone matching with `db.rollback()` — could mask constraint violations
- `proactive_service.py` line 161: catches all exceptions during match commit
- `vendor_score.py` line 222: catches flush exceptions
- `engagement_scorer.py` line 306: catches flush exceptions

Many of these `except Exception` blocks log and continue, which is appropriate for background jobs. However, the `db.rollback()` calls at lines 100 and 124 of `activity_service.py` roll back the session even when the exception is just a SQLite incompatibility (not a real error).

---

### Issue 4.2 [MEDIUM] — `requisition_service.py` raises `HTTPException` from service layer

**File:** `app/services/requisition_service.py`  
**Lines:** 49, 58, 60, 68–71

The service layer should not depend on FastAPI's `HTTPException`. Services should raise domain exceptions (e.g., `ValueError`, custom exceptions), and routers should translate them to HTTP errors. This creates a coupling between the service and the HTTP framework.

---

### Issue 4.3 [MEDIUM] — `buyplan_workflow.py` raises `PermissionError` 

**File:** `app/services/buyplan_workflow.py`  
**Lines:** 109, 157, 245

Uses `PermissionError` (a Python built-in for OS-level permissions) to signal authorization failures. Should use a custom `AuthorizationError` or `ForbiddenError`.

---

## 5. Security Issues

### Issue 5.1 [CRITICAL] — SQL injection risk in `email_threads.py`

**File:** `app/services/email_threads.py`  
**Lines:** 322, 359, 429

The `conversation_id` is interpolated directly into OData `$filter` strings sent to Graph API:

```python
"$filter": f"conversationId eq '{conv_id}'"
```

If `conv_id` contains a single quote, the OData filter could be manipulated. While this is Graph API (not SQL), OData injection is possible. The `conversation_id` should be sanitized or the values should be validated.

---

### Issue 5.2 [HIGH] — Credential salt is hardcoded

**File:** `app/services/credential_service.py`  
**Lines:** 36

```python
salt=b"availai-credential-salt-v1",
```

The PBKDF2 salt is hardcoded. While this isn't catastrophic (the secret_key provides entropy), best practice is to use a random per-installation salt stored alongside the encrypted data.

---

### Issue 5.3 [MEDIUM] — `admin_service.py` does not validate `name` field length

**File:** `app/services/admin_service.py`  
**Lines:** 72–73

```python
if "name" in updates and updates["name"] is not None:
    target.name = updates["name"].strip()
```

No length validation on the name field. An extremely long name could cause issues with database column limits or UI rendering.

---

### Issue 5.4 [MEDIUM] — HTML injection possible in ownership warning emails

**File:** `app/services/ownership_service.py`  
**Lines:** 643–654

`company.name` is passed through `html.escape()`, which is correct. However, `settings.app_url` at line 646 is interpolated directly into an `href` attribute. If `app_url` contains malicious content, it could lead to XSS in the email.

---

## 6. Logging Issues

### Issue 6.1 [LOW] — No `print()` usage detected

All service files consistently use `from loguru import logger`. No violations of the `print()` ban were found. This is excellent.

### Issue 6.2 [LOW] — Inconsistent log format strings

Some files use f-strings in logger calls (which defeats lazy evaluation):
- `ownership_service.py` lines 79, 101, 136: `logger.info(f"...")`
- `activity_service.py` lines 189, 191, 437, 558: `logger.info(f"...")`
- `buyplan_notifications.py` lines 175, 300: `logger.info(f"...")`

Should use `logger.info("... {} ...", var)` format for lazy evaluation.

---

## 7. Code Duplication

### Issue 7.1 [HIGH] — `_clean_email_body()` duplicated across files

**Files:**
- `app/services/ai_email_parser.py` lines 207–229
- `app/services/response_parser.py` lines 233–251

Both files implement `_clean_email_body()` with slightly different behavior. The `ai_email_parser` version preserves newlines (for table parsing), while `response_parser` collapses all whitespace. These should be unified in `app/utils/normalization.py` with a parameter to control newline handling.

---

### Issue 7.2 [MEDIUM] — Duplicate scoring logic between `vendor_score.py` and `engagement_scorer.py`

Both services compute vendor scores with overlapping but different methodologies. `vendor_score.py` uses "order advancement" scoring while `engagement_scorer.py` uses "engagement" scoring. Both update `VendorCard.engagement_score` (line 217 in `vendor_score.py`, line 290 in `engagement_scorer.py`), creating a write conflict if both run.

---

### Issue 7.3 [MEDIUM] — HTML email template duplication

**Files:**
- `buyplan_notifications.py` — 4 separate HTML email builders (lines 96–151, 237–278, 330–341, 403–432)
- `ownership_service.py` — 2 HTML email builders (lines 640–654, 681–711)
- `proactive_service.py` — HTML email builder (lines 399–441)

All build HTML emails with inline CSS. Should extract a shared email template utility.

---

### Issue 7.4 [LOW] — `should_auto_apply()` and `should_flag_review()` duplicated

**Files:**
- `app/services/ai_email_parser.py` lines 196–204
- `app/services/response_parser.py` lines 254–263

Identical logic, identical constants (`CONFIDENCE_AUTO = 0.8`, `CONFIDENCE_REVIEW = 0.5`).

---

## 8. Missing Input Validation

### Issue 8.1 [HIGH] — `merge_vendor_cards` does not validate ownership/permissions

**File:** `app/services/vendor_merge_service.py`  
**Lines:** 17–108

The function accepts `keep_id` and `remove_id` directly. There's no check that the caller has permission to merge vendors. The router should validate this, but defense-in-depth suggests the service should also check.

---

### Issue 8.2 [MEDIUM] — `sourcing_score.py` does not validate negative inputs

**File:** `app/services/sourcing_score.py`  
**Lines:** 40–83

`score_requirement()` accepts raw counts that could theoretically be negative (if data is corrupt). The sigmoid function would handle this gracefully, but explicit validation would catch data issues early.

---

### Issue 8.3 [MEDIUM] — `buyplan_workflow.py` `_apply_line_edits` trusts `edit["requirement_id"]`

**File:** `app/services/buyplan_workflow.py`  
**Lines:** 448–450

```python
for edit in edits:
    edits_by_req.setdefault(edit["requirement_id"], []).append(edit)
```

No validation that `requirement_id` exists or belongs to this plan. A malicious or buggy client could link lines to arbitrary requirements.

---

### Issue 8.4 [MEDIUM] — No limit validation on `get_recent_intelligence`

**File:** `app/services/email_intelligence_service.py`  
**Lines:** 274–276

The `limit` parameter has a default of 50 but no maximum. A caller could request `limit=1000000`, causing excessive memory usage.

---

## 9. Race Conditions and Concurrency

### Issue 9.1 [HIGH] — TOCTOU race in `proactive_service.py` match dedup

**File:** `app/services/proactive_service.py`  
**Lines:** 121–131

```python
existing = db.query(ProactiveMatch).filter(...).first()
if existing:
    continue
db.add(ProactiveMatch(...))
```

Between the check and the insert, another concurrent scan could create the same match. No `SELECT ... FOR UPDATE` or unique constraint guard.

---

### Issue 9.2 [MEDIUM] — Global mutable state in `proactive_service.py`

**File:** `app/services/proactive_service.py`  
**Line:** 37

```python
_last_proactive_scan = datetime.min.replace(tzinfo=timezone.utc)
```

This global variable is shared across all requests/workers. If multiple scheduler instances run simultaneously, they'll read/write this unsynchronized, potentially skipping or double-processing offers.

---

### Issue 9.3 [MEDIUM] — In-memory cache not thread-safe in `email_threads.py`

**File:** `app/services/email_threads.py`  
**Lines:** 31–55

```python
_thread_cache: dict[str, tuple[float, list]] = {}
```

The cache is a plain dict with no locking. Concurrent read/write from multiple async tasks could cause `RuntimeError: dictionary changed size during iteration` in Python.

---

### Issue 9.4 [MEDIUM] — Global `_ROUTING_MAPS` loaded without lock

**File:** `app/services/buyplan_scoring.py`  
**Lines:** 24–35

```python
_ROUTING_MAPS: dict | None = None

def _get_routing_maps() -> dict:
    global _ROUTING_MAPS
    if _ROUTING_MAPS is None:
        ...
```

Multiple concurrent calls could trigger redundant file reads. Not dangerous (file reads are idempotent) but wasteful.

---

## 10. Miscellaneous Issues

### Issue 10.1 [MEDIUM] — `sourcing_score.py` uses synchronous `Session` (not async)

**File:** `app/services/sourcing_score.py`  
**Line:** 23

```python
from sqlalchemy.orm import Session
```

This is the only scoring service that uses sync Session directly in a function that could be called from async routers. All other scoring services have the same issue, but this one is on the hot path (called for every requisition list view).

---

### Issue 10.2 [MEDIUM] — `buyplan_builder.py` generates AI summary before saving

**File:** `app/services/buyplan_builder.py`  
**Lines:** 110–112

```python
plan.ai_summary = generate_ai_summary(plan)
plan.ai_flags = [f.__dict__ if hasattr(f, "__dict__") else f for f in generate_ai_flags(plan, db)]
```

`generate_ai_summary` accesses `plan.lines` before the plan is saved. Since the lines are added via `line.buy_plan = plan` (line 97), they exist on the ORM relationship but haven't been flushed. If `generate_ai_flags` queries the DB for related data (it does — line 282 calls `db.get(Offer, ...)`), it may find stale data.

---

### Issue 10.3 [MEDIUM] — `vendor_score.py` and `engagement_scorer.py` both modify `VendorCard.engagement_score`

**File:** `app/services/vendor_score.py` line 217  
**File:** `app/services/engagement_scorer.py` line 290

Both services write to the same column. If both run in the same scheduler cycle, the last one to commit wins. This creates non-deterministic scoring.

---

### Issue 10.4 [LOW] — `crm_service.py` sequential quote number generation

**File:** `app/services/crm_service.py`  
**Lines:** 14–26

```python
last = db.query(Quote).filter(Quote.quote_number.like(f"{prefix}%")).order_by(Quote.id.desc()).first()
```

Under concurrent requests, two callers could get the same "next" quote number. Needs a database sequence or `SELECT ... FOR UPDATE`.

---

### Issue 10.5 [LOW] — `buyplan_workflow.py` line 676 references `plan.completed_at` but subtracts from `approved`

**File:** `app/services/buyplan_workflow.py`  
**Lines:** 676–678

```python
if approved and completed:
    days = (approved - completed).days if completed > approved else (completed - approved).days
```

The conditional logic `(approved - completed).days if completed > approved` is inverted — it calculates `approved - completed` when `completed > approved`, which would be negative. Should always be `(completed - approved).days`.

---

### Issue 10.6 [LOW] — `health_monitor.py` `run_health_checks` creates its own session

**File:** `app/services/health_monitor.py`  
**Lines:** 296–297

```python
db = SessionLocal()
```

Creates a raw session without the FastAPI dependency injection pattern. This works for scheduler-invoked calls but bypasses any middleware or lifecycle hooks.

---

## Summary Table

| Category | Critical | High | Medium | Low | Total |
|---|---|---|---|---|---|
| Transaction Handling | 1 | 2 | 1 | 0 | 4 |
| N+1 / DB Efficiency | 0 | 3 | 3 | 1 | 7 |
| Async/Await | 1 | 1 | 1 | 0 | 3 |
| Error Handling | 0 | 1 | 2 | 0 | 3 |
| Security | 1 | 1 | 2 | 0 | 4 |
| Logging | 0 | 0 | 0 | 2 | 2 |
| Code Duplication | 0 | 1 | 2 | 1 | 4 |
| Input Validation | 0 | 1 | 3 | 0 | 4 |
| Race Conditions | 0 | 1 | 3 | 0 | 4 |
| Miscellaneous | 0 | 0 | 3 | 3 | 6 |
| **Totals** | **3** | **11** | **20** | **7** | **41** |

## Positive Observations

1. **Consistent architecture:** All services follow "fat services, thin routers" — no business logic in routers.
2. **Loguru everywhere:** Zero `print()` statements found across all 134 files.
3. **Good file headers:** Every service file has a docstring explaining purpose, called-by, and depends-on.
4. **Defensive coding:** Most functions handle None/empty inputs gracefully.
5. **Batch processing:** `vendor_score.py` and `engagement_scorer.py` use batch-processing with `BATCH_SIZE` to limit memory.
6. **Proper `with_for_update()` usage:** `ownership_service.py` lines 126–127 and `claim_site` lines 437–442 correctly use row-level locking for concurrent claims.
7. **AI confidence routing:** The two-tier confidence system (auto-apply >= 0.8, review 0.5–0.8) is consistently implemented across `response_parser.py`, `ai_email_parser.py`, and `email_intelligence_service.py`.
8. **Good separation of concerns:** Scoring logic (`buyplan_scoring.py`) is separate from workflow logic (`buyplan_workflow.py`) and notification logic (`buyplan_notifications.py`).

## Priority Recommendations

1. **[P0] Fix async/sync mismatch** — Either convert to AsyncSession or wrap sync DB calls in `run_in_executor`. This is blocking the event loop on every DB query.
2. **[P0] Fix `auto_dedup_service.py` `run_until_complete`** — This will crash in production when called from the scheduler's event loop.
3. **[P1] Add unique constraints or upsert logic** for proactive match dedup and quote number generation.
4. **[P1] Batch N+1 queries** in `avail_score_service.py` — the B3, B4, and sales B3 metrics fire hundreds of individual queries per score computation.
5. **[P2] Extract shared email template utility** to reduce HTML duplication.
6. **[P2] Resolve `engagement_score` write conflict** between `vendor_score.py` and `engagement_scorer.py`.
