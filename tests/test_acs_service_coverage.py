"""tests/test_acs_service_coverage.py — Coverage tests for app/services/acs_service.py.

Covers: initiate_call (success, no connection string, ImportError, generic error)
        handle_call_completed (success, empty targets, KeyError/IndexError branch)
"""

import os

os.environ["TESTING"] = "1"

from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# initiate_call
# ---------------------------------------------------------------------------


async def test_initiate_call_no_connection_string():
    from app.services.acs_service import initiate_call

    result = await initiate_call("+15550001234", "+15550009999", "https://cb.example.com/hook", "")
    assert result is None


async def test_initiate_call_no_from_phone():
    from app.services.acs_service import initiate_call

    result = await initiate_call("+15550001234", "", "https://cb.example.com/hook", "endpoint=abc")
    assert result is None


async def test_initiate_call_success():
    from app.services.acs_service import initiate_call

    mock_call_result = MagicMock()
    mock_call_result.call_connection_id = "conn-abc-123"

    mock_client = MagicMock()
    mock_client.create_call.return_value = mock_call_result

    mock_acs_module = MagicMock()
    mock_acs_module.CallAutomationClient.from_connection_string.return_value = mock_client
    mock_acs_module.PhoneNumberIdentifier = MagicMock(side_effect=lambda phone: phone)
    mock_acs_module.CallInvite = MagicMock()

    with patch.dict(
        "sys.modules",
        {"azure.communication.callautomation": mock_acs_module},
    ):
        result = await initiate_call(
            "+15550001234",
            "+15550009999",
            "https://cb.example.com/hook",
            "endpoint=https://test.communication.azure.com;accesskey=abc123",
        )

    assert result is not None
    assert result["call_connection_id"] == "conn-abc-123"
    assert result["status"] == "initiated"


async def test_initiate_call_import_error():
    """When azure-communication-callautomation is not installed, return None."""
    from app.services.acs_service import initiate_call

    with patch.dict("sys.modules", {"azure.communication.callautomation": None}):
        result = await initiate_call(
            "+15550001234",
            "+15550009999",
            "https://cb.example.com/hook",
            "endpoint=https://test.communication.azure.com;accesskey=abc123",
        )
    assert result is None


async def test_initiate_call_generic_exception():
    """Any unexpected exception during call creation returns None."""
    from app.services.acs_service import initiate_call

    mock_acs_module = MagicMock()
    mock_acs_module.CallAutomationClient.from_connection_string.side_effect = RuntimeError("service unavailable")
    mock_acs_module.PhoneNumberIdentifier = MagicMock()
    mock_acs_module.CallInvite = MagicMock()

    with patch.dict(
        "sys.modules",
        {"azure.communication.callautomation": mock_acs_module},
    ):
        result = await initiate_call(
            "+15550001234",
            "+15550009999",
            "https://cb.example.com/hook",
            "endpoint=https://test.communication.azure.com;accesskey=abc123",
        )
    assert result is None


# ---------------------------------------------------------------------------
# handle_call_completed
# ---------------------------------------------------------------------------


def test_handle_call_completed_full_event():
    from app.services.acs_service import handle_call_completed

    event = {
        "callConnectionId": "conn-xyz-789",
        "callDurationInSeconds": 142,
        "targets": [{"rawId": "+15559876543"}],
    }
    result = handle_call_completed(event)
    assert result is not None
    assert result["call_connection_id"] == "conn-xyz-789"
    assert result["duration_seconds"] == 142
    assert result["to_phone"] == "+15559876543"
    assert result["direction"] == "outbound"


def test_handle_call_completed_empty_targets():
    """Empty targets list → to_phone is empty string."""
    from app.services.acs_service import handle_call_completed

    event = {
        "callConnectionId": "conn-001",
        "callDurationInSeconds": 0,
        "targets": [],
    }
    result = handle_call_completed(event)
    assert result is not None
    assert result["to_phone"] == ""
    assert result["call_connection_id"] == "conn-001"


def test_handle_call_completed_missing_keys():
    from app.services.acs_service import handle_call_completed

    result = handle_call_completed({})
    assert result is not None
    assert result["call_connection_id"] is None
    assert result["duration_seconds"] == 0
    assert result["to_phone"] == ""
    assert result["direction"] == "outbound"


def test_handle_call_completed_no_targets_key():
    from app.services.acs_service import handle_call_completed

    # Missing "targets" key entirely — get([]) default means [{}][0].get("rawId","")
    event = {"callConnectionId": "conn-002", "callDurationInSeconds": 30}
    result = handle_call_completed(event)
    assert result is not None
    assert result["to_phone"] == ""
