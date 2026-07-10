"""tests/test_on_add_enrichment.py — on-add auto-enrichment create flows.

Covers: POST /api/materials/add (manual/100 writes, blank=blank, inline deterministic
passes, priority-lane stamp, V3 422 validation), bulk part-number / stock imports
(inline passes, per-row warnings, NO stamp), the enrich-status badge route (HTTP 286
stop), the add-form modal route, and the has_validation_conflict faceted filter.
Depends on: conftest.py (db_session, client), commodity_registry seeds.
"""

from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.models import MaterialCard
from app.services.commodity_registry import seed_commodity_schemas

DRAM_MPN = "M393A2K43DB3-CWE"  # deterministically decodable Samsung DDR4 RDIMM


def _get_card(db: Session, mpn: str) -> MaterialCard:
    from app.utils.normalization import normalize_mpn_key

    card = db.query(MaterialCard).filter_by(normalized_mpn=normalize_mpn_key(mpn)).first()
    assert card is not None, f"no card created for {mpn}"
    return card


def _make_card(db: Session, normalized_mpn: str, display_mpn: str, **fields) -> MaterialCard:
    """Build, add, and flush a MaterialCard with created_at defaulted to now."""
    fields.setdefault("created_at", datetime.now(UTC))
    card = MaterialCard(normalized_mpn=normalized_mpn, display_mpn=display_mpn, **fields)
    db.add(card)
    db.flush()
    return card


def _category_conflict() -> dict:
    """The standard manual=dram vs mpn_decode=hdd category conflict record."""
    return {
        "key": "category",
        "manual": {"value": "dram", "updated_at": ""},
        "evidence": {"source": "mpn_decode", "tier": 85, "confidence": 0.95, "value": "hdd", "observed_at": ""},
    }


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
    # Manufacturer entered the F1 ladder too (set_manufacturer — durable column
    # provenance, not just the enrichment_provenance JSONB mirror).
    assert card.manufacturer_source == "manual"
    assert card.manufacturer_tier == 100
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
    # canonical dedup key (normalize_mpn_key strips dashes)
    existing = _make_card(db_session, "zztestpart88", "ZZTESTPART-88")
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
    from app.utils.normalization import normalize_mpn_key

    before = db_session.query(MaterialCard).count()
    resp = client.post("/api/materials/add", data={"mpn": "ZZTESTPART-99", "category": "FLUX_CAPACITOR"})
    assert resp.status_code == 422
    assert "not a recognized commodity" in resp.text
    # Card NOT created on a blocking validation failure — query by the REAL dedup key
    # (normalize_mpn_key strips dashes; "zztestpart-99" could never match anything).
    key = normalize_mpn_key("ZZTESTPART-99")
    assert key == "zztestpart99"
    assert db_session.query(MaterialCard).filter_by(normalized_mpn=key).first() is None
    assert db_session.query(MaterialCard).count() == before


def test_add_part_rejects_bad_condition_422(client, db_session: Session):
    resp = client.post("/api/materials/add", data={"mpn": "ZZTESTPART-99", "condition": "Mint"})
    assert resp.status_code == 422
    assert "not a valid condition" in resp.text


def test_add_part_punctuation_only_mpn_rerenders_modal_422(client, db_session: Session):
    """An MPN that passes normalize_mpn (display normalizer keeps dashes) but empties
    normalize_mpn_key must re-render the modal like every other 422 — never a raw JSON
    body (the beforeSwap allowlist would inject it into #modal-content as text)."""
    before = db_session.query(MaterialCard).count()
    resp = client.post("/api/materials/add", data={"mpn": "---"})
    assert resp.status_code == 422
    assert "Enter a valid MPN" in resp.text
    assert 'name="mpn"' in resp.text  # the form re-renders, not a JSON error body
    assert db_session.query(MaterialCard).count() == before


def test_add_part_manual_category_vs_decode_records_conflict(client, db_session: Session):
    """V2 end-to-end through the modal: manual category=hdd on a deterministically-DRAM
    MPN keeps the manual value, blocks the cross-commodity decoded specs, and records
    ONE conflict on key='category' with mpn_decode evidence."""
    seed_commodity_schemas(db_session)
    resp = client.post("/api/materials/add", data={"mpn": DRAM_MPN, "category": "hdd"})
    assert resp.status_code == 200

    card = _get_card(db_session, DRAM_MPN)
    assert card.category == "hdd"  # manual/100 kept — never overwritten by the system
    assert card.category_source == "manual"
    assert not (card.specs_structured or {})  # cross-commodity guard blocked DRAM specs
    assert card.has_validation_conflict
    (entry,) = [c for c in card.validation_conflicts if c["key"] == "category"]
    assert entry["manual"]["value"] == "hdd"
    assert entry["evidence"]["source"] == "mpn_decode"
    assert entry["evidence"]["value"] == "dram"


