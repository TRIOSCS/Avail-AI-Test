"""Graph API client — retry wrapper, Delta Query, Immutable IDs.

Hardening: H1 (Immutable IDs), H6 (Retry with backoff), H8 (Delta Query).

Usage:
    from app.utils.graph_client import GraphClient
    gc = GraphClient(access_token)
    messages = await gc.get_json("/me/messages", params={"$top": "50"})
    delta_msgs, new_token = await gc.delta_query("/me/mailFolders/Inbox/messages/delta", old_token)
"""

import asyncio
import os
from datetime import UTC, datetime, timedelta
from typing import cast

from loguru import logger

from app.http_client import http

GRAPH_BASE = "https://graph.microsoft.com/v1.0"


class GraphSyncStateExpired(Exception):
    """Raised when Graph API returns 410 — the delta token is stale and must be
    discarded."""


class GraphAPIError(Exception):
    """Raised when a paged request (delta_query / get_all_pages) hits an error page.

    _request_with_retry returns ``{"error": <status>, "detail": ...}`` dicts for
    non-retryable failures (401/4xx/max_retries) — a contract webhook_service
    branches on, so it stays. Pagination helpers must NOT treat those dicts as
    empty pages (that silently ends the round and lets callers persist a token
    past unfetched data), so they raise this instead.
    """

    def __init__(self, status: int | str, detail: str = ""):
        self.status = status
        self.detail = detail
        super().__init__(f"Graph API error {status}: {detail}")


# H1: Immutable IDs — prevents ID changes when messages are moved between folders
IMMUTABLE_ID_HEADER = {"Prefer": 'IdType="ImmutableId"'}

# H6: Retry config — in TESTING mode, fail fast (no retries, no sleep)
_TESTING = bool(os.environ.get("TESTING"))
MAX_RETRIES = 0 if _TESTING else 3
BACKOFF_BASE = 0 if _TESTING else 2  # seconds — exponential: 2, 4, 8


