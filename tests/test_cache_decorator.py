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

    with (
        patch("app.cache.decorators.get_cached", return_value=None),
        patch("app.cache.decorators.set_cached") as mock_set,
    ):
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

    @cached_endpoint(prefix="test_keys", ttl_hours=1, key_params=["x"])
    def my_func(x, db=None):
        return {"value": x}

    with (
        patch("app.cache.decorators.get_cached", return_value=None) as mock_get,
        patch("app.cache.decorators.set_cached"),
    ):
        my_func(x=1)
        my_func(x=2)

    # Should have been called with different cache keys
    keys = [call.args[0] for call in mock_get.call_args_list]
    assert len(set(keys)) == 2  # Two unique keys


def test_invalidate_prefix():
    """invalidate_prefix deletes matching PG cache entries."""
    from app.cache.decorators import invalidate_prefix

    with (
        patch("app.cache.intel_cache._get_redis", return_value=None),
        patch("app.database.SessionLocal") as mock_session_cls,
    ):
        mock_db = mock_session_cls.return_value.__enter__.return_value
        invalidate_prefix("test_prefix")
        mock_db.execute.assert_called_once()
        mock_db.commit.assert_called_once()


# ── Additional coverage for uncovered lines ────────────────────────


def test_key_params_none_uses_all_kwargs():
    """When key_params is None, all kwargs except db/user/request are used (line 43)."""
    from app.cache.decorators import cached_endpoint

    @cached_endpoint(prefix="test_all_kwargs", ttl_hours=1, key_params=None)
    def my_func(sort_by="name", order="asc", db=None, user=None, request=None):
        return {"data": True}

    with (
        patch("app.cache.decorators.get_cached", return_value=None) as mock_get,
        patch("app.cache.decorators.set_cached"),
    ):
        my_func(sort_by="name", order="asc", db="ignored", user="ignored", request="ignored")

    # The key should include sort_by and order, but NOT db, user, request
    mock_get.assert_called_once()


def test_cache_read_error_handled():
    """get_cached exception is caught, function still runs (lines 56-57)."""
    from app.cache.decorators import cached_endpoint

    @cached_endpoint(prefix="test_read_err", ttl_hours=1, key_params=["x"])
    def my_func(x):
        return {"value": x}

    with (
        patch("app.cache.decorators.get_cached", side_effect=Exception("Redis error")),
        patch("app.cache.decorators.set_cached"),
    ):
        result = my_func(x=1)

    assert result == {"value": 1}


def test_cache_write_error_handled():
    """set_cached exception is caught, result still returned (lines 67-68)."""
    from app.cache.decorators import cached_endpoint

    @cached_endpoint(prefix="test_write_err", ttl_hours=1, key_params=["x"])
    def my_func(x):
        return {"value": x}

    with (
        patch("app.cache.decorators.get_cached", return_value=None),
        patch("app.cache.decorators.set_cached", side_effect=Exception("Write failed")),
    ):
        result = my_func(x=1)

    assert result == {"value": 1}


def test_non_dict_result_not_cached():
    """Non-dict/list results (e.g. Response objects) are NOT cached."""
    from app.cache.decorators import cached_endpoint

    @cached_endpoint(prefix="test_no_cache", ttl_hours=1, key_params=["x"])
    def my_func(x):
        return "plain string"

    with (
        patch("app.cache.decorators.get_cached", return_value=None),
        patch("app.cache.decorators.set_cached") as mock_set,
    ):
        result = my_func(x=1)

    assert result == "plain string"
    mock_set.assert_not_called()


def test_list_result_is_cached():
    """List results are cached (same as dict)."""
    from app.cache.decorators import cached_endpoint

    @cached_endpoint(prefix="test_list", ttl_hours=1, key_params=["x"])
    def my_func(x):
        return [1, 2, 3]

    with (
        patch("app.cache.decorators.get_cached", return_value=None),
        patch("app.cache.decorators.set_cached") as mock_set,
    ):
        result = my_func(x=1)

    assert result == [1, 2, 3]
    mock_set.assert_called_once()


def test_cache_prefix_attribute():
    """Wrapper exposes cache_prefix attribute for invalidation."""
    from app.cache.decorators import cached_endpoint

    @cached_endpoint(prefix="my_prefix", ttl_hours=1)
    def my_func():
        return {}

    assert my_func.cache_prefix == "my_prefix"


def test_invalidate_prefix_with_redis():
    """invalidate_prefix uses Redis SCAN to delete by pattern (lines 89-99)."""
    from unittest.mock import MagicMock

    from app.cache.decorators import invalidate_prefix

    mock_redis = MagicMock()
    mock_redis.scan.side_effect = [
        (42, ["intel:perf:key1", "intel:perf:key2"]),
        (0, ["intel:perf:key3"]),
    ]

    with (
        patch("app.cache.intel_cache._get_redis", return_value=mock_redis),
        patch("app.database.SessionLocal") as mock_session_cls,
    ):
        mock_db = mock_session_cls.return_value.__enter__.return_value
        invalidate_prefix("perf")

    assert mock_redis.delete.call_count == 2
    mock_db.execute.assert_called_once()


def test_invalidate_prefix_redis_error():
    """Redis error during prefix invalidation is caught (line 99)."""
    from unittest.mock import MagicMock

    from app.cache.decorators import invalidate_prefix

    mock_redis = MagicMock()
    mock_redis.scan.side_effect = Exception("Redis error")

    with (
        patch("app.cache.intel_cache._get_redis", return_value=mock_redis),
        patch("app.database.SessionLocal") as mock_session_cls,
    ):
        mock_db = mock_session_cls.return_value.__enter__.return_value
        invalidate_prefix("perf")  # Should not raise

    mock_db.execute.assert_called_once()


def test_invalidate_prefix_pg_error():
    """PG error during prefix invalidation is caught (lines 113-114)."""
    from app.cache.decorators import invalidate_prefix

    with (
        patch("app.cache.intel_cache._get_redis", return_value=None),
        patch("app.database.SessionLocal") as mock_session_cls,
    ):
        mock_session_cls.return_value.__enter__.side_effect = Exception("PG error")
        invalidate_prefix("perf")  # Should not raise
