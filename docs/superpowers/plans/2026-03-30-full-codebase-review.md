# Full Codebase Review Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Comprehensive quality sweep of the entire AvailAI codebase — find and fix all bugs, security issues, dead code, performance problems, frontend anti-patterns, data integrity gaps, type errors, and Docker stability issues.

**Architecture:** 4-phase hybrid audit. Pre-sweep fixes test infrastructure for a clean baseline. Phase 1 runs static tools. Phase 2 dispatches 7 read-only specialist agents in parallel. Phase 3 fixes all findings with parallel subagents. Phase 4 verifies clean state.

**Tech Stack:** Python 3.11, FastAPI, SQLAlchemy 2.0, HTMX 2.x, Alpine.js 3.x, Docker Compose, pytest, ruff, mypy, bandit

**Spec:** `docs/superpowers/specs/2026-03-30-full-codebase-review-design.md`

---

## File Map

This is an audit — no new files are planned upfront. Files to modify will be determined by agent findings. The only planned outputs are:

- **Create:** `docs/audit-2026-03-30.md` — final audit report
- **Modify:** Various `app/` and `tests/` files based on findings

---

### Task 1: Pre-Sweep — Triage and Fix Test Failures

**Goal:** Reduce 244 test failures to <20 so Phase 4 verify has a clean baseline.

**Files:**
- Read: All files in `tests/` that have failures
- Modify: Test files and app code as needed

- [ ] **Step 1: Collect and categorize all test failures**

```bash
cd /root/availai
TESTING=1 PYTHONPATH=/root/availai pytest tests/ --tb=line -q 2>&1 | grep "FAILED" > /tmp/all_failures.txt
wc -l /tmp/all_failures.txt
```

Then group by file:
```bash
cat /tmp/all_failures.txt | sed 's/.*FAILED //' | sed 's/::.*//' | sort | uniq -c | sort -rn > /tmp/failures_by_file.txt
cat /tmp/failures_by_file.txt
```

Then group by error pattern:
```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/ --tb=short -q 2>&1 | grep -A2 "FAILED\|Error\|assert" > /tmp/failure_patterns.txt
```

- [ ] **Step 2: Identify cascade failures**

Look for files with 10+ failures — these are likely caused by one broken fixture or import. Read those test files and the fixtures they depend on in `tests/conftest.py`. Fix the root cause (fixture, import, or model change that broke the contract).

- [ ] **Step 3: Fix fixture/infrastructure failures**

These are tests that fail because of test setup, not because of actual bugs. Common patterns:
- Missing fixture (fixture was renamed or removed)
- Import error (module moved or renamed)
- Database schema mismatch (model changed, test uses old column names)
- Missing mock (external API call not mocked)

Fix each root cause. After each fix, run the affected test file:
```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/<affected_file>.py -v --tb=short
```

- [ ] **Step 4: Fix stale assertion failures**

Tests where the code is correct but the test expectation is outdated. Update assertions to match current behavior. Only do this when the current behavior is demonstrably correct.

- [ ] **Step 5: Document genuinely broken tests**

Any test that fails because of a real bug in app code — don't fix the test, document it. These will be caught by the agent sweep in Phase 2.

Write the list to `/tmp/known_failures.txt`.

- [ ] **Step 6: Run full suite and record baseline**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/ -v --tb=short -q 2>&1 | tail -5
```

Target: <20 failures. Record exact count as the pre-agent-sweep baseline.

- [ ] **Step 7: Commit test fixes**

```bash
cd /root/availai
git add tests/
git commit -m "test: fix test infrastructure — reduce failures from 244 to <N>

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: Phase 1 — Run Static Analysis Tools

**Goal:** Produce categorized output from bandit, mypy, and coverage tools.

- [ ] **Step 1: Run bandit security scan**

```bash
cd /root/availai
bandit -r app/ -f json -o /tmp/bandit_results.json -ll 2>&1
bandit -r app/ -f txt -ll 2>&1 | tail -30
```

The `-ll` flag shows only medium and high severity. Record the count and categories.

- [ ] **Step 2: Categorize mypy errors**

```bash
cd /root/availai
mypy app/ --ignore-missing-imports 2>&1 | grep -oP '\[.*\]' | sort | uniq -c | sort -rn > /tmp/mypy_categories.txt
cat /tmp/mypy_categories.txt
```

This shows the distribution — e.g., how many are `[attr-defined]` vs `[arg-type]` vs `[assignment]`.

Then extract Category C (real type bugs — the ones most likely to be actual runtime errors):
```bash
mypy app/ --ignore-missing-imports 2>&1 | grep '\[arg-type\]\|\[return-value\]\|\[call-overload\]\|\[override\]' > /tmp/mypy_category_c.txt
wc -l /tmp/mypy_category_c.txt
```

