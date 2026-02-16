"""Graph API client — retry wrapper, Delta Query, Immutable IDs.

Hardening: H1 (Immutable IDs), H6 (Retry with backoff), H8 (Delta Query).

Usage:
    from app.utils.graph_client import GraphClient
    gc = GraphClient(access_token)
    messages = await gc.get_json("/me/messages", params={"$top": "50"})
    delta_msgs, new_token = await gc.delta_query("/me/mailFolders/Inbox/messages/delta", old_token)
"""
import asyncio
import logging

import httpx

log = logging.getLogger("avail.graph")

GRAPH_BASE = "https://graph.microsoft.com/v1.0"

# H1: Immutable IDs — prevents ID changes when messages are moved between folders
IMMUTABLE_ID_HEADER = {"Prefer": 'IdType="ImmutableId"'}

# H6: Retry config
MAX_RETRIES = 3
BACKOFF_BASE = 2  # seconds — exponential: 2, 4, 8


class GraphClient:
    """Thin wrapper around Microsoft Graph with retry + immutable IDs."""

    def __init__(self, access_token: str):
        self.token = access_token
        self._base_headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
            **IMMUTABLE_ID_HEADER,
        }

    async def get_json(self, path: str, params: dict | None = None,
                       timeout: int = 30) -> dict:
        """GET → parsed JSON. Raises on non-200 after retries."""
        url = path if path.startswith("http") else f"{GRAPH_BASE}{path}"
        async with httpx.AsyncClient(timeout=timeout) as client:
            return await self._request_with_retry(client, "GET", url, params=params)

    async def post_json(self, path: str, json_data: dict,
                        timeout: int = 30) -> dict:
        """POST → parsed JSON or empty dict on 202."""
        url = path if path.startswith("http") else f"{GRAPH_BASE}{path}"
        async with httpx.AsyncClient(timeout=timeout) as client:
            return await self._request_with_retry(client, "POST", url, json_data=json_data)

    async def get_all_pages(self, path: str, params: dict | None = None,
                            max_items: int = 500, timeout: int = 30) -> list[dict]:
        """GET with auto-pagination. Returns flat list of items."""
        url = path if path.startswith("http") else f"{GRAPH_BASE}{path}"
        items: list[dict] = []
        async with httpx.AsyncClient(timeout=timeout) as client:
            while url and len(items) < max_items:
                data = await self._request_with_retry(
                    client, "GET", url,
                    params=params if GRAPH_BASE in url else None
                )
                items.extend(data.get("value", []))
                url = data.get("@odata.nextLink")
                params = None  # nextLink has params baked in
        return items[:max_items]

    # ── H8: Delta Query ─────────────────────────────────────────────

    async def delta_query(self, path: str, delta_token: str | None = None,
                          params: dict | None = None,
                          max_items: int = 1000, timeout: int = 30
                          ) -> tuple[list[dict], str | None]:
        """Run a Delta Query. Returns (items, new_delta_token).

        If delta_token is None, performs a full sync and returns the initial token.
        On subsequent calls, only returns changes since the last token.
        """
        url = path if path.startswith("http") else f"{GRAPH_BASE}{path}"
        if delta_token:
            # Use the stored delta link directly
            url = delta_token
            params = None

        items: list[dict] = []
        new_token: str | None = None

        async with httpx.AsyncClient(timeout=timeout) as client:
            while url and len(items) < max_items:
                data = await self._request_with_retry(
                    client, "GET", url,
                    params=params if not delta_token else None
                )
                items.extend(data.get("value", []))

                # Check for delta link (means we've consumed all changes)
                new_token = data.get("@odata.deltaLink")
                if new_token:
                    break

                # More pages of changes
                url = data.get("@odata.nextLink")
                params = None

        return items[:max_items], new_token

    # ── Internal retry logic ────────────────────────────────────────

    async def _request_with_retry(
        self, client: httpx.AsyncClient,
        method: str, url: str,
        params: dict | None = None,
        json_data: dict | None = None,
    ) -> dict:
        """Execute HTTP request with exponential backoff on 429 / 5xx."""
        last_error: Exception | None = None

        for attempt in range(MAX_RETRIES + 1):
            try:
                if method == "GET":
                    resp = await client.get(url, params=params,
                                            headers=self._base_headers)
                else:
                    resp = await client.post(url, json=json_data,
                                             headers=self._base_headers)

                # Success
                if resp.status_code in (200, 201):
                    return resp.json()
                if resp.status_code == 202:
                    return {}  # Accepted (e.g., sendMail)
                if resp.status_code == 204:
                    return {}  # No content

                # Throttled — respect Retry-After
                if resp.status_code == 429:
                    wait = int(resp.headers.get("Retry-After", BACKOFF_BASE ** (attempt + 1)))
                    log.warning(f"Graph 429 — retry in {wait}s (attempt {attempt + 1})")
                    await asyncio.sleep(wait)
                    continue

                # Server error — exponential backoff
                if resp.status_code >= 500:
                    wait = BACKOFF_BASE ** (attempt + 1)
                    log.warning(f"Graph {resp.status_code} — retry in {wait}s (attempt {attempt + 1})")
                    await asyncio.sleep(wait)
                    continue

                # Client error (400, 401, 403, 404) — don't retry
                log.error(f"Graph {resp.status_code}: {resp.text[:300]}")
                return {"error": resp.status_code, "detail": resp.text[:300]}

            except (httpx.TimeoutException, httpx.ConnectError) as e:
                last_error = e
                wait = BACKOFF_BASE ** (attempt + 1)
                log.warning(f"Graph connection error — retry in {wait}s: {e}")
                await asyncio.sleep(wait)

        # All retries exhausted
        log.error(f"Graph request failed after {MAX_RETRIES} retries: {url}")
        if last_error:
            raise last_error
        return {"error": "max_retries", "detail": "All retries exhausted"}
