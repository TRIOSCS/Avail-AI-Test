# Memory System Optimization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Consolidate 29 memory files down to 11, move non-negotiable rules to CLAUDE.md (guaranteed load), eliminate dead weight and redundancy, and establish a design philosophy north star.

**Architecture:** Two-layer enforcement — CLAUDE.md holds non-negotiable rules (always loaded), memory files hold contextual guidance (relevance-matched). Deduplicate existing CLAUDE.md, add Standing Workflow Rules section, enhance Testing and Deployment sections, create 3 new focused memory files, delete 21 obsolete ones.

**Tech Stack:** Markdown files only — no code changes, no tests. All files in `/root/availai/CLAUDE.md` and `/root/.claude/projects/-root/memory/`.

**Spec:** `docs/superpowers/specs/2026-03-29-memory-optimization-design.md`

---

## File Map

**Modify:**
- `/root/availai/CLAUDE.md` — deduplicate, add Standing Workflow Rules, enhance Testing + Deployment sections

**Create:**
- `/root/.claude/projects/-root/memory/philosophy.md`
- `/root/.claude/projects/-root/memory/workflow_tooling.md`
- `/root/.claude/projects/-root/memory/workflow_page_names.md`

**Rewrite:**
- `/root/.claude/projects/-root/memory/MEMORY.md`

**Add frontmatter to:**
- `/root/.claude/projects/-root/memory/app_map_architecture.md`
- `/root/.claude/projects/-root/memory/app_map_routes.md`
- `/root/.claude/projects/-root/memory/app_map_models.md`
- `/root/.claude/projects/-root/memory/app_map_patterns.md`
- `/root/.claude/projects/-root/memory/app_map_templates.md`

**Delete (21 files):**
- `/root/.claude/projects/-root/memory/project_active_investigations.md`
- `/root/.claude/projects/-root/memory/project_remediation_audit.md`
- `/root/.claude/projects/-root/memory/project_audit_2026_03_26.md`
- `/root/.claude/projects/-root/memory/project_reqs_tab_polish.md`
- `/root/.claude/projects/-root/memory/project_sightings_wip.md`
- `/root/.claude/projects/-root/memory/project_open_items_2026_03_29.md`
- `/root/.claude/projects/-root/memory/project_session_2026_03_29b.md`
- `/root/.claude/projects/-root/memory/feedback_aggressive_tooling.md`
- `/root/.claude/projects/-root/memory/feedback_always_subagent.md`
- `/root/.claude/projects/-root/memory/feedback_max_subagents.md`
- `/root/.claude/projects/-root/memory/feedback_always_load_skills.md`
- `/root/.claude/projects/-root/memory/feedback_no_shortcuts.md`
- `/root/.claude/projects/-root/memory/feedback_fix_all_issues.md`
- `/root/.claude/projects/-root/memory/feedback_ux_principles.md`
- `/root/.claude/projects/-root/memory/feedback_no_unauthorized_changes.md`
- `/root/.claude/projects/-root/memory/feedback_no_visual_companion.md`
- `/root/.claude/projects/-root/memory/feedback_verify_template_routing.md`
- `/root/.claude/projects/-root/memory/feedback_tiered_testing.md`
- `/root/.claude/projects/-root/memory/feedback_deploy_means_everything.md`
- `/root/.claude/projects/-root/memory/feedback_page_names.md`
- `/root/.claude/projects/-root/memory/user_environment.md`

---

### Task 1: Deduplicate CLAUDE.md

**Files:**
- Modify: `/root/availai/CLAUDE.md`

- [ ] **Step 1: Remove duplicate Key Request Flows section (lines 313-325)**

The "Key Request Flows" section at line 313 duplicates "Key Workflows" at lines 200-222. Remove lines 313-325 (the `## Key Request Flows` header and the 3 summarized flows beneath it). Keep the detailed "Key Workflows" section at lines 200-222.

The exact text to remove:
```markdown
## Key Request Flows

**Search**: User submits part numbers → `search_service.search_requirement()` fires all connectors via `asyncio.gather()` → results deduped and scored by `scoring.py` (6 weighted factors) → material cards auto-upserted.

**RFQ**: `email_service.send_batch_rfq()` sends via Graph API, tagged with `[AVAIL-{id}]` → scheduler polls inbox every 30min → `response_parser.py` uses Claude to extract structured data → confidence >=0.8 auto-creates Offer, 0.5-0.8 flags for review.

**Proactive Matching**: Offers matched to customer purchase history → SQL scorecard (0-100) → batch prepare/send workflow → sent offers grouped by customer.
```