- [ ] **Step 3: Run coverage analysis**

```bash
cd /root/availai
TESTING=1 PYTHONPATH=/root/availai pytest tests/ --cov=app --cov-report=term-missing --tb=no -q 2>&1 | tail -50 > /tmp/coverage_report.txt
cat /tmp/coverage_report.txt
```

Identify files with <50% coverage — these are the highest-risk uncovered code paths.

- [ ] **Step 4: Summarize Phase 1 findings**

Create a summary file:
```bash
echo "=== PHASE 1 SUMMARY ===" > /tmp/phase1_summary.txt
echo "" >> /tmp/phase1_summary.txt
echo "--- Bandit ---" >> /tmp/phase1_summary.txt
bandit -r app/ -f txt -ll 2>&1 | grep "Severity\|Confidence\|Issue\|^>>" | head -50 >> /tmp/phase1_summary.txt
echo "" >> /tmp/phase1_summary.txt
echo "--- Mypy Categories ---" >> /tmp/phase1_summary.txt
cat /tmp/mypy_categories.txt >> /tmp/phase1_summary.txt
echo "" >> /tmp/phase1_summary.txt
echo "--- Low Coverage Files ---" >> /tmp/phase1_summary.txt
grep -E "^\s*app/.*\s+[0-4][0-9]%" /tmp/coverage_report.txt >> /tmp/phase1_summary.txt
cat /tmp/phase1_summary.txt
```

---

### Task 3: Phase 2 — Dispatch 7 Specialist Agents (All Parallel)

**Goal:** Scan the full codebase with 7 specialist lenses. All read-only — no fixes.

Each agent must output findings in this format:
```
[SEVERITY] file:line — description
```

**IMPORTANT:** All 7 agents must be dispatched in a SINGLE message for maximum parallelism. Do NOT wait for one to finish before launching the next.

- [ ] **Step 1: Dispatch all 7 agents in parallel**

Launch all of these in one message using the Agent tool:

**Agent 1: Security & Error Handling**
- Scope: `app/services/`, `app/routers/`, `app/jobs/`, `app/utils/`
- Search for: XSS vectors (innerHTML, `| safe` misuse), SQL injection (string formatting in queries), auth bypass (endpoints missing require_user/require_admin/require_buyer deps), secrets in code, bare `except:` blocks, empty catch blocks, functions that silently return None on failure, rate limiting gaps
- Feed it the bandit results from `/tmp/bandit_results.json` as additional context
- Output: list of `[SEVERITY] file:line — description` findings

**Agent 2: Architecture & Hygiene**
- Scope: Full codebase (`app/`)
- Search for: dead code (unused imports/functions/files), code duplication (>10 similar lines), files >500 lines, boundary violations (routers doing `db.query()` directly), circular imports, API contract inconsistency (bare dict returns, inconsistent error shapes), `os.environ` access outside `app/config.py`
- Output: list of findings

**Agent 3: Data Integrity**
- Scope: `app/models/`, `alembic/`, `app/services/`
- Search for: missing FK constraints, dangerous cascades, wrong nullability, missing unique constraints, missing indexes on FK columns, missing `@validates`, orphan risks on delete paths
- Output: list of findings

**Agent 4: Frontend Patterns**
- Scope: `app/templates/`, `app/static/`
- Search for: innerHTML usage, Alpine `_x_dataStack`, inconsistent hx-swap, missing hx-on::error, accessibility gaps (missing aria labels), Tailwind inconsistencies, template inheritance issues
- Output: list of findings

**Agent 5: Performance**
- Scope: `app/services/`, `app/routers/`, `app/models/`, `app/jobs/`
- Search for: N+1 queries, missing indexes, unbounded `.all()` queries, expensive per-request operations, missing caching opportunities, concurrency issues, memory leaks in jobs
- Output: list of findings

**Agent 6: Mypy Deep Dive**
- Scope: Full codebase (guided by `/tmp/mypy_categories.txt` and `/tmp/mypy_category_c.txt`)
- Task: Read the mypy output files. Categorize all 2,054 errors into A (annotations), B (third-party), C (real bugs), D (SQLAlchemy), E (Pydantic). For Category C, provide exact file:line and what the actual type mismatch is. For Category D, recommend whether to use mypy plugin, mapped_column syntax, or targeted ignores.
- Output: categorized findings with counts and specific fix recommendations

**Agent 7: Docker & Infrastructure Reliability**
- Scope: `Dockerfile`, `docker-compose.yml`, `deploy.sh`, `app/main.py`, `app/config.py`, `app/scheduler.py`, `app/database.py`
- Search for: Docker layer ordering (deps before code?), `.dockerignore` completeness, health check coverage, startup race conditions (app before DB ready), missing `depends_on` conditions, graceful shutdown handling, memory limits, connection pool settings (pool_size, max_overflow, pool_recycle, pool_pre_ping), APScheduler startup timing, Redis failure handling, log rotation, volume conflicts
- Output: list of findings

