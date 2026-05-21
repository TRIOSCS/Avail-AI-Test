# Connector Failure Contract — Round 2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task.

**Goal:** Apply the 12 critical/important review findings from the 6-agent pipeline so the connector contract actually delivers what its docs claim — broken upstreams stop self-DOSing, the operator sees the right chip in the UI, and auth-vs-rate-limit are first-class distinct signals.

**Architecture:** Add a typed exception hierarchy (`ConnectorError` and three subtypes) plus two `StrEnum`s for `ApiSource.status` and `source_stats[i].status`. Update `BaseConnector` so the circuit-breaker open path raises (instead of silently empty), and so connector hard-error raises bypass the retry loop. Replace generic `RuntimeError` raises in all 7 connectors (Mouser, BrokerBin, Nexar, DigiKey, Element14, OEMSecrets, Sourcengine) with the specific type. Branch in `health_monitor.ping_source` to produce distinct operator messages. Wire `stream_search_mpn` to publish `source-status` SSE events for non-ok sources at search start (the missing UI hop).

**Tech Stack:** Python 3.11, FastAPI, SQLAlchemy 2.0, pytest, httpx, APScheduler. Existing `app/constants.py` `StrEnum` pattern, existing `app/services/sse_broker.py` SSE event protocol.

---

## File Structure

| File | Action | Purpose |
|---|---|---|
| `app/connectors/errors.py` | Create | `ConnectorError`, `ConnectorAuthError`, `ConnectorRateLimitError`, `ConnectorQuotaError` |
| `app/constants.py` | Modify | Add `ApiSourceStatus` and `SourceRunStatus` StrEnums |
| `app/connectors/sources.py` | Modify | `BaseConnector` open-breaker raise; `except ConnectorError: raise`; httpx 429 exhausted raises; NexarConnector + BrokerBinConnector typed exceptions |
| `app/connectors/digikey.py` | Modify | Replace `RuntimeError` with `ConnectorRateLimitError` |
| `app/connectors/element14.py` | Modify | Typed exceptions + new HTTP-403 raise |
| `app/connectors/oemsecrets.py` | Modify | Typed exceptions |
| `app/connectors/sourcengine.py` | Modify | Typed exceptions |
| `app/connectors/mouser.py` | Modify | Remove HTTP-403/429 silent-empty carve-out; raise typed exceptions |
| `app/services/health_monitor.py` | Modify | Branch on exception type for distinct `last_error` messages |
| `app/search_service.py` | Modify | Use `SourceRunStatus` enum; `stream_search_mpn` UI gap fix |
| `tests/test_connectors.py` | Modify | Switch to typed exception assertions; parametrize Sourcengine/Element14 status tests; update Mouser tests; replace 644b823c hash refs |
| `tests/test_connector_rate_limits.py` | Modify | Typed exceptions; new `TestBaseConnectorContract`; replace hash refs |
| `tests/test_sourcengine_connector.py` | Modify | Typed exceptions; replace hash refs |
| `tests/test_search_streaming.py` | Modify | New `TestBuildConnectorsErroredBranch`; new `TestStreamSearchMpnNonOkChips` |
| `tests/test_health_monitor.py` | Modify | New `TestPingSourceTypedErrors` |
| `docs/APP_MAP_INTERACTIONS.md` | Modify | Rewrite "Connector Failure Contract" subsection — auto-recovery, drop ping-loop-stops claim |

---

## Task 1: Foundation — exception hierarchy + StrEnums

**Files:**
- Create: `app/connectors/errors.py`
- Modify: `app/constants.py`
- Test: `tests/test_connector_errors.py` (new)
- Test: `tests/test_constants.py` (existing — append cases)

- [ ] **Step 1: Write failing test for errors.py**

Create `tests/test_connector_errors.py`:

```python
"""Tests for app/connectors/errors.py — connector exception hierarchy."""

import pytest

from app.connectors.errors import (
    ConnectorAuthError,
    ConnectorError,
    ConnectorQuotaError,
    ConnectorRateLimitError,
)


class TestConnectorErrorHierarchy:
    """All connector hard-error types must inherit from ConnectorError, which
    in turn inherits from RuntimeError so existing catches still work."""

    def test_connector_error_is_runtime_error(self):
        assert issubclass(ConnectorError, RuntimeError)

    def test_auth_error_is_connector_error(self):
        assert issubclass(ConnectorAuthError, ConnectorError)

    def test_rate_limit_error_is_connector_error(self):
        assert issubclass(ConnectorRateLimitError, ConnectorError)

    def test_quota_error_is_connector_error(self):
        assert issubclass(ConnectorQuotaError, ConnectorError)

    def test_specific_types_are_distinct(self):
        """Operator code branches on the specific type to produce distinct
        messages — auth (rotate creds) vs rate-limit (wait) vs quota
        (upgrade plan). The types must not overlap."""
        assert not issubclass(ConnectorAuthError, ConnectorRateLimitError)
        assert not issubclass(ConnectorRateLimitError, ConnectorAuthError)
        assert not issubclass(ConnectorQuotaError, ConnectorAuthError)
        assert not issubclass(ConnectorQuotaError, ConnectorRateLimitError)

    def test_message_propagates(self):
        with pytest.raises(ConnectorAuthError, match="DigiKey auth"):
            raise ConnectorAuthError("DigiKey auth error: HTTP 401 unauthorized")

    def test_runtime_error_catch_still_catches(self):
        """Backward-compat: existing `except RuntimeError` and `except Exception`
        catches should continue to catch the new types."""
        with pytest.raises(RuntimeError):
            raise ConnectorAuthError("test")
        with pytest.raises(Exception):
            raise ConnectorRateLimitError("test")
```

- [ ] **Step 2: Run test to verify failure**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_connector_errors.py -v --override-ini="addopts="`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.connectors.errors'`

- [ ] **Step 3: Create app/connectors/errors.py**

```python
"""Connector exception hierarchy.

Connectors raise these to signal hard failures that should:
  1. Flip ApiSource.status to 'error' via health_monitor.ping_source
  2. Bypass BaseConnector retry (these are not transient)
  3. Surface to the operator with a type-specific message

Called by: app/connectors/{sources,digikey,element14,mouser,oemsecrets,sourcengine}.py
Depends on: nothing (pure exception types)
"""


class ConnectorError(RuntimeError):
    """Base for connector hard failures.

    Subclass this for any condition that should flip ApiSource.status to
    'error' and bypass BaseConnector's retry loop. Inheriting from
    RuntimeError keeps backward compatibility with `except RuntimeError`
    and `except Exception` catches in legacy code paths.
    """


class ConnectorAuthError(ConnectorError):
    """401/403 — bad/expired/revoked credentials, or 401-as-quota
    (e.g. OEMSecrets returns 401 for both bad-key and quota-exhausted).

    Operator action: rotate the API key in Admin > API Sources.
    """


class ConnectorRateLimitError(ConnectorError):
    """429 — rate limited, persistent across in-connector retries.

    Operator action: usually none; auto-recovers when the upstream's
    rate-limit window expires and the next health ping returns 200.
    Persistent rate-limiting from quota burn-down warrants a quota-plan
    upgrade — surfaced separately as ConnectorQuotaError.
    """


class ConnectorQuotaError(ConnectorError):
    """Explicit monthly/plan quota exhaustion (e.g. Nexar GraphQL
    'You have exceeded your part limit').

    Operator action: upgrade plan or wait for monthly cycle reset.
    """
```

