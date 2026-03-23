# Redis Workflows Reference

## Contents
- Adding cache to a new endpoint (checklist)
- Choosing TTL
- Debugging cache misses
- Testing with cache disabled
- Monthly enrichment cache flush

---

## Adding Cache to a New Endpoint

Copy this checklist and track progress:

- [ ] 1. Choose a unique `prefix` (snake_case, matches the data being cached)
- [ ] 2. Identify which query params differentiate results → these are `key_params`
- [ ] 3. Wrap the DB query in an inner `_fetch` function
- [ ] 4. Apply `@cached_endpoint` with `prefix`, `ttl_hours`, `key_params`
- [ ] 5. Call `_fetch(...)` with all params as kwargs
- [ ] 6. Identify all mutation endpoints that dirty this data
- [ ] 7. Add `invalidate_prefix(prefix)` to those mutations
- [ ] 8. Confirm `TESTING=1` tests pass (cache is disabled in tests)

**Example: new materials endpoint**

```python
from app.cache.decorators import cached_endpoint

@router.get("/materials")
def list_materials(
    q: str = "",
    limit: int = 50,
    offset: int = 0,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    @cached_endpoint(
        prefix="material_list",
        ttl_hours=2,
        key_params=["q", "limit", "offset"],
    )
    def _fetch(q, limit, offset, user, db):
        return build_material_list(db, q, limit, offset)

    return _fetch(q=q, limit=limit, offset=offset, user=user, db=db)
```

---

## Choosing TTL

Use these heuristics:

| Data changes | TTL |
|-------------|-----|
| On every user action (active list) | 30s (`ttl_hours=0.0083`) |
| On explicit saves (vendor/company list) | 30min (`ttl_hours=0.5`) |
| Computed metrics, rarely mutated | 1–4h |
| External API enrichment (metered) | 7–14 days |

For requisition lists use `ttl_hours=0.0083` (30 seconds) — users expect near-real-time updates. For enrichment data from paid APIs, use 14 days to preserve API credits.

---

## Debugging Cache Hits/Misses

The decorator logs at DEBUG level. Enable debug logs for the cache module:

```bash
# In .env or docker compose environment
LOG_LEVEL=DEBUG
```

Look for:
```
DEBUG | Cache HIT: vendor_list:a3f9d1c2b8e4
DEBUG | Cache MISS: vendor_list:a3f9d1c2b8e4
WARNING | Cache read failed for vendor_list:...: Connection refused
```

**Check Redis directly:**

```bash
docker compose exec redis redis-cli
> KEYS intel:company_list:*
> TTL intel:company_list:a3f9d1c2b8e4
> GET intel:company_list:a3f9d1c2b8e4
```

**Check PostgreSQL fallback:**

```sql
SELECT cache_key, expires_at, created_at
FROM intel_cache
WHERE cache_key LIKE 'company_list:%'
ORDER BY created_at DESC
LIMIT 10;
```

**Common causes of unexpected misses:**
1. `key_params` includes a param that changes value on every request (e.g., a timestamp)
2. TTL expired — check `expires_at` in PostgreSQL
3. Redis not running + PostgreSQL fallback also expired
4. Invalidation runs too aggressively (check mutations that call `invalidate_prefix`)

---

## Testing with Cache Disabled

`TESTING=1` prevents Redis initialization (`_get_redis()` returns `None`) and skips PostgreSQL cache reads/writes. No mocking needed.

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_routers.py -v
```

If you need to assert that caching *would* be called (not typical but occasionally useful):

```python
from unittest.mock import patch

def test_vendor_list_calls_cache(client, db_session):
    with patch("app.cache.intel_cache.get_cached", return_value=None) as mock_get, \
         patch("app.cache.intel_cache.set_cached") as mock_set:
        response = client.get("/api/vendors")
        assert response.status_code == 200
        mock_get.assert_called_once()
        mock_set.assert_called_once()
```

**Note:** Mock at `app.cache.intel_cache`, not at the import site in decorators. See the **fastapi** skill for TestClient setup.

---

## Monthly Enrichment Cache Flush

When paid enrichment API credits reset, flush all enrichment cache entries to force fresh queries:

```python
from app.cache.intel_cache import flush_enrichment_cache

count = flush_enrichment_cache()
logger.info("Flushed %d enrichment entries", count)
```

This deletes all `enrich:*` keys from Redis and PostgreSQL. It is safe to call at any time — the next request to any enriched entity will re-query the provider.

The APScheduler job in `app/jobs/` handles this automatically. See the **fastapi** skill for adding a manual admin endpoint to trigger it.

---

## Cache Backend Configuration

```bash
# .env — use Redis (default)
REDIS_URL=redis://redis:6379/0
CACHE_BACKEND=redis

# .env — force PostgreSQL cache (no Redis needed)
CACHE_BACKEND=postgres
```

`CACHE_BACKEND=postgres` is useful in bare-metal deployments without Redis. All `get_cached`/`set_cached` calls fall through to the `intel_cache` table automatically — no code changes needed.
