"""Clay enrichment via the backend MCP client (no webhook).

Calls the hosted Clay MCP (https://api.clay.com/v3/mcp) with an OAuth access token:
- enrich_company: find-and-enrich-company returns base firmographics synchronously
  under result.structuredContent.companies[domain].
- find_contacts: find-and-enrich-contacts-at-company returns a base contact list inline
  under result.structuredContent.contacts[]; emails (Email data point) are polled via
  get-task-context (bounded, _POLL_TRIES).

Called by: app/services/enrichment_router.py. Depends on: app/http_client.py,
app/services/clay_oauth (OAuth token), app/services/enrichment_credit_guard (circuit).

Transport (verified live 2026-06-23 against api.clay.com/v3/mcp): Clay speaks MCP
Streamable HTTP, so every tools/call requires a session established by the initialize
handshake — a bare tools/call returns 400 {"message": "Missing Mcp-Session-Id header"}.
Sequence: POST initialize → 200 carrying the Mcp-Session-Id response header +
protocolVersion 2025-06-18 → POST notifications/initialized (session header) → 202 →
POST tools/call (session header) → 200. tools/call responses are server-sent events
(content-type text/event-stream), NOT plain JSON, so the body is parsed from the
``data:`` line. The session is cached per access token and reused across calls.
Authentication: OAuth authorization_code + PKCE (Clay MCP is OAuth-gated, not key-based).
"""

import asyncio
import json
import re

import httpx
from loguru import logger

from app.http_client import http
from app.services import clay_oauth
from app.services.enrichment_credit_guard import ProviderQuotaError

MCP_URL = "https://api.clay.com/v3/mcp"
_PROTOCOL_VERSION = "2025-06-18"

_QUOTA_STATUSES = (402, 429)
_SESSION_STATUSES = (400, 404)  # missing/expired Mcp-Session-Id → re-initialize once
_POLL_TRIES = 5
_POLL_DELAY = 3.0

# Matches "(NYSE:ARW)" or "(NASDAQ:INTC)" in company descriptions.
_TICKER_RE = re.compile(r"\((?:NYSE|NASDAQ):\s*([A-Z]{1,6})\)")

# Server-side MCP session, cached per access token: one initialize handshake per token,
# reused across tools/call. A lock prevents concurrent callers racing to initialize.
_session: dict[str, str | None] = {"id": None, "token": None}
_session_lock = asyncio.Lock()


# ── credential ────────────────────────────────────────────────────────────────


async def _access_token() -> str | None:
    """Valid Clay OAuth access token (auto-refreshed), or None if not connected."""
    return await clay_oauth.get_access_token()


# ── transport ─────────────────────────────────────────────────────────────────


def _headers(token: str, session_id: str | None = None) -> dict[str, str]:
    """Build MCP request headers, adding the session header when present."""
    h = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if session_id:
        h["Mcp-Session-Id"] = session_id
    return h


def _parse_response(resp: httpx.Response) -> dict:
    """Parse an MCP response body, which may be JSON or SSE (text/event-stream).

    For SSE the JSON-RPC payload rides on ``data:`` lines; use the last well-formed one.
    Returns {} if nothing parses.
    """
    if "text/event-stream" in resp.headers.get("content-type", ""):
        payload: dict = {}
        for line in resp.text.splitlines():
            if line.startswith("data:"):
                try:
                    payload = json.loads(line[5:].strip())
                except ValueError:
                    continue
        return payload if isinstance(payload, dict) else {}
    try:
        body = resp.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


