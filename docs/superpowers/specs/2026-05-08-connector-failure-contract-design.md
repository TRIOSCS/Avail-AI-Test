# Connector Failure Contract — Round 2 Design

**Date:** 2026-05-08
**Branch:** chore/gradient-vestiges-and-docfmt
**Author:** Claude (with mike@mhk92660)
**Predecessor commits:** 1dfec5b2 (initial raise contract) + f5339ef0 (initial doc)

## Problem

The 6-agent review of commits 1dfec5b2 and f5339ef0 surfaced critical defects that
make the documented contract self-defeating. Live state confirms: nexar is
`status='error', is_active=true` and is still being pinged every 15 minutes,
each ping amplified to 3-6 upstream calls.

**Critical defects:**

1. **Circuit breaker masks the contract.** When the breaker opens (~5 raises),
   `BaseConnector.search()` returns `[]` silently. `health_monitor.ping_source`
   sees no exception, flips status back to `'live'`. Within ~2 health-pings of
   any sustained failure, the source flips error→live spuriously, and stays
   `'live'` indefinitely while continuing to silently return empty results to
   user searches.

2. **`_search_with_retry` retries `RuntimeError` 3x.** The generic
   `except Exception` arm at sources.py:143 retries the connector's hard-error
   raises. Each health ping issues 3-6 calls instead of 1. Auth/quota errors
   are not transient and should fast-fail.

3. **`stream_search_mpn` discards `source_stats_map`.** The whole UI payoff of
   the contract — operator sees per-source error chip — is wired up to nothing.
   `source_stats_map` is built by `_build_connectors`, then `stream_search_mpn`
   only publishes `source-status` SSE events for connectors it actually runs.
   Errored/disabled/skipped sources are silently absent from the chip strip.

4. **Mouser 403/429 silent-empty carve-out hides the same failure mode the PR
   was designed to fix.** Revoked Mouser keys return HTTP 403; current code
   returns `[]`. health_monitor sees success → status stays `'live'` → silent
   failure forever.

5. **Element14 HTTP-403 doesn't raise.** Goes through `r.raise_for_status()` →
   `httpx.HTTPStatusError` → `BaseConnector` raises a non-`ConnectorError`. The
   operator-actionable message format is lost.

6. **`sources.py:128` returns `[]` on httpx 429 exhausted.** Pre-existing silent
   failure path that contradicts the new contract.

7. **Comments in 8+ locations claim "ping loop stops hitting errored sources."**
   The code does not do this. `run_health_checks` filters only on `is_active`.
   Doc lies in test docstrings, connector comments, and APP_MAP_INTERACTIONS.md.

8. **`RuntimeError` is the wrong exception type.** Operators see the same red
   chip and same generic error string for "creds revoked" (rotate) vs "rate
   limited" (wait) vs "quota exhausted" (upgrade plan). Three distinct
   operator actions, one undifferentiated signal.

## Design Decisions

| Decision | Choice | Rationale |
|---|---|---|
| Auto-recovery vs operator-re-enable | **Auto-recovery** | App is single-user testing env. health_monitor's existing flip-back-on-200 behavior handles transient errors correctly. Persistent failures keep flipping back to error on each ping anyway. Manual re-enable adds friction without preventing actual failures. |
| Type hierarchy | **Add now** | Cheap, makes auth-vs-rate-limit distinction first-class, eliminates substring-matching in health_monitor. |
| `stream_search_mpn` UI gap | **Fix in same PR** | Without it, the operator-visible signal — the entire payoff of the contract — never reaches the UI. |
| Mouser carve-out | **Remove** | Silent failure was exactly the case to eliminate. Auto-recovery handles transient overload. |
| Scope | **One commit** | Splitting creates intermediate broken states. No band-aids per `feedback_no_band_aids`. |

## Architecture

### New module: `app/connectors/errors.py`

```python
class ConnectorError(RuntimeError):
    """Base for connector hard-failures. health_monitor flips
    ApiSource.status to 'error' on any subclass of this. BaseConnector
    re-raises immediately without retry."""

class ConnectorAuthError(ConnectorError):
    """401/403 — bad/expired/revoked credentials. Operator action:
    rotate the API key in Admin > API Sources."""

class ConnectorRateLimitError(ConnectorError):
    """429 — transient or sustained rate limiting. Operator action:
    none if transient (auto-recovers); reduce search frequency if
    sustained."""

class ConnectorQuotaError(ConnectorError):
    """Explicit monthly/plan quota exhaustion. Operator action:
    upgrade plan or wait for quota cycle."""
```

