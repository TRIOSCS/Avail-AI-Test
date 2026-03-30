# Stale Memory Cleanup — Design Spec

**Date:** 2026-03-30
**Problem:** Claude Code's memory system contained 60KB+ of stale code snapshots (app_map files) that caused wrong edits. CLAUDE.md had 38+ hardcoded counts/names that drifted from reality. Session cache directories accumulated 287 orphaned files.

**Root Cause:** Memory files stored concrete code state (model counts, router lists, template paths, function names) that went stale as the codebase evolved. Claude trusted these snapshots over the actual code.

**Evidence of Drift:**
| What | Memory Said | Reality | Delta |
|------|-------------|---------|-------|
| Routers | 34 | 22 | 12 fewer (consolidated) |
| Models | 73 | 86 | 13 more |
| Templates | 188 | 181 | 7 fewer |
| Migrations | 109 | 126 | 17 more |

---

## Fix — 5 Sections

### Section 1: Delete Memory App Maps
Delete 5 files from `/root/.claude/projects/-root/memory/`:
- `app_map_architecture.md`
- `app_map_models.md`
- `app_map_patterns.md`
- `app_map_routes.md`
- `app_map_templates.md`

Strip `workflow_page_names.md` to vocabulary only (no file paths or routes).

Add `feedback_no_code_in_memory.md` guardrail memory.

### Section 2: Clean CLAUDE.md
Remove all hardcoded counts, version numbers, and exhaustive lists. Replace with pointers to source of truth. Keep rules, conventions, workflows, and architecture descriptions.

Principle: CLAUDE.md describes what things are and where to find them, not how many there are or what they're named.

### Section 3: Purge Session Cache
- Delete all 38 files in `/root/.claude/plans/`
- Delete all 249 files in `/root/.claude/todos/`
- Leave `/root/availai/docs/superpowers/` alone (git-tracked project history)

### Section 4: Add Guardrail Feedback Memory
New `feedback_no_code_in_memory.md` that prevents future sessions from recreating stale maps. Explicitly states memory is for decisions/preferences/vocabulary, never for code structure.

### Section 5: Update MEMORY.md Index
Remove "App Maps" section. Add "Guardrails" section pointing to the new feedback memory.
