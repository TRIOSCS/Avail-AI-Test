"""Tests for POST /api/materials/import-part-numbers endpoint."""

import io


def test_bare_loader_creates_cards_without_enriching(monkeypatch, db_session, tmp_path):
    """The CLI loader upserts bare cards only — it never imports or calls enrich_card,
    so a large operator load cannot fire uncapped connector/web/AI calls (the worker is
    the single paced enrichment authority).

    Counts reflect what would be created.
    """
    import scripts.import_part_numbers as imp

    # Structural guard: the inline-enrichment plumbing must be gone.
    assert not hasattr(imp, "enrich_card")
    assert not hasattr(imp, "_connectors_in_order")

    monkeypatch.setattr(imp, "SessionLocal", lambda: db_session)
    f = tmp_path / "s.csv"
    f.write_text("mpn\nOK1\nOK2\n")

    result = imp._run(str(f), commit=False)  # dry-run: rolled back, counts still computed
    assert result["total"] == 2
    assert result["created"] == 2
    assert result["skipped"] == 0


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
