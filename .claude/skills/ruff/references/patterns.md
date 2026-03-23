# Ruff Patterns Reference

## Contents
- F401 Unused Imports (Re-export Pattern)
- E712 SQLAlchemy Boolean Comparisons
- Import Sorting
- noqa Suppression Strategy
- Anti-Patterns

---

## F401: Unused Imports (Re-export Pattern)

This codebase uses `__init__.py` files to re-export symbols for test patching. Ruff flags
these as unused — suppress them with a comment explaining why.

```python
# app/routers/v13_features/__init__.py
from sqlalchemy.orm import Session  # noqa: F401 — test patches app.routers.v13_features.Session
from ...config import settings  # noqa: F401 — test patches app.routers.v13_features.settings
from .activity import _activity_to_dict  # noqa: F401 — tests import this directly
```

**Rule:** Always include a reason after `# noqa: F401 —`. Future readers need to know why
the import exists. Without a reason, someone will delete it and break tests.

```python
# BAD — no reason given
from ...services.crm_service import next_quote_number  # noqa: F401

# GOOD — reason documents the intent
from ...services.crm_service import next_quote_number  # noqa: F401 — re-exported for CRM public API
```

---

## E712: SQLAlchemy Boolean Comparisons

SQLAlchemy ORM requires `== True` / `== False` in filter expressions. Python's `is True`
does not work with SQLAlchemy column expressions — it compares object identity, not
generates SQL. Ruff flags `== True` as E712 (comparison to True/False).

```python
# CORRECT for SQLAlchemy — suppress E712
.filter(ApiSource.is_active == True)   # noqa: E712
.filter(VendorCard.is_blacklisted == False)  # noqa: E712
.filter(SiteContact.is_active == True, Company.is_active == True)  # noqa: E712

# WRONG — generates invalid SQL or Python-level comparison
.filter(ApiSource.is_active is True)   # Python identity check, not SQL
.filter(ApiSource.is_active)           # Works for non-nullable columns only
```

**Never remove E712 suppressions from SQLAlchemy filter clauses.** See the **sqlalchemy**
skill for ORM query patterns.

---

## Import Sorting (I001)

Ruff enforces isort-compatible import ordering. Correct order:

```python
# 1. Standard library
import asyncio
from datetime import datetime
from typing import Any

# 2. Third-party
from fastapi import APIRouter, Depends, HTTPException
from loguru import logger
from sqlalchemy.orm import Session

# 3. Local (absolute)
from app.constants import RequisitionStatus
from app.database import get_db
from app.dependencies import require_user

# 4. Local (relative)
from ..models import Requisition
from .schemas import RequisitionCreate
```

Run `ruff check app/ --fix` to auto-sort. Do NOT manually reorder — let ruff do it.

---

## noqa Suppression Strategy

**NEVER use bare `# noqa`** — it silences all rules on that line with no audit trail.
Always name the specific rule.

```python
# BAD — silences everything, hides future issues
result = subprocess.run(cmd)  # noqa

# GOOD — explicit, auditable
result = subprocess.run(cmd)  # noqa: S603 — internal trusted command, no user input
```

**Use file-level ignores in pyproject.toml for systematic suppressions**, not per-line
noqa everywhere:

```toml
# pyproject.toml — add [tool.ruff] section when rules pile up
[tool.ruff]
line-length = 88

[tool.ruff.lint]
ignore = ["E712"]  # SQLAlchemy requires == True/False comparisons
per-file-ignores = { "app/routers/*/__init__.py" = ["F401"] }
```

This project currently has no `[tool.ruff]` section — suppressions are inline. If E712
or F401 noqa comments become widespread, add a config section to eliminate the noise.

---

## WARNING: Anti-Patterns

### WARNING: Suppressing F811 (Redefinition)

**The Problem:**
```python
def get_db():
    ...

def get_db():  # noqa: F811 — "override for testing"
    ...
```

**Why This Breaks:** Redefined functions are almost always a bug or dead code. The right
fix is a conditional import or pytest fixture override, not a second definition in the
same file.

**The Fix:** Use dependency injection or test fixtures instead.

---

### WARNING: Suppressing E501 Excessively

Long lines usually signal that a function is doing too much or a variable name is unclear.

```python
# BAD — suppressing symptoms
result = some_service.get_data(param1, param2, param3, param4, param5)  # noqa: E501

# GOOD — break it up
result = some_service.get_data(
    param1, param2, param3,
    param4, param5,
)
```

**Only use `# noqa: E501`** for URLs in comments that cannot be shortened.
