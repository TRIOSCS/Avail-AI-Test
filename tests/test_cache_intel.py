"""test_cache_intel.py — Tests for app/cache/intel_cache.py.

Tests Redis + PostgreSQL fallback caching for intel data.

Called by: pytest
Depends on: app/cache/intel_cache.py
"""

import json
import os
from unittest.mock import MagicMock, patch

import app.cache.intel_cache as cache_mod
from app.cache.intel_cache import (
    _get_redis,
    cleanup_expired,
    get_cached,
    invalidate,
    set_cached,
)

# ═══════════════════════════════════════════════════════════════════════
#  _get_redis — TESTING=1 returns None
# ═══════════════════════════════════════════════════════════════════════


class TestGetRedis:
    def setup_method(self):
        """Reset lazy-init state before each test."""
        cache_mod._redis_client = None
        cache_mod._redis_init_attempted = False

    def test_testing_env_returns_none(self):
        # TESTING=1 is set in conftest.py
        result = _get_redis()
        assert result is None

    def test_caches_result_on_second_call(self):
        _get_redis()
        cache_mod._redis_init_attempted = True
        result = _get_redis()
        assert result is None  # Same cached None


# ═══════════════════════════════════════════════════════════════════════
#  get_cached — Redis hit / miss / error
# ═══════════════════════════════════════════════════════════════════════


class TestGetCachedRedis:
    @patch("app.cache.intel_cache._get_redis")
    def test_redis_hit(self, mock_get_redis):
        mock_redis = MagicMock()
        mock_redis.get.return_value = json.dumps({"company": "Acme"})
        mock_get_redis.return_value = mock_redis

        result = get_cached("company:acme")
        assert result == {"company": "Acme"}
        mock_redis.get.assert_called_once_with("intel:company:acme")

    @patch("app.cache.intel_cache._get_redis")
    def test_redis_miss(self, mock_get_redis):
        mock_redis = MagicMock()
        mock_redis.get.return_value = None
        mock_get_redis.return_value = mock_redis

        result = get_cached("company:missing")
        assert result is None

    @patch("app.cache.intel_cache.SessionLocal")
    @patch("app.cache.intel_cache._get_redis")
    def test_redis_error_falls_through_to_pg(self, mock_get_redis, mock_session_local):
        mock_redis = MagicMock()
        mock_redis.get.side_effect = Exception("Redis connection lost")
        mock_get_redis.return_value = mock_redis

        # PG also returns None (miss)
        mock_db = MagicMock()
        mock_db.__enter__ = MagicMock(return_value=mock_db)
        mock_db.__exit__ = MagicMock(return_value=False)
        mock_db.execute.return_value.fetchone.return_value = None
        mock_session_local.return_value = mock_db

        result = get_cached("company:error")
        assert result is None


class TestGetCachedPostgres:
    @patch("app.cache.intel_cache.SessionLocal")
    @patch("app.cache.intel_cache._get_redis", return_value=None)
    def test_pg_hit(self, mock_redis, mock_session_local):
        mock_db = MagicMock()
        mock_db.__enter__ = MagicMock(return_value=mock_db)
        mock_db.__exit__ = MagicMock(return_value=False)
        mock_db.execute.return_value.fetchone.return_value = ({"intel": "data"},)
        mock_session_local.return_value = mock_db

        result = get_cached("company:test")
        assert result == {"intel": "data"}

    @patch("app.cache.intel_cache.SessionLocal")
    @patch("app.cache.intel_cache._get_redis", return_value=None)
    def test_pg_miss(self, mock_redis, mock_session_local):
        mock_db = MagicMock()
        mock_db.__enter__ = MagicMock(return_value=mock_db)
        mock_db.__exit__ = MagicMock(return_value=False)
        mock_db.execute.return_value.fetchone.return_value = None
        mock_session_local.return_value = mock_db

        result = get_cached("company:missing")
        assert result is None

    @patch("app.cache.intel_cache.SessionLocal")
    @patch("app.cache.intel_cache._get_redis", return_value=None)
    def test_pg_error(self, mock_redis, mock_session_local):
        mock_session_local.side_effect = Exception("DB connection failed")

        result = get_cached("company:error")
        assert result is None


# ═══════════════════════════════════════════════════════════════════════
#  set_cached — Redis write, Redis error → PG fallback
# ═══════════════════════════════════════════════════════════════════════


