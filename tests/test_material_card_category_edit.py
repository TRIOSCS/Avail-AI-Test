"""Manual category edits on PUT /v2/partials/materials/{card_id} route through the F1
ladder.

Covers: app/routers/htmx_views.py::update_material_card — the category field must go
through spec_tiers.set_category (source="manual", tier 100), never raw setattr. Pins the
three invariants a raw write breaks: (1) a human edit gets manual provenance stamped so
later enrichment passes cannot silently revert it; (2) a commodity flip purges the old
commodity's MaterialSpecFacet rows + specs_structured mirrors; (3) off-vocab free text is
rejected with a user-visible showToast HX-Trigger instead of persisting raw. Also pins
that an UNCHANGED submitted category is not re-stamped as manual, and that blanking is
rejected (set_category never blanks an existing category).

Called by: pytest
Depends on: conftest client/db_session fixtures, app/services/spec_tiers.py,
            app/services/category_normalizer.py, MaterialCard, MaterialSpecFacet.
"""

import json
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models.faceted_search import MaterialSpecFacet
from app.models.intelligence import MaterialCard


def _card(db: Session, mpn: str, **kw) -> MaterialCard:
    defaults = dict(
        normalized_mpn=mpn.lower().replace("-", ""),
        display_mpn=mpn,
        manufacturer="Seagate",
        description="4TB 7.2K SAS HDD",
        created_at=datetime.now(timezone.utc),
    )
    defaults.update(kw)
    card = MaterialCard(**defaults)
    db.add(card)
    db.commit()
    db.refresh(card)
    return card


def _toast(resp) -> dict | None:
    raw = resp.headers.get("HX-Trigger")
    if not raw:
        return None
    return json.loads(raw).get("showToast")


def test_manual_category_change_stamps_manual_provenance(client, db_session: Session):
    """A deliberate category change lands canonical with manual/100/1.0 provenance — not
    a raw write that leaves the old provenance attached to the new value."""
    card = _card(
        db_session,
        "ST4000NM0035",
        category="hdd",
        category_source="digikey_api",
        category_tier=90,
        category_confidence=0.9,
        category_updated_at=datetime.now(timezone.utc),
    )

    resp = client.put(f"/v2/partials/materials/{card.id}", data={"category": "ssd"})
    assert resp.status_code == 200
    db_session.refresh(card)
    assert card.category == "ssd"
    assert card.category_source == "manual"
    assert card.category_tier == 100
    assert card.category_confidence == 1.0
    assert card.category_updated_at is not None
    assert _toast(resp) is None  # accepted edits don't toast


def test_manual_edit_survives_subsequent_enrichment(client, db_session: Session):
    """The silent-reversion regression: after a manual edit, a later vendor-tier (90)
    enrichment write must LOSE the ladder — the human's correction sticks."""
    from app.services.spec_tiers import set_category

    card = _card(db_session, "MZ7LH480HAHQ", category="hdd")
    resp = client.put(f"/v2/partials/materials/{card.id}", data={"category": "ssd"})
    assert resp.status_code == 200
    db_session.refresh(card)
    assert card.category == "ssd"

    # Simulate the next enrichment pass disagreeing at vendor tier with a newer timestamp.
    assert set_category(card, "hdd", "digikey_api", 0.99) is False
    assert card.category == "ssd"
    assert card.category_source == "manual"


def test_manual_commodity_flip_purges_stale_facets(client, db_session: Session):
    """A manual commodity flip purges the OLD commodity's facet rows + specs_structured
    mirrors — otherwise the card keeps answering the old commodity's deep filters."""
    card = _card(
        db_session,
        "WD4003FRYZ",
        category="hdd",
        specs_structured={"rpm": {"value": 7200, "source": "desc_parse", "confidence": 0.9, "tier": 83}},
    )
    db_session.add(
        MaterialSpecFacet(
            material_card_id=card.id,
            category="hdd",
            spec_key="rpm",
            value_numeric=7200,
            source="desc_parse",
            confidence=0.9,
            tier=83,
        )
    )
    db_session.commit()

    resp = client.put(f"/v2/partials/materials/{card.id}", data={"category": "ssd"})
    assert resp.status_code == 200
    db_session.refresh(card)
    assert card.category == "ssd"
    assert db_session.query(MaterialSpecFacet).filter_by(material_card_id=card.id).count() == 0
    assert "rpm" not in (card.specs_structured or {})


def test_off_vocab_category_rejected_with_toast(client, db_session: Session):
    """Off-vocab free text never persists (it would be invisible to every commodity
    filter) and the rejection is surfaced via the showToast HX-Trigger."""
    card = _card(db_session, "HUS726T4TALA", category="hdd")

    resp = client.put(
        f"/v2/partials/materials/{card.id}",
        data={"category": "Spinning Rust", "manufacturer": "HGST"},
    )
    assert resp.status_code == 200
    db_session.refresh(card)
    assert card.category == "hdd"  # kept, not blanked, not overwritten with junk
    assert card.manufacturer == "HGST"  # other fields still update
    toast = _toast(resp)
    assert toast is not None
    assert "Spinning Rust" in toast["message"]
    assert toast["type"] == "warning"


def test_alias_category_canonicalized(client, db_session: Session):
    """Known aliases normalize to the canonical commodity key on the way in."""
    card = _card(db_session, "ST8000NM000A", category=None)

    resp = client.put(f"/v2/partials/materials/{card.id}", data={"category": "Hard Drive"})
    assert resp.status_code == 200
    db_session.refresh(card)
    assert card.category == "hdd"
    assert card.category_source == "manual"


def test_blank_category_keeps_existing_with_toast(client, db_session: Session):
    """The ladder never blanks an existing category (set_category contract) — the
    attempt is rejected with a user-visible message, not silently honored."""
    card = _card(db_session, "ST2000NM0008", category="hdd")

    resp = client.put(f"/v2/partials/materials/{card.id}", data={"category": ""})
    assert resp.status_code == 200
    db_session.refresh(card)
    assert card.category == "hdd"
    toast = _toast(resp)
    assert toast is not None
    assert "hdd" in toast["message"]


def test_unchanged_category_not_restamped_as_manual(client, db_session: Session):
    """The edit form always re-submits the pre-filled category: saving another field
    with the category untouched must NOT silently convert its provenance to manual
    (tier 100) — that would lock out every future enrichment correction."""
    card = _card(
        db_session,
        "MG08ACA16TE",
        category="hdd",
        category_source="digikey_api",
        category_tier=90,
        category_confidence=0.9,
        category_updated_at=datetime.now(timezone.utc),
    )

    resp = client.put(
        f"/v2/partials/materials/{card.id}",
        data={"category": "hdd", "description": "16TB 7.2K SATA HDD"},
    )
    assert resp.status_code == 200
    db_session.refresh(card)
    assert card.description == "16TB 7.2K SATA HDD"
    assert card.category == "hdd"
    assert card.category_source == "digikey_api"  # untouched — no manual re-stamp
    assert card.category_tier == 90
    assert _toast(resp) is None
