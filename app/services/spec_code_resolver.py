"""SpecCodeResolver — translate OEM spec codes (e.g. IBM SPREJ) to approved MPNs.

Called by: app/search_service.py:search_requirement() when the synchronous
    connector fanout returns zero sightings (wired in a later PR).

Depends on: app/models/sourcing.py (OemSpecCode, OemSpecCodePending,
    OemSpecCodeBlacklist), app/utils/claude_client.claude_json, and
    app/schemas/spec_codes (ResolverLlmResponse).

The resolver is read-mostly: it short-circuits at any cached layer before
issuing an LLM call. When it does call the LLM, it writes a row to
``oem_spec_codes_pending``; that row is consumed by the admin UI for human
approval before being promoted to ``oem_spec_codes``.

Resolution order (from spec §5):
    1. ``OemSpecCode`` hit                  -> ``approved`` / ``source="table"``
    2. ``OemSpecCodePending`` hit           -> ``pending``  / ``source="llm"``
    3. Load ``OemSpecCodeBlacklist`` rejected_mpns as LLM exclusion set
    4. Call Claude (web_search grounded), validate against ``ResolverLlmResponse``
    5. Apply confidence floor + no-citations penalty
    6. Persist ``OemSpecCodePending`` row (idempotent under UNIQUE collisions)
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable

from loguru import logger
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.models.sourcing import (
    OemSpecCode,
    OemSpecCodeBlacklist,
    OemSpecCodePending,
)
from app.schemas.spec_codes import ResolverLlmResponse, ResolverSource, ResolverStatus


@dataclass
class ResolverResult:
    """Outcome of a single ``resolve()`` call.

    Attributes:
        status: ``approved`` (table hit), ``pending`` (LLM proposal in queue),
            or ``unresolved`` (no usable mapping).
        avl: Approved Vendor List entries; empty when unresolved.
        confidence: 1.0 for table hits, LLM-rated for pending, 0.0 unresolved.
        citations: Grounding citations from the LLM web-search call; empty for
            table hits.
        source: ``table``, ``llm``, or ``none``.
    """

    status: ResolverStatus
    avl: list[dict] = field(default_factory=list)
    confidence: float = 0.0
    citations: list[dict] = field(default_factory=list)
    source: ResolverSource = "none"


_SYSTEM_PROMPT = """You are a parts-engineering expert with deep knowledge of IBM,
Cisco, HP, and Dell internal spec codes for electronic components. Given an OEM
spec code, return the Approved Vendor List (AVL) — the set of manufacturer part
numbers approved by that OEM for parts matching the spec.

Return STRICT JSON matching this schema:
{
  "avl": [{"mpn": "<MPN>", "manufacturer": "<Name>", "rank": <int>, "notes": "<str|null>"}],
  "confidence": <float 0..1>,
  "citations": [{"url": "<url>", "snippet": "<short verbatim quote>"}],
  "reasoning": "<one-paragraph explanation>"
}

