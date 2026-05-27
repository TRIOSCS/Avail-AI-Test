# Stale Memory Cleanup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Eliminate all stale code snapshots from Claude Code's memory system, CLAUDE.md, and session cache to prevent wrong edits caused by outdated assumptions.

**Architecture:** Delete 5 memory app_map files, strip stale counts/lists from CLAUDE.md, purge orphaned session cache, add a guardrail feedback memory that prevents recreation.

**Tech Stack:** File operations only — no code changes, no tests needed.

---

### Task 1: Delete Memory App Map Files

**Files:**
- Delete: `/root/.claude/projects/-root/memory/app_map_architecture.md`
- Delete: `/root/.claude/projects/-root/memory/app_map_models.md`
- Delete: `/root/.claude/projects/-root/memory/app_map_patterns.md`
- Delete: `/root/.claude/projects/-root/memory/app_map_routes.md`
- Delete: `/root/.claude/projects/-root/memory/app_map_templates.md`

- [ ] **Step 1: Delete all 5 app_map files**

```bash
rm /root/.claude/projects/-root/memory/app_map_architecture.md
rm /root/.claude/projects/-root/memory/app_map_models.md
rm /root/.claude/projects/-root/memory/app_map_patterns.md
rm /root/.claude/projects/-root/memory/app_map_routes.md
rm /root/.claude/projects/-root/memory/app_map_templates.md
```

- [ ] **Step 2: Verify deletion**

```bash
ls /root/.claude/projects/-root/memory/app_map_*.md
```

Expected: `No such file or directory`

---

### Task 2: Strip workflow_page_names.md to Vocabulary Only

**Files:**
- Modify: `/root/.claude/projects/-root/memory/workflow_page_names.md`

- [ ] **Step 1: Replace file contents with vocabulary-only version**

Write this exact content to `/root/.claude/projects/-root/memory/workflow_page_names.md`:

```markdown
---
name: Page Name Vocabulary
description: User refers to pages by bottom nav labels — map to concepts without asking for clarification
type: feedback
---

When the user says these names, they mean these pages. No clarification needed — go find the current route by reading the code.

**Bottom bar (5 primary slots):**
| User Says | Means |
|-----------|-------|
| "Reqs" | Requisitions page |
| "Search" | Sourcing search page |
| "Buy Plans" | Buy plans page |
| "Vendors" | Vendors list page |
| "Companies" | Companies list page |

**More menu:**
| User Says | Means |
|-----------|-------|
| "Proactive" | Proactive matching page |
| "My Vendors" | User's vendor list page |
| "Quotes" | Quotes page |
| "Prospecting" | Prospecting page |
| "Settings" | Settings page |

To find the actual route, partial, or template for any page: search `htmx_views.py` for the page name. Do NOT trust cached paths from memory.
```

- [ ] **Step 2: Verify the file reads correctly**

```bash
cat /root/.claude/projects/-root/memory/workflow_page_names.md
```

Expected: No file paths, no route strings, no template names — just vocabulary.

---

### Task 3: Add Guardrail Feedback Memory

**Files:**
- Create: `/root/.claude/projects/-root/memory/feedback_no_code_in_memory.md`

- [ ] **Step 1: Create the guardrail memory file**

Write this exact content to `/root/.claude/projects/-root/memory/feedback_no_code_in_memory.md`:

```markdown
---
name: Never store code structure in memory
description: Memory must never contain file paths, function names, model lists, route lists, template lists, counts, or any concrete code references — always read the actual codebase
type: feedback
---

Never store or trust code structure, file paths, function names, model schemas,
route definitions, template lists, counts, or version numbers in memory files.

**Why:** Stale app_map memory files caused persistent wrong edits — Claude edited
files that had moved, referenced routers that were consolidated (memory said 34,
reality was 22), and skipped exploring because it thought it already knew the answer.

**How to apply:**
- If asked to "save the architecture" or "remember the models" → refuse and explain why
- If you find yourself about to write a count, path, or function name into a memory file → stop
- Memory is for: decisions, preferences, vocabulary, project goals, workflow rules
- Memory is NOT for: anything derivable by reading the current codebase
- At the start of any task involving code changes, read the actual files — never rely on what you "remember"
- If a plan or spec references specific line numbers, verify them before acting
```

- [ ] **Step 2: Verify the file exists and reads correctly**

```bash
cat /root/.claude/projects/-root/memory/feedback_no_code_in_memory.md
```

---

### Task 4: Update MEMORY.md Index

**Files:**
- Modify: `/root/.claude/projects/-root/memory/MEMORY.md`

- [ ] **Step 1: Replace MEMORY.md with updated index**

Write this exact content to `/root/.claude/projects/-root/memory/MEMORY.md`:

```markdown
# MEMORY

## Philosophy
- [philosophy.md](philosophy.md) — North star: quality > speed, elegant frontend, follow patterns, current code is truth

## Workflow
- [workflow_tooling.md](workflow_tooling.md) — Always subagent, max parallelism, full pipeline, PR review agents, environment
- [workflow_page_names.md](workflow_page_names.md) — Bottom nav label → user vocabulary mapping (no file paths)

## Project
- [project_approved_product_direction.md](project_approved_product_direction.md) — HTMX+Alpine.js stack (NOT React)
- [project_enrichment_confidence.md](project_enrichment_confidence.md) — On-demand enrichment, 98% confidence target
- [project_crm_redesign.md](project_crm_redesign.md) — 4-phase CRM redesign roadmap

## Feedback
- [feedback_deploy_cache.md](feedback_deploy_cache.md) — Always --no-cache on deploy; Docker cached stale templates
- [feedback_no_code_in_memory.md](feedback_no_code_in_memory.md) — NEVER store code structure, paths, counts, or function names in memory
```

---

### Task 5: Clean CLAUDE.md — Remove All Stale Counts and Lists

**Files:**
- Modify: `/root/availai/CLAUDE.md`

This is the largest task. Each step is one edit to remove a specific stale reference.

- [ ] **Step 5.1: Remove duplicated version from header**

Change line 4 from:
```
**VERSION:** 3.1.0 (source of truth: `app/config.py` → `APP_VERSION`)
```
To:
```
**VERSION:** See `app/config.py` → `APP_VERSION`
```

- [ ] **Step 5.2: Remove stale counts from Tech Stack table**

Change the tech stack table entries:
```
| **Database** | PostgreSQL 16 | Primary data store (109+ migrations) |
```
To:
```
| **Database** | PostgreSQL 16 | Primary data store (see `alembic/versions/`) |
```

Change:
```
| **ORM** | SQLAlchemy 2.0 | Type-safe database access, 73 models |
```
To:
```
| **ORM** | SQLAlchemy 2.0 | Type-safe database access (see `app/models/`) |
```

Change:
```
| **Templates** | Jinja2 | Server-side rendering (188 templates) |
```
To:
```
| **Templates** | Jinja2 | Server-side rendering (see `app/templates/`) |
```

Change:
```
| **Scheduling** | APScheduler | Background jobs (14 modules) |
```
To:
```
| **Scheduling** | APScheduler | Background jobs (see `app/jobs/`) |
```

Change:
```
| **Testing** | pytest + Playwright | 8,553 tests, E2E coverage |
```
To:
```
| **Testing** | pytest + Playwright | Comprehensive test suite, E2E coverage |
```

- [ ] **Step 5.3: Remove stale counts from Project Structure tree**

Change line 133:
```
├── main.py                    # FastAPI app, 34 routers, middleware stack, lifespan
```
To:
```
├── main.py                    # FastAPI app, router registration, middleware stack, lifespan
```

Change line 137:
```
├── constants.py               # StrEnum status enums (19 enums) — ALWAYS use, never raw strings
```
To:
```
├── constants.py               # StrEnum status enums — ALWAYS use, never raw strings
```

Change line 138:
```
├── shared_constants.py        # JUNK_DOMAINS (68), JUNK_EMAIL_PREFIXES (17)
```
To:
```
├── shared_constants.py        # JUNK_DOMAINS, JUNK_EMAIL_PREFIXES
```

Change line 140:
```
├── scheduler.py               # APScheduler coordinator with 14 job modules
```
To:
```
├── scheduler.py               # APScheduler coordinator (see app/jobs/)
```

Change line 148:
```
├── models/                    # SQLAlchemy ORM models (73 models, 19 domain modules)
```
To:
```
├── models/                    # SQLAlchemy ORM models
```

Change line 149:
```
├── schemas/                   # Pydantic request/response schemas (26 files)
```
To:
```
├── schemas/                   # Pydantic request/response schemas
```

Change line 150:
```
├── routers/                   # API route handlers (34 routers, 200+ endpoints)
```
To:
```
├── routers/                   # API route handlers (see main.py for registration)
```

Change line 159:
```
│   └── ...                    # 25+ more routers (vendors, contacts, activity, etc.)
```
To:
```
│   └── ...                    # Additional routers (vendors, contacts, activity, etc.)
```

Change line 161:
```
├── services/                  # Business logic (120+ service files, decoupled from HTTP)
```
To:
```
├── services/                  # Business logic (decoupled from HTTP)
```

Change line 169:
```
├── jobs/                      # APScheduler job definitions (14 job modules)
```
To:
```
├── jobs/                      # APScheduler job definitions
```

Change line 183:
```
├── templates/                 # Jinja2 templates (188 files)
```
To:
```
├── templates/                 # Jinja2 templates
```

Change line 186:
```
│   ├── htmx/partials/         # 158 HTMX partials (29 subdirectories)
```
To:
```
│   ├── htmx/partials/         # HTMX partials
```

Change line 195:
```
├── migrations/                # Alembic migration files (109 revisions)
```
To:
```
├── migrations/                # Alembic migration files
```