- [ ] **Step 2: Remove duplicate File Rules section (lines 624-635)**

The "File Rules" section at line 624 duplicates line 562-564. Remove lines 622-635 (the `---` separator, `## File Rules` header, and the expanded code block). Keep the concise version at line 562-564.

The exact text to remove:
```markdown
---

## File Rules

- Every new file needs a header comment:
  ```python
  """
  Brief description of what this file does.

  Called by: [router/service/job that imports this]
  Depends on: [key imports and external services]
  """
  ```
```

- [ ] **Step 3: Remove duplicate Session Rules section (lines 638-645)**

The "Session Rules" section at line 638 duplicates line 566-568. Remove lines 636-645 (the `---` separator, `## Session Rules` header, and the numbered list). Keep the concise version at line 566-568.

The exact text to remove:
```markdown
---

## Session Rules

End each development session with:
1. **What changed:** List modified/added files
2. **Git commands:** `git status`, `git diff` summary
3. **What to test:** Which features or test suites to verify
4. **Tech debt:** Any "pay later" items or known issues
```

- [ ] **Step 4: Remove duplicate Triggers section (lines 648-654)**

The "Triggers" section at line 648 duplicates line 570-575. Remove lines 646-654 (the `---` separator, `## Triggers` header, and the expanded trigger list). Keep the concise version at line 570-575.

The exact text to remove:
```markdown
---

## Triggers

- **"new feature"** → Make a plan first, don't just start coding
- **"bug" or "error"** → Ask for full error message before trying to fix
- **"refactor"** → Check what's stable first, plan the approach
- **"quick" or "just"** → Warn about hidden complexity; small changes often have ripple effects
```

- [ ] **Step 5: Verify deduplication**

Run: `grep -n "## File Rules\|## Session Rules\|## Triggers\|## Key Request Flows" /root/availai/CLAUDE.md`

Expected: Each heading appears exactly once.

---

### Task 2: Add Standing Workflow Rules to CLAUDE.md

**Files:**
- Modify: `/root/availai/CLAUDE.md`

- [ ] **Step 1: Insert Standing Workflow Rules section after CODE RULES**

After line 98 (`- Use Alembic for database migrations. Always include rollback steps.`) and before line 100 (`## Project Structure`), insert the following:

```markdown

## Standing Workflow Rules

### Execution Model
- Always use subagent-driven execution for multi-step tasks — never ask, never offer inline
- Maximize parallel subagents for all independent work — never serialize what can parallelize
- Run the full skill pipeline on every task: brainstorm → plan → TDD → execute → simplify → review → verify (this order is canonical)
- Never skip a step because it seems "overkill" — use every available tool and skill aggressively
- Fix ALL review findings immediately — never defer as "lower priority" or "MVP acceptable"

### UI Guardrails
- Never add, remove, or rearrange UI elements without explicit user approval
- Follow existing codebase patterns — find a working example before creating new UI conventions

### Code Anti-Patterns (never introduce — in addition to Coding Conventions section)
- `innerHTML` → use `htmx.ajax()` or Alpine reactive binding
- Pydantic `class Config` → use `model_config = ConfigDict()`
- Alpine `_x_dataStack` → use `Alpine.store()`
- `db.query(Model).get(id)` → use `db.get(Model, id)`

### Linear Development
- Memory references specific code (line numbers, function names)? Verify against current files before acting
- App map files are orientation, not source of truth — always confirm against current code
- App map files with `last_verified` older than 30 days: confirm model/route counts against current code before citing as fact
- Never mix old patterns with new — if the codebase has moved to a new pattern, follow the new one

### PR Reviews
- Run ALL pr-review-toolkit agents on every PR: comment-analyzer, pr-test-analyzer, type-design-analyzer, silent-failure-hunter, code-simplifier, code-reviewer
- Also run feature-dev:code-reviewer

```

- [ ] **Step 2: Verify insertion**

Run: `grep -n "## Standing Workflow Rules\|### Execution Model\|### PR Reviews" /root/availai/CLAUDE.md`

Expected: All 3 headings found, between `## CODE RULES` and `## Project Structure`.

---

### Task 3: Enhance CLAUDE.md Testing Section

**Files:**
- Modify: `/root/availai/CLAUDE.md`

- [ ] **Step 1: Add missing test commands after the "Coverage check" block**

