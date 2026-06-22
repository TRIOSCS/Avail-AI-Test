"""Clay enrichment via the backend MCP client (no webhook).

Calls the hosted Clay MCP (https://api.clay.com/v3/mcp) with an OAuth access token:
- enrich_company: find-and-enrich-company returns base firmographics synchronously.
- find_contacts: find-and-enrich-contacts-at-company returns a base contact list inline;
  emails (Email data point) are polled via get-task-context (bounded, _POLL_TRIES).

Called by: app/services/enrichment_router.py (Task 6). Depends on: app/http_client.py,
app/services/clay_oauth (OAuth token), app/services/enrichment_credit_guard (circuit).

Transport confirmed by Task 0 spike: JSON-RPC 2.0 over HTTPS, tools/call method,
Authorization: Bearer, response unwrapped from result.structuredContent.
Authentication: OAuth authorization_code + PKCE (Clay MCP is OAuth-gated, not key-based).
"""

import asyncio
import re

import httpx
from loguru import logger

from app.http_client import http
from app.services import clay_oauth
from app.services.enrichment_credit_guard import ProviderQuotaError

MCP_URL = "https://api.clay.com/v3/mcp"

_QUOTA_STATUSES = (402, 429)
_POLL_TRIES = 5
_POLL_DELAY = 3.0

# Matches "(NYSE:ARW)" or "(NASDAQ:INTC)" in company descriptions.
_TICKER_RE = re.compile(r"\((?:NYSE|NASDAQ):\s*([A-Z]{1,6})\)")


# ── credential ────────────────────────────────────────────────────────────────


async def _access_token() -> str | None:
    """Valid Clay OAuth access token (auto-refreshed), or None if not connected."""
    return await clay_oauth.get_access_token()


# ── transport ─────────────────────────────────────────────────────────────────


async def _mcp_call(tool: str, args: dict) -> dict:
    """POST a JSON-RPC tools/call to the Clay MCP and return the structured result.

    On HTTP 401 the token is refreshed once and the request is retried. 402/429 → raises
    ProviderQuotaError (not swallowed). Other non-200 → logs warning and returns {}.
    """
    token = await _access_token()
    if not token:
        return {}

    async def _post(tok: str):
        return await http.post(
            MCP_URL,
            headers={
                "Authorization": f"Bearer {tok}",
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": tool, "arguments": args},
            },
            timeout=40,
        )

    resp = await _post(token)
    if resp.status_code == 401:  # token rejected → one refresh + retry
        new = await clay_oauth.refresh()
        if not new:
            logger.warning("Clay MCP 401 and refresh failed — not connected")
            return {}
        resp = await _post(new)
    if resp.status_code in _QUOTA_STATUSES:
        raise ProviderQuotaError(f"Clay MCP quota/rate-limit: {resp.status_code}")
    if resp.status_code != 200:
        logger.warning("Clay MCP {} failed: {}", tool, resp.status_code)
        return {}
    payload = resp.json()
    # Unwrap JSON-RPC envelope → structuredContent (per spike observation).
    result = payload.get("result", payload)
    content = result.get("structuredContent") or result
    return content if isinstance(content, dict) else {}


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
        "revenue_range": comp.get("annual_revenue"),
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
