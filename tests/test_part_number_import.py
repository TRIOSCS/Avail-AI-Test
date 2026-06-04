"""Tests for POST /api/materials/import-part-numbers endpoint."""

import io


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
