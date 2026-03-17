"""Server-Sent Events broker for real-time page updates.

Manages a set of connected SSE clients per channel. When a change
event fires (e.g. requisition status change), all listeners on
that channel receive a push notification.

Called by: app/routers/requisitions2.py (stream endpoint + action endpoints)
Depends on: asyncio
"""

import asyncio
from collections import defaultdict
from typing import AsyncGenerator

from loguru import logger


class SSEBroker:
    """Fan-out broker for SSE channels.

    Each channel (e.g. 'requisitions') has a set of asyncio.Queue listeners. publish()
    pushes to all queues; subscribe() yields from one queue.
    """

    def __init__(self):
        self._channels: dict[str, set[asyncio.Queue]] = defaultdict(set)
        self._queue_maxsize = 200

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
        """Push an event to all listeners on the channel."""
        listeners = list(self._channels.get(channel, set()))
        for q in listeners:
            try:
                if q.full():
                    # Keep queue bounded for slow subscribers.
                    q.get_nowait()
                q.put_nowait({"event": event, "data": data})
            except asyncio.QueueFull:
                logger.warning("SSE: dropped event — queue full")

    async def listen(self, channel: str) -> AsyncGenerator[dict, None]:
        """Yield events from the channel as they arrive."""
        q = self.subscribe(channel)
        try:
            while True:
                msg = await q.get()
                yield msg
        finally:
            self.unsubscribe(channel, q)


# Singleton broker instance
broker = SSEBroker()
