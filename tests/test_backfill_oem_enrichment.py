"""Backfill: dry-run writes a coverage report and commits nothing; budget cap halts;
a bad card does not abort the run; consecutive Claude errors abort early."""

import csv
from unittest.mock import AsyncMock, patch

import pytest

from app.constants import MaterialEnrichmentStatus
from app.models import MaterialCard
from app.utils.claude_errors import ClaudeError


def _seed_not_found(db_session, n):
    """Add n NOT_FOUND MaterialCards (01HW000..) and commit."""
    for i in range(n):
        db_session.add(
            MaterialCard(
                display_mpn=f"01HW{i:03d}",
                normalized_mpn=f"01hw{i:03d}",
                enrichment_status=MaterialEnrichmentStatus.NOT_FOUND,
            )
        )
    db_session.commit()


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
    with patch.object(bf, "enrich_card", new=AsyncMock(side_effect=fake_enrich)):
        counts = await bf.run(commit=False, limit=None, max_web_calls=100, csv_path=str(out), db=db_session)

    db_session.expire_all()
    assert db_session.get(MaterialCard, c.id).enrichment_status == MaterialEnrichmentStatus.NOT_FOUND  # rolled back
    assert counts["oem_sourced"] == 1
    rows = list(csv.DictReader(out.open()))
    assert rows[0]["projected_status"] == "oem_sourced"


@pytest.mark.asyncio
async def test_budget_cap_halts(db_session, tmp_path):
    import scripts.backfill_oem_enrichment as bf

    _seed_not_found(db_session, 5)

    async def fake_enrich(card, db, *, web_meter=None, **kw):
        if web_meter is not None:
            web_meter.reserve_web_call()
            web_meter.reserve_web_call()
        card.enrichment_status = MaterialEnrichmentStatus.NOT_CATALOGUED
        return MaterialEnrichmentStatus.NOT_CATALOGUED

    with patch.object(bf, "enrich_card", new=AsyncMock(side_effect=fake_enrich)):
        counts = await bf.run(
            commit=False, limit=None, max_web_calls=3, csv_path=str(tmp_path / "c.csv"), db=db_session
        )

    # 3-call budget, 2 calls per card → the gate trips before the 3rd card (web_total=4 >= 3).
    assert counts["processed"] == 2
    assert counts["web_calls"] == 4


@pytest.mark.asyncio
async def test_select_includes_not_catalogued(db_session, tmp_path):
    import scripts.backfill_oem_enrichment as bf

    c = MaterialCard(
        display_mpn="01HW917", normalized_mpn="01hw917", enrichment_status=MaterialEnrichmentStatus.NOT_CATALOGUED
    )
    db_session.add(c)
    db_session.commit()

    seen: list[str] = []

    async def fake_enrich(card, db, **kw):
        seen.append(card.display_mpn)
        return MaterialEnrichmentStatus.OEM_SOURCED

    with patch.object(bf, "enrich_card", new=AsyncMock(side_effect=fake_enrich)):
        counts = await bf.run(
            commit=False, limit=None, max_web_calls=100, csv_path=str(tmp_path / "c.csv"), db=db_session
        )

    assert "01HW917" in seen  # a not_catalogued card is picked up for re-enrichment
    assert counts["processed"] == 1


@pytest.mark.asyncio
async def test_bad_card_does_not_abort_run(db_session, tmp_path):
    import scripts.backfill_oem_enrichment as bf

    _seed_not_found(db_session, 3)

    calls = {"n": 0}

    async def fake_enrich(card, db, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ValueError("boom")  # non-Claude error on the first card
        return MaterialEnrichmentStatus.OEM_SOURCED

    out = tmp_path / "c.csv"
    with patch.object(bf, "enrich_card", new=AsyncMock(side_effect=fake_enrich)):
        counts = await bf.run(commit=False, limit=None, max_web_calls=100, csv_path=str(out), db=db_session)

    assert counts["processed"] == 3  # run continued past the bad card
    assert counts.get("error") == 1
    rows = list(csv.DictReader(out.open()))
    assert rows[0]["projected_status"] == "error"


@pytest.mark.asyncio
async def test_consecutive_claude_errors_abort(db_session, tmp_path):
    import scripts.backfill_oem_enrichment as bf

    _seed_not_found(db_session, 10)

    async def fake_enrich(card, db, **kw):
        raise ClaudeError("backend down")

    with patch.object(bf, "enrich_card", new=AsyncMock(side_effect=fake_enrich)):
        counts = await bf.run(
            commit=False, limit=None, max_web_calls=100, csv_path=str(tmp_path / "c.csv"), db=db_session
        )

    # 5 consecutive ClaudeErrors abort the loop early (outage — stop burning budget).
    assert counts["processed"] == 5
    assert counts["claude_error"] == 5