- [ ] **Step 4: Run errors test to verify pass**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_connector_errors.py -v --override-ini="addopts="`
Expected: 6 passed.

- [ ] **Step 5: Write failing test for StrEnums**

Append to `tests/test_constants.py` (or create if it doesn't exist):

```python
def test_api_source_status_strenum():
    """ApiSource.status takes one of these StrEnum values, written by
    health_monitor.ping_source. The enum lets type-checkers and IDE
    autocomplete catch typos like `status='errored'`."""
    from app.constants import ApiSourceStatus

    assert ApiSourceStatus.PENDING == "pending"
    assert ApiSourceStatus.LIVE == "live"
    assert ApiSourceStatus.ERROR == "error"
    assert ApiSourceStatus.DEGRADED == "degraded"
    assert ApiSourceStatus.DISABLED == "disabled"
    # Each value is also a string for filter_by(status=...) compatibility
    assert isinstance(ApiSourceStatus.LIVE.value, str)


def test_source_run_status_strenum():
    """source_stats[i]['status'] takes one of these. error_skipped is the
    new value introduced in 1dfec5b2 — health_monitor flipped this source
    to status='error' on a prior ping, so search_service skips it."""
    from app.constants import SourceRunStatus

    assert SourceRunStatus.OK == "ok"
    assert SourceRunStatus.ERROR == "error"
    assert SourceRunStatus.ERROR_SKIPPED == "error_skipped"
    assert SourceRunStatus.SKIPPED == "skipped"
    assert SourceRunStatus.DISABLED == "disabled"
```

- [ ] **Step 6: Run test to verify failure**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_constants.py -v --override-ini="addopts=" -k "strenum"`
Expected: FAIL with `ImportError: cannot import name 'ApiSourceStatus'`

- [ ] **Step 7: Add StrEnums to app/constants.py**

Append to `app/constants.py`:

```python
class ApiSourceStatus(StrEnum):
    """ApiSource.status — managed by health_monitor.ping_source.

    Single source of truth for the api_sources.status string column.
    health_monitor.ping_source is the only writer of LIVE / ERROR.
    DISABLED is set when no connector is available for the source.
    DEGRADED is reserved for future ConnectorRateLimitError handling
    where the source should be auto-retry-after-window without
    exclusion from user searches.
    """

    PENDING = "pending"
    LIVE = "live"
    ERROR = "error"
    DEGRADED = "degraded"
    DISABLED = "disabled"


class SourceRunStatus(StrEnum):
    """Per-search-run status for source_stats[i] entries.

    Returned to the streaming search response so the per-source chip
    strip in the UI can render the right state (green / red / dim /
    pulsing).

    error_skipped means the source was excluded from this run because
    health_monitor previously flipped its ApiSource.status to ERROR;
    the operator sees a distinct chip with an actionable message.
    """

    OK = "ok"
    ERROR = "error"
    ERROR_SKIPPED = "error_skipped"
    SKIPPED = "skipped"
    DISABLED = "disabled"
```

- [ ] **Step 8: Run test to verify pass**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_constants.py -v --override-ini="addopts=" -k "strenum"`
Expected: 2 passed.

---

## Task 2: BaseConnector — open-breaker raise + no retry on ConnectorError + 429 exhausted raises

**Files:**
- Modify: `app/connectors/sources.py:90-154`
- Test: `tests/test_connector_rate_limits.py` (append `TestBaseConnectorContract`)

- [ ] **Step 1: Write failing tests**

Append to `tests/test_connector_rate_limits.py`:

```python
# ═══════════════════════════════════════════════════════════════════════
#  BaseConnector contract — open breaker raises, ConnectorError fast-fails
# ═══════════════════════════════════════════════════════════════════════


class TestBaseConnectorContract:
    """Verify BaseConnector wraps the connector contract correctly.

    The new contract (per docs/APP_MAP_INTERACTIONS.md § Connector Failure
    Contract): open circuit breaker raises ConnectorError, ConnectorError
    from _do_search bypasses retry, persistent httpx 429 raises
    ConnectorRateLimitError instead of silently returning [].
    """

    @pytest.mark.asyncio
    async def test_open_breaker_raises_connector_error(self):
        """When the breaker is open, BaseConnector.search() must raise
        ConnectorError (not return []). Returning [] previously masked
        the contract — health_monitor saw 'success' and flipped status
        back to 'live', defeating the whole fix."""
        from app.connectors.errors import ConnectorError
        from app.connectors.sources import BaseConnector

        class FakeConnector(BaseConnector):
            async def _do_search(self, part_number):
                return [{"ok": True}]

        c = FakeConnector(timeout=5.0, max_retries=0)
        # Force the breaker open by recording enough failures
        for _ in range(10):
            c._breaker.record_failure()
        assert c._breaker.current_state == "open"

        with pytest.raises(ConnectorError, match="circuit breaker open"):
            await c.search("TEST123")

    @pytest.mark.asyncio
    async def test_connector_error_in_do_search_bypasses_retry(self):
        """When _do_search raises a ConnectorError, BaseConnector must
        re-raise immediately without retrying. ConnectorError signals a
        hard failure (auth/quota); retrying just burns more upstream
        calls against an already-broken endpoint."""
        from app.connectors.errors import ConnectorAuthError
        from app.connectors.sources import BaseConnector

        class FakeConnector(BaseConnector):
            call_count = 0

            async def _do_search(self, part_number):
                self.call_count += 1
                raise ConnectorAuthError("test auth error")

        c = FakeConnector(timeout=5.0, max_retries=2)
        with pytest.raises(ConnectorAuthError):
            await c.search("TEST123")
        # Exactly one attempt — no retry
        assert c.call_count == 1

    @pytest.mark.asyncio
    async def test_httpx_429_exhausted_raises_rate_limit_error(self):
        """When BaseConnector exhausts retries on httpx 429, it must
        raise ConnectorRateLimitError (not return []). Returning [] was
        a pre-existing silent-failure path that contradicts the new
        contract."""
        from app.connectors.errors import ConnectorRateLimitError
        from app.connectors.sources import BaseConnector

        class FakeConnector(BaseConnector):
            async def _do_search(self, part_number):
                resp = _mock_response(429, headers={"Retry-After": "0.01"})
                raise httpx.HTTPStatusError("429", request=MagicMock(), response=resp)

        c = FakeConnector(timeout=5.0, max_retries=1)
        with pytest.raises(ConnectorRateLimitError, match="rate limited"):
            await c.search("TEST123")
```

Also update `TestBaseConnector429::test_429_exhausted_returns_empty` to reflect the new behavior — rename to `test_429_exhausted_raises_rate_limit_error` and assert raise:

```python
    @pytest.mark.asyncio
    async def test_429_exhausted_raises_rate_limit_error(self):
        """BaseConnector raises ConnectorRateLimitError after all 429 retries
        exhausted. Replaces the prior silent-empty contract — see
        docs/APP_MAP_INTERACTIONS.md § Connector Failure Contract."""
        from app.connectors.errors import ConnectorRateLimitError
        from app.connectors.sources import BaseConnector

        class FakeConnector(BaseConnector):
            async def _do_search(self, part_number):
                resp = _mock_response(429, headers={"Retry-After": "0.01"})
                raise httpx.HTTPStatusError("429", request=MagicMock(), response=resp)

        c = FakeConnector(timeout=5.0, max_retries=1)
        with pytest.raises(ConnectorRateLimitError):
            await c.search("TEST123")
