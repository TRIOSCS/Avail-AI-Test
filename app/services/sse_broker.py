"""Server-Sent Events broker for real-time page updates.

Manages a set of connected SSE clients per channel. When a change event fires (e.g.
requisition status change), all listeners on that channel receive a push notification.

Redis-backed fan-out (new): when Redis is configured and reachable, publish() does a
Redis PUBLISH on a namespaced channel (``sse:{channel}``) and every listen() call runs a
background pubsub reader task that relays messages onto the caller's local queue. This is
what lets a publish() issued in one worker process reach a listener connected to a
*different* worker process — the scenario that matters once the scheduler moves out of
the web process (it already publishes today, e.g. app/email_service.py) and once uvicorn
runs multiple workers. Redis unavailable/TESTING: falls straight back to the original
in-process dict fan-out, byte-behavior-identical to before Redis support existed.

The public API (subscribe/unsubscribe/publish/listen) and the module-level ``broker``
singleton are unchanged — only the internals of publish()/listen() gained a Redis path.

Called by: app/routers/events.py, app/routers/htmx/search_views.py,
app/routers/sightings.py, app/routers/crm/offers.py, app/search_service.py,
app/email_service.py (scheduler path), app/services/customer_enrichment_service.py
Depends on: asyncio, redis (for the RedisError/OSError exception types), redis.asyncio
(lazy import, only when Redis is configured — no connection opens at import time),
app.config.settings, app.utils.json_helpers
"""

import asyncio
import contextlib
import os
import time
from collections import defaultdict
from collections.abc import AsyncGenerator

import redis
from loguru import logger

from app.utils import json_helpers as json

_REDIS_CHANNEL_PREFIX = "sse:"

# While degraded, re-attempt the real Redis at most this often — mirrors
# app.cache.redis_probe.REPROBE_INTERVAL_S so a Redis blip self-heals without hammering
# the socket-connect timeout on every publish()/listen() call.
_REDIS_REPROBE_INTERVAL_S = 30.0


def _redis_channel(channel: str) -> str:
    """Namespace a broker channel name for the Redis pub/sub keyspace."""
    return f"{_REDIS_CHANNEL_PREFIX}{channel}"


