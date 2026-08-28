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
- self-healing: a mid-stream drop reconnects and resumes delivery instead of ending the
  reader task; a listener started before Redis was reachable attaches once it recovers;
  cancellation during the reconnect backoff sleep exits promptly; local (same-process)
  delivery via publish()'s fallback keeps working for the whole outage window
- a true two-instance cross-process check, gated on INTEGRATION_REDIS_URL — same
  convention as tests/integration/test_redis_integration.py (conftest.py blanks
  REDIS_URL for isolation, so a dedicated var carries the live server URL in CI)

Called by: pytest
Depends on: app.services.sse_broker, app.utils.json_helpers, tests.conftest
"""

import asyncio
import contextlib
import os
import time

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


# ── Self-healing: reconnect after a drop / late Redis recovery ─────────


class _DropOncePubSub:
    """Pubsub double whose FIRST generation delivers exactly one message, then raises a
    simulated connection drop — exercising _redis_reader's reconnect path end-to-end.

    Every later generation (post-reconnect) reads normally from the same shared queue.
    ``ConnectionError`` is a real builtin subclassing ``OSError``, so it's caught by
    _redis_reader's ``except (redis.RedisError, OSError)`` exactly like a genuine
    redis-py connection error would be.
    """

    def __init__(self, fake_redis: "_FlakyFakeRedis", channel_queue: asyncio.Queue):
        self._generation = fake_redis.subscribe_count
        self._channel_queue = channel_queue
        self.subscribed = False
        self.closed = False

    async def subscribe(self, _channel):
        self.subscribed = True

    async def listen(self):
        if self._generation == 1:
            msg = await self._channel_queue.get()
            yield msg
            raise ConnectionError("simulated mid-stream drop")
        while True:
            msg = await self._channel_queue.get()
            yield msg

    async def unsubscribe(self, _channel=None):
        pass

    async def aclose(self):
        self.closed = True


class _FlakyFakeRedis:
    """Fake whose pubsub drops the connection once (generation 1) then works normally
    (generation 2+) — the "fails once then works" double the fix-round brief asked
    for."""

    def __init__(self):
        self.channel_queue: asyncio.Queue = asyncio.Queue()
        self.subscribe_count = 0

    def pubsub(self):
        self.subscribe_count += 1
        return _DropOncePubSub(self, self.channel_queue)

    async def publish(self, channel: str, payload: str):
        await self.channel_queue.put({"type": "message", "channel": channel, "data": payload})


async def test_reader_reconnects_after_mid_stream_drop_and_resumes_delivery(monkeypatch):
    """Finding (a): a mid-stream RedisError/OSError must not end the reader task — it
    must reconnect and resume relaying, instead of leaving the listener's queue silent
    for the rest of the SSE connection."""
    import app.services.sse_broker as sse_broker_module

    monkeypatch.setattr(sse_broker_module, "_READER_BACKOFF_SCHEDULE_S", (0.01, 0.01, 0.01, 0.01))

    b = SSEBroker()
    fake_redis = _FlakyFakeRedis()

    async def _fake_get_redis():
        return fake_redis  # always "available" -- only the pubsub stream itself drops

    b._get_redis = _fake_get_redis

    gen = b.listen("flaky-chan")
    received = []

    async def _consume_two():
        async for msg in gen:
            received.append(msg)
            if len(received) == 2:
                return

    consume_task = asyncio.create_task(_consume_two())
    await asyncio.sleep(0)  # let listen() spawn the reader and subscribe (generation 1)

    await fake_redis.publish("sse:flaky-chan", json.dumps({"event": "one", "data": "1"}))
    # generation 1 delivers "one", then raises on its next read -- the simulated drop.
    # The reader reconnects (generation 2) and "two" is only deliverable after that.
    await fake_redis.publish("sse:flaky-chan", json.dumps({"event": "two", "data": "2"}))

    await asyncio.wait_for(consume_task, timeout=3)
    assert received == [{"event": "one", "data": "1"}, {"event": "two", "data": "2"}]
    assert fake_redis.subscribe_count == 2  # one initial connect + one reconnect

    await gen.aclose()


async def test_listener_started_during_outage_attaches_after_recovery(monkeypatch):
    """Finding (b): listen() must not evaluate _get_redis() only once at entry — a
    listener that starts while Redis is down (no client yet) must still attach once
    Redis recovers, instead of staying cross-process-deaf for the life of the
    connection."""
    import app.services.sse_broker as sse_broker_module

    monkeypatch.setattr(sse_broker_module, "_READER_BACKOFF_SCHEDULE_S", (0.01, 0.01, 0.01, 0.01))

    b = SSEBroker()
    fake_redis = _FakeRedis()
    calls = 0

    async def _fake_get_redis():
        nonlocal calls
        calls += 1
        # First few calls simulate the outage (no client yet); recovers after that.
        return None if calls <= 2 else fake_redis

    b._get_redis = _fake_get_redis

    gen = b.listen("recovers-chan")
    consume_task = asyncio.create_task(gen.__anext__())
    await asyncio.sleep(0.1)  # let the reader retry through the simulated outage

    await fake_redis.publish("sse:recovers-chan", json.dumps({"event": "e", "data": "d"}))

    msg = await asyncio.wait_for(consume_task, timeout=3)
    assert msg == {"event": "e", "data": "d"}
    assert calls > 2  # it kept retrying instead of giving up after the first None

    await gen.aclose()


async def test_reader_cancellation_during_backoff_exits_promptly():
    """A listener disconnecting while the reader is mid-backoff must not block cleanup —
    asyncio.sleep responds to task cancellation immediately, so listen()'s finally never
    waits out a pending backoff (uses the REAL 1s-first-step schedule, unpatched, so a
    regression back to a blocking/uncancellable wait would show up as a slow test, not a
    silent pass).

    No message is ever delivered here (Redis is never available), so the listener's own
    ``gen.__anext__()`` is permanently parked at ``await q.get()`` — cancelling that
    *task* (not calling ``gen.aclose()``, which would race a still-pending anext()) is
    what simulates the SSE request handler being torn down mid-connection; the
    CancelledError it raises drives the exact same listen()-finally cleanup chain.
    """
    b = SSEBroker()

    async def _fake_get_redis():
        return None  # Redis never available -- reader stays parked in its backoff loop

    b._get_redis = _fake_get_redis

    gen = b.listen("never-connects-chan")
    consume_task = asyncio.create_task(gen.__anext__())
    await asyncio.sleep(0)  # let listen() spawn the reader; it's now sleeping its ~1s backoff

    start = time.monotonic()
    consume_task.cancel()  # simulates the SSE client disconnecting mid-backoff
    with contextlib.suppress(asyncio.CancelledError):
        await consume_task
    elapsed = time.monotonic() - start

    assert elapsed < 0.5  # nowhere near the ~1s backoff -- cancellation was immediate


async def test_publish_during_outage_reaches_local_listener_same_process():
    """Local fallback must keep working for the whole outage window: publish() falls
    back to _publish_local() when Redis is unavailable, and that write lands on the SAME
    queue listen() reads from (subscribe() registers one queue regardless of Redis
    status) — so a listener in THIS process is never silent during an outage. A listener
    in a *different* worker process would still miss it (no Redis to relay through);
    that loss is the accepted, now explicitly-scoped, remainder of the outage risk."""
    b = SSEBroker()

    async def _fake_get_redis():
        return None  # Redis unavailable for the whole test

    b._get_redis = _fake_get_redis

    gen = b.listen("outage-chan")
    received = []

    async def _consumer():
        async for msg in gen:
            received.append(msg)
            break

    async def _producer():
        await asyncio.sleep(0)
        await b.publish("outage-chan", "still-here", "payload")

    await asyncio.gather(_consumer(), _producer())
    assert received == [{"event": "still-here", "data": "payload"}]

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
