"""Tests for SearchWorkerBase shared modules.

Diff findings from Task 14 (ICS vs NC worker comparison):
=========================================================

IDENTICAL (can be moved as-is):
  - mpn_normalizer.py: Identical except docstring (ICS/NC name). Pure function, no config deps.
  - human_behavior.py: Already moved to search_worker_base. Identical in both workers.

CONFIG-ONLY DIFFERENCES (can be parameterized):
  - config.py: Same structure, differs in env var prefix (ICS_ vs NC_) and NC has extra
    NC_ACCOUNT_NUMBER field. Already has base in search_worker_base/config.py.
  - scheduler.py: Already moved to search_worker_base. Same logic, parameterized by prefix.
    Differs only in default values (ICS typical=270s vs NC typical=240s).
  - monitoring.py: Already moved to search_worker_base. Same logic, parameterized by
    component_name. ICS version hardcodes "ics_worker", NC hardcodes "nc_worker".
  - queue_manager.py: Same structure but references IcsSearchQueue vs NcSearchQueue model,
    "icsource" vs "netcomponents" source_type, and NC omits vendor_email/vendor_phone in
    sighting linking. Can be parameterized with model class + source_type string.
  - ai_gate.py: Same structure but references different models (IcsSearchQueue vs NcSearchQueue),
    different prompt text (ICsource vs NetComponents), different JSON field names
    (search_ics vs search_nc). Can be parameterized.
  - sighting_writer.py: Similar structure but NC has price_breaks, supplier_product_url,
    currency, unit_price extraction, and richer raw_data. ICS has vendor_email/vendor_phone.
    Different enough to need separate implementations or a callback/hook pattern.

GENUINELY DIFFERENT LOGIC:
  - circuit_breaker.py: ICS uses async check_page_health(page) reading from browser DOM.
    NC uses sync check_response_health(status_code, html, url) checking HTTP responses.
    Core state machine (trip, reset, counters) is identical — health check method differs.
    Can extract shared base class with abstract health check method.
  - session_manager.py: Fundamentally different. ICS is fully browser-based (Patchright).
    NC is hybrid HTTP + optional browser fallback. Different login flows, different
    session health checks. Cannot be meaningfully shared.
  - search_engine.py: Fundamentally different. ICS uses browser automation (fill form,
    click button, ASP.NET WebForms). NC uses HTTP GET with URL params + browser fallback.
    Cannot be meaningfully shared.

ALREADY IN search_worker_base/:
  - config.py (factory function build_worker_config)
  - human_behavior.py (identical, moved)
  - monitoring.py (parameterized by component_name)
  - scheduler.py (parameterized by prefix)

Tests below cover the shared/shareable modules.
"""

import asyncio
import os
import random
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Circuit breaker tests (using ICS version — will become base after refactor)
# ---------------------------------------------------------------------------
class TestCircuitBreaker:
    """Test circuit breaker state machine transitions.

    Tests the ICS CircuitBreaker since it was the original. After consolidation, the
    shared base will have the same state machine with an abstract health check.
    """

    def _make_breaker(self):
        from app.services.ics_worker.circuit_breaker import CircuitBreaker

        return CircuitBreaker()

    def test_initial_state_is_closed(self):
        cb = self._make_breaker()
        assert not cb.is_open
        assert not cb.should_stop()
        assert cb.trip_reason == ""
        assert cb.consecutive_failures == 0
        assert cb.captcha_count == 0
        assert cb.empty_results_streak == 0

    def test_trip_opens_breaker(self):
        cb = self._make_breaker()
        cb._trip("test reason")
        assert cb.is_open
        assert cb.should_stop()
        assert cb.trip_reason == "test reason"

    def test_reset_closes_breaker(self):
        cb = self._make_breaker()
        cb._trip("test reason")
        assert cb.is_open
        cb.reset()
        assert not cb.is_open
        assert not cb.should_stop()
        assert cb.trip_reason == ""
        assert cb.captcha_count == 0
        assert cb.consecutive_failures == 0
        assert cb.empty_results_streak == 0

    def test_empty_results_streak_trips_at_10(self):
        cb = self._make_breaker()
        for i in range(9):
            cb.record_empty_results()
            assert not cb.is_open, f"Should not trip at {i + 1} empty results"
        cb.record_empty_results()
        assert cb.is_open
        assert "10 consecutive empty results" in cb.trip_reason

    def test_record_results_resets_empty_streak(self):
        cb = self._make_breaker()
        for _ in range(5):
            cb.record_empty_results()
        assert cb.empty_results_streak == 5
        cb.record_results()
        assert cb.empty_results_streak == 0

    def test_get_trip_info_returns_full_state(self):
        cb = self._make_breaker()
        cb.captcha_count = 1
        cb.consecutive_failures = 2
        cb.empty_results_streak = 3
        info = cb.get_trip_info()
        assert info == {
            "is_open": False,
            "trip_reason": "",
            "captcha_count": 1,
            "consecutive_failures": 2,
            "empty_results_streak": 3,
        }

    def test_get_trip_info_after_trip(self):
        cb = self._make_breaker()
        cb._trip("blocked")
        info = cb.get_trip_info()
        assert info["is_open"] is True
        assert info["trip_reason"] == "blocked"