```

- [ ] **Step 2: Run tests to verify failure**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_connector_rate_limits.py::TestBaseConnectorContract tests/test_connector_rate_limits.py::TestBaseConnector429::test_429_exhausted_raises_rate_limit_error -v --override-ini="addopts="`
Expected: FAIL — `BaseConnector.search` returns `[]` on open breaker, ConnectorError gets retried, exhausted 429 returns `[]`.

- [ ] **Step 3: Modify BaseConnector**

In `app/connectors/sources.py`, edit the `search()` method (around line 90-98) and `_search_with_retry()` (around line 100-154).

Replace the `search` method:

```python
    async def search(self, part_number: str) -> list[dict]:
        # Short-circuit if the breaker is open (service is known-down).
        # Raise (don't return []) so health_monitor catches and stays at
        # status='error', and search-time _run_one renders the per-source
        # error chip. Returning [] here masks the contract — see
        # docs/APP_MAP_INTERACTIONS.md § Connector Failure Contract.
        if self._breaker.current_state == "open":
            raise ConnectorError(
                f"{self.__class__.__name__} circuit breaker open"
            )

        # Per-connector concurrency limit — avoids hammering one API
        async with self._semaphore:
            return await self._search_with_retry(part_number)
```

Replace the `_search_with_retry` exception arms (preserve the 5xx/timeout retry behavior, add explicit ConnectorError fast-fail, change 429-exhausted to raise):

```python
    async def _search_with_retry(self, part_number: str) -> list[dict]:
        last_err = None
        for attempt in range(self.max_retries + 1):
            try:
                result = await self._do_search(part_number)
                self._breaker.record_success()
                return result
            except ConnectorError:
                # Hard error from the connector — auth, quota, persistent
                # rate-limit. Do NOT retry; fast-fail so health_monitor
                # flips status='error' and the upstream stops getting hit.
                self._breaker.record_failure()
                raise
            except (httpx.ConnectTimeout, httpx.ConnectError) as e:
                # Server unreachable — no point retrying
                self._breaker.record_failure()
                logger.warning(f"{self.__class__.__name__} failed for {part_number}: {type(e).__name__}")
                raise
            except httpx.HTTPStatusError as e:
                status = e.response.status_code

                # 429 Too Many Requests — always retry with Retry-After
                if status == 429:
                    retry_after = _parse_retry_after(e.response)
                    logger.warning(
                        f"{self.__class__.__name__} rate limited (429) for {part_number}, "
                        f"retry after {retry_after:.1f}s (attempt {attempt + 1}/{self.max_retries + 1})"
                    )
                    if attempt < self.max_retries:
                        await asyncio.sleep(retry_after)
                        last_err = e
                        continue
                    # Final attempt exhausted — raise so health_monitor flips
                    # status='error'. Was: return [] (silent failure).
                    self._breaker.record_failure()
                    raise ConnectorRateLimitError(
                        f"{self.__class__.__name__} rate limited (persistent 429): {e.response.text[:200]}"
                    )

                # Auth/permission errors — fail fast (connector-specific
                # _do_search can override to handle gracefully)
                if status in (401, 403, 422):
                    self._breaker.record_failure()
                    logger.warning(f"{self.__class__.__name__} auth error {status} for {part_number} — not retrying")
                    raise

                self._breaker.record_failure()
                last_err = e
                if attempt < self.max_retries:
                    await asyncio.sleep(2**attempt + random.uniform(0, 1))
                else:
                    logger.warning(f"{self.__class__.__name__} failed for {part_number}: {e}")
            except Exception as e:
                self._breaker.record_failure()
                last_err = e
                if attempt < self.max_retries:
                    await asyncio.sleep(2**attempt + random.uniform(0, 1))
                else:
                    logger.warning(f"{self.__class__.__name__} failed for {part_number}: {e}")
        if last_err is not None:
            raise last_err  # propagate so caller can track the error
        raise RuntimeError(
            f"{self.__class__.__name__}: search loop completed without result or error for {part_number}"
        )
```

Add the import at the top of `app/connectors/sources.py` (after the existing imports):

```python
from .errors import ConnectorError, ConnectorRateLimitError
```

- [ ] **Step 4: Run tests to verify pass**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_connector_rate_limits.py -v --override-ini="addopts="`
Expected: All TestBaseConnectorContract + TestBaseConnector429 pass.

---

## Task 3: Connector raise sites — typed exceptions in 4 connectors

**Files:**
- Modify: `app/connectors/digikey.py`
- Modify: `app/connectors/oemsecrets.py`
- Modify: `app/connectors/sourcengine.py`
- Modify: `app/connectors/element14.py` (also adds 403 raise)
- Modify: `tests/test_connectors.py` (update assertions)

- [ ] **Step 1: Update digikey.py**

In `app/connectors/digikey.py` line ~113, replace:

```python
raise RuntimeError(f"DigiKey rate limited (persistent 429): {r.text[:200]}")
```

with:

```python
raise ConnectorRateLimitError(f"DigiKey rate limited (persistent 429): {r.text[:200]}")
```

Add import at top:

```python
from .errors import ConnectorRateLimitError
```

- [ ] **Step 2: Update oemsecrets.py**

In `app/connectors/oemsecrets.py` lines 46-48, replace:

```python
if r.status_code == 401:
    raise RuntimeError(f"OEMSecrets auth/quota error: HTTP 401 {r.text[:200]}")
if r.status_code == 429:
    raise RuntimeError(f"OEMSecrets rate limited: {r.text[:200]}")
```

with:

```python
if r.status_code == 401:
    # OEMSecrets returns 401 for both bad/expired key AND quota
    # exhaustion. Operator action is the same in both cases (rotate
    # key or top up quota), so we treat both as auth.
    raise ConnectorAuthError(f"OEMSecrets auth/quota error: HTTP 401 {r.text[:200]}")
if r.status_code == 429:
    raise ConnectorRateLimitError(f"OEMSecrets rate limited: {r.text[:200]}")
```

Add import:

```python
from .errors import ConnectorAuthError, ConnectorRateLimitError
```

- [ ] **Step 3: Update sourcengine.py**

In `app/connectors/sourcengine.py` lines 41-43, replace:

```python
if r.status_code in (401, 403):
    raise RuntimeError(f"Sourcengine auth error: HTTP {r.status_code} {r.text[:200]}")
if r.status_code == 429:
    raise RuntimeError(f"Sourcengine rate limited: {r.text[:200]}")
```

with:

```python
if r.status_code in (401, 403):
    raise ConnectorAuthError(f"Sourcengine auth error: HTTP {r.status_code} {r.text[:200]}")
if r.status_code == 429:
    raise ConnectorRateLimitError(f"Sourcengine rate limited: {r.text[:200]}")
```

Add import.

- [ ] **Step 4: Update element14.py**

In `app/connectors/element14.py` lines 60-67, replace:

```python
if r.status_code == 401:
    raise RuntimeError(f"element14 auth error: HTTP 401 {r.text[:200]}")
if r.status_code == 429:
    raise RuntimeError(f"element14 rate limited: {r.text[:200]}")
