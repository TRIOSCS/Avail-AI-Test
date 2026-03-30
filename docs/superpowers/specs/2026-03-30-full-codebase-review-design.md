# Full Codebase Review — Design Spec

**Date:** 2026-03-30
**Goal:** Comprehensive quality sweep of the entire AvailAI codebase — catch bugs, security issues, dead code, performance problems, frontend anti-patterns, data integrity gaps, and type errors. Fix everything found.

---

## Baseline (as of 2026-03-30)

| Tool | Result |
|------|--------|
| ruff | Clean — 0 errors |
| mypy | 2,054 errors in 140 files |
| pytest | 10,184 pass / 244 fail / 29 skipped (176s) |
| bandit | Not yet run |
| Codebase | 314 Python files, 180 templates, 200+ endpoints, 73 models |
| Last audit | 2026-03-26 (120 commits ago) — all findings fixed at that time |

---

## Architecture: 4-Phase Hybrid Audit

```
Pre-Sweep (fix test infra) → Phase 1 (static tools) → Phase 2 (6 agents parallel) → Phase 3 (fix all) → Phase 4 (verify clean)
```

### Pre-Sweep: Test Infrastructure Fix

**Goal:** Reduce 244 test failures to a clean baseline before agents scan.

**Why first:** Agents need a reliable test suite to validate against. If 244 tests are already failing, Phase 4 verify can't distinguish pre-existing failures from regressions.

**Steps:**
1. Run `pytest --tb=line 2>&1 | grep FAILED` to list all failures
2. Group by error pattern (fixture issues, import errors, stale assertions, real bugs)
3. Fix cascade/fixture issues first (one fixture fix often clears 50+ tests)
4. Fix stale/wrong tests (update assertions to match current code)
5. Document any genuinely broken tests as known issues
6. Target: <20 failures remaining before agent sweep

### Phase 1: Static Analysis

Run all static tools and produce summary files. Most already done.

| Tool | Command | Purpose |
|------|---------|---------|
| ruff | `ruff check app/` | Lint (already clean) |
| mypy | `mypy app/ --ignore-missing-imports` | Type errors — produce categorized summary |
| bandit | `bandit -r app/ -f json -o /tmp/bandit.json` | Security static analysis |
| pytest --cov | `pytest tests/ --cov=app --cov-report=term-missing --tb=no -q` | Coverage gaps |

**Mypy categorization** (bash, not agent):
```bash
mypy app/ 2>&1 | grep -oP '\[.*\]' | sort | uniq -c | sort -rn > /tmp/mypy_categories.txt
```
This tells us whether it's 80% `[attr-defined]` or scattered real bugs.

### Phase 2: Agent Sweep (7 specialists, all parallel, READ-ONLY)

All agents scan and report findings only. No fixes during this phase. Each agent outputs findings as:
```
[SEVERITY] file:line — description
```
Where severity is CRITICAL, HIGH, MEDIUM, or LOW.

#### Agent 1: Security & Error Handling

**Scope:** `app/services/`, `app/routers/`, `app/jobs/`, `app/utils/`

**Search for:**
- XSS vectors: innerHTML, unescaped user input in templates, `| safe` filter misuse
- SQL injection: raw SQL with string formatting, unsanitized query params
- Auth bypass: endpoints missing `require_user`/`require_admin`/`require_buyer` deps
- Secrets: hardcoded API keys, tokens in code, credentials in default values
- Swallowed exceptions: bare `except:`, `except Exception: pass`, empty catch blocks
- Missing error propagation: functions that silently return None on failure
- Rate limiting gaps: endpoints that should be rate-limited but aren't

#### Agent 2: Architecture & Hygiene

**Scope:** Full codebase

**Search for:**
- Dead code: unused imports, unreachable functions, commented-out blocks, stale feature flags
- Duplication: near-identical code blocks (>10 lines) across files
- Oversized files: any file >500 lines that could be split
- Boundary violations: routers accessing `db.query()` directly instead of through services
- Circular imports: import chains that create circular dependencies
- API contract consistency: endpoints returning bare dicts instead of Pydantic models, inconsistent error response shapes, missing pagination params
- Config sprawl: `os.environ` access outside `app/config.py`

