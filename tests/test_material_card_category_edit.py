"""Manual category AND manufacturer edits on PUT /v2/partials/materials/{card_id} route
through the F1 ladder.

Covers: app/routers/htmx_views.py::update_material_card — the category field must go
through spec_tiers.set_category (source="manual", tier 100), never raw setattr. Pins the
three invariants a raw write breaks: (1) a human edit gets manual provenance stamped so
later enrichment passes cannot silently revert it; (2) a commodity flip purges the old
commodity's MaterialSpecFacet rows + specs_structured mirrors; (3) off-vocab free text is
rejected with a user-visible showToast HX-Trigger instead of persisting raw. Also pins
that an UNCHANGED submitted category is not re-stamped as manual, and that blanking is
rejected (set_category never blanks an existing category). The manufacturer field
carries the same contract (set_manufacturer at manual/100, conflict-clearing on
re-assertion, no re-stamp on unchanged, never blanked) — same semantics as
routers/materials.py::update_material.

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


# ── Manufacturer (dual-brand provenanced column — same manual/100 contract) ──────────


def _manufacturer_conflict(value: str = "Western Digital") -> list[dict]:
    return [
        {
            "key": "manufacturer",
            "manual": {"value": "Seagate", "updated_at": "2026-01-01T00:00:00+00:00"},
            "evidence": {
                "source": "mpn_decode",
                "tier": 85,
                "confidence": 0.9,
                "value": value,
                "observed_at": "2026-01-02T00:00:00+00:00",
            },
        }
    ]


def test_manual_manufacturer_change_stamps_manual_and_clears_conflict(client, db_session: Session):
    """A deliberate maker change routes through set_manufacturer at manual/100 (never
    raw setattr — that would leave NULL provenance at the legacy floor and be reverted
    by the next decode), and a manual (re-)assertion resolves any recorded manufacturer
    conflict — same contract as routers/materials.py::update_material."""
    card = _card(
        db_session,
        "ST4000NM0036",
        manufacturer="Seagate",
        manufacturer_source="digikey_api",
        manufacturer_tier=90,
        manufacturer_confidence=0.9,
        manufacturer_updated_at=datetime.now(timezone.utc),
        validation_conflicts=_manufacturer_conflict(),
        has_validation_conflict=True,
    )

    resp = client.put(f"/v2/partials/materials/{card.id}", data={"manufacturer": "Western Digital"})
    assert resp.status_code == 200
    db_session.refresh(card)
    assert card.manufacturer == "Western Digital"
    assert card.manufacturer_source == "manual"
    assert card.manufacturer_tier == 100
    assert card.manufacturer_confidence == 1.0
    assert card.validation_conflicts in (None, [])
    assert card.has_validation_conflict is False


def test_manual_manufacturer_edit_survives_subsequent_enrichment(client, db_session: Session):
    """After a manual maker edit, a later vendor-tier (90) write must LOSE the ladder —
    the human's correction sticks."""
    from app.services.spec_tiers import set_manufacturer

    card = _card(db_session, "HUS728T8TALE", manufacturer="HGST")
    resp = client.put(f"/v2/partials/materials/{card.id}", data={"manufacturer": "Western Digital"})
    assert resp.status_code == 200
    db_session.refresh(card)
    assert card.manufacturer == "Western Digital"

    assert set_manufacturer(card, "HGST", "digikey_api", 0.99) is False
    assert card.manufacturer == "Western Digital"
    assert card.manufacturer_source == "manual"


def test_unchanged_manufacturer_not_restamped_but_clears_conflict(client, db_session: Session):
    """The edit form always re-submits the pre-filled maker: saving with it untouched
    must NOT convert its provenance to manual/100 (that would lock out future
    enrichment corrections) — but the re-assertion still resolves a recorded conflict
    (the human looked and confirmed the value)."""
    card = _card(
        db_session,
        "MG08ACA16TEY",
        manufacturer="Seagate",
        manufacturer_source="digikey_api",
        manufacturer_tier=90,
        manufacturer_confidence=0.9,
        manufacturer_updated_at=datetime.now(timezone.utc),
        validation_conflicts=_manufacturer_conflict(),
        has_validation_conflict=True,
    )

    resp = client.put(
        f"/v2/partials/materials/{card.id}",
        data={"manufacturer": "Seagate", "description": "16TB 7.2K SATA HDD"},
    )
    assert resp.status_code == 200
    db_session.refresh(card)
    assert card.manufacturer == "Seagate"
    assert card.manufacturer_source == "digikey_api"  # untouched — no manual re-stamp
    assert card.manufacturer_tier == 90
    assert card.has_validation_conflict is False  # re-assertion resolved the conflict


