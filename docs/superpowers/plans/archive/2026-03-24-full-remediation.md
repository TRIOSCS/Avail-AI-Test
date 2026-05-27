# Full Codebase Remediation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix all critical, high, and medium findings from the 2026-03-24 codebase review — security, silent failures, data integrity, performance, and code quality.

**Architecture:** Surgical fixes across existing files. No new modules, no refactors. Each task is a focused fix that can be tested independently.

**Tech Stack:** Python 3.11, FastAPI, SQLAlchemy 2.0, PostgreSQL 16, pytest

---

## File Map

### Phase 1 — Data Integrity & Security
| File | Change |
|------|--------|
| `app/jobs/eight_by_eight_jobs.py` | Replace phantom status strings with StrEnum constants |
| `app/jobs/task_jobs.py` | Replace phantom status strings with StrEnum constants |
| `app/jobs/lifecycle_jobs.py` | Add missing `raise` after exception |
| `app/services/proactive_service.py` | Replace raw "sent" strings; fix send-failure marking |
| `app/services/sighting_status.py` | Replace "delivered" with valid ContactStatus |
| `app/services/knowledge_service.py` | Replace "in_progress" with valid RequisitionStatus |
| `app/routers/requisitions/core.py` | Replace "closed" with valid RequisitionStatus |
| `app/routers/htmx_views.py:2365` | Fix script injection in ai_cleanup_email |
| `app/routers/htmx_views.py:7985` | Fix script injection in proactive_draft |
| `app/routers/error_reports.py` | Add require_admin to ticket endpoints |
| `app/template_env.py` | Remove img from sanitizer; add rel to anchor attrs |

### Phase 2 — Silent Failures
| File | Change |
|------|--------|
| `app/email_service.py:620` | Re-raise after batch commit failure |
| `app/email_service.py:1061-1181` | Move try/except inside offer loop |
| `app/search_service.py:1440` | Add logging to audit card except block |
| `app/search_service.py:1223,1656,1693` | Upgrade DEBUG→WARNING |
| `app/routers/htmx_views.py:1057,2797` | Upgrade DEBUG→WARNING for auto-search |
| `app/enrichment_service.py:326` | Add logging for non-200 Explorium response |
| `app/connectors/email_mining.py:119` | Narrow except to IntegrityError |

### Phase 3 — Performance & Quality
| File | Change |
|------|--------|
| `app/routers/htmx_views.py:2306` | Fix N+1 vendor contacts with subqueryload |
| `app/routers/htmx_views.py:5510` | Fix N+1 buy plans with joinedload chain |
| `app/services/crm_service.py` | Add SELECT FOR UPDATE to next_quote_number |
| `app/search_service.py:86` | Add TTL-based retry for Redis init |
| `app/services/vendor_affinity_service.py:177` | Add escape_like() |
| `app/connectors/mouser.py:95` | Move `import re` to module level |
| `app/main.py:269` | Remove quick-create CSRF exemption |
| `app/dependencies.py:58` + `app/startup.py` | Seed agent user |

---

## Task 1: Fix Phantom Status Strings in Jobs

**Files:**
- Modify: `app/jobs/eight_by_eight_jobs.py`
- Modify: `app/jobs/task_jobs.py`
- Modify: `app/services/proactive_service.py`
- Modify: `app/services/sighting_status.py`
- Modify: `app/services/knowledge_service.py`
- Modify: `app/routers/requisitions/core.py`

**Context:** The codebase has StrEnum constants in `app/constants.py` but many files use raw strings, some of which are INVALID values that silently produce empty query results. This is the highest-impact fix.

