"""events.py — SSE stream endpoint for real-time notifications.

Provides a single SSE connection per user session. Events are published
by background tasks and services via the SSE broker.

Called by: base.html (sse-connect attribute)
Depends on: app/services/sse_broker.py, app/dependencies.py
"""

from fastapi import APIRouter, Depends, Request
from loguru import logger
from sse_starlette.sse import EventSourceResponse

from app.dependencies import require_user
from app.services.sse_broker import broker

router = APIRouter(tags=["events"])


@router.get("/api/events/stream")
async def event_stream(request: Request, user=Depends(require_user)):
    """SSE endpoint — one connection per user session.

    Listens on user-specific channel and yields events as they arrive. Frontend connects
    via hx-ext="sse" sse-connect="/api/events/stream".
    """
    logger.info("SSE stream opened for user {user_id}", user_id=user.id)

    async def generate():
        async for msg in broker.listen(f"user:{user.id}"):
            if await request.is_disconnected():
                break
            yield {"event": msg["event"], "data": msg.get("data", "")}

    return EventSourceResponse(generate())