```

with (note the new 403 case):

```python
if r.status_code in (401, 403):
    # 401 = bad/expired API key; 403 = key rejected for the requested
    # store/region. Both require operator credential rotation.
    raise ConnectorAuthError(f"element14 auth error: HTTP {r.status_code} {r.text[:200]}")
if r.status_code == 429:
    raise ConnectorRateLimitError(f"element14 rate limited: {r.text[:200]}")
```

Add import.

- [ ] **Step 5: Update test assertions in test_connectors.py**

In `tests/test_connectors.py`, update the 5 raise-tests added in 1dfec5b2 to assert the typed exceptions. Replace each `pytest.raises(RuntimeError, match="...")` with the specific subclass.

DigiKey test (around line 366):
- `pytest.raises(RuntimeError, match="DigiKey rate limited")` → `pytest.raises(ConnectorRateLimitError, match="DigiKey rate limited")`

OEMSecrets tests (around line 1014, 1027):
- `pytest.raises(RuntimeError, match="OEMSecrets auth/quota error")` → `pytest.raises(ConnectorAuthError, match="OEMSecrets auth/quota error")`
- `pytest.raises(RuntimeError, match="OEMSecrets rate limited")` → `pytest.raises(ConnectorRateLimitError, match="OEMSecrets rate limited")`

Sourcengine tests (3 cases):
- 401 / 403 → `ConnectorAuthError`
- 429 → `ConnectorRateLimitError`

Element14 tests (2 cases):
- 401 → `ConnectorAuthError`
- 429 → `ConnectorRateLimitError`

Also add an Element14 403 test (parallel to the 401 test, asserts the new 403 raise):

```python
    @pytest.mark.asyncio
    async def test_search_403_raises_for_health_monitor(self):
        """element14 403 (key rejected for region/store) raises
        ConnectorAuthError. Same operator action as 401: rotate the key."""
        c = self._make_connector()
        resp = _mock_response(403, text="Forbidden")
        resp.raise_for_status = MagicMock()
        with patch("app.connectors.element14.http") as mock_http:
            mock_http.get = AsyncMock(return_value=resp)
            with pytest.raises(ConnectorAuthError, match="element14 auth error"):
                await c._do_search("LM317T")
            # Fallback short-circuited by the raise — only one upstream call
            assert mock_http.get.call_count == 1
```

Add `from app.connectors.errors import ConnectorAuthError, ConnectorRateLimitError` at the top of `tests/test_connectors.py`.

- [ ] **Step 6: Update existing typed-rename tests in test_connector_rate_limits.py + test_sourcengine_connector.py**

In `tests/test_connector_rate_limits.py`:
- `test_429_twice_raises_for_health_monitor` → assert `ConnectorRateLimitError`
- `test_401_quota_raises_for_health_monitor` → assert `ConnectorAuthError`
- `test_429_raises_for_health_monitor` (OEMSecrets) → assert `ConnectorRateLimitError`

In `tests/test_sourcengine_connector.py`:
- `test_status_429_raises_for_health_monitor` → assert `ConnectorRateLimitError`
- `test_status_401_raises_for_health_monitor` → assert `ConnectorAuthError`

Add imports.

- [ ] **Step 7: Run all connector tests to verify pass**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_connectors.py tests/test_connector_rate_limits.py tests/test_sourcengine_connector.py -v --override-ini="addopts="`
Expected: All pass (~190 tests).

---

## Task 4: Mouser carve-out removal + typed exceptions

**Files:**
- Modify: `app/connectors/mouser.py`
- Modify: `tests/test_connectors.py` (Mouser tests)
- Modify: `tests/test_connector_rate_limits.py` (TestMouser403 tests)

- [ ] **Step 1: Write failing tests**

In `tests/test_connector_rate_limits.py`, replace the existing `TestMouser403` tests:

```python
class TestMouser403:
    """Mouser HTTP-403/429 must raise (not return []). Revoked keys also
    return 403; the prior silent-empty carve-out hid that case. Auto-
    recovery handles transient overload — when upstream returns 200 on
    the next ping, status flips back to 'live' automatically."""

    def _make_connector(self):
        from app.connectors.mouser import MouserConnector

        return MouserConnector(api_key="test-key")

    @pytest.mark.asyncio
    async def test_403_raises_auth_error(self):
        """HTTP 403 raises ConnectorAuthError so health_monitor flips
        status to 'error'. Bad/revoked keys, quota-rejected keys, and
        region-locked keys all surface the same operator action."""
        from app.connectors.errors import ConnectorAuthError

        c = self._make_connector()
        resp_403 = _mock_response(403, text="Forbidden")

        with patch("app.connectors.mouser.http") as mock_http:
            mock_http.post = AsyncMock(return_value=resp_403)
            with pytest.raises(ConnectorAuthError, match="Mouser auth error"):
                await c._do_search("SN74HC595N")

    @pytest.mark.asyncio
    async def test_429_raises_rate_limit_error(self):
        """HTTP 429 raises ConnectorRateLimitError. Auto-recovers on next
        ping success."""
        from app.connectors.errors import ConnectorRateLimitError

        c = self._make_connector()
        resp_429 = _mock_response(429, text="Too Many Requests")

        with patch("app.connectors.mouser.http") as mock_http:
            mock_http.post = AsyncMock(return_value=resp_429)
            with pytest.raises(ConnectorRateLimitError, match="Mouser rate limited"):
                await c._do_search("SN74HC595N")

    @pytest.mark.asyncio
    async def test_body_rate_error_raises_rate_limit(self):
        """Mouser body-level 'too many requests' raises (was return [])."""
        from app.connectors.errors import ConnectorRateLimitError

        c = self._make_connector()
        resp = _mock_response(
            200,
            json_data={
                "Errors": [{"Code": "429", "Message": "Too many requests per second"}],
                "SearchResults": {},
            },
        )

        with patch("app.connectors.mouser.http") as mock_http:
            mock_http.post = AsyncMock(return_value=resp)
            with pytest.raises(ConnectorRateLimitError, match="Mouser rate"):
                await c._do_search("SN74HC595N")
```

In `tests/test_connectors.py`, update `test_do_search_auth_error_in_body_raises` to assert `ConnectorAuthError`. Update `test_do_search_invalid_part_number_still_raises` (catalog "Invalid part number" path) to assert it raises `RuntimeError` (NOT `ConnectorError` — this is a transient catalog mismatch, not a hard contract failure; preserves existing semantics).

