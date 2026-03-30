"""Azure Communication Services — click-to-call with auto-logging.

Called by: app/routers/v13_features/activity.py (call initiate + webhook)
Depends on: app/config.py (acs_connection_string), app/services/activity_service.py
"""

from loguru import logger


async def initiate_call(to_phone: str, callback_url: str, connection_string: str) -> dict | None:
    """Initiate a PSTN call via Azure Communication Services.

    Returns call connection info or None on failure.
    """
    if not connection_string:
        logger.warning("ACS connection string not configured")
        return None

    try:
        from azure.communication.callautomation import (
            CallAutomationClient,
            PhoneNumberIdentifier,
        )

        client = CallAutomationClient.from_connection_string(connection_string)

        call_result = client.create_call(
            target_participant=PhoneNumberIdentifier(to_phone),
            callback_url=callback_url,
        )
        return {
            "call_connection_id": call_result.call_connection_id,
            "status": "initiated",
        }
    except ImportError:
        logger.error("azure-communication-callautomation package not installed")
        return None
    except Exception as e:
        logger.error(f"ACS call initiation failed: {e}")
        return None


def handle_call_completed(event_data: dict) -> dict | None:
    """Extract call details from ACS CallCompleted webhook event.

    Returns normalized call data for activity logging.
    """
    try:
        return {
            "call_connection_id": event_data.get("callConnectionId"),
            "duration_seconds": event_data.get("callDurationInSeconds", 0),
            "to_phone": event_data.get("targets", [{}])[0].get("rawId", ""),
            "direction": "outbound",
        }
    except (KeyError, IndexError) as e:
        logger.warning(f"Failed to parse ACS call event: {e}")
        return None
