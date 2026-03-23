# Redis Patterns Reference

## Contents
- Decorator pattern (inner function)
- Manual get/set pattern
- Invalidation patterns
- Key naming conventions
- Anti-patterns

---

## Decorator Pattern: Inner Function

The `@cached_endpoint` decorator works on an **inner function** inside the route, not the route itself. This is required because FastAPI dependencies (`db`, `user`) must be excluded from the cache key.

```python
# app/routers/crm/companies.py
from app.cache.decorators import cached_endpoint

@router.get("/companies")
def list_companies(
    search: str = "",
    limit: int = 50,
    offset: int = 0,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    @cached_endpoint(
        prefix="company_list",
        ttl_hours=0.5,
        key_params=["search", "limit", "offset"],
    )
    def _fetch(search, limit, offset, db):
        return db.query(Company).filter(...).all()

    return _fetch(search=search, limit=limit, offset=offset, db=db)
```

**Why inner function:** `db` and `user` are injected by FastAPI and vary per request (connection object). Including them in the cache key would produce a unique key every request, making caching useless. The decorator automatically excludes `db`, `user`, and `request` from key generation.

**Critical:** Always pass cacheable params as **keyword arguments** to `_fetch`. Positional args are ignored by the key builder.

---

## Per-User Caching

When results differ per user, include `user` as a kwarg — the decorator automatically appends `user.id` to the key.

```python
# app/routers/proactive.py
@cached_endpoint(prefix="proactive_scorecard", ttl_hours=1, key_params=["salesperson_id"])
def _fetch(salesperson_id, db):
    return get_scorecard(db, salesperson_id)

return _fetch(salesperson_id=salesperson_id, db=db)
```

If `user` is passed as a kwarg (even without being in `key_params`), `_uid` is appended to the key dict. For admin-scoped data that doesn't vary by user, omit `user` from `_fetch` args.

---

## Manual get/set in Services

Use `get_cached` / `set_cached` directly in service functions that call expensive external APIs.

```python
# app/services/ai_service.py
from app.cache.intel_cache import get_cached, set_cached

def get_company_intel(company_id: int) -> dict:
    cache_key = f"intel:company:{company_id}"
    cached = get_cached(cache_key)
    if cached:
        return cached

    intel = call_apollo_api(company_id)  # expensive
    set_cached(cache_key, intel, ttl_days=7)
    return intel
```

`set_cached` accepts `ttl_days` as a **float** (`0.5` = 12 hours). It only stores `dict` — never pass a Pydantic model or SQLAlchemy object directly; serialize first.

---

## Cache-Aside for Enrichment Data

```python
# app/enrichment_service.py
cache_key = f"enrich:{domain}"
cached = get_cached(cache_key)
if cached:
    return cached

result = fetch_from_provider(domain)
normalized = normalize_enrichment(result)
set_cached(cache_key, normalized, ttl_days=14)
return normalized
```

Enrichment uses 14-day TTL because provider API credits are metered monthly. The `flush_enrichment_cache()` function in `intel_cache.py` wipes all `enrich:*` keys when credits reset.

---

## Invalidation Patterns

### Prefix invalidation after mutations

```python
# app/routers/htmx_views.py — after company update
from app.cache.decorators import invalidate_prefix

invalidate_prefix("companies_typeahead")
invalidate_prefix("company_list")
```

`invalidate_prefix("company_list")` deletes all keys matching `intel:company_list:*` in Redis and `company_list:%` in PostgreSQL. Always invalidate **all prefixes** that could show stale data after a mutation.

### Single-key invalidation

```python
from app.cache.intel_cache import invalidate

invalidate(f"intel:company:{company_id}")
```

---

## Key Naming Convention

| Layer | Format | Example |
|-------|--------|---------|
| Decorator (auto-generated) | `{prefix}:{md5hash[:12]}` | `company_list:a3f9d1c2b8e4` |
| Manual (service layer) | `{domain}:{entity}:{id}` | `enrich:company:1042` |
| Redis stored with prefix | `intel:{key}` | `intel:company_list:a3f9d1c2b8e4` |

The `_REDIS_PREFIX = "intel:"` is prepended automatically by `get_cached`/`set_cached`. Don't include `intel:` in your keys when calling these functions.

---

## WARNING: Caching Response Objects

**The Problem:**

```python
# BAD — caches an HTMLResponse, not serializable data
@cached_endpoint(prefix="vendor_page", ttl_hours=1, key_params=["id"])
def _fetch(id, db):
    return templates.TemplateResponse("vendor.html", {...})
```

**Why This Breaks:**
The decorator only caches `dict` and `list` results. `TemplateResponse` is skipped silently, so every request is a cache miss with no error logged — you'll never know caching is broken.

**The Fix:**

```python
# GOOD — cache the data dict, render in the route
@cached_endpoint(prefix="vendor_data", ttl_hours=1, key_params=["id"])
def _fetch(id, db):
    return {"vendor": vendor.to_dict(), "metrics": compute_metrics(db, id)}

data = _fetch(id=card_id, db=db)
return templates.TemplateResponse("vendor.html", {"request": request, **data})
```

---

## WARNING: Stale Cache After Writes

**The Problem:**

```python
# BAD — updates company but never invalidates cache
@router.put("/companies/{company_id}")
def update_company(company_id: int, ...):
    company.name = data.name
    db.commit()
    return {"ok": True}
    # company_list and company_detail are now stale
```

**The Fix:**

```python
# GOOD — always invalidate related prefixes after mutations
from app.cache.decorators import invalidate_prefix
from app.cache.intel_cache import invalidate

company.name = data.name
db.commit()

invalidate_prefix("company_list")
invalidate_prefix("companies_typeahead")
invalidate(f"company_detail:{company_id}")
```

Map every write operation to the cache prefixes it can invalidate. Keep this mapping near the mutation code, not in a separate file.
