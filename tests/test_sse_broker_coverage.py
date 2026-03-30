"""Tests for app/services/sse_broker.py — SSE fan-out broker.

Targets missing branches to bring coverage from 76% to 85%+.
Covers subscribe, unsubscribe, publish, and listen.

Called by: pytest
Depends on: app.services.sse_broker
"""

import os

os.environ["TESTING"] = "1"

import asyncio

from tests.conftest import engine  # noqa: F401


class TestSSEBrokerInit:
    """Tests for SSEBroker.__init__."""

    def test_broker_has_empty_channels_on_init(self):
        """New broker starts with empty channel dict."""
        from app.services.sse_broker import SSEBroker

        b = SSEBroker()
        assert len(b._channels) == 0

    def test_broker_singleton_exists(self):
        """Module exports a singleton broker instance."""
        from app.services.sse_broker import broker

        assert broker is not None

    def test_queue_maxsize_default(self):
        """Default queue max size is 200."""
        from app.services.sse_broker import SSEBroker

        b = SSEBroker()
        assert b._queue_maxsize == 200


class TestSSEBrokerSubscribe:
    """Tests for SSEBroker.subscribe()."""

    def test_subscribe_creates_queue(self):
        """subscribe() returns an asyncio.Queue."""
        from app.services.sse_broker import SSEBroker

        b = SSEBroker()
        q = b.subscribe("test-channel")
        assert isinstance(q, asyncio.Queue)

    def test_subscribe_adds_queue_to_channel(self):
        """subscribe() adds queue to the channel set."""
        from app.services.sse_broker import SSEBroker

        b = SSEBroker()
        q = b.subscribe("my-channel")
        assert q in b._channels["my-channel"]

    def test_subscribe_multiple_listeners_same_channel(self):
        """Multiple subscribers on same channel each get their own queue."""
        from app.services.sse_broker import SSEBroker

        b = SSEBroker()
        q1 = b.subscribe("channel-a")
        q2 = b.subscribe("channel-a")
        assert q1 is not q2
        assert len(b._channels["channel-a"]) == 2

    def test_subscribe_queue_respects_maxsize(self):
        """Subscribed queue uses the configured maxsize."""
        from app.services.sse_broker import SSEBroker

        b = SSEBroker()
        q = b.subscribe("size-test")
        assert q.maxsize == b._queue_maxsize


class TestSSEBrokerUnsubscribe:
    """Tests for SSEBroker.unsubscribe()."""

    def test_unsubscribe_removes_queue(self):
        """unsubscribe() removes the queue from the channel."""
        from app.services.sse_broker import SSEBroker

        b = SSEBroker()
        q = b.subscribe("unsub-test")
        assert q in b._channels["unsub-test"]

        b.unsubscribe("unsub-test", q)
        assert q not in b._channels["unsub-test"]

    def test_unsubscribe_nonexistent_queue_is_safe(self):
        """unsubscribe() of a queue not in channel does not raise."""
        from app.services.sse_broker import SSEBroker

        b = SSEBroker()
        fake_q = asyncio.Queue()
        # Should not raise even if queue was never subscribed
        b.unsubscribe("nonexistent-channel", fake_q)

    def test_unsubscribe_leaves_other_queues_intact(self):
        """Unsubscribing one queue doesn't remove others."""
        from app.services.sse_broker import SSEBroker

        b = SSEBroker()
        q1 = b.subscribe("multi")
        q2 = b.subscribe("multi")

        b.unsubscribe("multi", q1)
        assert q1 not in b._channels["multi"]
        assert q2 in b._channels["multi"]