def test_readd_of_enriched_card_does_not_stamp(client, db_session: Session):
    """The priority lane stamps only cards select_batch can pick — an already-enriched
    re-add would hold a stamp nothing ever clears (run_one_batch is the sole
    clearer)."""
    existing = _make_card(
        db_session,
        "zztestpart66",
        "ZZTESTPART-66",
        enrichment_status="verified",
        enriched_at=datetime.now(UTC),
    )
    db_session.commit()

    resp = client.post("/api/materials/add", data={"mpn": "ZZTESTPART-66"})
    assert resp.status_code == 200
    db_session.refresh(existing)
    assert existing.enrich_requested_at is None


def test_readd_with_category_clears_conflict(client, db_session: Session):
    """Re-adding an existing conflicted card with a category is a manual re-assertion —
    it clears the category conflict exactly like both PUT paths."""
    from app.services.spec_tiers import set_category

    card = _make_card(db_session, "zztestpart55", "ZZTESTPART-55")
    set_category(card, "dram", "manual", 1.0)
    set_category(card, "hdd", "mpn_decode", 0.95)  # loses, records the conflict
    db_session.commit()
    assert card.has_validation_conflict

    resp = client.post("/api/materials/add", data={"mpn": "ZZTESTPART-55", "category": "dram"})
    assert resp.status_code == 200
    db_session.refresh(card)
    assert card.category == "dram"
    assert not card.has_validation_conflict
    assert (card.validation_conflicts or []) == []


def test_readd_with_manufacturer_clears_conflict(client, db_session: Session):
    """Re-adding an existing conflicted card with a manufacturer is a manual re-
    assertion — same clearing contract as category (the hook lives in
    _set_provenanced_column, so manufacturer carries the full conflict contract)."""
    from app.services.spec_tiers import set_manufacturer

    card = _make_card(db_session, "zztestpart56", "ZZTESTPART-56")
    set_manufacturer(card, "Seagate Technology", "manual", 1.0)
    set_manufacturer(card, "Samsung", "mpn_decode", 0.95)  # loses, records the conflict
    db_session.commit()
    assert card.has_validation_conflict

    resp = client.post(
        "/api/materials/add",
        data={"mpn": "ZZTESTPART-56", "manufacturer": "Seagate Technology"},
    )
    assert resp.status_code == 200
    db_session.refresh(card)
    assert card.manufacturer == "Seagate Technology"
    assert card.manufacturer_source == "manual"
    assert card.manufacturer_tier == 100
    assert not card.has_validation_conflict
    assert (card.validation_conflicts or []) == []


def test_put_rejects_offvocab_category_422(client, db_session: Session):
    """The JSON API surfaces an off-vocab category rejection — a 200 with the edit
    silently dropped is indistinguishable from acceptance."""
    from app.services.spec_tiers import set_category

    card = _make_card(db_session, "zztestpart44", "ZZTESTPART-44")
    set_category(card, "dram", "manual", 1.0)
    db_session.commit()

    resp = client.put(f"/api/materials/{card.id}", json={"category": "FLUX_CAPACITOR"})
    assert resp.status_code == 422
    assert "not a recognized commodity" in resp.json()["error"]
    db_session.refresh(card)
    assert card.category == "dram"  # untouched

    # Blanking is rejected too — the ladder never clears a category.
    resp = client.put(f"/api/materials/{card.id}", json={"category": "  "})
    assert resp.status_code == 422
    assert "can't be cleared" in resp.json()["error"]
    db_session.refresh(card)
    assert card.category == "dram"


