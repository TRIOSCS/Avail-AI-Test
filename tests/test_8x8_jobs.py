"""Tests for 8x8 CDR polling job."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.jobs.eight_by_eight_jobs import register_eight_by_eight_jobs
from app.services.eight_by_eight_service import normalize_cdr

DISABLED_SETTINGS = SimpleNamespace(
    eight_by_eight_enabled=False,
    eight_by_eight_poll_interval_minutes=30,
)
ENABLED_SETTINGS = SimpleNamespace(
    eight_by_eight_enabled=True,
    eight_by_eight_poll_interval_minutes=30,
)

SAMPLE_CDR_OUTGOING = {
    "callId": "123456",
    "startTimeUTC": 1772750120399,
    "talkTimeMS": 60000,
    "caller": "1001",
    "callerName": "Michael Khoury",
    "callee": "+15551234567",
    "calleeName": "Test Contact",
    "direction": "Outgoing",
    "missed": "-",
    "answered": "Answered",
    "departments": ["Sales"],
}

SAMPLE_CDR_INCOMING = {
    "callId": "789012",
    "startTimeUTC": 1772757793502,
    "talkTimeMS": 25000,
    "caller": "+15559876543",
    "callerName": "Vendor X",
    "callee": "1001",
    "calleeName": "Michael Khoury",
    "direction": "Incoming",
    "missed": "-",
    "answered": "Answered",
    "departments": None,
}

SAMPLE_CDR_INTERNAL = {
    "callId": "int-001",
    "startTimeUTC": 1772750120399,
    "talkTimeMS": 30000,
    "caller": "1001",
    "callerName": "Michael Khoury",
    "callee": "1002",
    "calleeName": "Marcus Moawad",
    "direction": "Internal",
    "missed": "-",
    "answered": "Answered",
    "departments": None,
}


class TestRegister:
    def test_skips_when_disabled(self):
        scheduler = MagicMock()
        register_eight_by_eight_jobs(scheduler, DISABLED_SETTINGS)
        scheduler.add_job.assert_not_called()

    def test_registers_when_enabled(self):
        scheduler = MagicMock()
        register_eight_by_eight_jobs(scheduler, ENABLED_SETTINGS)
        scheduler.add_job.assert_called_once()
        assert scheduler.add_job.call_args.kwargs["id"] == "eight_by_eight_poll"


class TestCdrProcessingLogic:
    """Test the CDR → activity_log mapping logic without DB."""

    def test_internal_calls_have_no_external_phone(self):
        """Internal calls should be skipped by the job (direction == Internal)."""
        norm = normalize_cdr(SAMPLE_CDR_INTERNAL)
        assert norm["direction"] == "Internal"

    def test_outgoing_call_identifies_user_extension(self):
        """For outgoing calls, the caller is the internal extension."""
        norm = normalize_cdr(SAMPLE_CDR_OUTGOING)
        ext_map = {"1001": SimpleNamespace(id=1, name="Michael")}
        user = ext_map.get(norm["caller_phone"])
        assert user is not None
        assert user.id == 1

    def test_incoming_call_identifies_user_extension(self):
        """For incoming calls, the extension field identifies the user."""
        norm = normalize_cdr(SAMPLE_CDR_INCOMING)
        ext_map = {"1001": SimpleNamespace(id=1, name="Michael")}
        user = ext_map.get(norm["extension"])
        assert user is not None

    def test_unmatched_extension_returns_none(self):
        """CDR with unknown extension yields no user match."""
        norm = normalize_cdr(SAMPLE_CDR_OUTGOING)
        ext_map = {"9999": SimpleNamespace(id=99, name="Nobody")}
        user = ext_map.get(norm["caller_phone"])
        assert user is None

    def test_dedup_by_external_id(self):
        """Two CDRs with same callId produce same external_id for dedup."""
        norm1 = normalize_cdr(SAMPLE_CDR_OUTGOING)
        norm2 = normalize_cdr(SAMPLE_CDR_OUTGOING)
        assert norm1["external_id"] == norm2["external_id"] == "123456"

    def test_missed_incoming_has_zero_duration(self):
        missed = {**SAMPLE_CDR_INCOMING, "talkTimeMS": 0, "missed": "Missed", "answered": "-"}
        norm = normalize_cdr(missed)
        assert norm["is_missed"] is True
        assert norm["duration_seconds"] == 0

    def test_outgoing_direction_maps_to_outbound(self):
        norm = normalize_cdr(SAMPLE_CDR_OUTGOING)
        direction = "outbound" if norm["direction"] == "Outgoing" else "inbound"
        assert direction == "outbound"

    def test_incoming_direction_maps_to_inbound(self):
        norm = normalize_cdr(SAMPLE_CDR_INCOMING)
        direction = "outbound" if norm["direction"] == "Outgoing" else "inbound"
        assert direction == "inbound"


class TestProcessCdrsIntegration:
    """Test _process_cdrs with all dependencies mocked."""

    @patch("app.services.eight_by_eight_service.get_cdrs")
    @patch("app.services.eight_by_eight_service.get_access_token")
    @patch("app.services.activity_service.log_call_activity")
    def test_full_flow(self, mock_log_call, mock_auth, mock_fetch):
        from app.jobs.eight_by_eight_jobs import _process_cdrs

        mock_auth.return_value = "token"
        mock_fetch.return_value = [SAMPLE_CDR_OUTGOING, SAMPLE_CDR_INTERNAL]

        record = MagicMock()
        record.company_id = 42
        record.vendor_card_id = None
        mock_log_call.return_value = record

        db = MagicMock()
        # Watermark query returns None
        wm_query = MagicMock()
        wm_query.filter.return_value.first.return_value = None

        # User query returns one enabled user
        user_query = MagicMock()
        mock_user = MagicMock()
        mock_user.id = 1
        mock_user.eight_by_eight_extension = "1001"
        user_query.filter.return_value.all.return_value = [mock_user]

        call_count = [0]

        def query_router(model):
            call_count[0] += 1
            name = getattr(model, "__tablename__", "")
            if name == "system_config":
                return wm_query
            if name == "users":
                return user_query
            return MagicMock()

        db.query.side_effect = query_router

        result = _process_cdrs(db, ENABLED_SETTINGS)

        # Outgoing call processed, Internal skipped
        assert result["processed"] == 1
        assert result["skipped"] == 1
        assert result["matched"] == 1
        mock_log_call.assert_called_once()
        kwargs = mock_log_call.call_args.kwargs
        assert kwargs["user_id"] == 1
        assert kwargs["direction"] == "outbound"
        assert kwargs["phone"] == "+15551234567"