- [ ] **Step 5.4: Remove stale counts from Shared Constants section**

Change lines 391-392:
```
- Junk email domains: use `JUNK_DOMAINS` from `app/shared_constants.py` (68 domains)
- Junk email prefixes: use `JUNK_EMAIL_PREFIXES` from `app/shared_constants.py` (17 prefixes)
```
To:
```
- Junk email domains: use `JUNK_DOMAINS` from `app/shared_constants.py`
- Junk email prefixes: use `JUNK_EMAIL_PREFIXES` from `app/shared_constants.py`
```

- [ ] **Step 5.5: Remove stale plugin/extension counts**

Change lines 320-321:
```
**Alpine.js Plugins (9 loaded):** focus, persist, intersect, collapse, morph, mask, sort, anchor, resize
**HTMX Extensions (14 active):** alpine-morph, preload, sse, loading-states, multi-swap, and more
```
To:
```
**Alpine.js Plugins:** See `htmx_app.js` for current plugin list
**HTMX Extensions:** See `htmx_app.js` and `base.html` for current extension list
```

- [ ] **Step 5.6: Remove stale test count from Testing section**

Change line 472:
```
### Strategy: Tiered Approach (8,553 tests total)
```
To:
```
### Strategy: Tiered Approach
```

- [ ] **Step 5.7: Remove stale "Linear Development" section referencing app maps**

Change lines 119-123:
```
### Linear Development
- Memory references specific code (line numbers, function names)? Verify against current files before acting
- App map files are orientation, not source of truth — always confirm against current code
- App map files with `last_verified` older than 30 days: confirm model/route counts against current code before citing as fact
- Never mix old patterns with new — if the codebase has moved to a new pattern, follow the new one
```
To:
```
### Linear Development
- Memory references specific code (line numbers, function names)? Verify against current files before acting
- Plans or specs with line numbers? Verify those lines are still correct before editing
- Never mix old patterns with new — if the codebase has moved to a new pattern, follow the new one
- Always read the actual codebase before making changes — never rely on cached assumptions
```

- [ ] **Step 5.8: Remove "Last Updated" line**

Delete lines 697-698:
```
**Last Updated:** 2026-03-23
**Maintained by:** Development team
```

Replace with:
```
**Maintained by:** Development team
```

- [ ] **Step 5.9: Remove stale migration reference**

Change line 525:
```
- **Current production:** Migration 048+
```
To:
```
- **Current production:** Run `alembic current` to check
```

- [ ] **Step 5.10: Verify CLAUDE.md has no remaining hardcoded counts**

```bash
grep -n -E '\b\d{2,4}\b.*(models|routers|templates|tests|migrations|modules|enums|files|endpoints|domains|prefixes|partials|subdirectories|revisions|extensions|plugins)' /root/availai/CLAUDE.md
```

Expected: No matches (or only non-count references like version numbers in config examples).

---

### Task 6: Purge Orphaned Session Cache

**Files:**
- Delete: All files in `/root/.claude/plans/`
- Delete: All files in `/root/.claude/todos/`

- [ ] **Step 1: Delete all orphaned session plans**

```bash
rm -f /root/.claude/plans/*.md
```

- [ ] **Step 2: Delete all orphaned todo stubs**

```bash
rm -f /root/.claude/todos/*.json
```

- [ ] **Step 3: Verify cleanup**

```bash
echo "Plans remaining:"; ls /root/.claude/plans/ 2>/dev/null | wc -l
echo "Todos remaining:"; ls /root/.claude/todos/ 2>/dev/null | wc -l
```

Expected: `0` for both.

---

### Task 7: Commit All Changes

- [ ] **Step 1: Commit CLAUDE.md changes in the project repo**

```bash
cd /root/availai
git add CLAUDE.md docs/superpowers/specs/2026-03-30-stale-memory-cleanup-design.md docs/superpowers/plans/2026-03-30-stale-memory-cleanup.md
git commit -m "fix: remove stale hardcoded counts from CLAUDE.md

Replaced all hardcoded model/router/template/test/migration counts with
pointers to source of truth directories. Prevents Claude Code from making
edits based on outdated assumptions about codebase structure."
```

- [ ] **Step 2: Verify final state**

```bash
echo "=== Memory files ==="
ls /root/.claude/projects/-root/memory/
echo ""
echo "=== Session cache ==="
echo "Plans:"; ls /root/.claude/plans/ 2>/dev/null | wc -l
echo "Todos:"; ls /root/.claude/todos/ 2>/dev/null | wc -l
echo ""
echo "=== CLAUDE.md count check ==="
grep -c -E '\b\d{2,4}\b.*(models|routers|templates|tests|migrations|modules|enums|files|endpoints)' /root/availai/CLAUDE.md
```

Expected:
- Memory files: philosophy, workflow_tooling, workflow_page_names, project_*, feedback_* — no app_map_*
- Session cache: 0 plans, 0 todos
- CLAUDE.md count check: 0 or near-0 matches
