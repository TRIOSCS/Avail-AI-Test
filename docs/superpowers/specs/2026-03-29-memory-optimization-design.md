# Memory System Optimization — Design Spec

**Date:** 2026-03-29
**Goal:** Optimize Claude Code memory for build quality, simplicity, elegant frontend design, and linear development continuity.

---

## Problem Statement

1. **Redundancy:** 3 files say "use subagents" differently, causing confusion
2. **Dead weight:** 6 completed-work memories clutter retrieval with stale context
3. **No design philosophy:** Quality rules scattered across 5+ files with no north star
4. **Code confusion:** AI mixes old code patterns with current code — no freshness discipline
5. **Wrong enforcement layer:** Critical non-negotiable rules live in memory (relevance-based) instead of CLAUDE.md (guaranteed-load)
6. **Skill catalog waste:** 68-line skill list in memory duplicates system prompt

---

## Architecture Decision: Memory vs CLAUDE.md

**CLAUDE.md** = guaranteed to load every conversation. Use for non-negotiable rules where a single violation causes real harm.

**Memory** = loaded on relevance match. Use for contextual guidance, project state, and preferences that enhance quality but aren't catastrophic if missed once.

**Principle:** If asking "would one conversation without this rule cause harm?" yields yes → CLAUDE.md. Otherwise → memory.

---

## Changes to CLAUDE.md

Add a new section `## Standing Workflow Rules` after the existing `## CODE RULES` section. Only includes rules that are NOT already in CLAUDE.md elsewhere.

**Note:** Several rules being promoted from memory already exist in CLAUDE.md (template routing at line 301-305, StrEnum at line 342-343, tiered testing at lines 433-456, deploy at lines 499-513). These are NOT duplicated. Only net-new rules are added below.

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

**Additional changes to existing CLAUDE.md sections:**

1. **Testing section (lines 433-456)** — add these missing lines:
   - `- Fast subset: TESTING=1 PYTHONPATH=/root/availai pytest -m "not slow" -v (~1:10)`
   - `- Slowest tests are marked @pytest.mark.slow — skip with -m "not slow"`
   - `- NEVER add --cov to iterative dev runs — only before PR`

2. **Deployment section (lines 499-513)** — add this rationale:
   - `- IMPORTANT: deploy.sh uses --no-cache on build (prevents stale cached layers) and --force-recreate on up (prevents reusing old containers). Never use bare "docker compose up -d --build" — it causes "code didn't update" bugs.`
   - `- For rebuild without commit: ./deploy.sh --no-commit`

**Rules NOT added to Standing Workflow Rules (already exist in CLAUDE.md):**
- Template routing golden rule → already at line 301-305
- StrEnum constants → already at line 342-343
- `list[dict]` → Pydantic schemas → already at line 329-337
- Tiered testing strategy → already at lines 433-456 (enhanced above)
- Deploy pipeline → already at lines 499-513 (enhanced above)
- Headless environment → already implied by server context; no visual companion handled by memory system's brainstorming skip

3. **Deduplicate existing CLAUDE.md sections** — remove the second occurrence of:
   - File Rules (lines 600-613 duplicate lines 539-541)
   - Session Rules (lines 615-621 duplicate lines 543-544)
   - Triggers (lines 623-629 duplicate lines 546-550)
   - Key Request Flows (lines 307-313 duplicate Key Workflows at lines 202-217)
   This reclaims ~40 lines, making the new additions attention-neutral.

---

## Memory File Changes

### DELETE — 7 stale project files (all value is in git)

| File | Reason |
|------|--------|
| `project_active_investigations.md` | Empty — "all resolved, no open work" |
| `project_remediation_audit.md` | All findings fixed, superseded by later audit |
| `project_audit_2026_03_26.md` | All 25/25 fixed and deployed |
| `project_reqs_tab_polish.md` | All deployed, no remaining work |
| `project_sightings_wip.md` | All deployed, no remaining work |
| `project_open_items_2026_03_29.md` | All PRs merged, issue closed |
| `project_session_2026_03_29b.md` | Session work completed and deployed (manual search + MPN uppercase) |