- [ ] **Step 2: Run to verify failure**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_connector_rate_limits.py::TestMouser403 tests/test_connectors.py::TestMouserConnector -v --override-ini="addopts="`
Expected: FAIL — current code returns `[]` on 403/429.

- [ ] **Step 3: Modify mouser.py**

Replace the HTTP-status branches and the body-error branch:

```python
        # 403 — bad/revoked key, quota-rejected, or region-locked. Raise
        # so health_monitor flips status='error' and the source is excluded
        # from user searches; auto-recovers on next ping success if it was
        # transient.
        if r.status_code == 403:
            raise ConnectorAuthError(f"Mouser auth error: HTTP 403 {r.text[:200]}")

        # 429 — explicit rate limit. Auto-recovers on next ping success.
        if r.status_code == 429:
            raise ConnectorRateLimitError(f"Mouser rate limited: HTTP 429 {r.text[:200]}")

        r.raise_for_status()
        data = r.json()

        # Mouser returns errors in body even on HTTP 200
        errors = data.get("Errors") or []
        if errors:
            msg = errors[0].get("Message", "Unknown Mouser API error")
            msg_lower = msg.lower()
            # Quota/rate errors in body — raise rate-limit so status flips
            # to 'error' and the operator sees the chip.
            if "too many" in msg_lower or "rate" in msg_lower or "quota" in msg_lower:
                raise ConnectorRateLimitError(f"Mouser rate/quota error: {msg}")
            # Auth errors (bad / revoked / missing API key)
            is_auth_error = (
                "api key" in msg_lower
                or "unauthorized" in msg_lower
                or ("invalid" in msg_lower and ("identifier" in msg_lower or "key" in msg_lower))
            )
            if is_auth_error:
                raise ConnectorAuthError(f"Mouser auth error: {msg}")
            logger.warning(f"Mouser API errors for {part_number}: {errors}")
            # Catalog errors ("Invalid part number") aren't hard contract
            # failures — keep them as plain RuntimeError so the caller
            # treats them as transient.
            raise RuntimeError(f"Mouser API: {msg}")
```

Add imports:

```python
from .errors import ConnectorAuthError, ConnectorRateLimitError
```

- [ ] **Step 4: Run tests to verify pass**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_connector_rate_limits.py::TestMouser403 tests/test_connectors.py::TestMouserConnector -v --override-ini="addopts="`
Expected: All pass.

---

## Task 5: NexarConnector + BrokerBinConnector — typed exceptions in sources.py

**Files:**
- Modify: `app/connectors/sources.py` (NexarConnector around line 411-417, BrokerBinConnector around line 614-621)
- Modify: `tests/test_connectors.py` (Nexar tests if any)

- [ ] **Step 1: Find existing Nexar/BrokerBin RuntimeError raises**

Run: `grep -n "raise RuntimeError" app/connectors/sources.py`
Note all locations.

- [ ] **Step 2: Replace each with typed exception**

For each `raise RuntimeError("Nexar quota exceeded: ...")` → `raise ConnectorQuotaError("Nexar quota exceeded: ...")`.
For each `raise RuntimeError("BrokerBin auth error: ...")` → `raise ConnectorAuthError("BrokerBin auth error: ...")`.
For each `raise RuntimeError("BrokerBin rate limited: ...")` → `raise ConnectorRateLimitError("BrokerBin rate limited: ...")`.

Add `ConnectorAuthError, ConnectorQuotaError` to the existing import line (already has `ConnectorError, ConnectorRateLimitError` from Task 2).

- [ ] **Step 3: Update existing tests**

If `tests/test_connectors.py` has tests asserting `RuntimeError` from Nexar/BrokerBin paths, update to the specific subclass. Use grep:

`grep -n "Nexar quota\|BrokerBin auth\|BrokerBin rate" tests/test_connectors.py`

Update each match.

- [ ] **Step 4: Run tests to verify pass**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_connectors.py -v --override-ini="addopts=" -k "Nexar or BrokerBin"`
Expected: All pass.

---

## Task 6: health_monitor — branch on exception type

**Files:**
- Modify: `app/services/health_monitor.py` (ping_source around line 185-194; deep_test_source around line 265-275)
- Test: `tests/test_health_monitor.py` (append `TestPingSourceTypedErrors`)

- [ ] **Step 1: Write failing test**

Append to `tests/test_health_monitor.py`:

```python
class TestPingSourceTypedErrors:
    """ping_source produces distinct last_error messages for each
    ConnectorError subtype, giving operators type-specific guidance:
    auth → rotate creds, rate-limit → auto-recovers, quota → upgrade
    plan."""

    @pytest.mark.asyncio
    async def test_auth_error_message(self, db_session, monkeypatch):
        """ConnectorAuthError → last_error mentions 'rotate credentials'."""
        from app.connectors.errors import ConnectorAuthError
        from app.services.health_monitor import ping_source
        from app.models.config import ApiSource

        src = ApiSource(name="test_auth", display_name="Test", category="api",
                         source_type="search", status="live", is_active=True)
        db_session.add(src)
        db_session.commit()

        async def raises_auth(*a, **kw):
            raise ConnectorAuthError("test auth fail")

        from app.services import health_monitor
        monkeypatch.setattr(health_monitor, "_get_connector",
                            lambda *a, **kw: type("FakeC", (), {"search": raises_auth})())

        result = await ping_source(src, db_session)
        db_session.refresh(src)
        assert src.status == "error"
        assert "rotate credentials" in (src.last_error or "").lower()
        assert "test auth fail" in (src.last_error or "")

    @pytest.mark.asyncio
    async def test_rate_limit_error_message(self, db_session, monkeypatch):
        """ConnectorRateLimitError → last_error mentions auto-recovery."""
        from app.connectors.errors import ConnectorRateLimitError
        from app.services.health_monitor import ping_source
        from app.models.config import ApiSource

        src = ApiSource(name="test_rl", display_name="Test", category="api",
                         source_type="search", status="live", is_active=True)
        db_session.add(src)
        db_session.commit()

        async def raises_rl(*a, **kw):
            raise ConnectorRateLimitError("test rate limit")

        from app.services import health_monitor
        monkeypatch.setattr(health_monitor, "_get_connector",
                            lambda *a, **kw: type("FakeC", (), {"search": raises_rl})())

        await ping_source(src, db_session)
        db_session.refresh(src)
        assert src.status == "error"
        assert "rate limited" in (src.last_error or "").lower() or "auto-recover" in (src.last_error or "").lower()

    @pytest.mark.asyncio
    async def test_quota_error_message(self, db_session, monkeypatch):
        """ConnectorQuotaError → last_error mentions 'upgrade plan' or 'quota'."""
        from app.connectors.errors import ConnectorQuotaError
        from app.services.health_monitor import ping_source
        from app.models.config import ApiSource

        src = ApiSource(name="test_quota", display_name="Test", category="api",
                         source_type="search", status="live", is_active=True)
        db_session.add(src)
        db_session.commit()

        async def raises_quota(*a, **kw):
            raise ConnectorQuotaError("test quota")

        from app.services import health_monitor
        monkeypatch.setattr(health_monitor, "_get_connector",
                            lambda *a, **kw: type("FakeC", (), {"search": raises_quota})())

        await ping_source(src, db_session)
        db_session.refresh(src)
        assert src.status == "error"
        assert "quota" in (src.last_error or "").lower() or "upgrade" in (src.last_error or "").lower()
```

- [ ] **Step 2: Run to verify failure**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_health_monitor.py::TestPingSourceTypedErrors -v --override-ini="addopts="`
Expected: FAIL — current code uses one generic except.

- [ ] **Step 3: Modify ping_source and deep_test_source**

In `app/services/health_monitor.py`, replace the `except Exception as e:` block (around lines 185-210) with:

