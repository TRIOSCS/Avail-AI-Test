"""
tests/test_cache_decorator.py — Tests for the @cached_endpoint decorator

Verifies cache hit/miss behavior, key generation, and prefix invalidation.
"""

from unittest.mock import patch


def test_cache_miss_calls_function():
    """On cache miss, the wrapped function is called and result is cached."""
    from app.cache.decorators import cached_endpoint

    call_count = 0

    @cached_endpoint(prefix="test_miss", ttl_hours=1, key_params=["x"])
    def my_func(x, db=None):
        nonlocal call_count
        call_count += 1
        return {"value": x}

    with patch("app.cache.decorators.get_cached", return_value=None), \
         patch("app.cache.decorators.set_cached") as mock_set:
        result = my_func(x=42)

    assert result == {"value": 42}
    assert call_count == 1
    mock_set.assert_called_once()


def test_cache_hit_skips_function():
    """On cache hit, the wrapped function is NOT called."""
    from app.cache.decorators import cached_endpoint

    call_count = 0

    @cached_endpoint(prefix="test_hit", ttl_hours=1, key_params=["x"])
    def my_func(x, db=None):
        nonlocal call_count
        call_count += 1
        return {"value": x}

    cached_data = {"value": 42, "cached": True}
    with patch("app.cache.decorators.get_cached", return_value=cached_data):
        result = my_func(x=42)

    assert result == cached_data
    assert call_count == 0


def test_different_params_different_keys():
    """Different parameter values produce different cache keys."""
    from app.cache.decorators import cached_endpoint

    results = []

    @cached_endpoint(prefix="test_keys", ttl_hours=1, key_params=["x"])
    def my_func(x, db=None):
        return {"value": x}

    with patch("app.cache.decorators.get_cached", return_value=None) as mock_get, \
         patch("app.cache.decorators.set_cached"):
        my_func(x=1)
        my_func(x=2)

    # Should have been called with different cache keys
    keys = [call.args[0] for call in mock_get.call_args_list]
    assert len(set(keys)) == 2  # Two unique keys


def test_invalidate_prefix():
    """invalidate_prefix deletes matching PG cache entries."""
    from app.cache.decorators import invalidate_prefix

    with patch("app.cache.intel_cache._get_redis", return_value=None), \
         patch("app.database.SessionLocal") as mock_session_cls:
        mock_db = mock_session_cls.return_value.__enter__.return_value
        invalidate_prefix("test_prefix")
        mock_db.execute.assert_called_once()
        mock_db.commit.assert_called_once()
