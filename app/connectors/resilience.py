"""Outbound resilience for provider API calls.

Wraps a provider HTTP call with three protections so one flaky/over-limit
vendor API can't waste credits or cascade into failures:

  1. **Circuit breaker** (per provider) — stop hammering a known-down API.
  2. **Retry with backoff** on 429 / 5xx / timeouts, honoring ``Retry-After``.
  3. **Token-bucket rate limit** (per provider, requests/min from config).

It also records health: on success/auth-failure it updates the matching
``ApiSource`` row (status / last_success / last_error) and, on an auth failure,
fires a throttled Teams alert.

Usage (keeps the caller's own ``http`` as the patch point for tests):

    from app.connectors.resilience import resilient_call
    resp = await resilient_call("lusha", lambda: http.post(url, json=payload))
    if resp.status_code != 200:
        ...

On an open breaker or exhausted timeouts, ``ProviderUnavailable`` is raised —
callers already wrap provider calls in try/except and degrade to empty results.

Called by: app/connectors/* and app/services/* enrichment providers.
Depends on: app.connectors.sources (circuit breaker), app.config, app.database.
"""

import asyncio
import logging
import os
import random
import time

import httpx

from app.config import settings

log = logging.getLogger("avail.resilience")

# Statuses worth retrying — transient server/throttle errors.
RETRYABLE_STATUS = {429, 500, 502, 503, 504}
AUTH_STATUS = {401, 403}

# Map short provider name → the ApiSource.name used in the credential store.
_SOURCE_NAMES = {
    "lusha": "lusha_enrichment",
    "apollo": "apollo_enrichment",
    "explorium": "explorium_enrichment",
    "clay": "clay_enrichment",
    "hunter": "hunter_enrichment",
    "rocketreach": "rocketreach_enrichment",
    "clearbit": "clearbit_enrichment",
    "anthropic": "anthropic_ai",
}


class ProviderUnavailable(Exception):
    """Raised when the breaker is open or all retries are exhausted."""


def _testing() -> bool:
    return bool(os.environ.get("TESTING"))


# ── Per-provider token-bucket rate limiting ──────────────────────────


class _TokenBucket:
    def __init__(self, rate_per_sec: float, capacity: float):
        self.rate = rate_per_sec
        self.capacity = capacity
        self.tokens = capacity
        self.updated = time.monotonic()
        self.lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self.lock:
            now = time.monotonic()
            self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
            self.updated = now
            if self.tokens < 1:
                wait = (1 - self.tokens) / self.rate
                await asyncio.sleep(wait)
                self.tokens = 0.0
                self.updated = time.monotonic()
            else:
                self.tokens -= 1


_buckets: dict[str, _TokenBucket] = {}


def _rpm_for(provider: str, override: int | None) -> int:
    if override:
        return override
    return {
        "lusha": settings.lusha_rpm,
        "apollo": settings.apollo_rpm,
        "explorium": settings.explorium_rpm,
        "clay": settings.clay_rpm,
        "hunter": settings.hunter_rpm,
    }.get(provider, settings.default_provider_rpm)


def _bucket_for(provider: str, rpm: int | None) -> _TokenBucket:
    if provider not in _buckets:
        rate = max(_rpm_for(provider, rpm), 1) / 60.0
        # Allow a small burst (~10s of traffic) but never less than 1.
        capacity = max(1.0, _rpm_for(provider, rpm) / 6.0)
        _buckets[provider] = _TokenBucket(rate, capacity)
    return _buckets[provider]


# ── Backoff ──────────────────────────────────────────────────────────


def _retry_after_seconds(resp: httpx.Response) -> float | None:
    val = resp.headers.get("Retry-After")
    if not val:
        return None
    try:
        return max(0.0, float(val))
    except ValueError:
        return None  # HTTP-date form — fall back to exponential backoff


def _backoff(attempt: int) -> float:
    if _testing():
        return 0.0
    return min(30.0, 2 ** attempt + random.uniform(0, 1))


# ── Health bookkeeping ───────────────────────────────────────────────

_last_success_write: dict[str, float] = {}
_last_alert: dict[str, float] = {}
_SUCCESS_WRITE_INTERVAL = 60.0      # throttle last_success writes
_ALERT_INTERVAL = 3600.0            # throttle auth-failure alerts