```python
    except ConnectorAuthError as e:
        elapsed_ms = int((time.time() - start) * 1000)
        error_msg = f"Auth error — rotate credentials: {str(e)[:380]}"
        source.status = ApiSourceStatus.ERROR.value
        source.last_error = error_msg
        source.last_error_at = now
        source.last_ping_at = now
        source.error_count_24h = (source.error_count_24h or 0) + 1
        _check_status_transition(source, old_status, "error", db, error_msg)
        log = ApiUsageLog(source_id=source.id, timestamp=now, endpoint="ping",
                          response_ms=elapsed_ms, success=False,
                          error_message=error_msg, check_type="ping")
        db.add(log)
        db.flush()
        logger.warning("Health ping auth error for {}: {}", source.name, error_msg)
        return {"success": False, "elapsed_ms": elapsed_ms, "error": error_msg}

    except ConnectorRateLimitError as e:
        elapsed_ms = int((time.time() - start) * 1000)
        error_msg = f"Rate limited — auto-recovers when window expires: {str(e)[:340]}"
        source.status = ApiSourceStatus.ERROR.value
        source.last_error = error_msg
        source.last_error_at = now
        source.last_ping_at = now
        source.error_count_24h = (source.error_count_24h or 0) + 1
        _check_status_transition(source, old_status, "error", db, error_msg)
        log = ApiUsageLog(source_id=source.id, timestamp=now, endpoint="ping",
                          response_ms=elapsed_ms, success=False,
                          error_message=error_msg, check_type="ping")
        db.add(log)
        db.flush()
        logger.warning("Health ping rate-limited for {}: {}", source.name, error_msg)
        return {"success": False, "elapsed_ms": elapsed_ms, "error": error_msg}

    except ConnectorQuotaError as e:
        elapsed_ms = int((time.time() - start) * 1000)
        error_msg = f"Quota exhausted — upgrade plan or wait for cycle: {str(e)[:340]}"
        source.status = ApiSourceStatus.ERROR.value
        source.last_error = error_msg
        source.last_error_at = now
        source.last_ping_at = now
        source.error_count_24h = (source.error_count_24h or 0) + 1
        _check_status_transition(source, old_status, "error", db, error_msg)
        log = ApiUsageLog(source_id=source.id, timestamp=now, endpoint="ping",
                          response_ms=elapsed_ms, success=False,
                          error_message=error_msg, check_type="ping")
        db.add(log)
        db.flush()
        logger.warning("Health ping quota exhausted for {}: {}", source.name, error_msg)
        return {"success": False, "elapsed_ms": elapsed_ms, "error": error_msg}

    except Exception as e:
        elapsed_ms = int((time.time() - start) * 1000)
        error_msg = str(e)[:500]
        source.status = ApiSourceStatus.ERROR.value
        source.last_error = error_msg
        source.last_error_at = now
        source.last_ping_at = now
        source.error_count_24h = (source.error_count_24h or 0) + 1
        _check_status_transition(source, old_status, "error", db, error_msg)
        log = ApiUsageLog(source_id=source.id, timestamp=now, endpoint="ping",
                          response_ms=elapsed_ms, success=False,
                          error_message=error_msg, check_type="ping")
        db.add(log)
        db.flush()
        logger.warning("Health ping failed for {}: {}", source.name, error_msg)
        return {"success": False, "elapsed_ms": elapsed_ms, "error": error_msg}
```

Apply the same pattern to `deep_test_source`'s except block (around line 265-289).

Replace `source.status = "live"` and `source.status = "disabled"` with `source.status = ApiSourceStatus.LIVE.value` / `ApiSourceStatus.DISABLED.value` for consistency.

Add imports:

```python
from app.connectors.errors import (
    ConnectorAuthError,
    ConnectorQuotaError,
    ConnectorRateLimitError,
)
from app.constants import ApiSourceStatus
```

- [ ] **Step 4: Run tests to verify pass**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_health_monitor.py -v --override-ini="addopts="`
Expected: All TestPingSourceTypedErrors pass; existing tests still pass.

---

## Task 7: search_service enums + stream_search_mpn UI gap fix

**Files:**
- Modify: `app/search_service.py:774-776` (`_make_stat`), `:792-793` (errored_sources query), `:824-829` (`_add_or_skip` errored branch), `:1990-2145` (`stream_search_mpn`)
- Test: `tests/test_search_streaming.py` (append two new test classes)

- [ ] **Step 1: Inspect stream_search_mpn**

Run: `grep -n "source_stats_map\|publish.*source-status\|source-status" app/search_service.py | head -20`
Note: where source_stats_map is built, where source-status events are published, and where the discard happens.

- [ ] **Step 2: Write failing tests**

Append to `tests/test_search_streaming.py`:

```python
class TestBuildConnectorsErroredBranch:
    """_build_connectors must exclude sources where ApiSource.status='error'
    (set by health_monitor) and surface them as 'error_skipped' in
    source_stats_map. Operator sees a distinct chip with actionable
    message."""

    def test_errored_source_excluded_with_error_skipped_status(self, db_session):
        """A source with status='error' is not instantiated; source_stats_map
        gets 'error_skipped' with the operator-actionable message."""
        from app.models.config import ApiSource
        from app.search_service import _build_connectors

        src = ApiSource(name="oemsecrets", display_name="OEMSecrets",
                         category="api", source_type="search",
                         status="error", is_active=True,
                         credentials={"api_key": "test"})
        db_session.add(src)
        db_session.commit()

        connectors, source_stats_map, _ = _build_connectors(db_session)

        # OEMSecrets connector excluded
        assert not any(c.__class__.__name__ == "OEMSecretsConnector" for c in connectors)
        # source_stats_map carries the error_skipped chip
        assert source_stats_map.get("oemsecrets", {}).get("status") == "error_skipped"
        msg = source_stats_map["oemsecrets"].get("error", "")
        assert "rotate" in msg.lower() or "re-enable" in msg.lower()


class TestStreamSearchMpnNonOkChips:
    """stream_search_mpn must publish a source-status SSE event for every
    non-ok entry in source_stats_map at search start. Without this fix
    the chip strip never renders the error_skipped/disabled/skipped state
    even though the contract claims it does."""

    @pytest.mark.asyncio
    async def test_error_skipped_publishes_source_status_event(self, db_session, monkeypatch):
        """An error_skipped source emits a source-status event so the chip
        renders. Verifies the contract's UI hop end-to-end."""
        # Implementation sketch — exact patching depends on stream_search_mpn
        # signature. Test asserts that for an error_skipped source,
        # sse_broker.publish was called with event type 'source-status' and
        # status='error_skipped' before any per-connector run begins.
        # See app/search_service.py stream_search_mpn for current
        # implementation.
        from app.models.config import ApiSource

        src = ApiSource(name="oemsecrets", display_name="OEMSecrets",
                         category="api", source_type="search",
                         status="error", is_active=True,
                         credentials={"api_key": "test"})
        db_session.add(src)
        db_session.commit()

        published_events = []

        def fake_publish(*args, **kwargs):
            published_events.append((args, kwargs))

        from app.services import sse_broker
        monkeypatch.setattr(sse_broker, "publish", fake_publish)

        from app.search_service import stream_search_mpn

        # Run one search; assert source-status published with error_skipped
        async for _ in stream_search_mpn(["LM317T"], db_session, requirement_id=None):
            break  # only first event needed

        non_ok_events = [e for e in published_events
                         if e[1].get("event") == "source-status"
                         and e[1].get("data", {}).get("status") == "error_skipped"]
        assert len(non_ok_events) >= 1, f"Expected source-status with error_skipped, got: {published_events}"
```

(The exact API of sse_broker.publish and stream_search_mpn may need patching once the actual code is read. Implementer adapts.)

- [ ] **Step 3: Run to verify failure**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_search_streaming.py::TestBuildConnectorsErroredBranch tests/test_search_streaming.py::TestStreamSearchMpnNonOkChips -v --override-ini="addopts="`
Expected: FAIL.