### Updated: `app/constants.py`

Add two `StrEnum`s following the existing pattern:

```python
class ApiSourceStatus(StrEnum):
    """ApiSource.status — managed by health_monitor.ping_source."""
    PENDING = "pending"
    LIVE = "live"
    ERROR = "error"
    DEGRADED = "degraded"  # reserved for ConnectorRateLimitError handling later
    DISABLED = "disabled"

class SourceRunStatus(StrEnum):
    """Per-search-run status for source_stats[i] entries."""
    OK = "ok"
    ERROR = "error"
    ERROR_SKIPPED = "error_skipped"
    SKIPPED = "skipped"
    DISABLED = "disabled"
```

### Updated: `app/connectors/sources.py` BaseConnector

```python
async def search(self, part_number: str) -> list[dict]:
    if self._breaker.current_state == "open":
        # Was: return []. Silently masked the contract.
        # Now: raise so health_monitor stays at status='error' and the
        # search-time _run_one wraps in the per-source error chip.
        raise ConnectorError(
            f"{self.__class__.__name__} circuit breaker open"
        )
    async with self._semaphore:
        return await self._search_with_retry(part_number)

async def _search_with_retry(self, part_number: str) -> list[dict]:
    last_err = None
    for attempt in range(self.max_retries + 1):
        try:
            result = await self._do_search(part_number)
            self._breaker.record_success()
            return result
        except ConnectorError:
            # Hard error from connector — do NOT retry. Fast-fail to
            # stop quota burn against a known-broken upstream.
            self._breaker.record_failure()
            raise
        except (httpx.ConnectTimeout, httpx.ConnectError):
            self._breaker.record_failure()
            raise
        except httpx.HTTPStatusError as e:
            status = e.response.status_code
            if status == 429:
                # ... existing retry-after logic ...
                if attempt < self.max_retries:
                    await asyncio.sleep(retry_after)
                    last_err = e
                    continue
                # Was: return []. Now: raise.
                self._breaker.record_failure()
                raise ConnectorRateLimitError(
                    f"{self.__class__.__name__} rate limited (persistent 429)"
                )
            if status in (401, 403, 422):
                self._breaker.record_failure()
                raise
            # ... existing 5xx retry logic ...
        except Exception as e:
            # ... existing generic retry ...
```

### Updated: 5 connector raise sites

Replace `raise RuntimeError(...)` with specific types:

- `digikey.py:113` — `ConnectorRateLimitError("DigiKey rate limited (persistent 429): ...")`
- `element14.py:65` — `ConnectorAuthError("element14 auth error: HTTP 401 ...")`
- `element14.py:67` — `ConnectorRateLimitError("element14 rate limited: ...")`
- `element14.py` (NEW) — `ConnectorAuthError("element14 auth error: HTTP 403 ...")`
- `oemsecrets.py:46` — `ConnectorAuthError("OEMSecrets auth/quota error: HTTP 401 ...")` (still 401 because OEMSecrets conflates these)
- `oemsecrets.py:48` — `ConnectorRateLimitError("OEMSecrets rate limited: ...")`
- `sourcengine.py:41` — `ConnectorAuthError("Sourcengine auth error: HTTP 401/403 ...")`
- `sourcengine.py:43` — `ConnectorRateLimitError("Sourcengine rate limited: ...")`
- `mouser.py` — replace HTTP-403 silent-empty with `ConnectorAuthError`; replace HTTP-429 silent-empty with `ConnectorRateLimitError`; the existing body-level "Invalid unique identifier" raise stays (becomes `ConnectorAuthError`)
- `sources.py` BrokerBin/Nexar — same swap (RuntimeError → typed)

### Updated: `app/services/health_monitor.py`

Branch on exception type for distinct operator messages and `ApiSource.status` values:

```python
except ConnectorAuthError as e:
    source.status = ApiSourceStatus.ERROR
    source.last_error = f"Auth error — rotate credentials: {str(e)[:400]}"
except ConnectorRateLimitError as e:
    source.status = ApiSourceStatus.ERROR
    source.last_error = f"Rate limited — auto-recovers when window expires: {str(e)[:380]}"
except ConnectorQuotaError as e:
    source.status = ApiSourceStatus.ERROR
    source.last_error = f"Quota exhausted — upgrade plan or wait for cycle: {str(e)[:380]}"
except Exception as e:
    source.status = ApiSourceStatus.ERROR
    source.last_error = str(e)[:500]
```