class TestNCCircuitBreaker:
    """Test the NC-specific check_response_health method."""

    def _make_breaker(self):
        from app.services.nc_worker.circuit_breaker import CircuitBreaker

        return CircuitBreaker()

    def test_healthy_response(self):
        cb = self._make_breaker()
        result = cb.check_response_health(200, "<html>normal content</html>", "https://www.netcomponents.com/search")
        assert result == "HEALTHY"
        assert not cb.is_open

    def test_session_expired_on_login_redirect(self):
        cb = self._make_breaker()
        result = cb.check_response_health(302, "", "https://www.netcomponents.com/account/login")
        assert result == "SESSION_EXPIRED"
        assert not cb.is_open  # Session expired is not a trip

    def test_rate_limited_429(self):
        cb = self._make_breaker()
        result = cb.check_response_health(429, "", "https://www.netcomponents.com/search")
        assert result == "RATE_LIMITED"
        assert cb.is_open

    def test_access_denied_403(self):
        cb = self._make_breaker()
        result = cb.check_response_health(403, "", "https://www.netcomponents.com/search")
        assert result == "ACCESS_DENIED"
        assert cb.is_open

    def test_server_error_trips_after_3(self):
        cb = self._make_breaker()
        cb.check_response_health(500, "", "https://www.netcomponents.com/search")
        assert not cb.is_open
        cb.check_response_health(502, "", "https://www.netcomponents.com/search")
        assert not cb.is_open
        cb.check_response_health(503, "", "https://www.netcomponents.com/search")
        assert cb.is_open

    def test_captcha_in_content_trips_after_2(self):
        cb = self._make_breaker()
        result = cb.check_response_health(200, "please verify you are human", "https://www.netcomponents.com/search")
        assert result == "CAPTCHA_WARNING"
        assert not cb.is_open
        cb.check_response_health(200, "recaptcha widget loaded", "https://www.netcomponents.com/search")
        assert cb.is_open

    def test_rate_limit_in_content(self):
        cb = self._make_breaker()
        result = cb.check_response_health(200, "too many requests please wait", "https://www.netcomponents.com/search")
        assert result == "RATE_LIMITED"
        assert cb.is_open

    def test_access_denied_in_content(self):
        cb = self._make_breaker()
        result = cb.check_response_health(200, "access denied by policy", "https://www.netcomponents.com/search")
        assert result == "ACCESS_DENIED"
        assert cb.is_open

    def test_healthy_resets_failure_counter(self):
        cb = self._make_breaker()
        cb.check_response_health(500, "", "https://www.netcomponents.com/search")
        assert cb.consecutive_failures == 1
        cb.check_response_health(200, "<html>ok</html>", "https://www.netcomponents.com/search")
        assert cb.consecutive_failures == 0


