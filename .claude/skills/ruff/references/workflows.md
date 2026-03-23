# Ruff Workflows Reference

## Contents
- Pre-Commit Failure Recovery
- Fixing Bulk Lint Errors
- Adding Ruff Configuration
- CI / Deploy Checklist

---

## Pre-Commit Failure Recovery

Pre-commit runs ruff with `--fix`, so many errors are auto-corrected. When a commit is
blocked:

```
ruff.....................................................................Failed
- hook id: ruff
- exit code: 1
- files were modified by this hook
```

This means ruff **fixed files** but couldn't fix everything automatically. Follow this loop:

```bash
# 1. See what ruff fixed (auto-safe fixes were already applied)
git diff

# 2. Check what still needs manual fixing
ruff check app/ --no-fix

# 3. Fix remaining issues manually, then re-stage
git add -p    # stage intentional changes only

# 4. Commit again — pre-commit reruns automatically
git commit -m "your message"
```

**Iterate until `ruff check app/` exits 0.**

---

## Fixing Bulk Lint Errors

When starting work on a file with many existing violations:

```bash
# Fix everything ruff can fix automatically (safe transforms only)
ruff check app/services/my_service.py --fix

# Fix including "unsafe" fixes (import removals, etc.) — review carefully
ruff check app/services/my_service.py --fix --unsafe-fixes

# See all violations without fixing
ruff check app/ --output-format=grouped
```

**WARNING:** `--unsafe-fixes` can remove imports that look unused but are re-exported.
Always review the diff before committing — especially `F401` removals in `__init__.py` files.

---

## Adding Ruff Configuration

The project currently has no `[tool.ruff]` section in `pyproject.toml`. Add one when:
- The same `noqa` comment appears in 5+ files
- A rule is causing false positives across the codebase
- You need to extend the default ruleset

```toml
# pyproject.toml — add alongside existing [tool.mypy] section
[tool.ruff]
line-length = 88
target-version = "py311"

[tool.ruff.lint]
# Add rules beyond the default E/F/W set
select = ["E", "F", "W", "I", "UP", "B"]

# Project-wide suppressions (better than per-line noqa everywhere)
ignore = [
    "E712",   # SQLAlchemy requires == True/False, not `is`
]

[tool.ruff.lint.per-file-ignores]
"app/routers/*/__init__.py" = ["F401"]   # re-export files
"tests/*" = ["S101"]                      # assert allowed in tests
```

After adding config, run `ruff check app/` to verify no unintended changes.

---

## CI / Deploy Checklist

Per the project's pre-deploy checklist in CLAUDE.md, linting must pass before deploy:

```bash
# Full lint check (no auto-fix — verify clean state)
ruff check app/

# Format check (exits non-zero if formatting needed)
ruff format app/ --check

# Type check (runs after ruff in pre-commit)
mypy app/
```

Copy this checklist before deploying:

- [ ] `ruff check app/` exits 0 (no violations)
- [ ] `ruff format app/ --check` exits 0 (no formatting needed)
- [ ] All pre-commit hooks pass: `pre-commit run --all-files`
- [ ] `mypy app/` passes (see the **mypy** skill)

---

## Upgrading Ruff

Ruff version is pinned in `.pre-commit-config.yaml`:

```yaml
- repo: https://github.com/astral-sh/ruff-pre-commit
  rev: v0.9.6    # ← pin here
```

When upgrading, run `pre-commit autoupdate` to bump the rev, then:

```bash
# Run against all files to catch newly-enabled rules
pre-commit run ruff --all-files

# Fix any new violations before committing the version bump
ruff check app/ --fix
```

New ruff versions sometimes enable rules that fire on existing code. Always test the full
`app/` directory after a version bump, not just staged files.

---

## Debugging: Rule Not Firing

If you expect a rule to fire but it doesn't:

```bash
# Check which rules are currently active
ruff rule E712

# Check if a file is excluded
ruff check app/startup.py --verbose

# List all rules ruff would apply to a file
ruff check app/main.py --show-settings | grep "select"
```

The project uses default rule selection (E, F, W, I). Rules like `B` (bugbear), `UP`
(pyupgrade), or `S` (bandit/security) are NOT active unless added to `[tool.ruff.lint]
select`.