Find this text in the Testing section:
```markdown
**Coverage check: Before PR only**
```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/ --cov=app --cov-report=term-missing --tb=no -q
```
```

After that code block (after the closing triple backticks), insert:

```markdown

**Fast subset (skip slow tests):**
```bash
TESTING=1 PYTHONPATH=/root/availai pytest -m "not slow" -v
```
- Slowest tests are marked `@pytest.mark.slow` — skip with `-m "not slow"` for ~1:10 runtime
- NEVER add `--cov` to iterative dev runs — only before PR
```

- [ ] **Step 2: Verify**

Run: `grep -n "not slow\|NEVER add --cov" /root/availai/CLAUDE.md`

Expected: Both strings found in Testing section.

---

### Task 4: Enhance CLAUDE.md Deployment Section

**Files:**
- Modify: `/root/availai/CLAUDE.md`

- [ ] **Step 1: Add deploy rationale after "What deploy means" line**

Find this text:
```
**What "deploy" means:** Commit + push + rebuild + verify logs. No questions asked.
```

After that line, insert:

```markdown

**IMPORTANT:** `deploy.sh` uses `--no-cache` on build (prevents stale cached layers) and `--force-recreate` on up (prevents reusing old containers). Never use bare `docker compose up -d --build` — it causes "code didn't update" bugs. For rebuild without commit: `./deploy.sh --no-commit`.
```

- [ ] **Step 2: Verify**

Run: `grep -n "no-cache\|force-recreate\|no-commit" /root/availai/CLAUDE.md`

Expected: All 3 found in Deployment section.

---

### Task 5: Create 3 New Memory Files

**Files:**
- Create: `/root/.claude/projects/-root/memory/philosophy.md`
- Create: `/root/.claude/projects/-root/memory/workflow_tooling.md`
- Create: `/root/.claude/projects/-root/memory/workflow_page_names.md`

- [ ] **Step 1: Create philosophy.md**

Write to `/root/.claude/projects/-root/memory/philosophy.md`:

```markdown
---
name: Design & Quality Philosophy
description: North star — build quality over speed, elegant frontend, follow existing patterns, no shortcuts, no tech debt
type: feedback
---

## Core Principles

1. **Quality over speed, always.** Proper abstractions, correct patterns, clean architecture. Never suggest "the quick way." Quick fixes accumulate tech debt.

2. **Elegant, production-grade frontend.** Scannable layouts, efficient flow (minimal clicks list→detail→action), clean detail views with whitespace and hierarchy, tables for lists (dense but readable). Present what's needed — context-aware, not everything at once. Keep current navigation granularity — don't collapse or restructure nav without explicit approval.

3. **Follow existing patterns.** Before writing any component, find a working example in the codebase and follow it. Never invent new conventions when a pattern already exists for modals, tables, forms, Alpine stores, or HTMX partials.

4. **Simple workflows, aggressive tooling.** The user experience stays clean and simple. Complexity is handled by the full tool/skill pipeline running automatically behind the scenes.

5. **Current code is truth.** Memory is context, not fact. Always read the actual file before acting on anything recalled from memory. At session start, `git log --oneline -20` to see what changed recently.
```

- [ ] **Step 2: Create workflow_tooling.md**

Write to `/root/.claude/projects/-root/memory/workflow_tooling.md`:

```markdown
---
name: Execution & Tooling Rules
description: Always subagent, max parallelism, full pipeline, never skip, PR review agents — behavioral rules for how to execute work
type: feedback
---

## Execution Model

- **Always subagent-driven.** Never ask. Never offer inline as an alternative. Every multi-step task gets broken into subtasks dispatched to subagents.
- **Maximum parallelism.** When facing 2+ independent tasks, launch ALL subagents in a single message. Never serialize what can be parallelized.
- **Full pipeline every time.** brainstorm → plan → TDD → execute with subagents → simplify → code review → verification → commit. Never skip a step.
- **Background agents by default** unless the result is needed before the next step.

## PR Reviews

Run ALL pr-review-toolkit agents on every PR:
- comment-analyzer, pr-test-analyzer, type-design-analyzer, silent-failure-hunter, code-simplifier, code-reviewer
- Also run feature-dev:code-reviewer

## Environment

- Headless DigitalOcean server — no browser, no visual companion
- Skip "Offer visual companion" in brainstorming — go straight to questions

**Why:** User has confirmed these preferences repeatedly. These are standing rules, not per-task decisions.
```

- [ ] **Step 3: Create workflow_page_names.md**