- [ ] **Step 2: Wait for all agents to complete**

Collect all 7 output files. Do not proceed to Phase 3 until all agents have reported.

- [ ] **Step 3: Aggregate findings**

Combine all agent outputs into one file:
```bash
cat /tmp/agent1_findings.txt /tmp/agent2_findings.txt ... /tmp/agent7_findings.txt > /tmp/all_findings.txt
```

Count findings by severity:
```bash
grep -c "^\[CRITICAL\]" /tmp/all_findings.txt
grep -c "^\[HIGH\]" /tmp/all_findings.txt
grep -c "^\[MEDIUM\]" /tmp/all_findings.txt
grep -c "^\[LOW\]" /tmp/all_findings.txt
```

Deduplicate by file:line (keep highest severity):
```bash
sort -t: -k1,2 -u /tmp/all_findings.txt > /tmp/deduped_findings.txt
```

Report the total to the user before proceeding to Phase 3.

---

### Task 4: Phase 3 — Fix All Findings

**Goal:** Fix every CRITICAL, HIGH, and MEDIUM finding. Fix LOW findings where practical.

- [ ] **Step 1: Build the file-touch map**

For each finding, extract the file path. Group findings by file. Identify contested files (files appearing in 2+ finding groups).

```
Exclusive files → assigned to their finding group's fix agent
Contested files → assigned to contested-files agent
```

- [ ] **Step 2: Dispatch fix agents in parallel**

Launch one subagent per finding group. Each agent gets:
- The list of findings assigned to it
- The exclusive file list it owns
- Instructions to: fix → run targeted tests → verify no regressions

**Fix groups (in priority order, but all launched in parallel):**
1. Security fixes
2. Type-C mypy bugs (real type errors)
3. Data integrity fixes (constraints, cascades, validators)
4. Docker & infrastructure fixes
5. Performance fixes (N+1, indexes)
6. Frontend fixes (innerHTML, Alpine anti-patterns)
7. Architecture fixes (dead code, duplication, boundary violations)
8. Type-A mypy annotations (mechanical, high volume)
9. Contested-files agent (all multi-concern files, sequenced: security → bugs → types → cleanup)

Each agent commits its own fixes with a descriptive message.

- [ ] **Step 3: Verify no conflicts between fix agents**

After all fix agents complete:
```bash
cd /root/availai
git log --oneline -20
git status
```

If any merge issues, resolve them.

---

### Task 5: Phase 4 — Verify Clean State

**Goal:** Confirm all tools pass and no regressions were introduced.

- [ ] **Step 1: Run full test suite**

```bash
cd /root/availai
TESTING=1 PYTHONPATH=/root/availai pytest tests/ -v --tb=short -q 2>&1 | tail -10
```

Expected: 0 failures (or only the documented known failures from Task 1 Step 5).

- [ ] **Step 2: Run ruff**

```bash
ruff check app/
```

Expected: 0 errors (was already clean).

- [ ] **Step 3: Run mypy and compare**

```bash
mypy app/ --ignore-missing-imports 2>&1 | tail -3
```

Expected: significantly fewer than 2,054 errors. Target: <200 (justified ignores only).

Compare to baseline:
```bash
echo "Baseline: 2054 errors"
mypy app/ --ignore-missing-imports 2>&1 | grep "Found"
```

- [ ] **Step 4: Run bandit and compare**

```bash
bandit -r app/ -f txt -ll 2>&1 | tail -10
```

Expected: 0 medium/high findings.

- [ ] **Step 5: Generate audit report**

Create `docs/audit-2026-03-30.md` with:

```markdown
# Full Codebase Audit — 2026-03-30

## Baseline
| Tool | Before | After |
|------|--------|-------|
| ruff | 0 errors | 0 errors |
| mypy | 2,054 errors | <N> errors |
| pytest | 244 fail / 10,184 pass | <N> fail / <N> pass |
| bandit | <N> findings | <N> findings |

## Findings by Agent
### Agent 1: Security & Error Handling
- [list findings and resolutions]

### Agent 2: Architecture & Hygiene
- [list findings and resolutions]

... (all 7 agents)

## Commits
- [list all fix commits with SHAs]

## Remaining Items
- [any LOW findings deferred, with justification]
```

- [ ] **Step 6: Commit audit report**

```bash
cd /root/availai
git add docs/audit-2026-03-30.md
git commit -m "docs: full codebase audit report 2026-03-30

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

- [ ] **Step 7: Final status report to user**

Present:
- Before/after metrics table
- Total findings: N found, N fixed, N deferred
- Key wins (biggest bugs caught, security issues resolved)
- Remaining items (if any)