### DELETE — 13 feedback/user files absorbed into CLAUDE.md or consolidated

| File | Where it went |
|------|--------------|
| `feedback_aggressive_tooling.md` | CLAUDE.md Standing Workflow Rules + workflow_tooling.md |
| `feedback_always_subagent.md` | CLAUDE.md Standing Workflow Rules + workflow_tooling.md |
| `feedback_max_subagents.md` | CLAUDE.md Standing Workflow Rules + workflow_tooling.md |
| `feedback_always_load_skills.md` | Deleted — skill list is in system prompt already |
| `feedback_no_shortcuts.md` | philosophy.md |
| `feedback_fix_all_issues.md` | CLAUDE.md Standing Workflow Rules + philosophy.md |
| `feedback_ux_principles.md` | philosophy.md |
| `feedback_no_unauthorized_changes.md` | CLAUDE.md Standing Workflow Rules |
| `feedback_no_visual_companion.md` | workflow_tooling.md (environment note) |
| `feedback_verify_template_routing.md` | CLAUDE.md (already exists at line 301-305) + workflow_page_names.md routing table |
| `feedback_tiered_testing.md` | CLAUDE.md Testing section (enhanced with -m "not slow" and --cov prohibition) |
| `feedback_deploy_means_everything.md` | CLAUDE.md Deployment section (enhanced with --no-cache/--force-recreate rationale) |
| `feedback_page_names.md` | Absorbed into workflow_page_names.md (renamed + enhanced) |

### DELETE — 1 user file absorbed

| File | Where it went |
|------|--------------|
| `user_environment.md` | CLAUDE.md context + workflow_tooling.md environment note |

Total deletions: 20 files.

### CREATE — 3 new memory files

#### 1. `philosophy.md`
North star document. Shapes every decision.

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