class SSEBroker:
    """Fan-out broker for SSE channels.

    Each channel (e.g. 'requisitions') has a set of asyncio.Queue listeners. publish()
    pushes to all queues; subscribe() yields from one queue. When Redis is configured
    and reachable, delivery is routed through Redis PUBLISH/SUBSCRIBE instead (see
    module docstring) so a publish() and a listen() can live in different worker
    processes; otherwise this is pure in-process fan-out, exactly as before Redis
    support was added.
    """

    def __init__(self):
        self._channels: dict[str, set[asyncio.Queue]] = defaultdict(set)
        self._queue_maxsize = 200
        # Lazy asyncio Redis client + probe state — mirrors app.cache.redis_probe.RedisProbe
        # (disabled / degraded / backoff), reimplemented async here because publish() and
        # listen() both run on the request event loop and RedisProbe's connect-and-ping
        # contract is synchronous (redis.from_url(...).ping()), which would block it.
        self._redis_client = None
        self._redis_disabled = False  # TESTING or cache_backend != "redis" — permanent
        self._redis_degraded = False  # a probe failed and we have not yet recovered
        self._redis_last_probe = 0.0

    async def _get_redis(self):
        """Return a live ``redis.asyncio`` client, or None when Redis is unavailable.

        Never raises — a connect/ping failure marks the broker degraded and returns None
        so callers fall back to the in-process path. Re-probes the real Redis at most once
        per ``_REDIS_REPROBE_INTERVAL_S`` while degraded and recovers automatically.
        TESTING or a non-Redis ``cache_backend`` disables the probe permanently (no
        retries), matching ``app.cache.intel_cache._connect_intel_redis``.
        """
        if self._redis_disabled:
            return None
        if self._redis_client is not None:
            return self._redis_client

        now = time.monotonic()
        if self._redis_last_probe and (now - self._redis_last_probe) < _REDIS_REPROBE_INTERVAL_S:
            return None  # inside the backoff window — stay degraded without hammering
        self._redis_last_probe = now

        if os.environ.get("TESTING"):
            self._redis_disabled = True
            return None

        from app.config import settings

        if settings.cache_backend != "redis":
            self._redis_disabled = True
            return None

        try:
            import redis.asyncio as aioredis

            client = aioredis.from_url(
                settings.redis_url,
                decode_responses=True,
                socket_connect_timeout=3,
                socket_timeout=2,
                retry_on_timeout=True,
            )
            await client.ping()
        except (redis.RedisError, OSError) as e:
            was_degraded = self._redis_degraded
            self._redis_degraded = True
            log = logger.warning if not was_degraded else logger.debug
            log("SSE broker: Redis degraded (in-process fallback active): {}", e)
            return None

        if self._redis_degraded:
            logger.info("SSE broker: Redis recovered — pub/sub fan-out re-enabled")
        self._redis_degraded = False
        self._redis_client = client
        return client

    def subscribe(self, channel: str) -> asyncio.Queue:
        """Create a new listener queue for the given channel."""
        q: asyncio.Queue = asyncio.Queue(maxsize=self._queue_maxsize)
        self._channels[channel].add(q)
        logger.debug(f"SSE: new subscriber on '{channel}' (total: {len(self._channels[channel])})")
        return q

    def unsubscribe(self, channel: str, q: asyncio.Queue):
        """Remove a listener queue from the channel."""
        self._channels[channel].discard(q)
        logger.debug(f"SSE: unsubscribed from '{channel}' (total: {len(self._channels[channel])})")

    async def publish(self, channel: str, event: str, data: str = ""):
        """Push an event to all listeners on the channel.

        Redis available: PUBLISH on the namespaced Redis channel — every listen() reader
        task (this process and any other worker process sharing the same Redis) picks it
        up. Redis unavailable/TESTING, or the PUBLISH itself fails: the original direct
        write onto each locally registered queue, so an event is never silently dropped
        just because of a transient Redis blip.
        """
        redis_client = await self._get_redis()
        if redis_client is not None:
            payload = json.dumps({"event": event, "data": data})
            try:
                await redis_client.publish(_redis_channel(channel), payload)
                return
            except (redis.RedisError, OSError) as e:
                logger.warning("SSE broker: Redis publish failed ({}) — falling back to in-process delivery", e)
        self._publish_local(channel, event, data)

    def _publish_local(self, channel: str, event: str, data: str) -> None:
        """Direct in-process fan-out — the original (pre-Redis) publish() body."""
        listeners = list(self._channels.get(channel, set()))
        for q in listeners:
            try:
                if q.full():
                    # Keep queue bounded for slow subscribers.
                    q.get_nowait()
                q.put_nowait({"event": event, "data": data})
            except asyncio.QueueFull:
                logger.warning("SSE: dropped event — queue full")

    async def listen(self, channel: str) -> AsyncGenerator[dict]:
        """Yield events from the channel as they arrive.

        Always registers a local queue via subscribe() — that queue is the delivery
        buffer callers await on either way. When Redis is available, a background task
        (``_redis_reader``) additionally subscribes to the namespaced Redis channel and
        relays each message onto that same queue; that reader task is what lets this
        listener see a publish() issued by a *different* process. The reader task is
        cancelled and its pubsub connection closed in ``finally`` — alongside the existing
        unsubscribe() — so a disconnecting SSE client never leaks a Redis pubsub
        connection, and never leaves a dangling entry in ``_channels`` either.
        """
        q = self.subscribe(channel)
        reader_task: asyncio.Task | None = None
        redis_client = await self._get_redis()
        if redis_client is not None:
            reader_task = asyncio.create_task(self._redis_reader(redis_client, channel, q))
        try:
            while True:
                msg = await q.get()
                yield msg
        finally:
            if reader_task is not None:
                reader_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await reader_task
            self.unsubscribe(channel, q)

    async def _redis_reader(self, redis_client, channel: str, q: asyncio.Queue) -> None:
        """Background task: relay messages from the Redis channel onto *q*.

        One dedicated pubsub connection per listen() call while Redis is active — it is
        always closed in ``finally`` (whether from listen()'s cleanup cancelling this
        task, or the connection dropping on its own), so listeners never accumulate open
        pubsub connections.
        """
        pubsub = redis_client.pubsub()
        try:
            await pubsub.subscribe(_redis_channel(channel))
            async for message in pubsub.listen():
                if message["type"] != "message":
                    continue  # subscribe/unsubscribe confirmations — not payloads
                try:
                    msg = json.loads(message["data"])
                except (TypeError, ValueError):
                    logger.warning("SSE broker: dropped malformed Redis message on '{}'", channel)
                    continue
                try:
                    if q.full():
                        q.get_nowait()
                    q.put_nowait(msg)
                except asyncio.QueueFull:
                    logger.warning("SSE: dropped Redis-relayed event — queue full")
        except asyncio.CancelledError:
            raise
        except (redis.RedisError, OSError) as e:
            logger.warning("SSE broker: Redis pubsub reader for '{}' stopped: {}", channel, e)
        finally:
            # Best-effort teardown — the connection may already be dead (that's often why
            # we're here), so a failure closing it must not mask the original error.
            with contextlib.suppress(redis.RedisError, OSError):
                await pubsub.unsubscribe(_redis_channel(channel))
            with contextlib.suppress(redis.RedisError, OSError):
                await pubsub.aclose()


# Singleton broker instance
broker = SSEBroker()