**Invalid status strings to fix:**
- `eight_by_eight_jobs.py:179` — `"open"`, `"in_progress"` are NOT valid `RequisitionStatus`. Use `RequisitionStatus.ACTIVE`, `RequisitionStatus.SOURCING`, `RequisitionStatus.OFFERS`
- `task_jobs.py:27` — `"open"`, `"rfq_sent"` are NOT valid `RequisitionStatus`. Use `RequisitionStatus.ACTIVE`, `RequisitionStatus.SOURCING`
- `proactive_service.py:619,687` — `"approved"`, `"po_entered"` are NOT valid `BuyPlanStatus`. Use `BuyPlanStatus.ACTIVE`
- `sighting_status.py:62` — `"delivered"` is NOT valid `ContactStatus`. Use `ContactStatus.SENT`
- `knowledge_service.py:897` — `"in_progress"` is NOT valid `RequisitionStatus`. Use `RequisitionStatus.SOURCING`
- `requisitions/core.py:80,358` — `"closed"` is NOT valid `RequisitionStatus`. Use `RequisitionStatus.ARCHIVED`
- `htmx_views.py:4162,5342,8662` — `"open"` is NOT valid `RequisitionStatus`. Remove from these .in_() filters (other values in filter are valid)
- `htmx_views.py:5124` — `"followed_up"` is NOT valid `ContactStatus`. Use `ContactStatus.RESPONDED`
- `email_service.py:898` — `"parsed"` is NOT in `VendorResponseStatus`. Use `VendorResponseStatus.REVIEWED`
- `requisition_list_service.py:323` — `"closed"` is NOT valid `RequisitionStatus`. Use `RequisitionStatus.CANCELLED`

Also grep ALL jobs files for any remaining raw status strings and replace with enum imports.

- [ ] **Step 1:** Read each file and identify exact lines with phantom statuses
- [ ] **Step 2:** Add `from app.constants import RequisitionStatus, BuyPlanStatus, ContactStatus` imports to each file
- [ ] **Step 3:** Replace each invalid string with the correct StrEnum constant. For ambiguous cases, match the INTENT:
  - `"open"` in requisition context → `RequisitionStatus.ACTIVE` (the initial active state)
  - `"in_progress"` in requisition context → `RequisitionStatus.SOURCING`
  - `"rfq_sent"` → `RequisitionStatus.SOURCING` (requisition is being sourced)
  - `"closed"` → `RequisitionStatus.ARCHIVED`
  - `"delivered"` in contact context → `ContactStatus.SENT`
  - `"approved"` in buy plan context → `BuyPlanStatus.ACTIVE` (approved plans become active)
  - `"po_entered"` in buy plan context → `BuyPlanStatus.ACTIVE`
- [ ] **Step 4:** Grep all `app/jobs/*.py` for any remaining quoted status strings that should be enums. Fix any found.
- [ ] **Step 5:** Run tests: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_jobs*.py tests/test_proactive*.py tests/test_requisition*.py -v --timeout=30 -x`
- [ ] **Step 6:** Commit: `git commit -m "fix: replace phantom status strings with StrEnum constants across jobs and services"`

---

## Task 2: Fix Script Injection in AI Endpoints

**Files:**
- Modify: `app/routers/htmx_views.py` (lines ~2365 and ~7985)

**Context:** AI-generated content is injected into inline `<script>` tags. The escaping at line 2365 handles backtick/backslash/dollar but NOT `</script>` tag injection. A Claude response containing `</script><script>alert(1)</script>` executes arbitrary JS.

- [ ] **Step 1:** Read `htmx_views.py` around lines 2355-2375 and 7975-8000
- [ ] **Step 2:** At line ~2365 (ai_cleanup_email), add `</` escaping AFTER existing escapes:
```python
escaped = cleaned.replace("\\", "\\\\").replace("`", "\\`").replace("$", "\\$").replace("</", "<\\/")
```
- [ ] **Step 3:** At line ~7985 (proactive_draft), ensure `json.dumps` uses `ensure_ascii=True` for all AI-sourced values to prevent unicode escape bypass:
```python
subject_json = json.dumps(subject, ensure_ascii=True).replace("</", "<\\/")
body_json = json.dumps(body, ensure_ascii=True).replace("</", "<\\/")
```
- [ ] **Step 4:** Run related tests: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_routers_htmx*.py -v -k "cleanup_email or proactive" --timeout=30 -x`
- [ ] **Step 5:** Commit: `git commit -m "fix(security): prevent script injection from AI-generated content in inline scripts"`