def test_workspace_blocks_non_interactive_agent(db_session: Session):
    """The materials workspace is module-gated via require_access(MATERIALS).

    The
    non-interactive agent service account has no module access by design
    (ROLE_ACCESS_DEFAULTS[AGENT] is empty — least privilege), so it is blocked (403)
    before it can ever reach the buyer-gated Add-part action. This is a strictly
    stronger guarantee than hiding the button: the agent never renders the page.
    """
    from fastapi.testclient import TestClient

    from app.database import get_db
    from app.dependencies import require_user
    from app.main import app
    from app.models import User

    agent = User(
        email="agent-onadd@trioscs.com",
        name="Agent",
        role="agent",
        azure_id="test-azure-agent-onadd",
        created_at=datetime.now(UTC),
    )
    db_session.add(agent)
    db_session.commit()

    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[require_user] = lambda: agent
    try:
        with TestClient(app) as c:
            resp = c.get("/v2/partials/materials/workspace")
            assert resp.status_code == 403
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(require_user, None)


def test_workspace_shows_add_part_for_buyer(client, db_session: Session):
    resp = client.get("/v2/partials/materials/workspace")
    assert resp.status_code == 200
    assert "Add part" in resp.text


def test_has_buyer_role_matches_require_buyer_set():
    """has_buyer_role is the single source of truth for buyer-gated UI — it must track
    require_buyer's allowed set exactly (agent stays excluded by design)."""
    from types import SimpleNamespace

    from app.constants import UserRole
    from app.dependencies import has_buyer_role

    allowed = {UserRole.BUYER, UserRole.SALES, UserRole.TRADER, UserRole.MANAGER, UserRole.ADMIN}
    for role in UserRole:
        assert has_buyer_role(SimpleNamespace(role=role)) is (role in allowed)
    assert has_buyer_role(None) is False


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
    # V3 rejection surfaced per-row, never silent. `row` is the 1-based SOURCE-file
    # row (header = 1, DRAM_MPN = 2, "AB" = 3) — the line the user opens and fixes.
    (warning,) = body["warnings"]
    assert warning["row"] == 3
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
    card = _make_card(db_session, "poll-001", "POLL-001", enrichment_status="unenriched")
    db_session.commit()

    resp = client.get(f"/v2/partials/materials/{card.id}/enrich-status")
    assert resp.status_code == 200
    assert "Queued for enrichment" in resp.text
    assert "every 15s" in resp.text  # badge keeps polling itself


def test_enrich_status_terminal_returns_286(client, db_session: Session):
    card = _make_card(
        db_session,
        "poll-002",
        "POLL-002",
        enrichment_status="verified",
        enriched_at=datetime.now(UTC),
    )
    db_session.commit()

    resp = client.get(f"/v2/partials/materials/{card.id}/enrich-status")
    assert resp.status_code == 286  # htmx stop-polling status
    assert "VERIFIED" in resp.text
    assert "every 15s" not in resp.text


def test_enrich_status_missing_card_stops_polling(client, db_session: Session):
    """A deleted/missing card answers 286 (htmx stop-polling), NOT 404 — htmx neither
    swaps nor cancels an `every 15s` poll on a 4xx, so a 404 would poll forever."""
    resp = client.get("/v2/partials/materials/999999/enrich-status")
    assert resp.status_code == 286
    assert resp.text == ""


# --- Needs-review faceted filter -------------------------------------------------


def test_needs_review_filter_narrows(client, db_session: Session):
    _make_card(
        db_session,
        "review-001",
        "REVIEW-001",
        has_validation_conflict=True,
        validation_conflicts=[_category_conflict()],
    )
    _make_card(db_session, "clean-001", "CLEAN-001")
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
    card = _make_card(
        db_session,
        "review-002",
        "REVIEW-002",
        has_validation_conflict=True,
        validation_conflicts=[_category_conflict()],
    )
    db_session.commit()

    resp = client.get(f"/v2/partials/materials/{card.id}")
    assert resp.status_code == 200
    assert "Needs review" not in resp.text
    assert f"/v2/partials/materials/{card.id}/conflicts/category/accept" in resp.text
    assert "Use this value" in resp.text


def test_needs_review_badge_not_in_detail(client, db_session: Session):
    """Confirm the 'Needs review' badge is not rendered even when
    has_validation_conflict=True."""
    card = _make_card(
        db_session,
        "review-003",
        "REVIEW-003",
        has_validation_conflict=True,
        validation_conflicts=[_category_conflict()],
    )
    db_session.commit()

    resp = client.get(f"/v2/partials/materials/{card.id}")
    assert resp.status_code == 200
    # Badge removed — conflict detail panel (Specs section) still renders
    assert "Needs review" not in resp.text
    assert "Use this value" in resp.text