class TestSetCached:
    @patch("app.cache.intel_cache._get_redis")
    def test_redis_write_success(self, mock_get_redis):
        mock_redis = MagicMock()
        mock_get_redis.return_value = mock_redis

        set_cached("company:acme", {"intel": "data"}, ttl_days=7)

        mock_redis.setex.assert_called_once_with(
            "intel:company:acme",
            7 * 86400,
            '{"intel":"data"}',
        )

    @patch("app.cache.intel_cache.SessionLocal")
    @patch("app.cache.intel_cache._get_redis")
    def test_redis_error_falls_back_to_pg(self, mock_get_redis, mock_session_local):
        mock_redis = MagicMock()
        mock_redis.setex.side_effect = Exception("Redis write failed")
        mock_get_redis.return_value = mock_redis

        mock_db = MagicMock()
        mock_db.__enter__ = MagicMock(return_value=mock_db)
        mock_db.__exit__ = MagicMock(return_value=False)
        mock_session_local.return_value = mock_db

        set_cached("company:acme", {"intel": "data"}, ttl_days=7)

        mock_db.execute.assert_called_once()
        mock_db.commit.assert_called_once()

    @patch("app.cache.intel_cache.SessionLocal")
    @patch("app.cache.intel_cache._get_redis", return_value=None)
    def test_pg_write_when_no_redis(self, mock_redis, mock_session_local):
        mock_db = MagicMock()
        mock_db.__enter__ = MagicMock(return_value=mock_db)
        mock_db.__exit__ = MagicMock(return_value=False)
        mock_session_local.return_value = mock_db

        set_cached("company:test", {"data": True})

        mock_db.execute.assert_called_once()
        mock_db.commit.assert_called_once()

    @patch("app.cache.intel_cache.SessionLocal")
    @patch("app.cache.intel_cache._get_redis", return_value=None)
    def test_pg_error_logged_not_raised(self, mock_redis, mock_session_local):
        mock_session_local.side_effect = Exception("DB error")

        # Should not raise
        set_cached("company:err", {"data": True})


# ═══════════════════════════════════════════════════════════════════════
#  invalidate — Redis delete + PG delete
# ═══════════════════════════════════════════════════════════════════════


class TestInvalidate:
    @patch("app.cache.intel_cache.SessionLocal")
    @patch("app.cache.intel_cache._get_redis")
    def test_redis_delete(self, mock_get_redis, mock_session_local):
        mock_redis = MagicMock()
        mock_get_redis.return_value = mock_redis

        mock_db = MagicMock()
        mock_db.__enter__ = MagicMock(return_value=mock_db)
        mock_db.__exit__ = MagicMock(return_value=False)
        mock_session_local.return_value = mock_db

        invalidate("company:acme")

        mock_redis.delete.assert_called_once_with("intel:company:acme")
        mock_db.execute.assert_called_once()

    @patch("app.cache.intel_cache.SessionLocal")
    @patch("app.cache.intel_cache._get_redis", return_value=None)
    def test_pg_delete_when_no_redis(self, mock_redis, mock_session_local):
        mock_db = MagicMock()
        mock_db.__enter__ = MagicMock(return_value=mock_db)
        mock_db.__exit__ = MagicMock(return_value=False)
        mock_session_local.return_value = mock_db

        invalidate("company:test")

        mock_db.execute.assert_called_once()
        mock_db.commit.assert_called_once()

    @patch("app.cache.intel_cache.SessionLocal")
    @patch("app.cache.intel_cache._get_redis")
    def test_redis_error_still_tries_pg(self, mock_get_redis, mock_session_local):
        mock_redis = MagicMock()
        mock_redis.delete.side_effect = Exception("Redis error")
        mock_get_redis.return_value = mock_redis

        mock_db = MagicMock()
        mock_db.__enter__ = MagicMock(return_value=mock_db)
        mock_db.__exit__ = MagicMock(return_value=False)
        mock_session_local.return_value = mock_db

        invalidate("company:err")
        mock_db.execute.assert_called_once()


# ═══════════════════════════════════════════════════════════════════════
#  cleanup_expired
# ═══════════════════════════════════════════════════════════════════════


