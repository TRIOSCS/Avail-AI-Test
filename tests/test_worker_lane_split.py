"""tests/test_worker_lane_split.py — enrich_card lane split (call routing only).

Covers the bulk-vs-priority lane routing inside
``app.services.authoritative_enrichment_service.enrich_card`` (plan items 1.3):

- bulk lane (full_pipeline=False): connectors run, but the web tier
  (extract_part_from_web), the OEM tiers (cross_reference_mpn /
  extract_oem_description) and the Opus infer_part fallback are ALL skipped; a
  connector miss terminates not_found.
- priority lane (full_pipeline=True): the full pipeline runs (web + OEM + Opus).
- OEM/FRU-shaped MPNs skip extract_part_from_web on EVERY lane (even priority)
  when settings.enrichment_skip_web_for_oem_mpns, while the OEM tiers + Opus
  fallback still run on the priority lane.
- the split is CALL ROUTING ONLY: no write pre-gate is introduced — every write
  still arbitrates through the F1 ladder (a connector hit on the bulk lane still
  produces a verified write).

Depends on: conftest.py (db_session), authoritative_enrichment_service.enrich_card.
"""

from unittest.mock import AsyncMock, patch

import pytest

from app.constants import MaterialEnrichmentStatus
from app.models import MaterialCard
from app.services.authoritative_enrichment_service import enrich_card
from app.services.enrichment_worker.web_extractor import WebExtractResult

# A plain, non-OEM-shaped MPN (classify_oem_vendor returns None) so the OEM gate is
# out of the picture except in the tests that deliberately use an OEM shape.
_PLAIN_MPN = "STM32F407VGT6"
# An HP/HPE 6-3 spare — classify_oem_vendor -> "hpe" (HIGH_PRECISION_VENDORS).
_OEM_MPN = "918042-601"


def _mk(db, mpn=_PLAIN_MPN):
    card = MaterialCard(
        normalized_mpn=mpn.lower().replace("-", ""),
        display_mpn=mpn,
        enrichment_status=MaterialEnrichmentStatus.UNENRICHED,
    )
    db.add(card)
    db.flush()
    return card


def _patches(*, web="not_found", connectors_hit=False, oem_tiers_miss=False):
    """Patch every external tier enrich_card may call; return the cm dict.

    By default connectors miss (fetch_authoritative -> []) and the web/OEM/Opus tiers
    return their non-hit shapes, so the card terminates not_found unless a tier is
    asserted to have fired.

    With ``oem_tiers_miss=True`` the OEM tiers (cross_reference_mpn / extract_oem_description)
    and the Opus fallback (infer_part) are pre-wired to return their "no match" shapes, so an
    OEM-shaped card runs them all and terminates not_catalogued.
    """
    merged = (
        ({"description": "Found"}, {"description": {"source": "digikey_api"}}, ["digikey"])
        if connectors_hit
        else ({}, {}, [])
    )
    web_res = WebExtractResult(status=web)
    xref_kwargs = (
        {"return_value": type("Xr", (), {"status": "no_match", "resolved_mpn": None})()} if oem_tiers_miss else {}
    )
    oem_kwargs = {"return_value": type("Oem", (), {"status": "not_found"})()} if oem_tiers_miss else {}
    infer_kwargs = {"return_value": type("Inf", (), {"status": "not_found"})()} if oem_tiers_miss else {}
    return {
        "fetch": patch(
            "app.services.authoritative_enrichment_service.fetch_authoritative",
            new_callable=AsyncMock,
            return_value=[],
        ),
        "merge": patch(
            "app.services.authoritative_enrichment_service.merge_authoritative",
            return_value=merged,
        ),
        "web": patch(
            "app.services.authoritative_enrichment_service.extract_part_from_web",
            new_callable=AsyncMock,
            return_value=web_res,
        ),
        "xref": patch(
            "app.services.authoritative_enrichment_service.cross_reference_mpn",
            new_callable=AsyncMock,
            **xref_kwargs,
        ),
        "oem": patch(
            "app.services.authoritative_enrichment_service.extract_oem_description",
            new_callable=AsyncMock,
            **oem_kwargs,
        ),
        "infer": patch(
            "app.services.ai_inference_fallback.infer_part",
            new_callable=AsyncMock,
            **infer_kwargs,
        ),
    }