#### Agent 3: Data Integrity

**Scope:** `app/models/`, `app/migrations/`, `app/services/` (write paths)

**Search for:**
- Missing FK constraints: relationships without proper cascade rules
- Dangerous cascades: `cascade="all, delete-orphan"` that could silently destroy data
- Nullable columns that should be NOT NULL (or vice versa)
- Missing unique constraints: natural keys without uniqueness enforcement
- Missing indexes: FK columns without indexes, frequently-queried columns without indexes
- Missing `@validates` decorators: fields that accept invalid values (wrong enum, negative numbers, empty strings where content is required)
- Orphan risk: delete paths that don't clean up related records

#### Agent 4: Frontend Patterns

**Scope:** `app/templates/`, `app/static/`

**Search for:**
- `innerHTML` usage (should be `htmx.ajax()` or Alpine reactive binding)
- Alpine `_x_dataStack` (should be `Alpine.store()`)
- Inconsistent HTMX patterns: mixed `hx-swap` strategies, missing `hx-indicator`, broken partial chains
- Missing error states: HTMX requests without `hx-on::error` or toast handling
- Accessibility: missing aria labels, no keyboard navigation on interactive elements
- Tailwind inconsistencies: mixed custom CSS where Tailwind classes exist
- Template inheritance issues: partials that don't extend the right base

#### Agent 5: Performance

**Scope:** `app/services/`, `app/routers/`, `app/models/`, `app/jobs/`

**Search for:**
- N+1 queries: loops that execute queries per iteration instead of batch/join
- Missing indexes: slow query patterns without supporting indexes
- Unbounded queries: `db.query(Model).all()` without limits on large tables
- Hot-path bloat: expensive operations in per-request paths (file I/O, external API calls without caching)
- Missing caching: repeated identical queries that could use `@cached_endpoint`
- Concurrency issues: shared mutable state, missing locks on concurrent access
- Memory leaks: unbounded data structures, missing cleanup in long-running jobs

#### Agent 6: Mypy Deep Dive

**Scope:** Full codebase (guided by mypy output)

**Categorize all 2,054 errors into:**

| Category | Description | Action |
|----------|-------------|--------|
| **C (Bugs)** | Real type mismatches — function expects str, receives Optional[str], None not handled | Fix all — these are actual bugs |
| **A (Annotations)** | Missing return types, untyped function defs | Fix mechanically — low risk |
| **D (SQLAlchemy)** | ORM relationship typing friction, Column[str] vs str | Fix with mypy plugin config or targeted ignores |
| **E (Pydantic)** | v1/v2 compatibility issues | Fix with model_config patterns |
| **B (Third-party)** | Errors from untyped external libraries | Add `# type: ignore[import]` with comment |

**Priority:** Fix Category C first (real bugs). Then A (volume reduction). Then D and E (architectural). Category B last (justified ignores).

**Output:** Categorized list with counts and specific fix instructions per category.

#### Agent 7: Docker & Infrastructure Reliability

**Scope:** `docker-compose.yml`, `Dockerfile`, `deploy.sh`, `app/main.py` (lifespan/startup), `app/config.py`, `app/scheduler.py`, `app/database.py`

**Problems to diagnose:**
- **Stale code after deploy:** Verify `deploy.sh` always uses `--no-cache` and `--force-recreate`. Check for any code paths that bypass deploy.sh. Check Docker layer caching — are dependencies installed before code copy (proper layer ordering)? Is `.dockerignore` excluding `__pycache__`, `.git`, `node_modules` to prevent cache-busting?
- **Random instability:** Container restart loops, OOM kills, zombie processes. Check for:
  - Missing health checks or health checks that don't actually verify the app is ready
  - Startup race conditions (app starts before DB is ready, Redis not connected)
  - Missing `depends_on` with `condition: service_healthy` in docker-compose
  - No graceful shutdown handling (SIGTERM not caught, connections not drained)
  - Memory limits not set (container grows unbounded until OOM)
  - Worker processes spawning without limits
  - APScheduler jobs running during startup before app is fully initialized
  - Database connection pool exhaustion (pool too small, connections not returned)
  - Redis connection failures not handled gracefully (should degrade, not crash)
  - Volume mount conflicts between containers
  - Log files growing unbounded inside container (no rotation)