---

## Task 3: Add Admin Gate to Error Report Endpoints

**Files:**
- Modify: `app/routers/error_reports.py`

**Context:** Lines 305-488 have four endpoints (list, get, analyze, update) that only require `require_user`. Any buyer can read all tickets, trigger paid AI analysis, and update ticket status. These should require `require_admin`.

- [ ] **Step 1:** Read `error_reports.py` lines 300-490
- [ ] **Step 2:** Change `Depends(require_user)` to `Depends(require_admin)` on these four endpoints:
  - `GET /api/error-reports` (list)
  - `GET /api/error-reports/{report_id}` (get)
  - `POST /api/trouble-tickets/analyze` (analyze)
  - `PATCH /api/error-reports/{report_id}` (update)
- [ ] **Step 3:** Add import if not present: `from ..dependencies import require_admin`
- [ ] **Step 4:** Run tests: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_error_reports*.py -v --timeout=30 -x`
- [ ] **Step 5:** Commit: `git commit -m "fix(security): require admin role for trouble ticket management endpoints"`

---

## Task 4: Fix Proactive Email Send-Failure Marking as "sent"

**Files:**
- Modify: `app/services/proactive_service.py`

**Context:** At line 363, when email send fails, execution falls through to line 367-368 which marks all matches as `status = "sent"`. Customers never get the email but the system thinks they did.

- [ ] **Step 1:** Read `proactive_service.py` lines 345-380
- [ ] **Step 2:** Wrap the status update in the success path only:
```python
    try:
        await gc.post_json(...)
        logger.info(f"Proactive offer #{po.id} sent to {', '.join(recipient_emails)}")
        # Update match statuses only on successful send
        for m in matches:
            m.status = ProactiveMatchStatus.SENT
    except Exception as e:
        logger.error(f"Failed to send proactive offer email: {e}")
        for m in matches:
            m.status = "failed"
        return  # Don't update throttle entries either
```
- [ ] **Step 3:** Also add `from app.constants import ProactiveMatchStatus` and replace all raw `"sent"` strings in this file with the enum
- [ ] **Step 4:** Run tests: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_proactive*.py -v --timeout=30 -x`
- [ ] **Step 5:** Commit: `git commit -m "fix: don't mark proactive matches as sent when email delivery fails"`

---

## Task 5: Fix Email Sanitizer — Tracking Pixels and Tabnabbing

**Files:**
- Modify: `app/template_env.py`

**Context:** The `_sanitize_html_filter` allows `<img src>` (enables tracking pixels) and strips `rel` from `<a>` tags (enables reverse tabnabbing).

- [ ] **Step 1:** Read `template_env.py` lines 125-150
- [ ] **Step 2:** Remove `"img"` from allowed tags set. Remove `"src"` from img attribute allowlist. If `"img"` has its own entry in the `attributes` dict, remove that too.
- [ ] **Step 3:** Add `"rel"` to the `"a"` attribute set. If nh3 is used, add `link_rel="noopener noreferrer"` parameter to the `nh3.clean()` call.
- [ ] **Step 4:** Run tests: `TESTING=1 PYTHONPATH=/root/availai pytest tests/ -v -k "sanitize or template_env" --timeout=30 -x`
- [ ] **Step 5:** Commit: `git commit -m "fix(security): remove tracking pixel support and fix reverse tabnabbing in email sanitizer"`

---

## Task 6: Fix Silent Failures in Email Service

**Files:**
- Modify: `app/email_service.py`