Rules:
- If you are not reasonably confident, return {"avl": [], "confidence": 0.0, ...}.
- Lower `rank` = higher preference (1 is primary AVL).
- Use web_search to ground your answer in IBM redbooks, datasheets, or broker catalogs.
- NEVER propose an MPN from the user-provided blacklist.
- Do NOT include any field other than the four above. Extra fields cause rejection.
"""


def _build_user_prompt(spec_code: str, oem: str, blacklist_mpns: list[str]) -> str:
    """Assemble the per-request user prompt for the LLM."""
    return (
        f"OEM: {oem}\n"
        f"Spec code: {spec_code}\n"
        f"Blacklisted MPNs (do NOT propose): {json.dumps(blacklist_mpns)}\n"
        f"Return the AVL as strict JSON per the system prompt."
    )


async def _default_claude_call(
    *,
    system: str,
    user: str,
    tools: list[dict],
    max_tokens: int,
) -> dict | list | None:
    """Default LLM adapter — bridges the resolver's keyword call shape to
    ``claude_json``'s positional ``prompt`` parameter.

    Kept module-level (rather than nested in ``__init__``) so it stays
    importable for tests that want to assert on the wrapper itself, and so the
    constructor remains free of imports at instance-construction time.
    """
    from app.utils.claude_client import claude_json

    return await claude_json(
        user,
        system=system,
        model_tier="smart",
        max_tokens=max_tokens,
        tools=tools,
        timeout=60,
    )


class SpecCodeResolver:
    """Resolution pipeline: table → pending → blacklist → LLM → pending row.

    The class is a thin coordinator; all state lives in the DB. Construct one
    per request — it holds a reference to the caller's ``Session``.
    """

    def __init__(
        self,
        db: Session,
        claude_call: Callable[..., Any] | None = None,
    ) -> None:
        self._db = db
        # Dependency-injected for tests; defaults to the project's claude_json
        # via a thin adapter that normalizes keyword args.
        self._claude_call = claude_call if claude_call is not None else _default_claude_call

    async def resolve(self, spec_code: str, oem: str = "IBM") -> ResolverResult:
        """Resolve a single (oem, spec_code) pair to an AVL.

        Normalizes input (strip + uppercase), walks the table → pending → LLM ladder,
        and writes a pending row on a fresh LLM-derived hit.
        """
        norm_code = (spec_code or "").strip().upper()
        norm_oem = (oem or "IBM").strip().upper()
        if not norm_code:
            return ResolverResult(status="unresolved")

        # 1. Authoritative table
        approved = self._db.query(OemSpecCode).filter_by(oem=norm_oem, spec_code=norm_code).one_or_none()
        if approved is not None:
            return ResolverResult(
                status="approved",
                avl=list(approved.avl or []),
                confidence=1.0,
                source="table",
            )

        # 2. Pending — reuse prior LLM result
        pending = self._db.query(OemSpecCodePending).filter_by(oem=norm_oem, spec_code=norm_code).one_or_none()
        if pending is not None:
            return ResolverResult(
                status="pending",
                avl=list(pending.proposed_avl or []),
                confidence=pending.llm_confidence,
                citations=list(pending.citations or []),
                source="llm",
            )

        # 3. Blacklist — accumulated rejected MPNs feed into the LLM prompt
        blacklist_mpns = self._load_blacklist(norm_oem, norm_code)

        # 4. LLM call (validated against ResolverLlmResponse)
        llm_result = await self._call_llm(norm_code, norm_oem, blacklist_mpns)
        if llm_result is None:
            return ResolverResult(status="unresolved")

        # 5. Confidence floor — apply no-citations penalty first, then compare
        adjusted_confidence = llm_result.confidence
        if not llm_result.citations:
            adjusted_confidence *= 0.7
        if adjusted_confidence < settings.spec_resolver_min_confidence or not llm_result.avl:
            logger.info(
                "spec_resolver: below floor or empty avl; oem={} code={} conf={}",
                norm_oem,
                norm_code,
                adjusted_confidence,
            )
            return ResolverResult(status="unresolved")

        # 6. Persist pending row (idempotent under concurrency)
        avl_payload = [entry.model_dump() for entry in llm_result.avl]
        row = OemSpecCodePending(
            oem=norm_oem,
            spec_code=norm_code,
            proposed_avl=avl_payload,
            llm_confidence=adjusted_confidence,
            citations=list(llm_result.citations),
        )
        self._db.add(row)
        try:
            self._db.commit()
        except IntegrityError:
            # Concurrent resolver wrote first; re-read the winning row.
            self._db.rollback()
            winner = self._db.query(OemSpecCodePending).filter_by(oem=norm_oem, spec_code=norm_code).one()
            logger.info(
                "spec_resolver: lost insert race; reusing winner row id={}",
                winner.id,
            )
            return ResolverResult(
                status="pending",
                avl=list(winner.proposed_avl or []),
                confidence=winner.llm_confidence,
                citations=list(winner.citations or []),
                source="llm",
            )

        return ResolverResult(
            status="pending",
            avl=avl_payload,
            confidence=adjusted_confidence,
            citations=list(llm_result.citations),
            source="llm",
        )

    def _load_blacklist(self, oem: str, spec_code: str) -> list[str]:
        """Flatten every rejected MPN for this (oem, spec_code) pair."""
        rows = self._db.query(OemSpecCodeBlacklist).filter_by(oem=oem, spec_code=spec_code).all()
        flat: list[str] = []
        for r in rows:
            flat.extend(r.rejected_mpns or [])
        return flat

    async def _call_llm(
        self,
        spec_code: str,
        oem: str,
        blacklist_mpns: list[str],
    ) -> ResolverLlmResponse | None:
        """Call the LLM, validate the response against ``ResolverLlmResponse``.

        Returns ``None`` on any failure (network error, ``None`` response,
        schema violation) — the caller treats that as ``unresolved``.
        """
        try:
            raw = await self._claude_call(
                system=_SYSTEM_PROMPT,
                user=_build_user_prompt(spec_code, oem, blacklist_mpns),
                tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 6}],
                max_tokens=2000,
            )
        except Exception:
            logger.exception(
                "spec_resolver: LLM call failed; oem={} code={}",
                oem,
                spec_code,
            )
            return None

        if raw is None:
            return None

        try:
            return ResolverLlmResponse.model_validate(raw)
        except ValidationError:
            logger.exception(
                "spec_resolver: LLM response failed schema validation; oem={} code={} raw={}",
                oem,
                spec_code,
                raw,
            )
            return None