async def _initialize(token: str) -> str | None:
    """Run the MCP handshake and return the session id, or None on failure.

    initialize → read the Mcp-Session-Id response header → notifications/initialized.
    """
    resp = await http.post(
        MCP_URL,
        headers=_headers(token),
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": _PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "availai", "version": "1.0"},
            },
        },
        timeout=40,
    )
    if resp.status_code != 200:
        logger.warning("Clay MCP initialize failed: {}", resp.status_code)
        return None
    session_id: str | None = resp.headers.get("mcp-session-id")
    if not session_id:
        logger.warning("Clay MCP initialize returned no Mcp-Session-Id header")
        return None
    # Complete the handshake. If the server rejects the notification, the session is
    # half-open and must not be cached — return None so the caller re-handshakes.
    note = await http.post(
        MCP_URL,
        headers=_headers(token, session_id),
        json={"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
        timeout=40,
    )
    if note.status_code not in (200, 202):
        logger.warning("Clay MCP notifications/initialized failed: {}", note.status_code)
        return None
    return session_id


async def _ensure_session(token: str, *, invalidate: str | None = None) -> str | None:
    """Return a cached session id for *token*, establishing one if needed.

    *invalidate* is the session id the caller just saw fail; the cache is cleared only
    if it still holds that exact id, so a concurrent peer that already re-established a
    session short-circuits the herd (no thundering re-initialize, no clobber). A token
    change always invalidates. Serialised by a lock so callers share one initialize.
    """
    async with _session_lock:
        if _session["token"] != token or (invalidate is not None and _session["id"] == invalidate):
            _session["id"] = None
        if not _session["id"]:
            _session["id"] = await _initialize(token)
            _session["token"] = token
        return _session["id"]


def _content_text(result: dict) -> str:
    """Return the first text block from an MCP result's content array, or ''."""
    for block in result.get("content") or []:
        if isinstance(block, dict) and block.get("type") == "text" and block.get("text"):
            return block["text"]
    return ""


def _unwrap(payload: dict) -> dict:
    """Extract the tool's structured dict from a JSON-RPC tools/call payload.

    Handles, in order: a JSON-RPC envelope error; an MCP tool-level error
    (``result.isError`` at HTTP 200, which otherwise looks like 'no data'); the preferred
    ``result.structuredContent``; and a fallback of parsing ``result.content[0].text`` as
    JSON (structuredContent is only contractually present when the tool declares an
    outputSchema, which Clay's tools do not). Returns {} when nothing usable is found.
    """
    if "error" in payload and "result" not in payload:
        logger.warning("Clay MCP returned JSON-RPC error: {}", payload.get("error"))
        return {}
    result = payload.get("result")
    if not isinstance(result, dict):
        return {}
    if result.get("isError"):
        logger.warning("Clay MCP tool error: {}", _content_text(result) or "unknown")
        return {}
    structured = result.get("structuredContent")
    if isinstance(structured, dict):
        return structured
    text = _content_text(result)
    if text:
        try:
            parsed = json.loads(text)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            return parsed
    return {}


async def _mcp_call(tool: str, args: dict) -> dict:
    """Run a JSON-RPC tools/call against the Clay MCP and return the structured result.

    Establishes/reuses an MCP session (Clay rejects sessionless calls with 400). Performs
    at most one retry per call: on a 401 it refreshes the token + re-initializes the
    session; on a 400/404 it re-initializes the (expired) session. 402/429 → raises
    ProviderQuotaError (not swallowed). Other non-200 → logs a warning and returns {}.
    """
    token = await _access_token()
    if not token:
        return {}

    async def _post(tok: str, sid: str):
        return await http.post(
            MCP_URL,
            headers=_headers(tok, sid),
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": tool, "arguments": args},
            },
            timeout=40,
        )

    session_id = await _ensure_session(token)
    if not session_id:
        return {}
    resp = await _post(token, session_id)

    if resp.status_code == 401:  # token rejected → refresh, re-init session, retry once
        token = await clay_oauth.refresh()
        if not token:
            logger.warning("Clay MCP 401 and refresh failed — not connected")
            return {}
        session_id = await _ensure_session(token, invalidate=session_id)
        if not session_id:
            return {}
        resp = await _post(token, session_id)
    elif resp.status_code in _SESSION_STATUSES:  # session expired → re-init, retry once
        session_id = await _ensure_session(token, invalidate=session_id)
        if not session_id:
            return {}
        resp = await _post(token, session_id)

    if resp.status_code in _QUOTA_STATUSES:
        raise ProviderQuotaError(f"Clay MCP quota/rate-limit: {resp.status_code}")
    if resp.status_code != 200:
        logger.warning("Clay MCP {} failed: {}", tool, resp.status_code)
        return {}

    return _unwrap(_parse_response(resp))


# ── company mapping helpers ───────────────────────────────────────────────────


def _parse_locality(locality: str) -> tuple[str | None, str | None]:
    """Split "City, State" into (city, state).

    Returns (None, None) for empty.
    """
    if not locality:
        return None, None
    parts = [p.strip() for p in locality.split(",")]
    city = parts[0] or None
    state = parts[1].strip() if len(parts) > 1 else None
    return city, state or None