#### 2. `workflow_tooling.md`
Behavioral execution rules (no skill catalog — that's in system prompt).

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

#### 3. `workflow_page_names.md`
Lookup table with full routing chain — kept standalone because it's frequently referenced.

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

### KEEP — 3 project files (unchanged)

| File | Why |
|------|-----|
| `project_approved_product_direction.md` | Active — HTMX+Alpine stack decision |
| `project_enrichment_confidence.md` | Active — enrichment requirements (note: 98% confidence target, fixing stale 95% in old index) |
| `project_crm_redesign.md` | Active — current major initiative |

### KEEP — 5 app map files (add staleness metadata)

Add `last_verified` to frontmatter using actual per-file modification dates:
- `app_map_architecture.md` → `last_verified: 2026-03-21`
- `app_map_routes.md` → `last_verified: 2026-03-21`
- `app_map_models.md` → `last_verified: 2026-03-23`
- `app_map_patterns.md` → `last_verified: 2026-03-21`
- `app_map_templates.md` → `last_verified: 2026-03-21`

The CLAUDE.md 30-day staleness rule operationalizes these dates.

---

## New MEMORY.md Index

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

---

## Final Metrics

| Metric | Before | After |
|--------|--------|-------|
| Memory files (excl. MEMORY.md) | 22+ | 11 |
| Dead/stale files | 7 | 0 |
| Redundant files | 3 | 0 |
| CLAUDE.md net-new rules | 0 | ~25 lines (no duplication) |
| CLAUDE.md existing sections enhanced | 0 | 2 (Testing + Deployment) |
| CLAUDE.md existing duplication removed | 3 sections x2 | 0 duplication |
| Design philosophy | scattered | 1 north star |
| Non-negotiable enforcement | memory (unreliable) | CLAUDE.md (guaranteed) |
| MEMORY.md index lines | 46 | ~18 |
| Files deleted | — | 21 |
| Files created | — | 3 |

---

## Execution Plan

1. Deduplicate existing CLAUDE.md (remove 3 duplicated sections: File Rules, Session Rules, Triggers)
2. Add `## Standing Workflow Rules` section to CLAUDE.md (net-new rules only)
3. Enhance CLAUDE.md Testing section with `-m "not slow"` and `--cov` prohibition
4. Enhance CLAUDE.md Deployment section with `--no-cache`/`--force-recreate` rationale
5. Create 3 new memory files (philosophy, workflow_tooling, workflow_page_names)
6. Delete 21 old memory files
7. Rewrite MEMORY.md index
8. Add `last_verified` to app map frontmatter
9. Commit all changes

---

## Intentionally Dropped Content

These items from deleted files are intentionally NOT carried forward:

1. **Tech debt from `project_reqs_tab_polish.md`** — 3 items (escape key tab reload, manufacturer free-text not FK, substitutes_text PG limitation). These are known limitations documented in the spec files at `docs/superpowers/specs/2026-03-22-*`. Not worth memory space.

2. **Skill trigger mapping from `feedback_always_load_skills.md`** — "UI work → frontend-design, bugs → systematic-debugging" triggers. Superseded by "full pipeline every time" rule which runs everything anyway.

## Future Improvements (out of scope for this spec)

1. **Anti-pattern linting** — The 4 code anti-patterns in CLAUDE.md are machine-checkable. Add pre-commit hooks or custom ruff rules for innerHTML, Pydantic class Config, _x_dataStack, and db.query().get() to enforce automatically.

2. **App map auto-refresh** — Currently manual. Could build tooling to regenerate app maps on demand or detect when they're significantly stale.

3. **Hookify rule for stale memory** — When an app_map file is loaded, automatically verify its claims against current code before the AI acts on specifics.

---

## Review Findings Addressed

| Finding | Resolution |
|---------|-----------|
| C1: `feedback_deploy_means_everything.md` had no disposition | Enhanced CLAUDE.md Deployment section with --no-cache/--force-recreate rationale |
| C2: `feedback_tiered_testing.md` had no disposition | Enhanced CLAUDE.md Testing section with -m "not slow" and --cov prohibition |
| C3: Filename mismatch `project_product_direction.md` | Fixed to `project_approved_product_direction.md` throughout |
| I1: Template routing rule already in CLAUDE.md | Removed from Standing Workflow Rules (already line 301-305) |
| I2: StrEnum rule already in CLAUDE.md | Removed from anti-patterns (already line 342-343) |
| I3: Partial URL column dropped from page names | Restored full URL→Partial→Template table |
| I4: PR review agent list had no destination | Added to workflow_tooling.md and CLAUDE.md Standing Rules |
| I5: git log belongs in philosophy, not CLAUDE.md | Moved to philosophy.md principle #5 |
| I6: Environment duplicated in CLAUDE.md | Single mention in workflow_tooling.md, not in CLAUDE.md Standing Rules |
| I7: Enrichment confidence 95%→98% correction | Noted explicitly in KEEP table |
| M1: "Keep navigation granularity" dropped | Added to philosophy.md principle #2 |
| M2: File count excluded MEMORY.md without stating | Clarified "(excl. MEMORY.md)" in metrics |
| R1: Pipeline ordering inconsistent across spec | Fixed to canonical: brainstorm→plan→TDD→execute everywhere |
| R2: App map staleness threshold missing | Added 30-day rule to CLAUDE.md Linear Development section |
| R3: Tech debt items silently dropped from reqs tab polish | Documented in "Intentionally Dropped Content" section |
| R4: CLAUDE.md has 3 duplicated sections | Added deduplication step to execution plan |
| R5: Migration d4e7f2a19b83 deployment status | Verified: applied to production (current head f2ee82c7b17d is past it) |
| R6: Anti-patterns should be linting/hooks | Documented as future improvement (out of scope) |
| R7: App map last_verified should use actual dates | Fixed: per-file dates from filesystem |
| R8: New `project_session_2026_03_29b.md` not in original audit | Added to delete list (7 stale project files total) |
