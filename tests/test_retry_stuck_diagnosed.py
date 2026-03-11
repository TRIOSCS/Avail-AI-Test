"""Tests for the selfheal_jobs module after simplification.

The retry_stuck_diagnosed function and related logic were removed during
simplification. These tests verify the module's current stub state.

Called by: pytest
Depends on: app.jobs.selfheal_jobs
"""



class TestSelfhealJobsRemoved:
    def test_module_is_stub(self):
        """selfheal_jobs module was removed during simplification."""
        import app.jobs.selfheal_jobs as mod

        source = open(mod.__file__).read()
        assert "REMOVED" in source or len(source.strip()) < 200

    def test_no_retry_stuck_diagnosed(self):
        """retry_stuck_diagnosed no longer exists in the simplified module."""
        import app.jobs.selfheal_jobs as mod

        assert not hasattr(mod, "retry_stuck_diagnosed")

    def test_no_register_selfheal_jobs(self):
        """register_selfheal_jobs no longer exists in the simplified module."""
        import app.jobs.selfheal_jobs as mod

        assert not hasattr(mod, "register_selfheal_jobs")

    def test_no_max_retry_batch(self):
        """MAX_RETRY_BATCH constant no longer exists in the simplified module."""
        import app.jobs.selfheal_jobs as mod

        assert not hasattr(mod, "MAX_RETRY_BATCH")
