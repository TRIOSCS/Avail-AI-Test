"""Azure Communication Services — click-to-call with auto-logging.

Called by: app/routers/v13_features/activity.py (call initiate + webhook)
Depends on: app/config.py (acs_connection_string, acs_from_phone)
"""

import asyncio

from loguru import logger


async def initiate_call(to_phone: str, from_phone: str, callback_url: str, connection_string: str) -> dict | None:
    """Initiate a PSTN call via Azure Communication Services.

    Uses run_in_executor to avoid blocking the event loop (SDK is synchronous). Returns
    call connection info or None on failure.
    """
    if not connection_string:
        logger.warning("ACS connection string not configured")
        return None
    if not from_phone:
        logger.warning("ACS from_phone not configured — cannot initiate PSTN call")
        return None

    try:
        from azure.communication.callautomation import (
            CallAutomationClient,
            CallInvite,
            PhoneNumberIdentifier,
        )

        client = CallAutomationClient.from_connection_string(connection_string)
        call_invite = CallInvite(
            target=PhoneNumberIdentifier(to_phone),
            source_caller_id_number=PhoneNumberIdentifier(from_phone),
        )

        # Run synchronous SDK call in executor to avoid blocking event loop
        loop = asyncio.get_event_loop()
        call_result = await loop.run_in_executor(None, lambda: client.create_call(call_invite, callback_url))
        return {
            "call_connection_id": call_result.call_connection_id,
            "status": "initiated",
        }
    except ImportError:
        logger.error("azure-communication-callautomation package not installed")
        return None
    except Exception as e:
        logger.error("ACS call initiation failed: %s", e)
        return None


def handle_call_completed(event_data: dict) -> dict | None:
    """Extract call details from ACS CallCompleted webhook event.

    Returns normalized call data for activity logging.
    """
    targets = event_data.get("targets") or []
    to_phone = targets[0].get("rawId", "") if targets else ""
    return {
        "call_connection_id": event_data.get("callConnectionId"),
        "duration_seconds": event_data.get("callDurationInSeconds", 0),
        "to_phone": to_phone,
        "direction": "outbound",
    }