# ---------------------------------------------------------------------------
# MPN normalizer tests
# ---------------------------------------------------------------------------
class TestMPNNormalizer:
    """Test MPN normalization edge cases."""

    def _normalize(self, mpn):
        from app.services.ics_worker.mpn_normalizer import strip_packaging_suffixes

        return strip_packaging_suffixes(mpn)

    def test_empty_string(self):
        assert self._normalize("") == ""

    def test_none_input(self):
        assert self._normalize(None) == ""

    def test_uppercase(self):
        assert self._normalize("abc123") == "ABC123"

    def test_strip_whitespace(self):
        assert self._normalize("  ABC 123  ") == "ABC123"

    def test_strip_internal_whitespace(self):
        assert self._normalize("ABC 123 DEF") == "ABC123DEF"

    def test_strip_tape_reel_slash(self):
        assert self._normalize("LM358/TR") == "LM358"

    def test_strip_tape_reel_dash(self):
        assert self._normalize("LM358-TR") == "LM358"

    def test_strip_cut_tape_slash(self):
        assert self._normalize("SN74HC595/CT") == "SN74HC595"

    def test_strip_cut_tape_dash(self):
        assert self._normalize("SN74HC595-CT") == "SN74HC595"

    def test_strip_nd_suffix(self):
        assert self._normalize("LM358-ND") == "LM358"

    def test_strip_dkr_suffix(self):
        assert self._normalize("LM358-DKR") == "LM358"

    def test_strip_pbf_hash(self):
        assert self._normalize("IRF540N#PBF") == "IRF540N"

    def test_strip_pbf_dash(self):
        assert self._normalize("IRF540N-PBF") == "IRF540N"

    def test_strip_nopb_slash(self):
        assert self._normalize("TPS54331/NOPB") == "TPS54331"

    def test_strip_nopb_dash(self):
        assert self._normalize("TPS54331-NOPB") == "TPS54331"

    def test_strip_reel_suffix(self):
        assert self._normalize("ADP3338AKCZ-3.3-RL") == "ADP3338AKCZ-3.3"

    def test_strip_reel_with_number(self):
        assert self._normalize("ADP3338AKCZ-3.3-RL7") == "ADP3338AKCZ-3.3"

    def test_preserve_meaningful_suffix(self):
        # -R is not stripped (it's a package code, not packaging)
        assert self._normalize("STM32F103C8T6") == "STM32F103C8T6"

    def test_case_insensitive_suffix_strip(self):
        assert self._normalize("lm358/tr") == "LM358"
        assert self._normalize("lm358-nopb") == "LM358"

    def test_combined_whitespace_and_suffix(self):
        assert self._normalize("  LM 358 /TR  ") == "LM358"


# ---------------------------------------------------------------------------
# Monitoring tests (search_worker_base version — already parameterized)
# ---------------------------------------------------------------------------
class TestMonitoring:
    """Test monitoring module health check and reporting functions."""

    def test_html_structure_hash_empty(self):
        from app.services.search_worker_base.monitoring import check_html_structure_hash

        result = check_html_structure_hash("", "TEST-MPN")
        assert result == ""

    def test_html_structure_hash_deterministic(self):
        from app.services.search_worker_base.monitoring import check_html_structure_hash

        html = "<div><table><tr><td>data</td></tr></table></div>"
        h1 = check_html_structure_hash(html, "MPN1", component_name="TEST_DETERM")
        h2 = check_html_structure_hash(html, "MPN2", component_name="TEST_DETERM")
        assert h1 == h2
        assert len(h1) == 16  # sha256 hex truncated to 16 chars

    def test_html_structure_hash_differs_on_structure_change(self):
        from app.services.search_worker_base.monitoring import check_html_structure_hash

        html1 = "<div><table><tr><td>data</td></tr></table></div>"
        html2 = "<div><ul><li>data</li></ul></div>"
        h1 = check_html_structure_hash(html1, "MPN1", component_name="TEST_DIFF1")
        h2 = check_html_structure_hash(html2, "MPN2", component_name="TEST_DIFF2")
        assert h1 != h2

    def test_html_structure_hash_same_structure_different_content(self):
        from app.services.search_worker_base.monitoring import check_html_structure_hash

        html1 = "<div><span>hello</span></div>"
        html2 = "<div><span>world</span></div>"
        h1 = check_html_structure_hash(html1, "MPN1", component_name="TEST_SAME1")
        h2 = check_html_structure_hash(html2, "MPN2", component_name="TEST_SAME2")
        assert h1 == h2

    def test_log_daily_report_does_not_raise(self):
        from app.services.search_worker_base.monitoring import log_daily_report

        # Should not raise
        log_daily_report(
            searches_completed=10,
            sightings_created=50,
            parts_gated_out=5,
            parts_deduped=3,
            failed_searches=1,
            queue_remaining=20,
            circuit_breaker_status="closed",
            component_name="TEST",
        )

    def test_capture_sentry_error_without_sdk(self):
        from app.services.search_worker_base.monitoring import capture_sentry_error

        # Should not raise even without sentry_sdk
        capture_sentry_error(ValueError("test"), context={"key": "val"}, component_name="TEST")

    def test_capture_sentry_message_without_sdk(self):
        from app.services.search_worker_base.monitoring import capture_sentry_message

        capture_sentry_message("test message", level="info", component_name="TEST")