class TestSSEBrokerPublish:
    """Tests for SSEBroker.publish()."""

    async def test_publish_sends_to_all_subscribers(self):
        """publish() puts event on all subscriber queues."""
        from app.services.sse_broker import SSEBroker

        b = SSEBroker()
        q1 = b.subscribe("pub-test")
        q2 = b.subscribe("pub-test")

        await b.publish("pub-test", "my-event", "some-data")

        msg1 = q1.get_nowait()
        msg2 = q2.get_nowait()
        assert msg1 == {"event": "my-event", "data": "some-data"}
        assert msg2 == {"event": "my-event", "data": "some-data"}

    async def test_publish_to_empty_channel_is_safe(self):
        """publish() to a channel with no subscribers does not raise."""
        from app.services.sse_broker import SSEBroker

        b = SSEBroker()
        await b.publish("empty-channel", "ping", "")

    async def test_publish_drops_oldest_when_queue_full(self):
        """When queue is full, oldest event is dropped to make room."""
        from app.services.sse_broker import SSEBroker

        b = SSEBroker()
        b._queue_maxsize = 3
        q = b.subscribe("full-test")

        # Fill the queue
        for i in range(3):
            q.put_nowait({"event": f"old-{i}", "data": ""})

        assert q.full()

        # Publish one more — should drop oldest
        await b.publish("full-test", "new-event", "new-data")

        # Queue should now contain "new-event" (oldest was dropped)
        events = []
        while not q.empty():
            events.append(q.get_nowait())

        event_names = [e["event"] for e in events]
        assert "new-event" in event_names

    async def test_publish_default_data_is_empty_string(self):
        """publish() with no data argument defaults to empty string."""
        from app.services.sse_broker import SSEBroker

        b = SSEBroker()
        q = b.subscribe("default-data")

        await b.publish("default-data", "test-event")

        msg = q.get_nowait()
        assert msg["data"] == ""
        assert msg["event"] == "test-event"

    async def test_publish_handles_queue_full_exception_on_put(self):
        """publish() handles QueueFull gracefully when put_nowait fails (queue not full but race)."""
        import asyncio
        from unittest.mock import patch

        from app.services.sse_broker import SSEBroker

        b = SSEBroker()
        q = b.subscribe("full-exception-test")

        # Simulate a queue that claims NOT to be full but put_nowait still raises QueueFull
        with patch.object(q, "full", return_value=False):
            with patch.object(q, "put_nowait", side_effect=asyncio.QueueFull):
                # Should not raise — QueueFull is caught and logged
                await b.publish("full-exception-test", "event", "data")


class TestSSEBrokerListen:
    """Tests for SSEBroker.listen() async generator."""

    async def test_listen_yields_published_events(self):
        """listen() yields events as they are published."""
        from app.services.sse_broker import SSEBroker

        b = SSEBroker()

        received = []

        async def _consumer():
            async for msg in b.listen("listen-test"):
                received.append(msg)
                break  # Exit after first message

        # Publish and consume concurrently
        async def _producer():
            await asyncio.sleep(0.01)
            await b.publish("listen-test", "data-ready", "payload")

        await asyncio.gather(_consumer(), _producer())

        assert len(received) == 1
        assert received[0]["event"] == "data-ready"
        assert received[0]["data"] == "payload"

    async def test_listen_unsubscribes_on_cancel(self):
        """listen() unsubscribes from channel when generator exits via break."""
        from app.services.sse_broker import SSEBroker

        b = SSEBroker()

        result = []

        async def _consumer():
            async for msg in b.listen("cancel-test"):
                result.append(msg)
                break  # Exit immediately after first message

        async def _producer():
            await asyncio.sleep(0)  # Yield so consumer subscribes first
            await b.publish("cancel-test", "stop", "")

        await asyncio.gather(_consumer(), _producer())

        # Consumer exited — should have unsubscribed
        assert len(b._channels["cancel-test"]) == 0
        assert len(result) == 1

    async def test_listen_subscribes_on_start(self):
        """listen() subscribes to channel when generator starts iterating."""
        from app.services.sse_broker import SSEBroker

        b = SSEBroker()
        channel = "subscribe-check-2"

        received = []

        async def _consumer():
            async for msg in b.listen(channel):
                received.append(msg)
                break

        async def _producer():
            await asyncio.sleep(0)
            await b.publish(channel, "hello", "world")

        await asyncio.gather(_consumer(), _producer())
        assert len(received) == 1
        assert received[0]["event"] == "hello"
