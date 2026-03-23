---
name: redis
description: |
  Configures Redis caching with decorators and TTL for the AvailAI FastAPI stack.
  Use when: adding caching to a new endpoint, invalidating cache on mutations,
  manually calling get_cached/set_cached for enrichment data, or debugging cache hits/misses.
allowed-tools: Read, Edit, Write, Glob, Grep, Bash
---

# Redis

AvailAI uses a **Redis-primary, PostgreSQL-fallback** cache (see `app/cache/`). Redis is optional — if unavailable, all reads/writes transparently fall back to the `intel_cache` PostgreSQL table. Never check for Redis availability in application code; the cache layer handles it.

## Quick Start

### Cache an endpoint with `@cached_endpoint`

```python
from app.cache.decorators import cached_endpoint

@router.get("/vendors")
def list_vendors(q: str = "", limit: int = 50, db: Session = Depends(get_db)):
    @cached_endpoint(
        prefix="vendor_list",
        ttl_hours=0.5,
        key_params=["q", "limit"],
    )
    def _fetch(q, limit, db):
        return db.query(VendorCard).filter(...).all()

    return _fetch(q=q, limit=limit, db=db)
```

**Pattern:** define `_fetch` as an inner function decorated with `@cached_endpoint`, then call it with keyword arguments. The decorator reads `key_params` from kwargs, so all cache-differentiated values **must** be passed as kwargs.

### Manual get/set for service-layer caching

```python
from app.cache.intel_cache import get_cached, set_cached

cache_key = f"enrich:{company_id}"
cached = get_cached(cache_key)
if cached:
    return cached

result = call_expensive_api(company_id)
set_cached(cache_key, result, ttl_days=14)
return result
```

### Invalidate on mutation

```python
from app.cache.decorators import invalidate_prefix

# After updating a company record:
invalidate_prefix("company_list")
invalidate_prefix("companies_typeahead")
```

## Key Concepts

| Concept | Details |
|---------|---------|
| `cached_endpoint` | Decorator for endpoint inner functions; handles miss/hit/error silently |
| `get_cached` / `set_cached` | Low-level API for service-layer caching; `ttl_days` is a float |
| `invalidate_prefix` | Deletes all keys matching `prefix:*` in Redis + PostgreSQL |
| `invalidate` | Deletes a single exact cache key |
| Fallback | Redis unavailable → PostgreSQL `intel_cache` table; transparent |
| `TESTING=1` | Disables Redis entirely; no cache reads or writes in tests |

## TTL Guidelines

| Data type | TTL | Example prefix |
|-----------|-----|----------------|
| Volatile list (req, vendor) | 30s – 30min | `req_list`, `vendor_list` |
| Detail page | 1–2h | `company_detail`, `vendor_email_metrics` |
| Enrichment (external API) | 7–14 days | `enrich:*` |
| Static admin analysis | 4h | `analyze_prefixes` |

## See Also

- [patterns](references/patterns.md) — decorator patterns, key design, anti-patterns
- [workflows](references/workflows.md) — adding cache to a new endpoint, invalidation strategy

## Related Skills

- See the **fastapi** skill for route and dependency injection patterns
- See the **sqlalchemy** skill for the PostgreSQL fallback (`intel_cache` table queries)
- See the **ruff** skill for linting cache module files

## Documentation Resources

> Fetch latest redis-py documentation with Context7.

1. `mcp__plugin_context7_context7__resolve-library-id` → search `"redis-py"`
2. Prefer `/websites/` IDs over source repos
3. `mcp__plugin_context7_context7__query-docs` with resolved ID

**Recommended queries:** `"redis setex ttl"`, `"redis scan pattern delete"`, `"redis connection pool"`
