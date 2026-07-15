"""json_helpers.py — Fast JSON serialization via orjson.

Drop-in replacement for stdlib json.dumps/json.loads, used in the caching
layer for faster Redis/PostgreSQL serialization.

Called by: app/cache/decorators.py, app/cache/intel_cache.py
Depends on: orjson
"""

import orjson


def dumps(obj, *, sort_keys: bool = False, default=None) -> str:
    """Serialize obj to JSON string.

    Mirrors json.dumps() signature for the params we use. Returns str (not bytes) for
    compatibility with Redis and SQL.
    """
    option = orjson.OPT_SORT_KEYS if sort_keys else 0
    payload: str = orjson.dumps(obj, option=option, default=default).decode()
    return payload


def loads(s):
    """Deserialize JSON string or bytes to Python object."""
    return orjson.loads(s)
