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

import pytest


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

    @pytest.mark.parametrize(
        ("status_code", "html", "url", "expected_result", "expected_open"),
        [
            (200, "<html>normal content</html>", "https://www.netcomponents.com/search", "HEALTHY", False),
            # Session expired is not a trip
            (302, "", "https://www.netcomponents.com/account/login", "SESSION_EXPIRED", False),
            (429, "", "https://www.netcomponents.com/search", "RATE_LIMITED", True),
            (403, "", "https://www.netcomponents.com/search", "ACCESS_DENIED", True),
            (200, "too many requests please wait", "https://www.netcomponents.com/search", "RATE_LIMITED", True),
            (200, "access denied by policy", "https://www.netcomponents.com/search", "ACCESS_DENIED", True),
        ],
        ids=[
            "healthy_response",
            "session_expired_on_login_redirect",
            "rate_limited_429",
            "access_denied_403",
            "rate_limit_in_content",
            "access_denied_in_content",
        ],
    )
    def test_check_response_health_single_call(self, status_code, html, url, expected_result, expected_open):
        cb = self._make_breaker()
        result = cb.check_response_health(status_code, html, url)
        assert result == expected_result
        assert cb.is_open is expected_open

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

    @pytest.mark.parametrize(
        ("mpn", "expected"),
        [
            ("", ""),
            (None, ""),
            ("abc123", "ABC123"),
            ("  ABC 123  ", "ABC123"),
            ("ABC 123 DEF", "ABC123DEF"),
            ("LM358/TR", "LM358"),
            ("LM358-TR", "LM358"),
            ("SN74HC595/CT", "SN74HC595"),
            ("SN74HC595-CT", "SN74HC595"),
            ("LM358-ND", "LM358"),
            ("LM358-DKR", "LM358"),
            ("IRF540N#PBF", "IRF540N"),
            ("IRF540N-PBF", "IRF540N"),
            ("TPS54331/NOPB", "TPS54331"),
            ("TPS54331-NOPB", "TPS54331"),
            ("ADP3338AKCZ-3.3-RL", "ADP3338AKCZ-3.3"),
            ("ADP3338AKCZ-3.3-RL7", "ADP3338AKCZ-3.3"),
            # -R is not stripped (it's a package code, not packaging)
            ("STM32F103C8T6", "STM32F103C8T6"),
            ("lm358/tr", "LM358"),
            ("lm358-nopb", "LM358"),
            ("  LM 358 /TR  ", "LM358"),
            # New packaging/compliance suffixes (must merge)
            ("LM317-TRPBF", "LM317"),
            ("TPS54331/TRPBF", "TPS54331"),
            ("MAX232CPE+TR", "MAX232CPE"),
            ("SS14-E3", "SS14"),
            ("SS14-E4", "SS14"),
            ("LM358D/TR7", "LM358D"),
            ("ADP3338AKCZ-3.3-TR13", "ADP3338AKCZ-3.3"),
            ("lm358d/tr7", "LM358D"),
            # Ambiguous suffixes deliberately preserved (must NOT merge)
            ("LM317T-13", "LM317T-13"),
            ("TPS54331-Q1", "TPS54331-Q1"),
            ("LM317T-EP", "LM317T-EP"),
            ("LM317T", "LM317T"),
        ],
        ids=[
            "empty_string",
            "none_input",
            "uppercase",
            "strip_whitespace",
            "strip_internal_whitespace",
            "strip_tape_reel_slash",
            "strip_tape_reel_dash",
            "strip_cut_tape_slash",
            "strip_cut_tape_dash",
            "strip_nd_suffix",
            "strip_dkr_suffix",
            "strip_pbf_hash",
            "strip_pbf_dash",
            "strip_nopb_slash",
            "strip_nopb_dash",
            "strip_reel_suffix",
            "strip_reel_with_number",
            "preserve_meaningful_suffix",
            "case_insensitive_suffix_strip_slash",
            "case_insensitive_suffix_strip_nopb",
            "combined_whitespace_and_suffix",
            "strip_trpbf_dash",
            "strip_trpbf_slash",
            "strip_maxim_plus_tr",
            "strip_vishay_e3",
            "strip_vishay_e4",
            "strip_tr_reel_size_slash",
            "strip_tr_reel_size_dash_with_decimal_base",
            "strip_tr_reel_size_case_insensitive",
            "preserve_bare_digit_suffix_13",
            "preserve_automotive_q1_grade",
            "preserve_enhanced_product_ep",
            "preserve_bare_t_package_code",
        ],
    )
    def test_strip_packaging_suffixes(self, mpn, expected):
        assert self._normalize(mpn) == expected

    def test_lm317t_and_lm317_are_distinct_parts(self):
        """LM317T's trailing "T" is a TO-220 package code, not a reel suffix — must NOT
        collapse to the same normalized key as LM317."""
        assert self._normalize("LM317T") != self._normalize("LM317")

    def test_q1_and_non_q1_grades_stay_distinct(self):
        """-Q1 (AEC-Q100 automotive grade) is a different sellable SKU."""
        assert self._normalize("TPS54331-Q1") != self._normalize("TPS54331")

    def test_ep_and_non_ep_grades_stay_distinct(self):
        """-EP (TI Enhanced Product) is a different sellable SKU."""
        assert self._normalize("LM317T-EP") != self._normalize("LM317T")


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
        # Dead knobs removed from the shared factory: hourly cap was never
        # enforced and business-hours window is hardcoded in the scheduler.
        assert "TEST_MAX_HOURLY_SEARCHES" not in cfg
        assert "TEST_BUSINESS_HOURS_START" not in cfg
        assert "TEST_BUSINESS_HOURS_END" not in cfg

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
