"""Backfill: dry-run writes a coverage report and commits nothing; budget cap halts."""

import csv
from unittest.mock import AsyncMock, patch

import pytest

from app.constants import MaterialEnrichmentStatus
from app.models import MaterialCard


@pytest.mark.asyncio
async def test_dry_run_writes_csv_and_no_commit(db_session, tmp_path):
    import scripts.backfill_oem_enrichment as bf

    c = MaterialCard(
        display_mpn="01HW917", normalized_mpn="01hw917", enrichment_status=MaterialEnrichmentStatus.NOT_FOUND
    )
    db_session.add(c)
    db_session.commit()

    async def fake_enrich(card, db, **kw):
        card.enrichment_status = MaterialEnrichmentStatus.OEM_SOURCED
        return MaterialEnrichmentStatus.OEM_SOURCED

    out = tmp_path / "cov.csv"
    with (
        patch.object(bf, "enrich_card", new=AsyncMock(side_effect=fake_enrich)),
        patch.object(bf, "SessionLocal", return_value=db_session),
    ):
        counts = await bf.run(commit=False, limit=None, max_web_calls=100, csv_path=str(out))

    db_session.expire_all()
    assert db_session.get(MaterialCard, c.id).enrichment_status == MaterialEnrichmentStatus.NOT_FOUND  # rolled back
    assert counts["oem_sourced"] == 1
    rows = list(csv.DictReader(out.open()))
    assert rows[0]["projected_status"] == "oem_sourced"


@pytest.mark.asyncio
async def test_budget_cap_halts(db_session, tmp_path):
    import scripts.backfill_oem_enrichment as bf

    for i in range(5):
        db_session.add(
            MaterialCard(
                display_mpn=f"01HW{i:03d}",
                normalized_mpn=f"01hw{i:03d}",
                enrichment_status=MaterialEnrichmentStatus.NOT_FOUND,
            )
        )
    db_session.commit()

    async def fake_enrich(card, db, *, web_meter=None, **kw):
        if web_meter is not None:
            web_meter["web_calls"] = web_meter.get("web_calls", 0) + 2
        card.enrichment_status = MaterialEnrichmentStatus.NOT_CATALOGUED
        return MaterialEnrichmentStatus.NOT_CATALOGUED

    with (
        patch.object(bf, "enrich_card", new=AsyncMock(side_effect=fake_enrich)),
        patch.object(bf, "SessionLocal", return_value=db_session),
    ):
        counts = await bf.run(commit=False, limit=None, max_web_calls=3, csv_path=str(tmp_path / "c.csv"))

    # 3-call budget, 2 calls per card → stops after the 2nd card (4 calls would exceed).
    assert counts["processed"] <= 2