# ---------------------------------------------------------------------------
# Scheduler tests (search_worker_base version — already parameterized)
# ---------------------------------------------------------------------------
class TestSearchScheduler:
    """Test search scheduler timing and break management."""

    def _make_scheduler(self):
        from app.services.search_worker_base.scheduler import SearchScheduler

        config = MagicMock()
        config.ICS_TYPICAL_DELAY_SECONDS = 270
        config.ICS_MIN_DELAY_SECONDS = 150
        config.ICS_MAX_DELAY_SECONDS = 420
        config.ICS_BUSINESS_HOURS_START = 8
        config.ICS_BUSINESS_HOURS_END = 18
        return SearchScheduler(config, prefix="ICS")

    def test_next_delay_within_bounds(self):
        sched = self._make_scheduler()
        random.seed(42)
        for _ in range(50):
            delay = sched.next_delay()
            assert 150 <= delay <= 420, f"Delay {delay} out of bounds"

    def test_next_delay_increments_search_counter(self):
        sched = self._make_scheduler()
        assert sched.searches_since_break == 0
        sched.next_delay()
        assert sched.searches_since_break == 1
        sched.next_delay()
        assert sched.searches_since_break == 2

    def test_time_for_break_after_threshold(self):
        sched = self._make_scheduler()
        sched.break_threshold = 3
        assert not sched.time_for_break()
        for _ in range(3):
            sched.next_delay()
        assert sched.time_for_break()

    def test_reset_break_counter(self):
        sched = self._make_scheduler()
        sched.searches_since_break = 10
        sched.reset_break_counter()
        assert sched.searches_since_break == 0
        assert 8 <= sched.break_threshold <= 15

    def test_get_break_duration_within_bounds(self):
        sched = self._make_scheduler()
        random.seed(42)
        for _ in range(20):
            dur = sched.get_break_duration()
            assert 5 * 60 <= dur <= 25 * 60

    def test_business_hours_force_env(self):
        sched = self._make_scheduler()
        with patch.dict(os.environ, {"FORCE_BUSINESS_HOURS": "1"}):
            assert sched.is_business_hours() is True


# ---------------------------------------------------------------------------
# Config tests (search_worker_base config factory)
# ---------------------------------------------------------------------------
class TestWorkerConfig:
    """Test the shared config factory."""

    def test_build_worker_config_defaults(self):
        from app.services.search_worker_base.config import build_worker_config

        cfg = build_worker_config("TEST")
        assert cfg["TEST_MAX_DAILY_SEARCHES"] == 50
        assert cfg["TEST_MIN_DELAY_SECONDS"] == 150
        assert cfg["TEST_DEDUP_WINDOW_DAYS"] == 7

    def test_build_worker_config_with_env(self):
        from app.services.search_worker_base.config import build_worker_config

        with patch.dict(os.environ, {"TEST2_MAX_DAILY_SEARCHES": "100"}):
            cfg = build_worker_config("TEST2")
            assert cfg["TEST2_MAX_DAILY_SEARCHES"] == 100

    def test_build_worker_config_with_overrides(self):
        from app.services.search_worker_base.config import build_worker_config

        cfg = build_worker_config("TEST3", defaults={"MAX_DAILY_SEARCHES": "200"})
        assert cfg["TEST3_MAX_DAILY_SEARCHES"] == 200

    def test_ics_config_loads(self):
        from app.services.ics_worker.config import IcsConfig

        config = IcsConfig()
        assert hasattr(config, "ICS_MAX_DAILY_SEARCHES")
        assert hasattr(config, "ICS_USERNAME")
        assert config.ICS_MAX_DAILY_SEARCHES == 50  # default

    def test_nc_config_loads(self):
        from app.services.nc_worker.config import NcConfig

        config = NcConfig()
        assert hasattr(config, "NC_MAX_DAILY_SEARCHES")
        assert hasattr(config, "NC_USERNAME")
        assert hasattr(config, "NC_ACCOUNT_NUMBER")  # NC-specific field


