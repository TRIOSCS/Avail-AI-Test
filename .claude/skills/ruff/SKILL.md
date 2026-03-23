---
name: ruff
description: |
  Configures Ruff for Python linting and auto-formatting in the AvailAI FastAPI codebase.
  Use when: fixing lint errors, adding noqa suppressions, configuring ruff rules, debugging
  pre-commit failures, or running ruff in CI/pre-commit hooks.
allowed-tools: Read, Edit, Write, Glob, Grep, Bash, mcp__plugin_context7_context7__resolve-library-id, mcp__plugin_context7_context7__query-docs
---

# Ruff

Ruff is the project's Python linter and formatter. It runs via pre-commit (v0.9.6) with
`--fix` on every commit, and manually via `ruff check app/` and `ruff format app/`. There
is no `[tool.ruff]` config in `pyproject.toml` — the project uses Ruff defaults (line
length 88, E/F/W rules enabled).

## Quick Start

```bash
# Lint and auto-fix
ruff check app/ --fix

# Format only (black-compatible)
ruff format app/

# Check a single file
ruff check app/routers/crm/companies.py

# Show what rules would fire without fixing
ruff check app/ --no-fix
```

## Common noqa Patterns Used in This Codebase

```python
# Re-exported imports (test patching or public API surface)
from ...services.crm_service import next_quote_number  # noqa: F401

# SQLAlchemy boolean comparisons — must use == True/False, not `is`
.filter(ApiSource.is_active == True)  # noqa: E712

# Subprocess call in trusted internal helper
def _exec(conn, stmt: str) -> None:  # noqa: S603
```

## Key Rules to Know

| Rule | Meaning | Fix |
|------|---------|-----|
| `F401` | Unused import | Remove OR add `# noqa: F401` if re-exported |
| `E712` | `== True/False` comparison | Required for SQLAlchemy — suppress with `# noqa: E712` |
| `E501` | Line too long (>88 chars) | Break line or use `# noqa: E501` sparingly |
| `S603` | Subprocess without shell=True check | Suppress in trusted internal code only |
| `I001` | Import order wrong | Run `ruff check --fix` to auto-sort |

## Pre-Commit Integration

Pre-commit runs ruff automatically on staged files:

```yaml
# .pre-commit-config.yaml
- repo: https://github.com/astral-sh/ruff-pre-commit
  rev: v0.9.6
  hooks:
    - id: ruff
      args: [--fix]      # auto-fixes safe issues
    - id: ruff-format    # black-compatible formatting
```

If a commit is blocked by ruff, run `ruff check app/ --fix`, review the diff, re-stage,
and commit again.

## See Also

- [patterns](references/patterns.md) — noqa usage, SQLAlchemy exceptions, suppression strategy
- [workflows](references/workflows.md) — pre-commit workflow, CI integration, fixing bulk errors

## Related Skills

- See the **mypy** skill for type checking (runs alongside ruff in pre-commit)
- See the **fastapi** skill for router patterns ruff commonly flags
- See the **sqlalchemy** skill for E712 suppression context
- See the **pytest** skill for test-specific lint rules

## Documentation Resources

**How to use Context7:**
1. Use `mcp__plugin_context7_context7__resolve-library-id` to search for "ruff"
2. Prefer website documentation (IDs starting with `/websites/`) over source repos
3. Query with `mcp__plugin_context7_context7__query-docs` using the resolved ID

**Recommended Queries:**
- "ruff configuration pyproject.toml"
- "ruff noqa suppression"
- "ruff select ignore rules"
