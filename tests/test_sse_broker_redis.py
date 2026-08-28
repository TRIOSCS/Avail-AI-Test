"""tests/test_sse_broker_redis.py — Tests for the Redis-backed SSE broker fan-out.

Covers the Redis pub/sub path added to app.services.sse_broker.SSEBroker:

- channel namespacing / wire-payload serialization (pure unit tests)
- the in-process fallback under TESTING=1 (the actual condition the whole suite runs
  under) — reuses the shape of tests/test_sse_broker_coverage.py to prove nothing changed
- listener cleanup (reader task cancelled, pubsub connection closed, channel entry
  removed) on disconnect, driven against a hand-rolled fake pubsub — no fakeredis
  dependency exists in this repo (checked: not in requirements.txt, not importable), so
  the fake is scoped to exactly the surface _redis_reader calls (pubsub/subscribe/
  listen/unsubscribe/aclose, publish)
- a true two-instance cross-process check, gated on INTEGRATION_REDIS_URL — same
  convention as tests/integration/test_redis_integration.py (conftest.py blanks
  REDIS_URL for isolation, so a dedicated var carries the live server URL in CI)

Called by: pytest
Depends on: app.services.sse_broker, app.utils.json_helpers, tests.conftest
"""

import asyncio
import os

os.environ["TESTING"] = "1"

from uuid import uuid4

import pytest

from app.services.sse_broker import SSEBroker, _redis_channel
from app.utils import json_helpers as json
from tests.conftest import engine  # noqa: F401

INTEGRATION_REDIS_URL = os.environ.get("INTEGRATION_REDIS_URL") or ""


# ── Unit: channel namespacing + serialization ──────────────────────────


class TestRedisChannelNamespacing:
    """_redis_channel() — the sse:{channel} prefix that keeps broker traffic out of the
    rest of the Redis keyspace."""

    def test_namespaces_with_sse_prefix(self):
        assert _redis_channel("search:abc123") == "sse:search:abc123"

    def test_distinct_per_channel_name(self):
        assert _redis_channel("user:1") != _redis_channel("user:2")

    def test_preserves_channel_name_verbatim_after_prefix(self):
        assert _redis_channel("requisitions") == "sse:requisitions"


class TestWirePayloadSerialization:
    """The exact {'event', 'data'} shape publish() PUBLISHes must round-trip back to
    what listen() yields — this is the contract _redis_reader relies on."""

    def test_round_trips_through_json_helpers(self):
        payload = json.dumps({"event": "sighting-updated", "data": '{"requirement_id": 7}'})
        assert json.loads(payload) == {"event": "sighting-updated", "data": '{"requirement_id": 7}'}

    def test_empty_data_round_trips(self):
        payload = json.dumps({"event": "ping", "data": ""})
        assert json.loads(payload) == {"event": "ping", "data": ""}


# ── Fallback path: TESTING disables Redis, byte-identical in-process fan-out ──


class TestFallbackPathUnderTesting:
    """Reuses the shape of tests/test_sse_broker_coverage.py — confirms the Redis
    addition changes nothing under TESTING=1, the condition the whole suite runs
    under."""

    async def test_get_redis_returns_none_and_disables_permanently(self):
        b = SSEBroker()
        assert await b._get_redis() is None
        assert b._redis_disabled is True
        # Second call must not re-probe — still None, still disabled.
        assert await b._get_redis() is None

    async def test_publish_falls_back_to_local_queues(self):
        b = SSEBroker()
        q = b.subscribe("fallback-chan")
        await b.publish("fallback-chan", "evt", "payload")
        assert q.get_nowait() == {"event": "evt", "data": "payload"}

    async def test_listen_yields_without_a_redis_reader_task(self):
        """Listen() under TESTING never creates a Redis reader task — publish() delivers
        straight onto the local queue, exactly as before Redis support existed."""
        b = SSEBroker()
        received = []

        async def _consumer():
            async for msg in b.listen("fallback-listen"):
                received.append(msg)
                break

        async def _producer():
            await asyncio.sleep(0)
            await b.publish("fallback-listen", "hello", "world")

        await asyncio.gather(_consumer(), _producer())
        assert received == [{"event": "hello", "data": "world"}]
        assert len(b._channels["fallback-listen"]) == 0  # unsubscribed on exit


# ── Cleanup: no leaked pubsub connection on listener disconnect ────────


class _FakePubSub:
    """Minimal stand-in for redis.asyncio.client.PubSub — just enough surface
    (subscribe/listen/unsubscribe/aclose) for the reader-task lifecycle test below."""

    def __init__(self, channel_queue: asyncio.Queue):
        self._channel_queue = channel_queue
        self.subscribed = False
        self.unsubscribed = False
        self.closed = False

    async def subscribe(self, _channel):
        self.subscribed = True

    async def listen(self):
        while True:
            msg = await self._channel_queue.get()
            yield msg

    async def unsubscribe(self, _channel=None):
        self.unsubscribed = True

    async def aclose(self):
        self.closed = True