# ---------------------------------------------------------------------------
# Human behavior tests (search_worker_base version)
# ---------------------------------------------------------------------------
class TestHumanBehavior:
    """Test human behavior simulation."""

    def test_random_delay_within_bounds(self):
        from app.services.search_worker_base.human_behavior import HumanBehavior

        async def _test():
            random.seed(42)
            # Patch asyncio.sleep to not actually sleep
            with patch("asyncio.sleep", return_value=None):
                # Just verify it doesn't raise
                await HumanBehavior.random_delay(0.1, 0.5)

        asyncio.get_event_loop().run_until_complete(_test())

    def test_human_type_calls_keyboard(self):
        from unittest.mock import AsyncMock

        from app.services.search_worker_base.human_behavior import HumanBehavior

        async def _test():
            page = MagicMock()
            page.keyboard = MagicMock()
            page.keyboard.type = AsyncMock()
            locator = MagicMock()
            locator.click = AsyncMock()

            with patch("asyncio.sleep", new_callable=AsyncMock):
                await HumanBehavior.human_type(page, locator, "AB")

            locator.click.assert_called_once()
            assert page.keyboard.type.call_count == 2

        asyncio.get_event_loop().run_until_complete(_test())


# ---------------------------------------------------------------------------
# search_worker_base shared module tests (new parameterized versions)
# ---------------------------------------------------------------------------


class TestSharedMPNNormalizer:
    """Test the shared mpn_normalizer moved into search_worker_base."""

    def test_import_from_base(self):
        from app.services.search_worker_base.mpn_normalizer import strip_packaging_suffixes as base_norm

        assert base_norm("LM358/TR") == "LM358"

    def test_matches_ics_version(self):
        from app.services.ics_worker.mpn_normalizer import strip_packaging_suffixes as ics_norm
        from app.services.search_worker_base.mpn_normalizer import strip_packaging_suffixes as base_norm

        test_cases = [
            "",
            "abc123",
            "  LM 358 /TR  ",
            "ADP3338AKCZ-3.3-RL7",
            "IRF540N#PBF",
            "TPS54331-NOPB",
            "STM32F103C8T6",
        ]
        for mpn in test_cases:
            assert base_norm(mpn) == ics_norm(mpn), f"Mismatch for {mpn!r}"

    def test_export_from_init(self):
        from app.services.search_worker_base import strip_packaging_suffixes

        assert strip_packaging_suffixes("LM358-CT") == "LM358"


class TestSharedCircuitBreakerBase:
    """Test CircuitBreakerBase from search_worker_base."""

    def test_import_from_base(self):
        from app.services.search_worker_base.circuit_breaker import CircuitBreakerBase

        cb = CircuitBreakerBase()
        assert not cb.is_open

    def test_state_machine_matches_ics(self):
        """Base class has same state machine as ICS version."""
        from app.services.search_worker_base.circuit_breaker import CircuitBreakerBase

        cb = CircuitBreakerBase()
        cb._trip("test")
        assert cb.is_open
        assert cb.should_stop()
        cb.reset()
        assert not cb.is_open

    def test_empty_results_streak_trips_at_10(self):
        from app.services.search_worker_base.circuit_breaker import CircuitBreakerBase

        cb = CircuitBreakerBase()
        for _ in range(9):
            cb.record_empty_results()
        assert not cb.is_open
        cb.record_empty_results()
        assert cb.is_open

    def test_get_trip_info(self):
        from app.services.search_worker_base.circuit_breaker import CircuitBreakerBase

        cb = CircuitBreakerBase()
        cb._trip("reason")
        info = cb.get_trip_info()
        assert info["is_open"] is True
        assert info["trip_reason"] == "reason"

    def test_subclassable(self):
        """Workers can subclass and add their own health check."""
        from app.services.search_worker_base.circuit_breaker import CircuitBreakerBase

        class TestBreaker(CircuitBreakerBase):
            def check_health(self, status_code: int) -> str:
                if status_code == 429:
                    self._trip("rate limited")
                    return "RATE_LIMITED"
                self.consecutive_failures = 0
                return "HEALTHY"

        cb = TestBreaker()
        assert cb.check_health(200) == "HEALTHY"
        assert not cb.is_open
        cb.check_health(429)
        assert cb.is_open

    def test_export_from_init(self):
        from app.services.search_worker_base import CircuitBreakerBase

        cb = CircuitBreakerBase()
        assert hasattr(cb, "record_empty_results")


