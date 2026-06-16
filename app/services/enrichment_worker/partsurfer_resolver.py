"""Polite direct-HTTP PartSurfer description fetcher (HP/HPE spare → verbatim desc).

What: ``fetch_partsurfer_description`` does ONE GET against partsurfer.hpe.com's
      server-rendered Search.aspx for an HP/HPE spare/option PN and returns the OEM's own
      verbatim part DESCRIPTION (the ``ctl00_BodyContentPlaceHolder_lblDescription`` span),
      html-unescaped and stripped — e.g. "726719-B21" → "HPE 16GB (1X16GB) DUAL RANK X4
      DDR4-2133 CAS-15-15-15 REGISTERED MEMORY KIT". PartSurfer's Product Number just
      echoes the spare, so the canonical-MPN crosswalk is useless for HP; the rich
      description is the win — fed into the existing desc-grammar it categorizes the ~70k
      uncategorized HP cards. Best-effort and resilient: a non-200, a missing/empty
      description, or ANY httpx/parse error returns ``None`` and is logged — it NEVER
      raises into the worker.
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


async def fetch_partsurfer_description(spare_pn: str, *, timeout: int = 30) -> str | None:
    """Fetch the verbatim PartSurfer description for *spare_pn*, or ``None``.

    Single GET against the shared pooled ``http_redirect`` client with the contact UA.
    Returns the html-unescaped/stripped ``lblDescription`` text, or ``None`` for a blank
    spare, a non-200 status, a missing/empty description span, or ANY exception
    (httpx transport, regex/parse) — the latter logged at WARNING. Never raises: the
    worker pass must treat a failed lookup as "no description this card", not an error.
    """
    spare = (spare_pn or "").strip()
    if not spare:
        return None

    url = f"{_BASE_URL}?SearchText={quote(spare)}"
    try:
        resp = await http_redirect.get(url, headers={"User-Agent": _UA}, timeout=timeout)
    except (httpx.HTTPError, httpx.InvalidURL) as exc:
        logger.warning("partsurfer: fetch failed for {} ({}): {}", spare, type(exc).__name__, exc)
        return None

    status = getattr(resp, "status_code", None)
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