def test_blank_manufacturer_never_blanks_existing(client, db_session: Session):
    """The ladder never blanks a value (set_manufacturer contract) — the old raw write
    could silently blank the maker here.

    The rejection is surfaced via the showToast HX-Trigger (same feedback contract as
    the category blank path): a deliberate clear-attempt must be distinguishable from a
    successful save.
    """
    card = _card(
        db_session,
        "ST2000NM0018",
        manufacturer="Seagate",
        manufacturer_source="manual",
        manufacturer_tier=100,
        manufacturer_confidence=1.0,
        manufacturer_updated_at=datetime.now(timezone.utc),
    )

    resp = client.put(f"/v2/partials/materials/{card.id}", data={"manufacturer": ""})
    assert resp.status_code == 200
    db_session.refresh(card)
    assert card.manufacturer == "Seagate"
    assert card.manufacturer_source == "manual"
    toast = _toast(resp)
    assert toast is not None
    assert "Manufacturer can't be cleared" in toast["message"]
    assert "Seagate" in toast["message"]
    assert toast["type"] == "warning"


def test_unchanged_alias_manufacturer_not_restamped_with_seeded_alias_table(client, db_session: Session):
    """Legacy cards store NON-canonical aliases ("TI" — pre-ladder data), and the edit
    form round-trips the stored value verbatim.

    With a populated manufacturers table
    the no-re-stamp guard must compare canonical to CANONICAL: comparing
    canonical("TI") == "Texas Instruments" against the RAW stored "TI" would see a
    difference on every unrelated save and silently re-stamp the maker manual/100,
    locking out every future enrichment correction. (The suite's default empty
    manufacturers table makes normalize_brand_name a pass-through, so this test seeds
    a real alias row to exercise the production path.)
    """
    from app.models import Manufacturer

    db_session.add(Manufacturer(canonical_name="Texas Instruments", aliases=["TI"]))
    db_session.commit()
    card = _card(db_session, "LM317T-ALIAS", manufacturer="TI")  # NULL provenance — legacy

    resp = client.put(
        f"/v2/partials/materials/{card.id}",
        data={"manufacturer": "TI", "description": "Adjustable regulator"},
    )
    assert resp.status_code == 200
    db_session.refresh(card)
    assert card.description == "Adjustable regulator"
    assert card.manufacturer == "TI"  # not silently flipped to the canonical name
    assert card.manufacturer_source is None  # no manual/100 re-stamp
    assert card.manufacturer_tier is None


def test_canonical_resubmit_of_stored_alias_not_restamped(client, db_session: Session):
    """Canonical-to-canonical equality: re-submitting the CANONICAL form of a stored
    alias ("Texas Instruments" over stored "TI") is the same maker, not an edit — it
    must not be re-stamped manual/100 either."""
    from app.models import Manufacturer

    db_session.add(Manufacturer(canonical_name="Texas Instruments", aliases=["TI"]))
    db_session.commit()
    card = _card(db_session, "LM317T-CANON", manufacturer="TI")

    resp = client.put(
        f"/v2/partials/materials/{card.id}",
        data={"manufacturer": "Texas Instruments", "description": "Adj regulator"},
    )
    assert resp.status_code == 200
    db_session.refresh(card)
    assert card.manufacturer == "TI"
    assert card.manufacturer_source is None
    assert card.manufacturer_tier is None


def test_alias_manufacturer_change_still_lands_with_seeded_alias_table(client, db_session: Session):
    """A GENUINE maker change on an alias-stored card still routes through the ladder at
    manual/100 (the canonical-to-canonical guard must not eat real edits)."""
    from app.models import Manufacturer

    db_session.add(Manufacturer(canonical_name="Texas Instruments", aliases=["TI"]))
    db_session.commit()
    card = _card(db_session, "LM317T-EDIT", manufacturer="TI")

    resp = client.put(f"/v2/partials/materials/{card.id}", data={"manufacturer": "STMicroelectronics"})
    assert resp.status_code == 200
    db_session.refresh(card)
    assert card.manufacturer == "STMicroelectronics"
    assert card.manufacturer_source == "manual"
    assert card.manufacturer_tier == 100