Write to `/root/.claude/projects/-root/memory/workflow_page_names.md`:

```markdown
---
name: Page Name → Route → Partial → Template Mapping
description: User refers to pages by bottom nav labels — map to routes, partials, and templates without asking for clarification
type: feedback
---

**Bottom bar (5 primary slots):**
| Page Name | URL | Partial | Template |
|-----------|-----|---------|----------|
| "Reqs" | `/v2/requisitions` | `/v2/partials/parts/workspace` | `parts/workspace.html` → `parts/list.html` |
| "Search" | `/v2/search` | `/v2/partials/search` | `sourcing/workspace.html` |
| "Buy Plans" | `/v2/buy-plans` | `/v2/partials/buy-plans` | `buy_plans/list.html` |
| "Vendors" | `/v2/vendors` | `/v2/partials/vendors` | `vendors/list.html` |
| "Companies" | `/v2/companies` | `/v2/partials/companies` | `companies/list.html` |

**More menu:**
| Page Name | URL | Partial | Template |
|-----------|-----|---------|----------|
| "Proactive" | `/v2/proactive` | `/v2/partials/proactive` | `proactive/list.html` |
| "My Vendors" | `/v2/my-vendors` | `/v2/partials/my-vendors` | — |
| "Quotes" | `/v2/quotes` | `/v2/partials/quotes` | `quotes/list.html` |
| "Prospecting" | `/v2/prospecting` | `/v2/partials/prospecting` | `prospecting/list.html` |
| "Settings" | `/v2/settings` | `/v2/partials/settings` | — |

**Key gotcha:** Requisitions loads `parts/workspace` not `requisitions/list`. The mapping goes through `v2_page()` in `htmx_views.py`.

When user says a page name, go straight to that code. No clarification needed.
```

- [ ] **Step 4: Verify all 3 files exist**

Run: `ls -la /root/.claude/projects/-root/memory/philosophy.md /root/.claude/projects/-root/memory/workflow_tooling.md /root/.claude/projects/-root/memory/workflow_page_names.md`

Expected: All 3 files exist with non-zero size.

---

### Task 6: Delete 21 Old Memory Files

**Files:**
- Delete: 21 files listed in File Map above

- [ ] **Step 1: Delete 7 stale project files**

```bash
cd /root/.claude/projects/-root/memory && rm -f \
  project_active_investigations.md \
  project_remediation_audit.md \
  project_audit_2026_03_26.md \
  project_reqs_tab_polish.md \
  project_sightings_wip.md \
  project_open_items_2026_03_29.md \
  project_session_2026_03_29b.md
```

- [ ] **Step 2: Delete 13 feedback files**

```bash
cd /root/.claude/projects/-root/memory && rm -f \
  feedback_aggressive_tooling.md \
  feedback_always_subagent.md \
  feedback_max_subagents.md \
  feedback_always_load_skills.md \
  feedback_no_shortcuts.md \
  feedback_fix_all_issues.md \
  feedback_ux_principles.md \
  feedback_no_unauthorized_changes.md \
  feedback_no_visual_companion.md \
  feedback_verify_template_routing.md \
  feedback_tiered_testing.md \
  feedback_deploy_means_everything.md \
  feedback_page_names.md
```

- [ ] **Step 3: Delete 1 user file**

```bash
rm -f /root/.claude/projects/-root/memory/user_environment.md
```

- [ ] **Step 4: Verify only 11 content files remain**

Run: `ls -1 /root/.claude/projects/-root/memory/*.md | grep -v MEMORY.md | wc -l`

Expected: `11` (3 new + 3 kept project + 5 app maps)

Run: `ls -1 /root/.claude/projects/-root/memory/*.md | grep -v MEMORY.md | sort`

Expected:
```
app_map_architecture.md
app_map_models.md
app_map_patterns.md
app_map_routes.md
app_map_templates.md
philosophy.md
project_approved_product_direction.md
project_crm_redesign.md
project_enrichment_confidence.md
workflow_page_names.md
workflow_tooling.md
```

---

### Task 7: Rewrite MEMORY.md Index

**Files:**
- Rewrite: `/root/.claude/projects/-root/memory/MEMORY.md`

- [ ] **Step 1: Write new MEMORY.md**

Overwrite `/root/.claude/projects/-root/memory/MEMORY.md` with:

```markdown
# MEMORY

## Philosophy
- [philosophy.md](philosophy.md) — North star: quality > speed, elegant frontend, follow patterns, current code is truth

## Workflow
- [workflow_tooling.md](workflow_tooling.md) — Always subagent, max parallelism, full pipeline, PR review agents, environment
- [workflow_page_names.md](workflow_page_names.md) — Bottom nav label → route → partial → template mapping

## Project
- [project_approved_product_direction.md](project_approved_product_direction.md) — HTMX+Alpine.js stack (NOT React)
- [project_enrichment_confidence.md](project_enrichment_confidence.md) — On-demand enrichment, 98% confidence target
- [project_crm_redesign.md](project_crm_redesign.md) — 4-phase CRM redesign roadmap

## App Maps (verify against current code before citing specifics)
- [app_map_architecture.md](app_map_architecture.md) — Stack, directory tree, services, config, middleware
- [app_map_routes.md](app_map_routes.md) — All routers, endpoints, auth deps, response formats
- [app_map_models.md](app_map_models.md) — SQLAlchemy models, columns, FKs, indexes, enums
- [app_map_templates.md](app_map_templates.md) — Templates, Alpine stores, HTMX extensions, navigation
- [app_map_patterns.md](app_map_patterns.md) — Coding conventions, testing rules, common gotchas
```

- [ ] **Step 2: Verify line count**

Run: `wc -l /root/.claude/projects/-root/memory/MEMORY.md`

Expected: ~18 lines (well under 200-line truncation limit).

---

### Task 8: Add last_verified to App Map Frontmatter

**Files:**
- Modify: 5 app map files in `/root/.claude/projects/-root/memory/`

- [ ] **Step 1: Add last_verified to app_map_architecture.md**

In `/root/.claude/projects/-root/memory/app_map_architecture.md`, find:
```
---
name: App Map — Architecture & Structure
description: AvailAI v3.1.0 complete architecture — stack, directory tree, services, config, deployment, middleware, scheduled jobs
type: reference
---
```

Replace with:
```
---
name: App Map — Architecture & Structure
description: AvailAI v3.1.0 complete architecture — stack, directory tree, services, config, deployment, middleware, scheduled jobs
type: reference
last_verified: 2026-03-21
---
```

- [ ] **Step 2: Add last_verified to app_map_routes.md**

Same pattern — add `last_verified: 2026-03-21` before the closing `---` in frontmatter.

- [ ] **Step 3: Add last_verified to app_map_models.md**

Same pattern — add `last_verified: 2026-03-23` (this file was modified later than the others).

- [ ] **Step 4: Add last_verified to app_map_patterns.md**

Same pattern — add `last_verified: 2026-03-21`.

- [ ] **Step 5: Add last_verified to app_map_templates.md**

Same pattern — add `last_verified: 2026-03-21`.

- [ ] **Step 6: Verify all 5 have last_verified**

Run: `grep "last_verified" /root/.claude/projects/-root/memory/app_map_*.md`

Expected: 5 lines, one per file, with correct dates.

---

### Task 9: Commit All Changes

**Files:**
- All files modified/created/deleted in Tasks 1-8

- [ ] **Step 1: Stage CLAUDE.md changes**

```bash
cd /root/availai && git add CLAUDE.md
```

- [ ] **Step 2: Stage spec and plan**

```bash
cd /root/availai && git add docs/superpowers/specs/2026-03-29-memory-optimization-design.md docs/superpowers/plans/2026-03-29-memory-optimization.md
```

- [ ] **Step 3: Commit**

```bash
cd /root/availai && git commit -m "$(cat <<'EOF'
refactor: optimize Claude Code memory system — 29→11 files, add Standing Workflow Rules to CLAUDE.md

- Add Standing Workflow Rules section with execution model, UI guardrails, code anti-patterns, linear development, PR review rules
- Enhance Testing section with -m "not slow" and --cov prohibition
- Enhance Deployment section with --no-cache/--force-recreate rationale
- Deduplicate 4 repeated sections in CLAUDE.md
- Create philosophy.md (north star: quality > speed, elegant frontend)
- Create workflow_tooling.md (subagent rules, PR review agents, environment)
- Create workflow_page_names.md (full route→partial→template mapping)
- Delete 21 obsolete memory files (completed work, redundant feedback)
- Rewrite MEMORY.md index (46→18 lines)
- Add last_verified dates to 5 app map files

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>
EOF
)"
```

- [ ] **Step 4: Verify commit**

Run: `git log --oneline -1`

Expected: Shows the commit message starting with "refactor: optimize Claude Code memory system"

Run: `git status`

Expected: Clean working tree, nothing to commit.
