"""Polite direct-HTTP PartSurfer description fetcher (HP/HPE spare → verbatim desc).

What: ``fetch_partsurfer_description`` does ONE GET against partsurfer.hpe.com's
      server-rendered Search.aspx for an HP/HPE spare/option PN and returns the OEM's own
      verbatim part DESCRIPTION (the ``ctl00_BodyContentPlaceHolder_lblDescription`` span),
      html-unescaped and stripped — e.g. "726719-B21" → "HPE 16GB (1X16GB) DUAL RANK X4
      DDR4-2133 CAS-15-15-15 REGISTERED MEMORY KIT". PartSurfer's Product Number just
      echoes the spare, so the canonical-MPN crosswalk is useless for HP; the rich
      description is the win — fed into the existing desc-grammar it categorizes the ~70k
      uncategorized HP cards. Best-effort: a non-200 that is a genuine no-result (404/3xx),
      a missing/empty description, or permanently-bad input returns ``None`` and is logged.
      But a THROTTLE/OUTAGE signal (429, 5xx, or any httpx transport/timeout error) RAISES
      ``PartSurferTransient`` so the caller backs off this batch instead of mistaking the
      throttle for "no result" and hammering the host. ``None`` means a genuine no-result.
Called by: app/services/enrichment_worker/worker.py (the partsurfer_desc batch pass,
      gated by settings.partsurfer_desc_enabled).
Depends on: app.http_client.http_redirect (the shared pooled client — no new client),
      stdlib re / html / urllib. Politeness (1 req / 2s) is paced by the CALLER.
"""

from __future__ import annotations

import html
import re
from urllib.parse import quote

import httpx
from loguru import logger

from app.http_client import http_redirect

# Contact UA so PartSurfer's operators can reach us (robots.txt allows Search.aspx; only
# /WebResource.axd is disallowed). Keep this honest and reachable.
_UA = "AvailAI-PartLookup/1.0 (+sourcing enrichment; contact mkhoury@trioscs.com)"
_BASE_URL = "https://partsurfer.hpe.com/Search.aspx"

# The verbatim part description lives in this ASP.NET label span. Non-greedy to the first
# '<' so trailing markup is never captured.
_LBL_DESCRIPTION_RE = re.compile(r'lblDescription"[^>]*>([^<]*)')

# Throttle/outage statuses: the host is signalling "back off", not "no such part".
_TRANSIENT_STATUSES = frozenset({429, 500, 502, 503, 504})


class PartSurferTransient(Exception):
    """Throttle/outage signal (429, 5xx, timeout/transport) — caller should back off,
    NOT treat as 'no result'."""


async def fetch_partsurfer_description(spare_pn: str, *, timeout: int = 12) -> str | None:
    """Fetch the verbatim PartSurfer description for *spare_pn*, or ``None``.

    Single GET against the shared pooled ``http_redirect`` client with the contact UA.
    Returns the html-unescaped/stripped ``lblDescription`` text, or ``None`` for a GENUINE
    no-result: a blank spare, permanently-bad input (``httpx.InvalidURL``), a non-200 that
    is not a throttle (e.g. 404/3xx), or a missing/empty description span.

    RAISES ``PartSurferTransient`` on a throttle/outage so the caller can back off this
    batch instead of mistaking it for a no-result: a 429 / 5xx response, or any
    ``httpx.HTTPError`` (timeout, transport, transient) — each logged at WARNING first.
    The best-effort ``timeout`` is short (12s) so a slow host can't pin the single-threaded
    worker. A non-str/pathological ``.text`` is still swallowed to ``None`` (a parse
    failure on a 200 is a genuine no-description, not a throttle).
    """
    spare = (spare_pn or "").strip()
    if not spare:
        return None

    url = f"{_BASE_URL}?SearchText={quote(spare)}"
    try:
        resp = await http_redirect.get(url, headers={"User-Agent": _UA}, timeout=timeout)
    except httpx.InvalidURL as exc:
        # Permanently-bad input for this spare — a retry can't help, so it's a no-result.
        logger.warning("partsurfer: invalid URL for {} ({}): {}", spare, type(exc).__name__, exc)
        return None
    except httpx.HTTPError as exc:
        # Timeout / transport / any transient httpx failure → back off, don't no-result.
        logger.warning("partsurfer: transport error for {} ({}): {}", spare, type(exc).__name__, exc)
        raise PartSurferTransient(f"partsurfer transport error for {spare}: {type(exc).__name__}") from exc

    status = getattr(resp, "status_code", None)
    if status in _TRANSIENT_STATUSES:
        # Throttle / outage — signal back-off, do NOT treat as "no result".
        logger.warning("partsurfer: {} for {} — throttle/outage, backing off", status, spare)
        raise PartSurferTransient(f"partsurfer {status} for {spare}")
    if status != 200:
        logger.info("partsurfer: non-200 for {} (status={}) — no description", spare, status)
        return None

    try:
        match = _LBL_DESCRIPTION_RE.search(resp.text or "")
    except Exception as exc:  # defensive: a non-str .text or pathological input
        logger.warning("partsurfer: parse failed for {} ({}): {}", spare, type(exc).__name__, exc)
        return None

    if not match:
        logger.info("partsurfer: no lblDescription for {} — likely no result", spare)
        return None

    description = html.unescape(match.group(1)).strip()
    if not description:
        logger.info("partsurfer: empty lblDescription for {}", spare)
        return None

    logger.info("partsurfer: {} -> {!r}", spare, description)
    return description
