# Mypy Workflows Reference

## Contents
- Pre-commit integration
- Fixing errors iteratively
- Adding types to existing code
- mypy.ini configuration
- CI workflow

---

## Pre-Commit Integration

Mypy runs automatically on `git commit`. When it fails, the commit is blocked.

```bash
# Pre-commit runs this automatically on staged files:
mypy app/

# To run manually before committing:
mypy app/routers/vendors.py      # single file
mypy app/services/               # entire directory
mypy app/ --no-error-summary     # cleaner output for large runs
```

**Fix errors before staging:**

```
Iterate-until-pass workflow:
1. Run: mypy app/path/to/changed_file.py
2. Fix ALL reported errors (don't skip any)
3. Re-run: mypy app/path/to/changed_file.py
4. If errors remain, go to step 2
5. Stage and commit only when mypy exits 0
```

---

## Fixing Errors Iteratively

Most mypy errors in this codebase fall into 4 categories. Resolve in this order:

**1. Missing return type annotations**
```bash
# Find all untyped functions
mypy app/ 2>&1 | grep "Function is missing a return type annotation"
```
```python
# Before
async def get_vendor(vendor_id: int, db: Session = Depends(get_db)):
    return db.get(Vendor, vendor_id)

# After
async def get_vendor(vendor_id: int, db: Session = Depends(get_db)) -> Vendor | None:
    return db.get(Vendor, vendor_id)
```

**2. None-unsafe attribute access**
```python
# Error: Item "None" of "Vendor | None" has no attribute "name"
vendor = db.get(Vendor, vendor_id)
return vendor.name  # FAILS

# Fix: guard with 404
vendor = db.get(Vendor, vendor_id)
if vendor is None:
    raise HTTPException(status_code=404, detail="Vendor not found")
return vendor.name  # OK
```

**3. SQLAlchemy CursorResult issues**
```python
# Error: Incompatible return value type (got "Row[Any]", expected "tuple[int, str]")
result = db.execute(text("SELECT id, name FROM vendors")).fetchone()

# Fix: use ORM accessors or explicit cast
result = db.execute(select(Vendor.id, Vendor.name)).fetchone()
if result:
    vid, vname = result.id, result.name  # typed via mapped columns
```

**4. Incompatible types in assignment**
```python
# Error: Incompatible types in assignment (expression has type "bool", variable has type "int")
count: int = bool(db.query(Vendor).count())  # WRONG

# Fix: use correct type
found: bool = db.query(Vendor).filter_by(name=name).count() > 0
```

---

## Adding Types to Existing Code

When modifying a file that has no type annotations, add types only to the functions you touch. Don't type the entire file in one PR — that creates noise and risks breaking things.

```
Checklist for adding types to a modified function:
- [ ] Add return type annotation to the function signature
- [ ] Annotate all parameters with concrete types (no Any)
- [ ] Add None guards after db.get() calls
- [ ] Replace Any with TypedDict or proper model where used
- [ ] Run: mypy app/path/to/file.py — must exit 0
- [ ] Run: TESTING=1 pytest tests/test_<module>.py -v — must pass
```

---

## mypy.ini Configuration

```ini
# mypy.ini (project root)
[mypy]
python_version = 3.11
strict = false          # not full strict — selective enforcement
warn_return_any = true
warn_unused_ignores = true
disallow_untyped_defs = true
ignore_missing_imports = true  # handles third-party libs without stubs

# Per-module overrides for legacy code
[mypy-app.migrations.*]
ignore_errors = true    # Alembic-generated files, not worth typing

[mypy-tests.*]
disallow_untyped_defs = false  # tests use fixtures with complex types
```

**WARNING:** Never set `ignore_errors = true` for app modules. Use targeted `# type: ignore[code]` suppressions instead.

---

## CI Workflow

```bash
# Full pre-commit check (matches CI exactly)
pre-commit run --all-files

# Just mypy in isolation
mypy app/ --no-error-summary 2>&1 | tail -20

# Count remaining errors (useful for tracking progress)
mypy app/ 2>&1 | grep "error:" | wc -l
```

**Handling third-party stubs:**

```bash
# Install missing stubs when mypy reports "stub not found"
pip install types-requests types-redis types-python-dateutil

# For libraries with no stubs, suppress at import site only
import some_untyped_lib  # type: ignore[import-untyped]
```

---

## Integration with Ruff and Pre-Commit

Mypy and Ruff run in sequence in pre-commit. Fix Ruff errors first — Ruff runs faster and Ruff errors sometimes mask mypy errors.

```
Pre-commit order:
1. detect-private-key  (blocks leaked secrets)
2. ruff                (fast lint, blocks on errors)
3. ruff-format         (auto-formats, may change files)
4. mypy                (type check, blocks on errors)
5. docformatter        (docstring style)
```

See the **ruff** skill for linting patterns. Both tools must pass before a commit lands.