**Context:** Two issues:
1. Line 620-625: Batch commit failure returns `[]`, hiding data loss
2. Line 1061-1181: Single `except Exception` wraps entire offer creation loop — one failure kills all offers

- [ ] **Step 1:** Read `email_service.py` lines 610-630 and 1050-1185
- [ ] **Step 2:** Fix batch commit (line 620-625): Re-raise after rollback so the caller can retry:
```python
    try:
        db.commit()
    except Exception as e:
        logger.error(f"Batch commit failed during inbox poll: {e}")
        db.rollback()
        raise  # Let caller handle retry / watermark advancement
```
- [ ] **Step 3:** Fix offer creation loop (lines 1061-1181): Move the try/except INSIDE the `for draft in draft_offers:` loop so individual failures don't block other offers:
  - The `try:` should start just inside `for draft in draft_offers:`
  - The `except Exception as e:` should be at the end of the for-loop body
  - Log at error level: `logger.error(f"Failed to create offer for {draft.get('mpn', '?')}: {e}", exc_info=True)`
  - Continue to next draft on failure
- [ ] **Step 4:** Run tests: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_email_service*.py -v --timeout=30 -x`
- [ ] **Step 5:** Commit: `git commit -m "fix: re-raise batch commit failure and isolate individual offer creation errors"`

---

## Task 7: Fix Lifecycle Job Missing Raise + Audit Silent Pass

**Files:**
- Modify: `app/jobs/lifecycle_jobs.py`
- Modify: `app/search_service.py`

**Context:**
1. `lifecycle_jobs.py:75` — Only job that doesn't re-raise, invisible to Sentry
2. `search_service.py:1440` — Empty `except Exception: pass` with `# pragma: no cover`