(Rate-limit could later use `DEGRADED` instead of `ERROR` to skip exclusion;
deferred — for now both flip to `ERROR` and rely on auto-recovery.)

### Updated: `app/search_service.py`

`_build_connectors` already excludes `status='error'` — replace `errored_sources`
literal-string filter with `ApiSourceStatus.ERROR.value` for type safety.

`_make_stat` signature: `status: SourceRunStatus` (was `str`).

`stream_search_mpn` (the UI gap fix): immediately after `_build_connectors`
returns, publish a `source-status` SSE event for every entry in
`source_stats_map` whose status != "ok". This wires the chip strip to the
contract.

### Updated: docs/APP_MAP_INTERACTIONS.md

Rewrite "Connector Failure Contract" subsection to state actual auto-recovery
behavior:

- 401/403 raise `ConnectorAuthError`
- 429 raise `ConnectorRateLimitError`
- Quota raises `ConnectorQuotaError`
- All flip `ApiSource.status = 'error'` via health_monitor
- Source excluded from user searches until next ping success
- Persistent failures (revoked key) stay errored because each ping fails again
- Operator action only required to rotate credentials; re-enable is automatic

Drop the "ping loop stops hitting errored sources" claim.

### Tests

New tests:

- `tests/test_connector_rate_limits.py::TestBaseConnectorContract` — covers
  (1) circuit breaker open raises `ConnectorError`, (2) `ConnectorAuthError`
  from `_do_search` propagates through `connector.search()` without retry,
  (3) httpx 429 exhausted raises `ConnectorRateLimitError`.
- `tests/test_search_streaming.py::TestBuildConnectorsErroredBranch` — covers
  the `status='error'` exclusion in `_build_connectors` and the
  `error_skipped` stat output.
- `tests/test_search_streaming.py::TestStreamSearchMpnNonOkChips` — covers the
  new UI fix: SSE event published for `disabled`/`skipped`/`error_skipped`
  sources at search start.
- `tests/test_health_monitor.py::TestPingSourceTypedErrors` — covers each of
  `ConnectorAuthError`/`ConnectorRateLimitError`/`ConnectorQuotaError`
  producing distinct `last_error` messages.

Updates:

- All existing connector tests using `pytest.raises(RuntimeError, match=...)`
  switch to the specific subclass. (`RuntimeError` still matches via
  inheritance, but the precise type is more informative on test failure.)
- Parametrize Sourcengine 3 tests + Element14 2 tests in test_connectors.py.
- Replace `644b823c` hash refs with `docs/APP_MAP_INTERACTIONS.md` doc refs.
- Update `TestBaseConnector429::test_429_exhausted_returns_empty` to assert
  raise (rename to `test_429_exhausted_raises_for_health_monitor`).

## Sequencing / Risk

Single commit. Test-driven order:

1. Define `ConnectorError` hierarchy + StrEnums (no behavior change yet).
2. Update `BaseConnector` (open breaker raises, no retry on `ConnectorError`,
   429-exhausted raises).
3. Update connector raise sites to typed exceptions.
4. Update Mouser HTTP-403/429 to raise.
5. Update Element14 to add HTTP-403 raise.
6. Update health_monitor branch on exception type.
7. Update search_service `_build_connectors` to use enums.
8. Add `stream_search_mpn` non-ok chip publish (UI gap fix).
9. Update + add tests in dependency order.
10. Update APP_MAP doc + comment sweep.
11. Run full suite + pre-commit.
12. Deploy + live verify.

Risk surface:
- `BaseConnector.search()` raise on open breaker is a behavior change for any
  caller that relied on `[]` (e.g., search_service `_run_one`). Verified:
  `_run_one` catches Exception and surfaces as error chip. Safe.
- `stream_search_mpn` SSE event addition — new event type. Verified: chip
  template handles unknown statuses by class fallback. Safe.
- Auto-recovery is the existing health_monitor behavior; not a change.

## Out of scope

- `error_skipped` cache hole (pre-existing, low impact at current source count).
- DB CHECK constraint on `ApiSource.status` (defer to follow-up; StrEnum at
  the application boundary is enough leverage for now).
- `DEGRADED` status semantics for rate-limit (defer; consolidating to ERROR
  for now keeps the operator UX simple).
- Per-source retry budget config (defer).

## Out-of-band followups (don't block this PR)

- After deploy, monitor `api_sources.last_error` to confirm typed messages
  reach the dashboard.
- If rate-limit auto-recovery proves spammy in production, add a
  cooldown to `run_health_checks` (skip status='error' sources for first N
  minutes after last error).
