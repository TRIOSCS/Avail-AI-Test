"""tests/test_search_worker_monitoring.py — Tests for
app/services/search_worker_base/monitoring.py."""

import os

os.environ["TESTING"] = "1"

from unittest.mock import patch

from app.services.search_worker_base.monitoring import capture_sentry_message


class TestCaptureSentryMessage:
    def test_logs_when_sentry_unavailable(self):
        with patch("app.services.search_worker_base.monitoring.logger") as mock_log:
            with patch("app.services.search_worker_base.monitoring._sentry_scope") as mock_scope:
                mock_scope.side_effect = ImportError("no sentry")
                capture_sentry_message("test message", component_name="TEST")
                mock_log.warning.assert_called_once()
