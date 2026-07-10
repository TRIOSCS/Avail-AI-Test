import os

os.environ["TESTING"] = "1"
from datetime import UTC, datetime

from app.models.intelligence import MaterialCard, MaterialCardDatasheet


def test_datasheet_row_links_to_card(db_session):
    card = MaterialCard(normalized_mpn="lm317t", display_mpn="LM317T")
    db_session.add(card)
    db_session.flush()
    ds = MaterialCardDatasheet(
        material_card_id=card.id,
        file_name="LM317T-datasheet.pdf",
        library_item_id="01ABC",
        library_web_url="https://onedrive/x",
        content_type="application/pdf",
        size_bytes=12345,
        source="connector",
        original_url="https://ti.com/lm317t.pdf",
        verified=True,
        captured_at=datetime.now(UTC),
    )
    db_session.add(ds)
    db_session.commit()
    db_session.refresh(card)
    assert card.datasheets[0].file_name == "LM317T-datasheet.pdf"
    assert card.datasheets[0].verified is True


def test_card_has_datasheet_stamp_columns(db_session):
    card = MaterialCard(normalized_mpn="ne555", display_mpn="NE555")
    card.datasheet_searched_at = datetime.now(UTC)
    db_session.add(card)
    db_session.commit()
    assert card.datasheet_searched_at is not None
    assert card.datasheet_captured_at is None


def test_datasheet_has_library_drive_id(db_session):
    from app.models.intelligence import MaterialCard, MaterialCardDatasheet

    card = MaterialCard(normalized_mpn="lm317x", display_mpn="LM317X")
    db_session.add(card)
    db_session.flush()
    ds = MaterialCardDatasheet(material_card_id=card.id, file_name="x.pdf", library_drive_id="DRV123")
    db_session.add(ds)
    db_session.commit()
    db_session.refresh(card)
    assert card.datasheets[0].library_drive_id == "DRV123"
