"""Tests for POST /api/materials/import-part-numbers endpoint."""

import io


def test_poison_mpn_does_not_sink_chunk(monkeypatch, db_session, tmp_path):
    # one MPN raises inside enrich_card; the rest of the chunk must still be reported
    import scripts.import_part_numbers as imp

    async def fake_enrich(card, db, **kw):
        if card.display_mpn == "BOOM":
            raise RuntimeError("poison")
        card.enrichment_status = "not_found"
        return "not_found"

    monkeypatch.setattr(imp, "enrich_card", fake_enrich)
    monkeypatch.setattr(imp, "_connectors_in_order", lambda db: [])
    monkeypatch.setattr(imp, "SessionLocal", lambda: db_session)
    f = tmp_path / "s.csv"
    f.write_text("mpn\nOK1\nBOOM\nOK2\n")
    rep = tmp_path / "out.csv"
    import asyncio

    asyncio.run(imp._run(str(f), commit=False, report_path=str(rep), refresh=False, concurrency=4))
    rows = rep.read_text()
    assert "OK1" in rows and "OK2" in rows and "error" in rows  # poison -> status=error, chunk survives


def test_import_part_numbers_creates_bare_cards(client, db_session):
    from app.models import MaterialCard

    html = (
        b"<table><tr><td>Material: Material Name</td></tr>"
        b"<tr><td>NEWPART-001</td></tr><tr><td>NEWPART-002</td></tr></table>"
    )
    resp = client.post(
        "/api/materials/import-part-numbers",
        files={"file": ("report.xls", io.BytesIO(html), "application/vnd.ms-excel")},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["created"] == 2
    cards = db_session.query(MaterialCard).filter(MaterialCard.normalized_mpn.in_(["newpart001", "newpart002"])).all()
    assert len(cards) == 2
    assert all(c.enrichment_status == "unenriched" for c in cards)