- [ ] **Step 4: Update _build_connectors and _make_stat**

In `app/search_service.py`:

Replace the literal status strings in `_build_connectors` (around line 793, 824-829) with `SourceRunStatus` / `ApiSourceStatus` enum values:

```python
    disabled_sources = {src.name for src in db.query(ApiSource).filter_by(status=ApiSourceStatus.DISABLED.value).all()}
    errored_sources = {src.name for src in db.query(ApiSource).filter_by(status=ApiSourceStatus.ERROR.value).all()}
```

```python
        elif source_name in errored_sources:
            source_stats_map[source_name] = _make_stat(
                source_name,
                SourceRunStatus.ERROR_SKIPPED.value,
                "Skipped due to prior error — auto-recovers when next ping returns 200; rotate credentials if persistent",
            )
```

Update `_make_stat` signature to accept `SourceRunStatus | str`:

```python
def _make_stat(source: str, status: SourceRunStatus | str, error: str | None = None,
               results: int = 0, ms: int = 0) -> dict:
    """Build a source_stats[i] entry."""
    return {
        "source": source,
        "status": status.value if isinstance(status, SourceRunStatus) else status,
        "error": error,
        "results": results,
        "ms": ms,
    }
```

- [ ] **Step 5: Add the stream_search_mpn UI fix**

Read `app/search_service.py` around the stream_search_mpn function. After `_build_connectors` returns and before per-connector tasks start, add:

```python
    # Publish source-status SSE events for every non-ok source so the
    # chip strip renders the right state immediately. Without this, the
    # connector contract's UI hop is dead — error_skipped/disabled/
    # skipped sources never get a chip, and the operator sees an empty
    # column instead of the actionable message.
    for source_name, stat in source_stats_map.items():
        if stat.get("status") not in (None, SourceRunStatus.OK.value):
            await _publish_source_status_event(source_name, stat)
```

Define `_publish_source_status_event` as a helper that builds the SSE payload and calls `sse_broker.publish` (or equivalent). Match the format of the existing per-connector source-status events emitted later in the same function.

Add imports:

```python
from app.constants import ApiSourceStatus, SourceRunStatus
```

- [ ] **Step 6: Run tests to verify pass**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_search_streaming.py -v --override-ini="addopts="`
Expected: New tests pass; existing tests still pass.

---

## Task 8: Comment + doc cleanup (parallel-friendly)

**Files (all in parallel):**
- Modify: `app/connectors/digikey.py`, `element14.py`, `oemsecrets.py`, `sourcengine.py`, `mouser.py` — comments around raises
- Modify: `app/search_service.py` — `_build_connectors` docstring + chip text
- Modify: `tests/test_connectors.py`, `tests/test_connector_rate_limits.py`, `tests/test_sourcengine_connector.py` — replace 644b823c hash refs
- Modify: `docs/APP_MAP_INTERACTIONS.md` — rewrite Connector Failure Contract section

- [ ] **Step 1: Sweep — replace ping-loop-stops claims with auto-recovery reality**

Find: `grep -rn "stops the 15-min ping loop\|excludes the connector from subsequent pings\|stops hitting that connector\|ping loop stops" app/connectors/ tests/ docs/`

For each match, replace the false claim with the auto-recovery wording:

> "health_monitor flips api_sources.status to 'error' so search_service excludes this source from user searches. Status auto-recovers to 'live' on the next health ping that returns 200; if the upstream is permanently broken, status keeps flipping back to 'error' on each ping and the source stays excluded until the operator rotates credentials."

- [ ] **Step 2: Sweep — replace 644b823c hash refs with doc refs**

Find: `grep -rn "644b823c" tests/`

For each match, replace `commit 644b823c` (and similar) with `docs/APP_MAP_INTERACTIONS.md § Connector Failure Contract`.

- [ ] **Step 3: Tighten _fetch_fresh docstring**

In `app/search_service.py` around line 873-879, the docstring has a docformatter artifact (blank line splitting the status-enum sentence). Edit to one continuous sentence describing the SourceRunStatus values.

- [ ] **Step 4: Trim _build_connectors docstring redundancy**

The docstring and the inline comment at line 824-829 say nearly the same thing. Trim the docstring to one sentence; keep the inline comment as the operator-facing message source.

- [ ] **Step 5: Rewrite APP_MAP_INTERACTIONS.md "Connector Failure Contract" section**

Replace the existing section (added in f5339ef0) with:

```markdown
### Connector Failure Contract

External-API connectors (`app/connectors/*.py`) follow a single contract
for upstream failures: **auth, quota, and rate-limit conditions raise
typed `ConnectorError` subclasses; do not silently return `[]`**. The
exception propagates through `BaseConnector.search()` to the caller
(search orchestrator or `health_monitor.ping_source`).

```
connector._do_search(part_number)
    |
    +-- 200 OK         ----> parse + return list[dict]
    +-- 400 (bad input) --> log + return []   (input-domain error, not contract)
    +-- 401/403 (auth)  --> raise ConnectorAuthError
    +-- 429 (rate)      --> raise ConnectorRateLimitError
    +-- explicit quota  --> raise ConnectorQuotaError
    +-- 5xx             --> raise (httpx.HTTPStatusError via raise_for_status)
```

The `BaseConnector.search` wrapper:
- Re-raises `ConnectorError` immediately without retry (hard failures
  are not transient; retrying just burns more upstream calls).
- Raises `ConnectorError` on open circuit breaker (was: silently `[]`,
  which masked the contract — health_monitor saw success and flipped
  status back to live).
- Raises `ConnectorRateLimitError` on httpx 429 retries exhausted.

`health_monitor.ping_source` catches each subtype and writes a
type-specific `last_error` message:

| Exception | last_error prefix | Operator action |
|---|---|---|
| `ConnectorAuthError` | "Auth error — rotate credentials: ..." | Rotate API key in Admin > API Sources |
| `ConnectorRateLimitError` | "Rate limited — auto-recovers when window expires: ..." | Usually none |
| `ConnectorQuotaError` | "Quota exhausted — upgrade plan or wait for cycle: ..." | Upgrade plan or wait |

In all cases `api_sources.status` flips to `'error'`, and
`search_service._build_connectors` excludes the source from the next
user search with a `source_stats[i].status = 'error_skipped'` chip.

**Auto-recovery.** The 15-min ping loop continues to ping all
`is_active=True` sources, including those at `status='error'`. On the
first ping that returns 200, status flips back to `'live'` and the
source rejoins user searches automatically. Persistent failures (revoked
key, exhausted quota) keep flipping back to `'error'` on each ping,
keeping the source excluded until the operator intervenes.

**No carve-outs.** All seven connectors (Mouser, BrokerBin, Nexar,
DigiKey, Element14, OEMSecrets, Sourcengine) follow this contract
uniformly. The Mouser HTTP-403/429 silent-empty path that existed prior
to round-2 was the silent-failure mode the contract is designed to
eliminate; it has been removed.