def record_provider_result(
    provider: str, *, ok: bool, error: str | None = None, auth_failure: bool = False
) -> None:
    """Best-effort: persist provider health to ApiSource; alert on auth failure.

    No-ops under TESTING (no real DB / no outbound alert).
    """
    if _testing():
        return
    source_name = _SOURCE_NAMES.get(provider)
    if not source_name:
        return

    now = time.monotonic()
    if ok and (now - _last_success_write.get(provider, 0)) < _SUCCESS_WRITE_INTERVAL:
        return  # avoid a DB write on every successful call

    try:
        from datetime import datetime, timezone

        from app.database import SessionLocal
        from app.models import ApiSource

        with SessionLocal() as db:
            src = db.query(ApiSource).filter_by(name=source_name).first()
            if not src:
                return
            if ok:
                src.last_success = datetime.now(timezone.utc)
                if src.status == "error":
                    src.status = "active"
                _last_success_write[provider] = now
            else:
                src.last_error = (error or "request failed")[:500]
                if auth_failure:
                    src.status = "error"
            db.commit()
    except Exception as e:
        log.debug("record_provider_result skipped for %s: %s", provider, e)

    if auth_failure and (now - _last_alert.get(provider, 0)) >= _ALERT_INTERVAL:
        _last_alert[provider] = now
        asyncio.create_task(_alert_auth_failure(provider, error or ""))


async def _alert_auth_failure(provider: str, error: str) -> None:
    """Post a throttled auth-failure card to the Teams webhook, if configured."""
    webhook = getattr(settings, "teams_webhook_url", "")
    if not webhook:
        return
    from app.http_client import http

    try:
        await http.post(
            webhook,
            json={
                "type": "message",
                "attachments": [{
                    "contentType": "application/vnd.microsoft.card.adaptive",
                    "content": {
                        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                        "type": "AdaptiveCard",
                        "version": "1.4",
                        "body": [{
                            "type": "TextBlock",
                            "text": f"⚠️ {provider} API auth failure — check the key. ({error[:120]})",
                            "wrap": True,
                        }],
                    },
                }],
            },
            timeout=10,
        )
    except Exception as e:
        log.debug("Teams auth-failure alert failed for %s: %s", provider, e)


# ── The wrapper ──────────────────────────────────────────────────────


async def resilient_call(
    provider: str,
    factory,
    *,
    max_retries: int | None = None,
    rpm: int | None = None,
    retryable_status: set[int] | None = None,
) -> httpx.Response:
    """Run ``factory()`` (a coroutine returning an httpx.Response) with
    breaker + retry/backoff + rate limiting + health bookkeeping.

    Returns the Response (including non-retryable 4xx and auth responses, so the
    caller can inspect status_code). Raises ProviderUnavailable when the breaker
    is open or timeouts are exhausted.
    """
    from app.connectors.sources import get_breaker

    breaker = get_breaker(f"provider:{provider}")
    if breaker.current_state == "open":
        raise ProviderUnavailable(f"{provider} circuit breaker open")

    retries = settings.provider_max_retries if max_retries is None else max_retries
    retryable = RETRYABLE_STATUS if retryable_status is None else retryable_status
    bucket = _bucket_for(provider, rpm)

    for attempt in range(retries + 1):
        await bucket.acquire()
        try:
            resp = await factory()
        except (httpx.TimeoutException, httpx.TransportError) as e:
            breaker.record_failure()
            if attempt < retries:
                await asyncio.sleep(_backoff(attempt))
                continue
            record_provider_result(provider, ok=False, error=f"{type(e).__name__}: {e}")
            raise ProviderUnavailable(f"{provider}: {type(e).__name__}") from e

        status = resp.status_code
        if status in retryable:
            breaker.record_failure()
            if attempt < retries:
                delay = _retry_after_seconds(resp)
                await asyncio.sleep(delay if delay is not None else _backoff(attempt))
                continue
            record_provider_result(provider, ok=False, error=f"HTTP {status}")
            return resp

        if status in AUTH_STATUS:
            breaker.record_failure()
            record_provider_result(
                provider, ok=False, error=f"HTTP {status} (auth)", auth_failure=True
            )
            return resp

        breaker.record_success()
        record_provider_result(provider, ok=(status < 400),
                               error=None if status < 400 else f"HTTP {status}")
        return resp

    raise ProviderUnavailable(provider)  # pragma: no cover (loop returns first)


def reset_state() -> None:
    """Clear rate-limit buckets + health throttles (used by tests)."""
    _buckets.clear()
    _last_success_write.clear()
    _last_alert.clear()