- [ ] **Step 1:** In `lifecycle_jobs.py`, add `raise` after `db.rollback()` at line 77:
```python
    except Exception:
        logger.exception("lifecycle_sweep failed")
        db.rollback()
        raise
    finally:
        db.close()
```
- [ ] **Step 2:** In `search_service.py` line 1440, replace `pass` with logging:
```python
    except Exception:
        logger.warning("Audit log failed for card %s", card.normalized_mpn if hasattr(card, 'normalized_mpn') else 'unknown', exc_info=True)
```
Remove the `# pragma: no cover` comment.
- [ ] **Step 3:** Run tests: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_search_service*.py tests/test_lifecycle*.py -v --timeout=30 -x`
- [ ] **Step 4:** Commit: `git commit -m "fix: add missing raise in lifecycle job and logging in audit card creation"`

---

## Task 8: Upgrade DEBUG-Level Error Logging to WARNING

**Files:**
- Modify: `app/search_service.py` (lines ~1223, ~1656, ~1693)
- Modify: `app/routers/htmx_views.py` (lines ~1057, ~2797)

**Context:** Multiple error paths use `logger.debug()` which is filtered in production. These hide real failures.

- [ ] **Step 1:** In `search_service.py`, find and replace these three `logger.debug` calls in except blocks:
  - Line ~1223: `logger.debug("Tag propagation failed` → `logger.warning("Tag propagation failed`
  - Line ~1656: `logger.debug("Tag classification failed` → `logger.warning("Tag classification failed`
  - Line ~1693: `logger.debug("Background enrichment failed` → `logger.warning("Background enrichment failed`
- [ ] **Step 2:** In `htmx_views.py`, find and replace:
  - Line ~1057: `logger.debug("Auto-search failed` → `logger.warning("Auto-search failed`
  - Line ~2797: `logger.debug("Auto-search failed` → `logger.warning("Auto-search failed`
- [ ] **Step 3:** Also remove any `# pragma: no cover` from these except blocks
- [ ] **Step 4:** Run tests: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_search_service*.py tests/test_routers_htmx*.py -v --timeout=30 -x`
- [ ] **Step 5:** Commit: `git commit -m "fix: upgrade silent debug-level error logging to warning in production paths"`

---

## Task 9: Fix Enrichment Service Silent Failures

**Files:**
- Modify: `app/enrichment_service.py`
- Modify: `app/connectors/email_mining.py`

**Context:**
1. Line 326: Non-200 from Explorium returns empty `[]` with no logging
2. `email_mining.py:119`: `except Exception` should be `except IntegrityError`

- [ ] **Step 1:** In `enrichment_service.py` line 326, add logging before the return:
```python
        if resp.status_code != 200:
            logger.warning("Explorium contacts returned HTTP %d for %s", resp.status_code, domain)
            return []
```
- [ ] **Step 2:** In `email_mining.py` line 119, narrow the except clause:
```python
        from sqlalchemy.exc import IntegrityError
        # ... in the except block:
        except IntegrityError:
            db.rollback()  # or savepoint rollback
            logger.debug("Duplicate message %s, skipping", message_id)
```
  Keep a separate broader except that logs at warning level for unexpected errors.
- [ ] **Step 3:** Run tests: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_enrichment*.py tests/test_email_mining*.py -v --timeout=30 -x`
- [ ] **Step 4:** Commit: `git commit -m "fix: add logging for Explorium non-200 and narrow email_mining except to IntegrityError"`

---

## Task 10: Fix N+1 Queries

**Files:**
- Modify: `app/routers/htmx_views.py`

**Context:**
1. Line 2306-2308: One query per vendor card for contacts (N+1)
2. Line 5510-5515: Missing eager loads for `Quote.customer_site` and `CustomerSite.company`

- [ ] **Step 1:** Fix vendor contacts N+1 (line ~2285-2320). Instead of querying inside the loop, add `selectinload(VendorCard.vendor_contacts)` to the vendor_rows query. Then use `v.vendor_contacts[:5]` in the loop body instead of the separate query. Find the query that produces `vendor_rows` (should be a few lines above 2306) and add the option.
- [ ] **Step 2:** Fix buy plans N+1 (line 5510-5515). Add joinedload chain:
```python
    query = db.query(BuyPlan).options(
        joinedload(BuyPlan.quote).joinedload(Quote.customer_site).joinedload(CustomerSite.company),
        joinedload(BuyPlan.requisition),
        joinedload(BuyPlan.submitted_by),
        joinedload(BuyPlan.approved_by),
        joinedload(BuyPlan.lines),
    )
```
  Ensure imports: `from ..models.quotes import Quote` and `from ..models.crm import CustomerSite`
- [ ] **Step 3:** Run tests: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_routers_htmx*.py tests/test_buy_plan*.py -v --timeout=30 -x`
- [ ] **Step 4:** Commit: `git commit -m "perf: fix N+1 queries in vendor contacts and buy plans list"`

---

## Task 11: Fix Quote Number Race Condition

**Files:**
- Modify: `app/services/crm_service.py`

**Context:** `next_quote_number` reads the last quote and increments without locking. Concurrent requests can generate duplicates.

- [ ] **Step 1:** Read `crm_service.py` lines 10-30
- [ ] **Step 2:** Add `with_for_update()` locking and retry logic:
```python
def next_quote_number(db: Session) -> str:
    """Generate next sequential quote number: Q-YYYY-NNNN.

    Uses SELECT FOR UPDATE to prevent race conditions.
    """
    year = datetime.now(timezone.utc).year
    prefix = f"Q-{year}-"
    last = (
        db.query(Quote)
        .filter(Quote.quote_number.like(f"{prefix}%"))
        .order_by(Quote.id.desc())
        .with_for_update()
        .first()
    )
    if last:
        try:
            seq = int(last.quote_number.split("-")[-1]) + 1
        except ValueError:
            seq = 1
    else:
        seq = 1
    return f"{prefix}{seq:04d}"
```
- [ ] **Step 3:** Run tests: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_crm*.py tests/test_quotes*.py -v --timeout=30 -x`
- [ ] **Step 4:** Commit: `git commit -m "fix: add SELECT FOR UPDATE locking to prevent duplicate quote numbers"`

---

## Task 12: Fix Redis Init TTL + Vendor Affinity ILIKE + Mouser Import

**Files:**
- Modify: `app/search_service.py` (lines 82-113)
- Modify: `app/services/vendor_affinity_service.py` (line 177)
- Modify: `app/connectors/mouser.py` (line 95)

**Context:** Three smaller fixes:
1. Redis init permanently disables cache on temporary failure
2. AI response used in ILIKE without escaping
3. `import re` inside for-loop

- [ ] **Step 1:** Fix Redis TTL retry. Replace the permanent `_search_redis_attempted` flag with a timestamp-based retry:
```python
_search_redis_last_attempt = 0.0
_REDIS_RETRY_INTERVAL = 300  # 5 minutes

def _get_search_redis():
    global _search_redis, _search_redis_last_attempt
    import time
    now = time.monotonic()
    if _search_redis is not None:
        return _search_redis
    if now - _search_redis_last_attempt < _REDIS_RETRY_INTERVAL:
        return None
    _search_redis_last_attempt = now
    # ... rest of existing init code
```
- [ ] **Step 2:** Fix vendor_affinity ILIKE (line 177):
```python
from ..utils.sql_helpers import escape_like
# In the query:
.filter(MaterialCard.category.ilike(f"%{escape_like(category)}%"))
```
- [ ] **Step 3:** Move `import re` in mouser.py from line 95 to the top of the file (module-level imports)
- [ ] **Step 4:** Run tests: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_search_service*.py tests/test_vendor_affinity*.py tests/test_mouser*.py -v --timeout=30 -x`
- [ ] **Step 5:** Commit: `git commit -m "fix: Redis retry TTL, escape ILIKE in vendor affinity, move mouser import to module level"`

---

## Task 13: Remove CSRF Exemption + Seed Agent User

**Files:**
- Modify: `app/main.py` (line 269)
- Modify: `app/startup.py`

**Context:**
1. `quick-create` CSRF exemption allows cross-site form POST
2. Agent user `agent@availai.local` not seeded — valid API key silently 401s

- [ ] **Step 1:** In `main.py` line 269, change the CSRF exemption to only exempt `lookup` (read-only), not `quick-create`:
```python
re.compile(r"/v2/partials/customers/lookup"),  # AI company lookup (read-only)
```
- [ ] **Step 2:** In `startup.py`, add agent user seeding to `run_startup_migrations()`. Add near the other seed calls (around line 70):
```python
    _seed_agent_user()
```
Then add the function:
```python
def _seed_agent_user() -> None:
    """Seed the agent service account if it doesn't exist."""
    from .models.auth import User
    from .constants import UserRole
    db = SessionLocal()
    try:
        existing = db.query(User).filter_by(email="agent@availai.local").first()
        if existing:
            return
        user = User(email="agent@availai.local", name="Agent", role=UserRole.ADMIN, is_active=True)
        db.add(user)
        db.commit()
        logger.info("Seeded agent service account")
    except Exception:
        logger.exception("Failed seeding agent user")
        db.rollback()
    finally:
        db.close()
```
- [ ] **Step 3:** Run tests: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_routers_auth*.py tests/test_startup*.py -v --timeout=30 -x`
- [ ] **Step 4:** Commit: `git commit -m "fix: remove quick-create CSRF exemption and seed agent service account"`

---

## Task 14: Run Full Test Suite and Lint

- [ ] **Step 1:** Run full test suite: `TESTING=1 PYTHONPATH=/root/availai pytest tests/ -v --timeout=30 -x`
- [ ] **Step 2:** Run linter: `cd /root/availai && python3 -m ruff check app/`
- [ ] **Step 3:** Fix any failures from the above
- [ ] **Step 4:** Final commit if any lint fixes needed