class TestCleanupExpired:
    @patch("app.cache.intel_cache.SessionLocal")
    def test_deletes_expired_batched(self, mock_session_local):
        mock_db = MagicMock()
        mock_db.__enter__ = MagicMock(return_value=mock_db)
        mock_db.__exit__ = MagicMock(return_value=False)
        # First batch deletes 500, second batch deletes 0 → done
        result1 = MagicMock()
        result1.rowcount = 500
        result2 = MagicMock()
        result2.rowcount = 0
        mock_db.execute.side_effect = [result1, result2]
        mock_session_local.return_value = mock_db

        count = cleanup_expired()
        assert count == 500

    @patch("app.cache.intel_cache.SessionLocal")
    def test_no_expired_returns_zero(self, mock_session_local):
        mock_db = MagicMock()
        mock_db.__enter__ = MagicMock(return_value=mock_db)
        mock_db.__exit__ = MagicMock(return_value=False)
        result = MagicMock()
        result.rowcount = 0
        mock_db.execute.return_value = result
        mock_session_local.return_value = mock_db

        count = cleanup_expired()
        assert count == 0

    @patch("app.cache.intel_cache.SessionLocal")
    def test_db_error_returns_zero(self, mock_session_local):
        mock_session_local.side_effect = Exception("DB error")

        count = cleanup_expired()
        assert count == 0

    @patch("app.cache.intel_cache.SessionLocal")
    def test_multiple_batches(self, mock_session_local):
        mock_db = MagicMock()
        mock_db.__enter__ = MagicMock(return_value=mock_db)
        mock_db.__exit__ = MagicMock(return_value=False)
        # 1000 (full batch) → 200 (partial) → done
        r1 = MagicMock()
        r1.rowcount = 1000
        r2 = MagicMock()
        r2.rowcount = 200
        mock_db.execute.side_effect = [r1, r2]
        mock_session_local.return_value = mock_db

        count = cleanup_expired()
        assert count == 1200


# ═══════════════════════════════════════════════════════════════════════
#  _get_redis — non-TESTING paths (lines 39-59)
# ═══════════════════════════════════════════════════════════════════════


class TestGetRedisNonTesting:
    def setup_method(self):
        """Reset lazy-init state before each test."""
        cache_mod._redis_client = None
        cache_mod._redis_init_attempted = False

    def teardown_method(self):
        """Reset after each test and restore TESTING env."""
        cache_mod._redis_client = None
        cache_mod._redis_init_attempted = False
        os.environ["TESTING"] = "1"

    @patch.dict(os.environ, {"TESTING": ""})
    def test_postgres_backend_skips_redis(self):
        """When cache_backend is postgres, returns None without trying Redis."""
        with patch("app.config.settings") as mock_settings:
            mock_settings.cache_backend = "postgres"
            result = _get_redis()
            assert result is None

    @patch.dict(os.environ, {"TESTING": ""})
    def test_redis_connection_failure_returns_none(self):
        """When Redis connection fails, returns None."""
        with patch("app.config.settings") as mock_settings:
            mock_settings.cache_backend = "redis"
            mock_settings.redis_url = "redis://localhost:6379/15"
            with patch("redis.from_url") as mock_from_url:
                mock_from_url.return_value.ping.side_effect = Exception("Connection refused")
                result = _get_redis()
                assert result is None

    @patch.dict(os.environ, {"TESTING": ""})
    def test_redis_connection_success(self):
        """When Redis connects successfully, returns client."""
        with patch("app.config.settings") as mock_settings:
            mock_settings.cache_backend = "redis"
            mock_settings.redis_url = "redis://localhost:6379/0"
            mock_client = MagicMock()
            mock_client.ping.return_value = True
            with patch("redis.from_url", return_value=mock_client):
                result = _get_redis()
                assert result is mock_client


# ═══════════════════════════════════════════════════════════════════════
#  invalidate — PG error path (lines 151-152)
# ═══════════════════════════════════════════════════════════════════════


class TestInvalidatePgError:
    @patch("app.cache.intel_cache.SessionLocal")
    @patch("app.cache.intel_cache._get_redis", return_value=None)
    def test_pg_error_logged_not_raised(self, mock_redis, mock_session_local):
        """PG error during invalidate is caught silently."""
        mock_session_local.side_effect = Exception("DB connection error")
        invalidate("company:test")  # Should not raise