@pytest.mark.asyncio
async def test_bulk_lane_skips_web_oem_and_opus(db_session):
    """Bulk lane (full_pipeline=False): connectors run, every paid tier is skipped, the
    card terminates not_found without a single web/OEM/Opus call."""
    card = _mk(db_session, _OEM_MPN)  # OEM-shaped: proves the OEM tiers are skipped too
    p = _patches()
    with p["fetch"] as mfetch, p["merge"], p["web"] as mweb, p["xref"] as mxref, p["oem"] as moem, p["infer"] as minf:
        status = await enrich_card(card, db_session, connectors=[], full_pipeline=False)

    assert status == MaterialEnrichmentStatus.NOT_FOUND
    mfetch.assert_awaited_once()  # connectors DID run (free tier)
    mweb.assert_not_called()
    mxref.assert_not_called()
    moem.assert_not_called()
    minf.assert_not_called()


@pytest.mark.asyncio
async def test_bulk_lane_connector_hit_still_writes_through_ladder(db_session):
    """The lane split is CALL ROUTING ONLY — a connector hit on the bulk lane still
    produces a verified write (no write pre-gate was added)."""
    card = _mk(db_session)
    p = _patches(connectors_hit=True)
    with p["fetch"], p["merge"], p["web"] as mweb, p["xref"], p["oem"], p["infer"] as minf:
        status = await enrich_card(card, db_session, connectors=[], full_pipeline=False)

    assert status == MaterialEnrichmentStatus.VERIFIED
    mweb.assert_not_called()
    minf.assert_not_called()


@pytest.mark.asyncio
async def test_priority_lane_runs_full_pipeline_for_plain_mpn(db_session):
    """Priority lane (full_pipeline=True) on a plain MPN: web tier runs; with no OEM
    shape the OEM tiers don't apply, and the Opus fallback fires last."""
    card = _mk(db_session, _PLAIN_MPN)
    p = _patches()
    p["infer"] = patch(
        "app.services.ai_inference_fallback.infer_part",
        new_callable=AsyncMock,
        return_value=type("Inf", (), {"status": "not_found"})(),
    )
    with p["fetch"], p["merge"], p["web"] as mweb, p["xref"] as mxref, p["oem"] as moem, p["infer"] as minf:
        status = await enrich_card(card, db_session, connectors=[], full_pipeline=True, web_meter=None)

    assert status == MaterialEnrichmentStatus.NOT_FOUND  # plain MPN, no OEM, all miss
    mweb.assert_awaited_once()  # web tier ran on the priority lane
    mxref.assert_not_called()  # not OEM-shaped -> OEM tiers don't apply
    moem.assert_not_called()
    minf.assert_awaited_once()  # Opus fallback ran


@pytest.mark.asyncio
async def test_priority_lane_runs_oem_tiers_for_oem_mpn_but_skips_web(db_session):
    """Priority lane on an OEM/FRU-shaped MPN: the web tier is skipped (the ~95% no-
    trusted-source reject class), but the OEM tiers AND the Opus fallback still run.

    Terminates not_catalogued (HIGH_PRECISION vendor + oem_attempted).
    """
    card = _mk(db_session, _OEM_MPN)
    p = _patches(oem_tiers_miss=True)
    with p["fetch"], p["merge"], p["web"] as mweb, p["xref"] as mxref, p["oem"] as moem, p["infer"] as minf:
        status = await enrich_card(card, db_session, connectors=[], full_pipeline=True)

    mweb.assert_not_called()  # OEM-shaped -> web tier skipped on EVERY lane
    mxref.assert_awaited_once()  # OEM tiers still run on the priority lane
    moem.assert_awaited_once()
    minf.assert_awaited_once()  # Opus fallback still runs on the priority lane
    assert status == MaterialEnrichmentStatus.NOT_CATALOGUED


@pytest.mark.asyncio
async def test_oem_web_skip_can_be_disabled_by_flag(db_session):
    """With enrichment_skip_web_for_oem_mpns off, the priority lane runs the web tier
    even for an OEM-shaped MPN — pins the flag as the only gate on that skip."""
    card = _mk(db_session, _OEM_MPN)
    p = _patches(oem_tiers_miss=True)
    with (
        patch("app.config.settings.enrichment_skip_web_for_oem_mpns", False),
        p["fetch"],
        p["merge"],
        p["web"] as mweb,
        p["xref"],
        p["oem"],
        p["infer"],
    ):
        await enrich_card(card, db_session, connectors=[], full_pipeline=True)

    mweb.assert_awaited_once()  # flag off -> web tier runs for the OEM MPN too