class TestSharedQueueManager:
    """Test parameterized QueueManager from search_worker_base."""

    def test_import_from_base(self):
        from app.services.search_worker_base.queue_manager import QueueManager

        assert QueueManager is not None

    def test_constructor_stores_params(self):
        from app.services.search_worker_base.queue_manager import QueueManager

        class FakeModel:
            pass

        qm = QueueManager(
            queue_model=FakeModel,
            source_type="test_source",
            dedup_window_days=14,
            log_prefix="TEST",
        )
        assert qm.queue_model is FakeModel
        assert qm.source_type == "test_source"
        assert qm.dedup_window_days == 14
        assert qm.log_prefix == "TEST"

    def test_custom_link_sighting_fn(self):
        from app.services.search_worker_base.queue_manager import QueueManager

        custom_called = []

        def custom_linker(s, req_id, mat_id, source_type):
            custom_called.append((req_id, mat_id, source_type))
            return MagicMock()

        qm = QueueManager(
            queue_model=object,
            source_type="test",
            link_sighting_fn=custom_linker,
        )
        assert qm._link_sighting is custom_linker

    def test_export_from_init(self):
        from app.services.search_worker_base import QueueManager

        assert QueueManager is not None


class TestSharedAIGate:
    """Test parameterized AIGate from search_worker_base."""

    def test_import_from_base(self):
        from app.services.search_worker_base.ai_gate import AIGate

        assert AIGate is not None

    def test_constructor_builds_prompt_and_schema(self):
        from app.services.search_worker_base.ai_gate import AIGate

        gate = AIGate(
            queue_model=object,
            marketplace_name="TestMarket",
            search_field="search_test",
            log_prefix="TM",
        )
        assert "TestMarket" in gate._system_prompt
        assert "search_test" in gate._system_prompt
        items = gate._schema["properties"]["classifications"]["items"]
        assert "search_test" in items["properties"]
        assert "search_test" in items["required"]

    def test_clear_cache(self):
        from app.services.search_worker_base.ai_gate import AIGate

        gate = AIGate(queue_model=object, marketplace_name="X", search_field="search_x")
        gate._classification_cache[("MPN1", "mfr")] = ("semi", "search", "reason")
        assert len(gate._classification_cache) == 1
        gate.clear_classification_cache()
        assert len(gate._classification_cache) == 0

    def test_ics_prompt_matches_original(self):
        """ICS-configured gate prompt should match the original structure."""
        from app.services.search_worker_base.ai_gate import AIGate

        gate = AIGate(
            queue_model=object,
            marketplace_name="ICsource",
            search_field="search_ics",
        )
        assert "ICsource" in gate._system_prompt
        assert "search_ics=true" in gate._system_prompt
        assert "search_ics=false" in gate._system_prompt

    def test_nc_prompt_matches_original(self):
        """NC-configured gate prompt should match the original structure."""
        from app.services.search_worker_base.ai_gate import AIGate

        gate = AIGate(
            queue_model=object,
            marketplace_name="NetComponents",
            search_field="search_nc",
        )
        assert "NetComponents" in gate._system_prompt
        assert "search_nc=true" in gate._system_prompt
        assert "search_nc=false" in gate._system_prompt

    def test_classify_empty_batch(self):
        """classify_parts_batch with empty list should return []."""
        from app.services.search_worker_base.ai_gate import AIGate

        gate = AIGate(queue_model=object, marketplace_name="X", search_field="search_x")

        async def _test():
            result = await gate.classify_parts_batch([])
            assert result == []

        asyncio.get_event_loop().run_until_complete(_test())

    def test_export_from_init(self):
        from app.services.search_worker_base import AIGate

        assert AIGate is not None