**Test enforcement** lives in `tests/test_connectors.py`,
`tests/test_connector_rate_limits.py`, and
`tests/test_sourcengine_connector.py`.
```

---

## Task 9: Run full suite + pre-commit + commit

- [ ] **Step 1: Full suite, no slow markers**

Run: `cd /root/availai && find . -path ./node_modules -prune -o -name "__pycache__" -print 2>/dev/null | xargs rm -rf 2>/dev/null; TESTING=1 PYTHONPATH=/root/availai pytest tests/ -m "not slow" --tb=short -q`
Expected: All pass.

- [ ] **Step 2: Pre-commit on all files**

Run: `cd /root/availai && pre-commit run --all-files`
Expected: All pass. If reformats happen, re-stage and re-run.

- [ ] **Step 3: Stage everything and commit**

```bash
cd /root/availai && git add app/connectors/errors.py app/constants.py \
  app/connectors/sources.py app/connectors/digikey.py app/connectors/element14.py \
  app/connectors/oemsecrets.py app/connectors/sourcengine.py app/connectors/mouser.py \
  app/services/health_monitor.py app/search_service.py \
  tests/test_connectors.py tests/test_connector_rate_limits.py \
  tests/test_sourcengine_connector.py tests/test_search_streaming.py \
  tests/test_health_monitor.py tests/test_connector_errors.py tests/test_constants.py \
  docs/APP_MAP_INTERACTIONS.md
```

```bash
git commit -m "$(cat <<'EOF'
fix(connectors): typed exception hierarchy, breaker raise, UI chip wiring

Round-2 fix for the connector silent-failure contract. The 6-agent review of
1dfec5b2/f5339ef0 found the contract was self-defeating in 6 ways. Live state
(nexar status='error' is_active=true still being pinged 96x/day, each ping
amplified to 3-6 calls by retry loop) confirmed.

Critical fixes:
- BaseConnector.search() now raises ConnectorError on open breaker (was: return
  [] which silently masked the contract — health_monitor saw success, flipped
  status back to 'live').
- BaseConnector._search_with_retry now fast-fails on ConnectorError (was:
  retried 3x, amplifying quota burn).
- httpx 429-exhausted now raises ConnectorRateLimitError (was: return []).
- Mouser HTTP-403/429 silent-empty carve-out removed (was: revoked keys
  → silent empty → status stays 'live' forever, exactly the failure mode
  the PR was designed to eliminate).
- Element14 HTTP-403 now raises ConnectorAuthError explicitly.
- stream_search_mpn now publishes source-status SSE events for non-ok
  source_stats_map entries at search start. Without this, error_skipped
  chips never reached the UI — the entire operator-visible payoff of
  the contract was wired to nothing.

Type design:
- New app/connectors/errors.py: ConnectorError + ConnectorAuthError +
  ConnectorRateLimitError + ConnectorQuotaError. health_monitor branches
  on type for distinct operator messages (auth: rotate creds; rate-limit:
  auto-recovers; quota: upgrade plan).
- New app/constants.py StrEnums: ApiSourceStatus, SourceRunStatus.
  source_stats[i].status and api_sources.status now use enum values
  instead of free-form strings.
- All seven connectors (Mouser, BrokerBin, Nexar, DigiKey, Element14,
  OEMSecrets, Sourcengine) follow the contract uniformly. No carve-outs.

Doc rewrites match reality:
- Auto-recovery is the actual behavior (was previously claimed as
  "operator must re-enable manually" — false; health_monitor flips
  back to 'live' on next ping success).
- 644b823c hash refs in test docstrings replaced with doc-section refs.
- 8+ comment locations claiming "ping loop stops hitting errored
  sources" rewritten — the ping loop continues, and that's correct
  (it's the recovery probe).

Tests:
- New tests/test_connector_errors.py for the hierarchy.
- New TestBaseConnectorContract in test_connector_rate_limits.py covers
  open-breaker raise + ConnectorError fast-fail + 429-exhausted raise.
- New TestPingSourceTypedErrors in test_health_monitor.py covers the
  three branches.
- New TestBuildConnectorsErroredBranch + TestStreamSearchMpnNonOkChips
  in test_search_streaming.py cover the search-side fixes.
- Existing connector raise tests updated to assert specific subclasses
  instead of generic RuntimeError.

Refs: docs/superpowers/specs/2026-05-08-connector-failure-contract-design.md

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

- [ ] **Step 4: Push**

Run: `git push origin chore/gradient-vestiges-and-docfmt`
Expected: push succeeds.

---

## Task 10: Deploy + live verify

- [ ] **Step 1: Deploy**

Run: `./deploy.sh --no-commit`
Expected: build tag updated, container restarts, "Deploy complete" line.

- [ ] **Step 2: Verify build tag in container matches HEAD**

Run: `docker compose exec -T app printenv BUILD_COMMIT`
Expected: BUILD_COMMIT prefix matches `git rev-parse --short HEAD`.

- [ ] **Step 3: Verify ConnectorError hierarchy reachable**

Run: `docker compose exec -T app python -c "from app.connectors.errors import ConnectorAuthError, ConnectorRateLimitError, ConnectorQuotaError; print('ok')"`
Expected: `ok`.

- [ ] **Step 4: Verify BaseConnector breaker-open path**

Run: `docker compose exec -T app grep -n "circuit breaker open\|except ConnectorError" /app/app/connectors/sources.py`
Expected: matches confirming raise on open breaker and fast-fail arm.

- [ ] **Step 5: Verify Mouser carve-out removed**

Run: `docker compose exec -T app grep -nE "ConnectorAuthError|ConnectorRateLimitError" /app/app/connectors/mouser.py`
Expected: matches confirming raises in place of returns.

- [ ] **Step 6: Verify health_monitor branches**

Run: `docker compose exec -T app grep -n "except ConnectorAuthError\|except ConnectorRateLimitError\|except ConnectorQuotaError" /app/app/services/health_monitor.py`
Expected: 3 matches in `ping_source` and `deep_test_source` blocks.

- [ ] **Step 7: Watch logs for one health-ping cycle**

Run: `docker compose logs -f app --tail 20 | grep --line-buffered -E "Health ping|api_source"`
Expected: typed messages ("Health ping auth error", "Health ping rate-limited", etc.) when broken sources fail.

After a ping cycle, query the DB:

Run: `docker compose exec -T db psql -U availai -d availai -c "SELECT name, status, last_error, last_error_at FROM api_sources WHERE status='error' ORDER BY last_error_at DESC LIMIT 5;"`
Expected: errored sources show new typed-message format ("Auth error — rotate credentials: ...", etc.).

---

## Self-Review Checklist (run before handoff)

- Spec coverage: every section of the design doc maps to one or more tasks above. ✓
- Placeholders: none.
- Type consistency: `ConnectorError`, `ConnectorAuthError`, `ConnectorRateLimitError`, `ConnectorQuotaError`, `ApiSourceStatus`, `SourceRunStatus` referenced consistently across tasks.
- Mouser test name: tests use `test_403_raises_auth_error` and `test_429_raises_rate_limit_error` consistently.

---

## Execution

Per user's standing "continue thru all steps till done" instruction, executing
inline using subagent-driven-development with parallel dispatch where
dependencies allow.

Phase ordering:
1. Foundation (Task 1) — sequential.
2. BaseConnector (Task 2) — sequential, depends on Task 1.
3. Per-connector edits (Tasks 3, 4, 5) — parallel, depend on Task 1+2.
4. Integration (Tasks 6, 7) — sequential, depend on Tasks 3-5.
5. Cleanup (Task 8) — parallel.
6. Verify + ship (Tasks 9, 10) — sequential.