class _FakeRedis:
    """Minimal stand-in for redis.asyncio.Redis — .pubsub() is what _redis_reader calls;
    .publish() feeds the same in-memory queue so publish()/listen() can be driven end-
    to-end without a real server."""

    def __init__(self):
        self.channel_queue: asyncio.Queue = asyncio.Queue()
        self.published: list[tuple[str, str]] = []
        self.pubsubs: list[_FakePubSub] = []

    def pubsub(self):
        ps = _FakePubSub(self.channel_queue)
        self.pubsubs.append(ps)
        return ps

    async def publish(self, channel: str, payload: str):
        self.published.append((channel, payload))
        await self.channel_queue.put({"type": "message", "channel": channel, "data": payload})

    async def ping(self):
        return True


async def test_listen_cleanup_closes_pubsub_and_cancels_reader_on_disconnect():
    """Listen()'s finally cancels the reader task and closes its pubsub — the leak the
    Task 1 brief calls out explicitly."""
    b = SSEBroker()
    fake_redis = _FakeRedis()
    b._redis_client = fake_redis  # short-circuit the probe — pretend Redis is live

    gen = b.listen("cleanup-chan")
    consume_task = asyncio.create_task(gen.__anext__())
    await asyncio.sleep(0)  # let listen() run to `await q.get()`, starting the reader task

    await fake_redis.publish(_redis_channel("cleanup-chan"), json.dumps({"event": "e", "data": "d"}))

    msg = await asyncio.wait_for(consume_task, timeout=2)
    assert msg == {"event": "e", "data": "d"}

    assert len(fake_redis.pubsubs) == 1
    fake_pubsub = fake_redis.pubsubs[0]
    assert fake_pubsub.subscribed is True
    assert fake_pubsub.closed is False  # still open while the listener is connected

    await gen.aclose()  # simulates the SSE client disconnecting

    assert fake_pubsub.unsubscribed is True
    assert fake_pubsub.closed is True  # no leaked pubsub connection
    assert len(b._channels["cleanup-chan"]) == 0  # local queue entry cleaned up too


async def test_listen_reader_task_survives_unrelated_channel_noise():
    """A message of type != 'message' (e.g. the subscribe confirmation redis-py itself
    emits) must not be forwarded to the caller's queue."""
    b = SSEBroker()
    fake_redis = _FakeRedis()
    b._redis_client = fake_redis

    gen = b.listen("noise-chan")
    consume_task = asyncio.create_task(gen.__anext__())
    await asyncio.sleep(0)

    await fake_redis.channel_queue.put({"type": "subscribe", "channel": "sse:noise-chan", "data": 1})
    await fake_redis.publish(_redis_channel("noise-chan"), json.dumps({"event": "real", "data": "x"}))

    msg = await asyncio.wait_for(consume_task, timeout=2)
    assert msg == {"event": "real", "data": "x"}

    await gen.aclose()


# ── True cross-process check: two broker instances, one real Redis ─────


@pytest.mark.integration
@pytest.mark.skipif(
    not INTEGRATION_REDIS_URL,
    reason="INTEGRATION_REDIS_URL not set — real two-instance Redis fan-out test skipped",
)
async def test_publish_on_one_instance_reaches_listener_on_another(monkeypatch):
    """Two independent SSEBroker() instances (simulating two uvicorn workers), each with
    its own lazy Redis client, sharing ONE real Redis: publish() on instance A must
    reach listen() on instance B.

    This is the actual cross-process guarantee Task 1 exists to add — the fake-pubsub
    tests above only prove the reader-task lifecycle, not real Redis wire behavior. Runs
    only against a real server via INTEGRATION_REDIS_URL (set by the CI "Redis
    integration tests" step); everywhere else it's skipped, never flaky.
    """
    monkeypatch.delenv("TESTING", raising=False)
    from app.config import settings

    monkeypatch.setattr(settings, "cache_backend", "redis", raising=False)
    monkeypatch.setattr(settings, "redis_url", INTEGRATION_REDIS_URL, raising=False)

    broker_a = SSEBroker()
    broker_b = SSEBroker()
    channel = f"itest:{uuid4().hex}"

    gen = broker_b.listen(channel)
    consume_task = asyncio.create_task(gen.__anext__())
    await asyncio.sleep(0.2)  # let broker_b's reader task actually SUBSCRIBE on the wire

    await broker_a.publish(channel, "cross-process", "payload")

    msg = await asyncio.wait_for(consume_task, timeout=5)
    assert msg == {"event": "cross-process", "data": "payload"}

    await gen.aclose()
