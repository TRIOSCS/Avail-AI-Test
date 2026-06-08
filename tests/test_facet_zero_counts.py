"""Unit 2 — a fixed-vocab value with no data still renders, with a (0) count badge.

This is the "SATA (500), SCSI (0)" behaviour: the sidebar shows every canonical value
and its live count, including zero, designed for a catalog of thousands.
"""

from sqlalchemy.orm import Session

from app.models import CommoditySpecSchema, MaterialCard, MaterialSpecFacet


def test_unstocked_canonical_value_renders_with_zero_count(client, db_session: Session):
    db_session.add(
        CommoditySpecSchema(
            commodity="hdd",
            spec_key="interface",
            display_name="Interface",
            data_type="enum",
            enum_values=["SATA", "SAS", "SCSI"],
            sort_order=1,
            is_filterable=True,
            is_primary=True,
        )
    )
    card = MaterialCard(normalized_mpn="hdd-1", display_mpn="HDD-1", category="hdd")
    db_session.add(card)
    db_session.flush()
    db_session.add(MaterialSpecFacet(material_card_id=card.id, category="hdd", spec_key="interface", value_text="SATA"))
    db_session.commit()

    resp = client.get("/v2/partials/materials/filters/sub?commodity=hdd&sub_filters=%7B%7D")
    assert resp.status_code == 200
    # SCSI has zero rows but must still render as a selectable option...
    assert "SCSI" in resp.text
    # ...with a (0) count badge, and SATA with its real count (1).
    assert 'tabular-nums">0<' in resp.text
    assert 'tabular-nums">1<' in resp.text
    # The checkbox for SCSI is wired to toggleFilter, i.e. it is selectable.
    assert "toggleFilter" in resp.text