class GraphClient:
    """Thin wrapper around Microsoft Graph with retry + immutable IDs."""

    def __init__(self, access_token: str):
        self.token = access_token
        self._base_headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
            **IMMUTABLE_ID_HEADER,
        }

    async def get_json(self, path: str, params: dict | None = None, timeout: int = 30) -> dict:
        """GET → parsed JSON.

        Raises on non-200 after retries.
        """
        url = path if path.startswith("http") else f"{GRAPH_BASE}{path}"
        return await self._request_with_retry("GET", url, params=params, timeout=timeout)

    async def post_json(self, path: str, json_data: dict, timeout: int = 30) -> dict:
        """POST → parsed JSON or empty dict on 202."""
        url = path if path.startswith("http") else f"{GRAPH_BASE}{path}"
        return await self._request_with_retry("POST", url, json_data=json_data, timeout=timeout)

    async def patch_json(self, path: str, json_data: dict, timeout: int = 30) -> dict:
        """PATCH → parsed JSON or empty dict on 204."""
        url = path if path.startswith("http") else f"{GRAPH_BASE}{path}"
        return await self._request_with_retry("PATCH", url, json_data=json_data, timeout=timeout)

    async def get_all_pages(
        self,
        path: str,
        params: dict | None = None,
        max_items: int = 500,
        timeout: int = 30,
    ) -> list[dict]:
        """GET with auto-pagination.

        Returns a flat list of items, truncated to max_items. An error page
        ({"error": ...} from the retry layer) raises GraphAPIError instead of
        silently returning the short list fetched so far.
        """
        url = path if path.startswith("http") else f"{GRAPH_BASE}{path}"
        items: list[dict] = []
        while url and len(items) < max_items:
            data = await self._request_with_retry(
                "GET", url, params=params if GRAPH_BASE in url else None, timeout=timeout
            )
            if "error" in data:
                raise GraphAPIError(data["error"], str(data.get("detail", "")))
            items.extend(data.get("value", []))
            url = data.get("@odata.nextLink")
            params = None  # nextLink has params baked in
        return items[:max_items]

    # ── Sent folder search ─────────────────────────────────────────

    async def search_sent_messages(
        self,
        query: str,
        user_id: str | None = None,
        max_results: int = 10,
    ) -> list[dict]:
        """Search a user's Sent Items folder for messages matching a query string.

        Args:
            query: Search term (e.g. PO number) to find in sent messages.
            user_id: Azure AD user ID. If None, searches /me.
            max_results: Max messages to return.

        Returns:
            List of message dicts with id, subject, toRecipients, sentDateTime.
        """
        base = f"/users/{user_id}" if user_id else "/me"
        path = f"{base}/mailFolders/SentItems/messages"
        # Escape single quotes per OData convention to prevent filter injection
        safe_query = query.replace("'", "''")
        params = {
            "$filter": f"contains(subject,'{safe_query}') or contains(body/content,'{safe_query}')",
            "$select": "id,subject,toRecipients,sentDateTime",
            "$top": str(max_results),
            "$orderby": "sentDateTime desc",
        }
        data = await self.get_json(path, params=params)
        if isinstance(data, dict) and "error" in data:
            raise RuntimeError(f"Graph API error searching sent messages: {data}")
        messages: list[dict] = data.get("value", [])  # Graph JSON boundary
        return messages

    # ── H8: Delta Query ─────────────────────────────────────────────

    async def delta_query(
        self,
        path: str,
        delta_token: str | None = None,
        params: dict | None = None,
        max_items: int = 1000,
        timeout: int = 30,
        max_page_size: int | None = None,
        initial_lookback_days: int | None = None,
    ) -> tuple[list[dict], str | None]:
        """Run a Delta Query. Returns (items, new_state_token).

        If delta_token is None, performs a full sync from ``path``; otherwise it
        resumes from the stored token URL (a deltaLink OR a mid-round nextLink —
        both are opaque, durable Graph URLs and are interchangeable here).

        Contract — fetched items are NEVER dropped:
        - Round completes → (all items, deltaLink).
        - max_items reached mid-round → stop paging and return (all items fetched
          so far, current nextLink). The nextLink is the resumable state: persist
          it exactly like a deltaLink and the next call finishes the round.
          Because whole pages are returned, len(items) may exceed max_items by up
          to one page — max_items is a paging budget, not a slice.
        - Error page ({"error": ...} from the retry layer) → raise GraphAPIError;
          no token is returned, so sync state cannot advance past unfetched data.

        Initial full-sync rounds (delta_token=None) enumerate the ENTIRE
        collection — because the nextLink is resumable state, successive calls
        WILL drain it all. For message folders that means the whole mailbox
        history, so callers MUST bound it with initial_lookback_days: it adds
        ``$filter=receivedDateTime ge {ts}`` to the initial request — the only
        $filter Graph supports on message deltas — and Graph bakes the filter
        into every nextLink/deltaLink it returns, so the bound sticks for the
        life of the sync state (a filtered round also returns at most 5,000
        messages, per Graph docs). Ignored when resuming from a token. Leave it
        None for non-message deltas (e.g. /me/contacts/delta) — they don't
        support this filter and are finite collections anyway.

        max_page_size sets "Prefer: odata.maxpagesize" (the documented page-size
        control for delta queries), preserving the ImmutableId preference (H1).
        """
        url = path if path.startswith("http") else f"{GRAPH_BASE}{path}"
        if delta_token:
            # Use the stored state URL (deltaLink or nextLink) directly
            url = delta_token
            params = None
        elif initial_lookback_days:
            since = (datetime.now(UTC) - timedelta(days=initial_lookback_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
            params = {**(params or {}), "$filter": f"receivedDateTime ge {since}"}

        headers: dict[str, str] | None = None
        if max_page_size:
            # Prefer values combine — keep IdType="ImmutableId" (H1) alongside.
            headers = {"Prefer": f"{IMMUTABLE_ID_HEADER['Prefer']}, odata.maxpagesize={max_page_size}"}

        items: list[dict] = []
        while True:
            data = await self._request_with_retry("GET", url, params=params, timeout=timeout, headers=headers)
            if "error" in data:
                raise GraphAPIError(data["error"], str(data.get("detail", "")))
            items.extend(data.get("value", []))

            # Delta link → round complete, all changes consumed
            delta_link: str | None = data.get("@odata.deltaLink")
            if delta_link:
                return items, delta_link

            next_link: str | None = data.get("@odata.nextLink")
            if not next_link:
                # Defensive: Graph ends every delta round with a deltaLink, so a
                # link-less page is an anomaly — return None so the caller keeps
                # its previous token rather than persisting bogus state.
                logger.warning(f"Delta page had neither deltaLink nor nextLink: {url[:120]}")
                return items, None

            if len(items) >= max_items:
                # Per-run budget reached — the nextLink is durable, resumable
                # state; the next run picks up the round exactly here.
                return items, next_link

            url = next_link
            params = None  # nextLink has params baked in

    # ── Internal retry logic ────────────────────────────────────────

    async def _request_with_retry(
        self,
        method: str,
        url: str,
        params: dict | None = None,
        json_data: dict | None = None,
        timeout: int = 30,
        headers: dict[str, str] | None = None,
    ) -> dict:
        """Execute HTTP request with retry logic.

        headers, when given, are merged OVER the constructor-level base headers
        for this request only (a per-request "Prefer" replaces the base one, so
        callers overriding it must re-include IdType="ImmutableId").

        Retry strategy:
        - 429 (rate limit): always retry, honor Retry-After header (use max of
          backoff and Retry-After so we never wait less than the server asks).
        - 503 (service unavailable): retry with backoff, honor Retry-After if present.
        - Other 5xx: retry with exponential backoff.
        - 401 (auth expired): never retry — token must be refreshed by caller.
        - Other 4xx (400, 403, 404): never retry.
        - 410 (delta token expired): raise GraphSyncStateExpired immediately.
        """
        last_error: Exception | None = None
        request_headers = self._base_headers if headers is None else {**self._base_headers, **headers}

        for attempt in range(MAX_RETRIES + 1):
            try:
                if method == "GET":
                    resp = await http.get(url, params=params, headers=request_headers, timeout=timeout)
                elif method == "PATCH":
                    resp = await http.patch(url, json=json_data, headers=request_headers, timeout=timeout)
                else:
                    resp = await http.post(url, json=json_data, headers=request_headers, timeout=timeout)

                # Success
                if resp.status_code in (200, 201):
                    # cast: httpx .json() is untyped; Graph API bodies are JSON objects.
                    return cast(dict, resp.json())
                if resp.status_code == 202:
                    return {}  # Accepted (e.g., sendMail)
                if resp.status_code == 204:
                    return {}  # No content

                # 401 Unauthorized — don't retry, token needs refresh
                if resp.status_code == 401:
                    logger.warning("Graph 401 Unauthorized — not retrying (token expired)")
                    return {"error": 401, "detail": resp.text[:300]}

                # 410 Gone — delta token expired, caller must discard and re-sync
                if resp.status_code == 410:
                    logger.warning("Graph 410 SyncStateNotFound — delta token expired")
                    raise GraphSyncStateExpired(resp.text[:300])

                # Throttled (429) / Service Unavailable (503) — respect Retry-After,
                # using the max of backoff and header so we never wait less than asked.
                if resp.status_code in (429, 503):
                    backoff_wait = BACKOFF_BASE ** (attempt + 1)
                    retry_after = _parse_retry_after(resp)
                    wait = max(backoff_wait, retry_after) if retry_after else backoff_wait
                    logger.warning(f"Graph {resp.status_code} — retry in {wait}s (attempt {attempt + 1})")
                    await asyncio.sleep(wait)
                    continue

                # Other server errors — exponential backoff
                if resp.status_code >= 500:
                    wait = BACKOFF_BASE ** (attempt + 1)
                    logger.warning(f"Graph {resp.status_code} — retry in {wait}s (attempt {attempt + 1})")
                    await asyncio.sleep(wait)
                    continue

                # Other client errors (400, 403, 404) — don't retry
                logger.error(f"Graph {resp.status_code}: {resp.text[:300]}")
                return {"error": resp.status_code, "detail": resp.text[:300]}

            except GraphSyncStateExpired:
                raise
            except Exception as e:
                last_error = e
                wait = BACKOFF_BASE ** (attempt + 1)
                logger.warning(f"Graph connection error — retry in {wait}s: {e}")
                await asyncio.sleep(wait)

        # All retries exhausted
        logger.error(f"Graph request failed after {MAX_RETRIES} retries: {url}")
        if last_error:
            raise last_error
        return {"error": "max_retries", "detail": "All retries exhausted"}


def _parse_retry_after(resp) -> int | None:
    """Parse the Retry-After header value as an integer (seconds).

    Returns None if the header is absent or unparseable.
    """
    raw = resp.headers.get("Retry-After")
    if raw is None:
        return None
    try:
        return int(raw)
    except (ValueError, TypeError):
        return None
