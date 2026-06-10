"""tests/test_on_add_enrichment.py — on-add auto-enrichment create flows.

Covers: POST /api/materials/add (manual/100 writes, blank=blank, inline deterministic
passes, priority-lane stamp, V3 422 validation), bulk part-number / stock imports
(inline passes, per-row warnings, NO stamp), the enrich-status badge route (HTTP 286
stop), the add-form modal route, and the needs-review faceted filter.
Depends on: conftest.py (db_session, client), commodity_registry seeds.
"""

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models import MaterialCard
from app.services.commodity_registry import seed_commodity_schemas

DRAM_MPN = "M393A2K43DB3-CWE"  # deterministically decodable Samsung DDR4 RDIMM


def _get_card(db: Session, mpn: str) -> MaterialCard:
    from app.utils.normalization import normalize_mpn_key

    card = db.query(MaterialCard).filter_by(normalized_mpn=normalize_mpn_key(mpn)).first()
    assert card is not None, f"no card created for {mpn}"
    return card


# --- Single-add ---------------------------------------------------------------


def test_add_part_creates_card_runs_passes_and_stamps(client, db_session: Session):
    seed_commodity_schemas(db_session)
    resp = client.post(
        "/api/materials/add",
        data={
            "mpn": DRAM_MPN,
            "manufacturer": "Samsung",
            "description": "16GB DDR4-3200 RDIMM",
            "category": "dram",
            "condition": "New",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True and body["created"] is True
    # The modal redirects to the card detail.
    assert resp.headers["HX-Redirect"] == f"/v2/materials/{body['card_id']}"

    card = _get_card(db_session, DRAM_MPN)
    assert card.manufacturer == "Samsung"
    assert card.description == "16GB DDR4-3200 RDIMM"
    assert card.condition == "New"
    # Category entered the F1 ladder at manual/100.
    assert card.category == "dram"
    assert card.category_source == "manual"
    assert card.category_tier == 100
    # manual/100 provenance stamped for every supplied field.
    for field in ("manufacturer", "description", "condition", "category"):
        entry = card.enrichment_provenance[field]
        assert entry["source"] == "manual"
        assert entry["tier"] == 100
        assert entry["confidence"] == 1.0
    # Inline deterministic decode ran in the create request.
    assert card.specs_structured["ddr_type"]["value"] == "DDR4"
    assert card.specs_structured["ddr_type"]["source"] == "mpn_decode"
    # Priority lane stamped — single-add only.
    assert card.enrich_requested_at is not None


def test_add_part_blanks_stay_blank(client, db_session: Session):
    """Omitted fields stay NULL for enrichment to fill — never defaulted or guessed."""
    seed_commodity_schemas(db_session)
    resp = client.post("/api/materials/add", data={"mpn": "ZZTESTPART-77"})
    assert resp.status_code == 200

    card = _get_card(db_session, "ZZTESTPART-77")
    assert not card.manufacturer  # resolve_material_card default ("")
    assert card.description is None
    assert card.condition is None
    assert card.category is None  # not decodable — left for the worker
    assert not (card.enrichment_provenance or {})
    assert card.enrich_requested_at is not None


def test_add_part_existing_card_resolves_not_duplicates(client, db_session: Session):
    existing = MaterialCard(
        normalized_mpn="zztestpart88",  # canonical dedup key (normalize_mpn_key strips dashes)
        display_mpn="ZZTESTPART-88",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(existing)
    db_session.commit()

    resp = client.post("/api/materials/add", data={"mpn": "ZZTESTPART-88", "manufacturer": "NewCo"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["created"] is False
    assert body["card_id"] == existing.id
    db_session.refresh(existing)
    assert existing.manufacturer == "NewCo"
    assert existing.enrich_requested_at is not None


def test_add_part_rejects_short_mpn_422(client, db_session: Session):
    resp = client.post("/api/materials/add", data={"mpn": "AB"})
    assert resp.status_code == 422
    assert "valid MPN" in resp.text  # per-field message rendered in the modal
    assert 'name="mpn"' in resp.text  # the form re-renders


def test_add_part_rejects_offvocab_category_422(client, db_session: Session):
    resp = client.post("/api/materials/add", data={"mpn": "ZZTESTPART-99", "category": "FLUX_CAPACITOR"})
    assert resp.status_code == 422
    assert "not a recognized commodity" in resp.text
    # Card NOT created on a blocking validation failure.
    assert db_session.query(MaterialCard).filter_by(normalized_mpn="zztestpart-99").first() is None


def test_add_part_rejects_bad_condition_422(client, db_session: Session):
    resp = client.post("/api/materials/add", data={"mpn": "ZZTESTPART-99", "condition": "Mint"})
    assert resp.status_code == 422
    assert "not a valid condition" in resp.text


def test_add_form_partial_renders(client, db_session: Session):
    resp = client.get("/v2/partials/materials/add-form")
    assert resp.status_code == 200
    for field in ("mpn", "manufacturer", "description", "category", "condition"):
        assert f'name="{field}"' in resp.text


# --- Bulk imports: same pipeline, NO stamp, per-row warnings --------------------


def test_bulk_import_runs_passes_warns_and_does_not_stamp(client, db_session: Session):
    seed_commodity_schemas(db_session)
    csv = f"mpn\n{DRAM_MPN}\nAB\n".encode()
    resp = client.post(
        "/api/materials/import-part-numbers",
        files={"file": ("parts.csv", csv, "text/csv")},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["created"] == 1
    assert body["skipped"] == 1
    # V3 rejection surfaced per-row, never silent.
    (warning,) = body["warnings"]
    assert warning["row"] == 2
    assert warning["field"] == "mpn"
    assert "AB" in warning["reason"]

    card = _get_card(db_session, DRAM_MPN)
    # Inline deterministic passes ran server-side on the bulk path too.
    assert card.specs_structured["ddr_type"]["value"] == "DDR4"
    # Bulk imports must NOT monopolize the priority lane.
    assert card.enrich_requested_at is None


def test_stock_import_runs_passes_warns_and_does_not_stamp(client, db_session: Session):
    seed_commodity_schemas(db_session)
    csv = f"part number,qty,price\n{DRAM_MPN},10,42.50\n".encode()
    resp = client.post(
        "/api/materials/import-stock",
        data={"vendor_name": "Test Vendor Inc"},
        files={"file": ("stock.csv", csv, "text/csv")},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["imported_rows"] == 1
    assert body["warnings"] == []

    card = _get_card(db_session, DRAM_MPN)
    assert card.specs_structured["ddr_type"]["value"] == "DDR4"
    assert card.enrich_requested_at is None


# --- Enrich-status badge (polling + 286 stop) -----------------------------------


def test_enrich_status_unenriched_polls(client, db_session: Session):
    card = MaterialCard(
        normalized_mpn="poll-001",
        display_mpn="POLL-001",
        enrichment_status="unenriched",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(card)
    db_session.commit()

    resp = client.get(f"/v2/partials/materials/{card.id}/enrich-status")
    assert resp.status_code == 200
    assert "Queued for enrichment" in resp.text
    assert "every 15s" in resp.text  # badge keeps polling itself


def test_enrich_status_terminal_returns_286(client, db_session: Session):
    card = MaterialCard(
        normalized_mpn="poll-002",
        display_mpn="POLL-002",
        enrichment_status="verified",
        enriched_at=datetime.now(timezone.utc),
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(card)
    db_session.commit()

    resp = client.get(f"/v2/partials/materials/{card.id}/enrich-status")
    assert resp.status_code == 286  # htmx stop-polling status
    assert "VERIFIED" in resp.text
    assert "every 15s" not in resp.text


def test_enrich_status_404_on_missing_card(client, db_session: Session):
    resp = client.get("/v2/partials/materials/999999/enrich-status")
    assert resp.status_code == 404


# --- Needs-review faceted filter -------------------------------------------------


def test_needs_review_filter_narrows(client, db_session: Session):
    flagged = MaterialCard(
        normalized_mpn="review-001",
        display_mpn="REVIEW-001",
        has_validation_conflict=True,
        validation_conflicts=[
            {
                "key": "category",
                "manual": {"value": "dram", "updated_at": ""},
                "evidence": {"source": "mpn_decode", "tier": 85, "confidence": 0.95, "value": "hdd", "observed_at": ""},
            }
        ],
        created_at=datetime.now(timezone.utc),
    )
    clean = MaterialCard(
        normalized_mpn="clean-001",
        display_mpn="CLEAN-001",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add_all([flagged, clean])
    db_session.commit()

    resp = client.get("/v2/partials/materials/faceted", params={"has_validation_conflict": "true"})
    assert resp.status_code == 200
    assert "REVIEW-001" in resp.text
    assert "CLEAN-001" not in resp.text

    # Default (no param) keeps everything visible.
    resp = client.get("/v2/partials/materials/faceted")
    assert "REVIEW-001" in resp.text
    assert "CLEAN-001" in resp.text


def test_detail_renders_conflict_badge_and_accept(client, db_session: Session):
    card = MaterialCard(
        normalized_mpn="review-002",
        display_mpn="REVIEW-002",
        has_validation_conflict=True,
        validation_conflicts=[
            {
                "key": "category",
                "manual": {"value": "dram", "updated_at": ""},
                "evidence": {"source": "mpn_decode", "tier": 85, "confidence": 0.95, "value": "hdd", "observed_at": ""},
            }
        ],
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(card)
    db_session.commit()

    resp = client.get(f"/v2/partials/materials/{card.id}")
    assert resp.status_code == 200
    assert "Needs review" in resp.text
    assert "1 conflict" in resp.text
    assert f"/v2/partials/materials/{card.id}/conflicts/category/accept" in resp.text
    assert "Use this value" in resp.text