def _map_company(domain: str, payload: dict) -> dict | None:
    """Normalise a Clay company payload to the shared firmographic shape.

    Returns None if the payload contains no data for the requested domain.
    """
    comp = (payload.get("companies") or {}).get(domain) or {}
    if not comp:
        return None

    city, state = _parse_locality(comp.get("locality") or "")
    ticker_m = _TICKER_RE.search(comp.get("description") or "")

    out: dict = {
        "source": "clay",
        "legal_name": comp.get("name"),
        "domain": comp.get("domain") or domain,
        "website": comp.get("website"),
        "industry": comp.get("industry"),
        "employee_size": comp.get("size"),
        "hq_city": city,
        "hq_state": state,
        "hq_country": comp.get("country"),
        "linkedin_url": comp.get("url"),
        # No revenue_range: annual_revenue is not a base firmographic field and this call
        # does not request the paid Annual Revenue data point. ticker is parsed from the
        # description ("(NYSE:ARW)"), which IS present on the base response.
        "ticker": ticker_m.group(1) if ticker_m else None,
    }
    # Return None if every informative field is falsy (source + domain are metadata).
    if not any(v for k, v in out.items() if k not in ("source", "domain")):
        return None
    return out


# ── public interface ──────────────────────────────────────────────────────────


async def enrich_company(domain: str) -> dict | None:
    """Return firmographics for *domain* from Clay, or None if unavailable/error.

    402/429 → ProviderQuotaError (propagates; caller trips the circuit breaker). Token
    absent (not connected) → returns None immediately (no network call).
    """
    if not await _access_token():
        return None
    try:
        payload = await _mcp_call("find-and-enrich-company", {"companyIdentifier": domain})
        return _map_company(domain, payload)
    except ProviderQuotaError:
        raise  # propagate — do NOT swallow quota errors
    except (httpx.HTTPError, KeyError, ValueError) as exc:
        logger.warning("Clay MCP company enrichment error for {}: {}", domain, exc)
        return None


async def _poll_emails(task_id: str, n: int) -> dict:
    """Poll get-task-context until Email enrichments complete; return {entityId: email}.

    Stops early when all in-flight enrichments reach a terminal state. Bounded to *n*
    iterations with _POLL_DELAY seconds between each.
    """
    emails: dict = {}
    for _ in range(n):
        await asyncio.sleep(_POLL_DELAY)
        ctx = await _mcp_call("get-task-context", {"taskId": task_id})
        in_progress = False
        for contact in ctx.get("contacts") or []:
            for enr in contact.get("enrichments") or []:
                if (enr.get("name") or "").lower().startswith("email"):
                    state = enr.get("state")
                    if state == "completed" and enr.get("value"):
                        emails[contact.get("entityId")] = enr["value"]
                    elif state == "in-progress":
                        in_progress = True
        if not in_progress:
            break
    return emails


async def find_contacts(
    domain: str,
    title_filter: str,
    limit: int,
    want_email: bool,
) -> list[dict]:
    """Return contacts at *domain* from Clay, filtered to that domain only.

    Drops contacts whose current ``domain`` field does not match *domain* (ex-employees
    at other companies). If *want_email* is True the Email data point is requested and
    polled asynchronously (bounded poll).

    Not connected → returns [] immediately.
    402/429 → ProviderQuotaError propagates.
    """
    if not await _access_token():
        return []
    try:
        args: dict = {"companyIdentifier": domain}
        if title_filter:
            args["contactFilters"] = {"job_title_keywords": [title_filter]}
        if want_email:
            args["dataPoints"] = {"contactDataPoints": [{"type": "Email"}]}

        payload = await _mcp_call("find-and-enrich-contacts-at-company", args)

        # Filter to target domain before applying limit (ex-employee guard).
        raw = [c for c in (payload.get("contacts") or []) if c.get("domain") == domain]
        raw = raw[:limit]

        # Poll for emails only if the API returned a taskId and caller wants them.
        task_id = payload.get("taskId")
        emails: dict = {}
        if want_email and task_id:
            emails = await _poll_emails(task_id, _POLL_TRIES)

        out = []
        for contact in raw:
            full_name = contact.get("name")
            if not full_name:
                continue
            out.append(
                {
                    "source": "clay",
                    "full_name": full_name,
                    "title": contact.get("latest_experience_title"),
                    "linkedin_url": contact.get("url"),
                    "location": contact.get("location_name"),
                    "company": contact.get("latest_experience_company"),
                    "email": emails.get(contact.get("entityId")),
                    "verified": False,
                }
            )
        return out

    except ProviderQuotaError:
        raise
    except (httpx.HTTPError, KeyError, ValueError) as exc:
        logger.warning("Clay MCP contacts error for {}: {}", domain, exc)
        return []