**Search for:**
- `Dockerfile`: Layer ordering (COPY requirements before code?), multi-stage builds, proper CMD/ENTRYPOINT
- `docker-compose.yml`: Health checks on all services, restart policies, memory limits, `depends_on` conditions, volume configs
- `deploy.sh`: Verify full pipeline (--no-cache, --force-recreate, health wait, log tail)
- `app/main.py`: Lifespan startup/shutdown — proper connection cleanup, scheduler shutdown
- `app/database.py`: Connection pool settings (pool_size, max_overflow, pool_recycle, pool_pre_ping)
- `app/scheduler.py`: Graceful shutdown, job overlap prevention, startup timing
- Background workers: Any threads/processes that don't get cleaned up on shutdown

**Output:** Findings + specific fixes for each stability issue found.

### Phase 3: Fix (subagent-driven, max parallel)

**Input:** Aggregated findings from all 7 agents + bandit + mypy categories.

**Deduplication:** Multiple agents may flag the same file:line. Deduplicate by `(file_path, line_number)` — keep the highest-severity finding.

**Grouping strategy — hybrid (finding type + file exclusivity):**

1. Each finding group gets an exclusive file list
2. Files appearing in only one group → assigned to that group's fix agent
3. Files appearing in multiple groups → assigned to a **contested-files agent** that applies all fixes in sequence: security first, then bugs, then type annotations, then cleanup

**Fix groups:**
- Security fixes (highest priority, runs first)
- Type-C mypy bugs (real type errors)
- Data integrity fixes (constraint/cascade/validator additions)
- Performance fixes (N+1, missing indexes)
- Frontend fixes (innerHTML, Alpine anti-patterns)
- Architecture fixes (dead code, duplication, boundary violations)
- Type-A mypy annotations (mechanical, high volume)
- Docker & infrastructure fixes (Dockerfile, compose, deploy, startup)
- Contested-files agent (multi-concern files, sequenced)

Each fix agent: fix → run targeted tests → verify no regressions.

### Phase 4: Verify

Run full tool suite and compare to baseline:

| Tool | Baseline | Target |
|------|----------|--------|
| ruff | 0 errors | 0 errors |
| mypy | 2,054 errors | <200 (Category B/D justified ignores only) |
| pytest | 244 fail | 0 fail (after pre-sweep + fixes) |
| bandit | unknown | 0 high/medium findings |

Generate audit report at `docs/audit-2026-03-30.md`:
- Findings by category and severity
- What was fixed (with file:line references)
- Before/after metrics
- Any remaining items with justification

---

## Success Criteria

- All CRITICAL and HIGH findings fixed
- All MEDIUM findings fixed (per standing "fix all" rule)
- LOW findings fixed or documented with justification
- Test suite: 0 failures
- ruff: clean
- mypy: <200 errors (justified ignores only)
- bandit: 0 high/medium
- No regressions from fixes

---

## Execution Approach

- Pre-sweep: 1 subagent for test infrastructure fixes
- Phase 1: 4 parallel tool runs (bash)
- Phase 2: 7 parallel read-only agents
- Phase 3: 9+ parallel fix agents (one per finding group)
- Phase 4: sequential verification

Total estimated agents: ~17
All phases use subagent-driven development with maximum parallelism.

---

## Constraints

- Phase 2 agents are READ-ONLY — no edits, no commits
- Phase 3 fix agents get exclusive file lists — no merge conflicts
- Contested files handled by a single sequenced agent
- Fix priority: security > bugs > data integrity > performance > frontend > hygiene > annotations
- Every fix verified by targeted tests before moving on
