"""Tests for the verified-material-enrichment feature.

Covers: enrichment_status / enrichment_provenance model columns (Task 1),
and (future tasks) authoritative-enrichment service logic.

Called by: pytest
Depends on: app/models/intelligence.py, tests/conftest.py (db_session fixture)
"""

from datetime import datetime, timezone

from app.models import MaterialCard


def test_new_card_defaults_to_unenriched(db_session):
    card = MaterialCard(
        normalized_mpn="teststatusdefault",
        display_mpn="TEST-STATUS-DEFAULT",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(card)
    db_session.flush()
    db_session.refresh(card)
    assert card.enrichment_status == "unenriched"
    assert card.enrichment_provenance is None


from app.services.authoritative_enrichment_service import (
    merge_authoritative,
)


def _hit(source, mpn="LM317T", **over):
    base = {
        "source_type": source,
        "mpn_matched": mpn,
        "manufacturer": "TI",
        "description": f"desc from {source}",
        "category": None,
        "lifecycle_status": None,
        "package_type": None,
        "pin_count": None,
        "rohs_status": None,
        "datasheet_url": None,
    }
    base.update(over)
    return base


def test_exact_match_guard_rejects_mismatch():
    # connector returned a DIFFERENT part — must be ignored
    results = {"digikey": [_hit("digikey", mpn="LM317MT")]}
    merged, prov, contributors = merge_authoritative("lm317t", results)
    assert merged == {}
    assert contributors == []


def test_first_non_null_by_priority():
    results = {
        "mouser": [_hit("mouser", description="mouser desc", category="Linear")],
        "digikey": [_hit("digikey", description="digikey desc", lifecycle_status="active")],
    }
    merged, prov, contributors = merge_authoritative("lm317t", results)
    # digikey has higher priority -> its description wins
    assert merged["description"] == "digikey desc"
    assert prov["description"]["source"] == "digikey"
    # category only present from mouser -> taken from mouser
    assert merged["category"] == "Linear"
    assert prov["category"]["source"] == "mouser"
    assert merged["lifecycle_status"] == "active"
    assert "digikey" in contributors and "mouser" in contributors


from unittest.mock import AsyncMock, patch

from app.services.authoritative_enrichment_service import enrich_card


def _card(db_session, mpn="LM317T"):
    from app.utils.normalization import normalize_mpn_key

    c = MaterialCard(
        normalized_mpn=normalize_mpn_key(mpn),
        display_mpn=mpn,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(c)
    db_session.flush()
    return c


class _FakeConn:
    def __init__(self, source_name, hits):
        self.source_name = source_name
        self._hits = hits

    async def search(self, pn):
        return self._hits


@patch("app.services.authoritative_enrichment_service._connectors_in_order")
def test_enrich_card_verified(mock_conns, db_session):
    card = _card(db_session)
    mock_conns.return_value = [
        _FakeConn(
            "digikey",
            [
                {
                    "source_type": "digikey",
                    "mpn_matched": "LM317T",
                    "manufacturer": "TI",
                    "description": "Adjustable regulator",
                    "category": "Voltage Regulator",
                    "lifecycle_status": "active",
                }
            ],
        )
    ]
    import asyncio

    asyncio.run(enrich_card(card, db_session))
    assert card.enrichment_status == "verified"
    assert card.manufacturer == "TI"
    assert card.enrichment_provenance["description"]["source"] == "digikey"


@patch("app.services.ai_inference_fallback.claude_structured", new_callable=AsyncMock)
@patch("app.services.authoritative_enrichment_service._connectors_in_order")
def test_enrich_card_ai_inferred_when_no_authoritative(mock_conns, mock_claude, db_session):
    card = _card(db_session, "04M3HJ")
    mock_conns.return_value = [_FakeConn("digikey", [])]  # no hits anywhere
    mock_claude.return_value = {"description": "Dell laptop bezel", "category": "Mechanical", "confidence": 0.97}
    import asyncio

    asyncio.run(enrich_card(card, db_session))
    assert card.enrichment_status == "ai_inferred"
    assert card.description == "Dell laptop bezel"
    assert card.lifecycle_status is None  # never guessed
    assert card.enrichment_provenance["reconfirm_needed"] is True  # flagged for reconfirmation


@patch("app.services.ai_inference_fallback.claude_structured", new_callable=AsyncMock)
@patch("app.services.authoritative_enrichment_service._connectors_in_order")
def test_enrich_card_below_95_confidence_is_not_found(mock_conns, mock_claude, db_session):
    card = _card(db_session, "04M3HJ")
    mock_conns.return_value = [_FakeConn("digikey", [])]
    mock_claude.return_value = {"description": "maybe a bezel", "category": "Mechanical", "confidence": 0.8}
    import asyncio

    asyncio.run(enrich_card(card, db_session))
    assert card.enrichment_status == "not_found"
    assert card.description is None


@patch("app.services.ai_inference_fallback.claude_structured", new_callable=AsyncMock)
@patch("app.services.authoritative_enrichment_service._connectors_in_order")
def test_enrich_card_not_found(mock_conns, mock_claude, db_session):
    card = _card(db_session, "ZZ9PLURAL")
    mock_conns.return_value = [_FakeConn("digikey", [])]
    mock_claude.return_value = {"description": "", "category": "", "confidence": 0.0}
    import asyncio

    asyncio.run(enrich_card(card, db_session))
    assert card.enrichment_status == "not_found"
    assert card.description is None


def test_quota_error_disables_source_for_run():
    """A ConnectorQuotaError disables that source for the rest of the run."""
    import asyncio

    from app.connectors.errors import ConnectorQuotaError
    from app.services.authoritative_enrichment_service import fetch_authoritative

    calls = {"n": 0}

    class _QuotaConn:
        source_name = "digikey"

        async def search(self, pn):
            calls["n"] += 1
            raise ConnectorQuotaError("quota exceeded")

    disabled: set[str] = set()
    conn = _QuotaConn()
    asyncio.run(fetch_authoritative("X1", "x1", [conn], disabled))
    assert "digikey" in disabled
    assert calls["n"] == 1
    # Second MPN: source already disabled -> not retried.
    asyncio.run(fetch_authoritative("X2", "x2", [conn], disabled))
    assert calls["n"] == 1


# ── Additional coverage tests ────────────────────────────────────────────────

import asyncio

from app.connectors.errors import ConnectorAuthError
from app.services.authoritative_enrichment_service import (
    apply_authoritative,
    enrich_cards,
    fetch_authoritative,
)

# ── fetch_authoritative edge cases ───────────────────────────────────────────


def test_fetch_auth_error_disables_source():
    """ConnectorAuthError must also disable the source for the rest of the run."""

    class _AuthErrConn:
        source_name = "mouser"

        async def search(self, pn):
            raise ConnectorAuthError("bad credentials")

    disabled: set[str] = set()
    asyncio.run(fetch_authoritative("LM317T", "lm317t", [_AuthErrConn()], disabled))
    assert "mouser" in disabled


def test_fetch_transient_exception_returns_empty_result():
    """An unexpected exception must not propagate — source gets an empty result."""

    class _TransientConn:
        source_name = "element14"

        async def search(self, pn):
            raise RuntimeError("transient network blip")

    disabled: set[str] = set()
    results = asyncio.run(fetch_authoritative("LM317T", "lm317t", [_TransientConn()], disabled))
    # Source returned empty list, NOT removed from results (key present)
    assert results.get("element14") == []
    # Transient errors must NOT disable the source
    assert "element14" not in disabled


def test_fetch_nexar_skipped_when_adequate():
    """Nexar connector is skipped once description/manufacturer/category are present."""
    nexar_calls = {"n": 0}

    class _DigiKeyConn:
        source_name = "digikey"

        async def search(self, pn):
            return [
                {
                    "source_type": "digikey",
                    "mpn_matched": "LM317T",
                    "manufacturer": "TI",
                    "description": "Adjustable regulator",
                    "category": "Voltage Regulator",
                }
            ]

    class _NexarConn:
        source_name = "nexar"

        async def search(self, pn):
            nexar_calls["n"] += 1
            return []

    results = asyncio.run(fetch_authoritative("LM317T", "lm317t", [_DigiKeyConn(), _NexarConn()], set()))
    # DigiKey filled all _ADEQUATE fields -> nexar must be skipped
    assert nexar_calls["n"] == 0
    assert "digikey" in results


def test_fetch_nexar_called_when_not_adequate():
    """Nexar is queried when adequate fields are missing from earlier sources."""
    nexar_calls = {"n": 0}

    class _DigiKeyConn:
        source_name = "digikey"

        async def search(self, pn):
            # Only returns manufacturer — not all _ADEQUATE fields
            return [
                {
                    "source_type": "digikey",
                    "mpn_matched": "LM317T",
                    "manufacturer": "TI",
                    "description": None,
                    "category": None,
                }
            ]

    class _NexarConn:
        source_name = "nexar"

        async def search(self, pn):
            nexar_calls["n"] += 1
            return []

    asyncio.run(fetch_authoritative("LM317T", "lm317t", [_DigiKeyConn(), _NexarConn()], set()))
    assert nexar_calls["n"] == 1


# ── apply_authoritative ──────────────────────────────────────────────────────


def _prov_entry(source: str, confidence: float = 1.0) -> dict:
    return {"source": source, "confidence": confidence, "fetched_at": "2024-01-01"}


def test_apply_authoritative_writes_all_fields(db_session):
    """All merged fields, provenance, source and status are written to the card.

    category/manufacturer route through the F1 ladder at the connector's registered
    ``{name}_api`` source (tier 90) — provenance columns are stamped, not raw-set.
    """
    card = _card(db_session, "ST4000NM0035")
    merged = {
        "description": "4TB 7.2K SAS Enterprise HDD",
        "manufacturer": "Seagate Technology",
        "category": "hdd",
        "lifecycle_status": "active",
    }
    provenance = {
        "description": _prov_entry("digikey"),
        "manufacturer": _prov_entry("digikey"),
        "category": _prov_entry("digikey"),
        "lifecycle_status": _prov_entry("digikey"),
    }
    apply_authoritative(card, merged, provenance, ["digikey"])
    assert card.description == "4TB 7.2K SAS Enterprise HDD"
    assert card.manufacturer == "Seagate Technology"
    assert card.category == "hdd"
    assert card.lifecycle_status == "active"
    assert card.enrichment_status == "verified"
    assert card.enrichment_source == "digikey"
    assert card.enrichment_provenance == provenance
    assert card.enriched_at is not None
    # F1-ladder provenance stamped on both provenanced columns (vendor APIs = tier 90).
    assert card.category_source == "digikey_api"
    assert card.category_tier == 90
    assert card.category_confidence == 1.0
    assert card.manufacturer_source == "digikey_api"
    assert card.manufacturer_tier == 90


def test_apply_authoritative_category_displaces_decode_85(db_session):
    """Tier-90 vendor category evidence displaces a decode-85 category (the ladder
    decides the overwrite, not the writer)."""
    card = _card(db_session, "ST4000NM0035")
    card.category = "dram"
    card.category_source = "mpn_decode"
    card.category_confidence = 0.95
    card.category_tier = 85
    card.category_updated_at = datetime.now(timezone.utc)
    db_session.flush()

    apply_authoritative(
        card,
        {"category": "hdd"},
        {"category": _prov_entry("mouser")},
        ["mouser"],
    )
    assert card.category == "hdd"
    assert card.category_source == "mouser_api"
    assert card.category_tier == 90
    assert "category" in card.enrichment_provenance


def test_apply_authoritative_category_loses_to_trio_and_manual(db_session):
    """Tier-90 vendor category never overwrites trio_source (95) or manual (100); the
    rejected write's provenance entry is dropped so enrichment_provenance never claims a
    write that didn't land."""
    for source, tier in (("trio_source", 95), ("manual", 100)):
        card = _card(db_session, f"PIN-{source}")
        card.category = "dram"
        card.category_source = source
        card.category_confidence = 1.0
        card.category_tier = tier
        card.category_updated_at = datetime.now(timezone.utc)
        db_session.flush()

        apply_authoritative(
            card,
            {"category": "hdd", "description": "desc"},
            {"category": _prov_entry("digikey"), "description": _prov_entry("digikey")},
            ["digikey"],
        )
        assert card.category == "dram", source
        assert card.category_source == source
        assert "category" not in card.enrichment_provenance
        assert card.enrichment_provenance["description"]["source"] == "digikey"


def test_apply_authoritative_off_vocab_category_rejected(db_session):
    """An off-vocab connector category is rejected by the ladder's normalizer — never
    persisted as junk, and dropped from the persisted provenance."""
    card = _card(db_session, "LM317T")
    apply_authoritative(
        card,
        {"category": "Analog ICs", "description": "Adjustable Voltage Regulator"},
        {"category": _prov_entry("digikey"), "description": _prov_entry("digikey")},
        ["digikey"],
    )
    assert card.category is None
    assert "category" not in card.enrichment_provenance
    assert card.description == "Adjustable Voltage Regulator"
    assert card.enrichment_status == "verified"


def test_apply_authoritative_displaces_legacy_unprovenanced_value(db_session):
    """The dominant real-world population: a pre-ladder card with valued category/
    manufacturer and NULL provenance columns ranks at the legacy_backfill floor (50) —
    tier-90 connector evidence must still displace it (the stale-data fix this routing
    exists for), stamping ``{connector}_api``/90 provenance."""
    card = _card(db_session, "ST4000NM0035-LEG")
    card.category = "dram"  # stale legacy value — NULL category_* provenance
    card.manufacturer = "Segate"  # typo'd legacy maker — NULL manufacturer_* provenance
    db_session.flush()

    apply_authoritative(
        card,
        {"category": "hdd", "manufacturer": "Seagate Technology"},
        {"category": _prov_entry("digikey"), "manufacturer": _prov_entry("digikey")},
        ["digikey"],
    )
    assert card.category == "hdd"
    assert card.category_source == "digikey_api"
    assert card.category_tier == 90
    assert card.manufacturer == "Seagate Technology"
    assert card.manufacturer_source == "digikey_api"
    assert card.manufacturer_tier == 90
    assert card.enrichment_provenance["category"]["source"] == "digikey"
    assert card.enrichment_provenance["manufacturer"]["source"] == "digikey"


def test_apply_authoritative_manufacturer_loses_to_manual_records_conflict(db_session):
    """A vendor maker (90) reporting a DIFFERENT value than a manual maker (100) loses
    arbitration AND records a validation conflict for human review (tier >= 80 band)."""
    card = _card(db_session, "ST8000NM0055")
    card.manufacturer = "Western Digital"
    card.manufacturer_source = "manual"
    card.manufacturer_confidence = 1.0
    card.manufacturer_tier = 100
    card.manufacturer_updated_at = datetime.now(timezone.utc)
    db_session.flush()

    apply_authoritative(
        card,
        {"manufacturer": "Seagate Technology"},
        {"manufacturer": _prov_entry("digikey")},
        ["digikey"],
    )
    assert card.manufacturer == "Western Digital"  # manual kept
    assert "manufacturer" not in card.enrichment_provenance
    assert card.has_validation_conflict is True
    assert any(
        c.get("key") == "manufacturer" and (c.get("evidence") or {}).get("source") == "digikey_api"
        for c in card.validation_conflicts
    )


def test_apply_authoritative_empty_contributors_preserves_source(db_session):
    """When contributors is empty, enrichment_source stays unchanged."""
    card = _card(db_session, "LM317T")
    card.enrichment_source = "previous_source"
    apply_authoritative(card, {"description": "desc"}, {}, [])
    assert card.enrichment_source == "previous_source"
    assert card.enrichment_status == "verified"


# ── _connectors_in_order ─────────────────────────────────────────────────────


def test_connectors_in_order_filters_and_aliases(db_session):
    """_connectors_in_order returns connectors ordered by SOURCE_ORDER and applies
    aliases."""
    from app.services.authoritative_enrichment_service import SOURCE_ORDER, _connectors_in_order

    class _FakeConnector:
        def __init__(self, name):
            self.source_name = name

    # Provide connectors out of order, with octopart alias and an unknown source
    fake_conns = [
        _FakeConnector("mouser"),
        _FakeConnector("octopart"),  # alias -> nexar
        _FakeConnector("digikey"),
        _FakeConnector("unknown_source"),  # not in SOURCE_ORDER -> excluded
    ]

    with patch("app.search_service._build_connectors", return_value=(fake_conns, {}, {})):
        ordered = _connectors_in_order(db_session)

    names = [c.source_name for c in ordered]
    # Must follow SOURCE_ORDER; octopart aliased to nexar; unknown excluded
    for name in names:
        assert name in SOURCE_ORDER or name == "octopart"
    # digikey before mouser before nexar (octopart)
    positions = {c.source_name: i for i, c in enumerate(ordered)}
    assert positions["digikey"] < positions["mouser"]
    assert "octopart" in positions  # nexar slot, original source_name preserved


# ── enrich_cards (batch) ─────────────────────────────────────────────────────


@patch("app.services.authoritative_enrichment_service._connectors_in_order")
def test_enrich_cards_returns_counts(mock_conns, db_session):
    """enrich_cards commits per batch and returns status counts."""
    card1 = _card(db_session, "LM317T")
    card2 = _card(db_session, "NE555")
    db_session.flush()

    mock_conns.return_value = [
        _FakeConn(
            "digikey",
            [
                {
                    "source_type": "digikey",
                    "mpn_matched": "LM317T",
                    "manufacturer": "TI",
                    "description": "Regulator",
                    "category": "Analog",
                }
            ],
        )
    ]

    with patch("app.services.ai_inference_fallback.claude_structured", new_callable=AsyncMock) as mock_claude:
        mock_claude.return_value = {"description": "", "category": "", "confidence": 0.0}
        counts = asyncio.run(enrich_cards([card1.id, card2.id], db_session))

    assert counts["verified"] >= 1
    assert "verified" in counts or "not_found" in counts


@patch("app.services.authoritative_enrichment_service._connectors_in_order")
def test_enrich_cards_skips_missing_card(mock_conns, db_session):
    """enrich_cards silently skips card IDs that no longer exist."""
    mock_conns.return_value = []

    counts = asyncio.run(enrich_cards([999999], db_session))
    # Nothing to count — all statuses zero or absent
    assert counts.get("verified", 0) == 0
    assert counts.get("ai_inferred", 0) == 0
    assert counts.get("not_found", 0) == 0


@patch("app.services.authoritative_enrichment_service._connectors_in_order")
def test_enrich_cards_disabled_sources_logged(mock_conns, db_session):
    """enrich_cards records disabled_sources in counts when quota/auth errors occur."""
    card = _card(db_session, "XYZ999")
    db_session.flush()

    class _QuotaConn:
        source_name = "digikey"

        async def search(self, pn):
            from app.connectors.errors import ConnectorQuotaError

            raise ConnectorQuotaError("quota exceeded")

    mock_conns.return_value = [_QuotaConn()]

    with patch("app.services.ai_inference_fallback.claude_structured", new_callable=AsyncMock) as mock_claude:
        mock_claude.return_value = {"description": "", "category": "", "confidence": 0.0}
        counts = asyncio.run(enrich_cards([card.id], db_session))

    assert "disabled_sources" in counts
    assert "digikey" in counts["disabled_sources"]


@patch("app.services.authoritative_enrichment_service._connectors_in_order")
def test_enrich_cards_already_verified_skipped(mock_conns, db_session):
    """Cards already verified are not re-enriched unless refresh=True."""
    card = _card(db_session, "LM317T")
    card.enrichment_status = "verified"
    db_session.flush()

    # Connector should not be called since card is already verified
    call_count = {"n": 0}

    class _TrackingConn:
        source_name = "digikey"

        async def search(self, pn):
            call_count["n"] += 1
            return []

    mock_conns.return_value = [_TrackingConn()]

    counts = asyncio.run(enrich_cards([card.id], db_session, refresh=False))
    assert call_count["n"] == 0
    assert counts.get("verified", 0) == 1


from unittest.mock import AsyncMock, patch

import pytest

from app.constants import MaterialEnrichmentStatus
from app.services import authoritative_enrichment_service as aes
from app.services.enrichment_types import WebMeter
from app.services.enrichment_worker.oem_extractor import CrossRefResult, OemExtractResult
from app.utils.claude_errors import ClaudeError


def _oem_card(mpn="01HW917"):
    return MaterialCard(display_mpn=mpn, normalized_mpn=mpn.lower().replace("-", ""))


@pytest.mark.asyncio
async def test_crossref_double_verify_to_verified(db_session):
    card = _oem_card()
    xr = CrossRefResult(
        status="resolved",
        resolved_mpn="M393A2K40EB3-CWE",
        manufacturer="Samsung",
        linkage_source_url="https://support.lenovo.com/x",
        linkage_source_domain="support.lenovo.com",
        confidence=0.95,
    )

    # No distributor hit for the FRU; distributor DOES confirm the resolved MPN.
    async def fake_fetch(display, norm, conns, disabled, cooldown):
        if norm == "m393a2k40eb3cwe":
            return {
                "mouser": [
                    {"mpn_matched": "M393A2K40EB3-CWE", "description": "16GB DDR4 RDIMM", "manufacturer": "Samsung"}
                ]
            }
        return {}

    meter = WebMeter()
    with (
        # Force the distributor web tier to run for this OEM MPN so both web-search
        # tiers (web + cross-ref) bill — the §1.3 OEM-web-skip is exercised on its own
        # in test_worker_lane_split.py; here we pin the cross-ref double-verify contract.
        patch("app.config.settings.enrichment_skip_web_for_oem_mpns", False),
        patch.object(aes, "classify_oem_vendor", return_value="lenovo"),
        patch.object(aes, "extract_part_from_web", new=AsyncMock(return_value=type("W", (), {"status": "failed"})())),
        patch.object(aes, "cross_reference_mpn", new=AsyncMock(return_value=xr)),
        patch.object(aes, "fetch_authoritative", new=AsyncMock(side_effect=fake_fetch)),
    ):
        status = await aes.enrich_card(card, db_session, connectors=[], web_meter=meter)

    assert status == MaterialEnrichmentStatus.VERIFIED
    assert card.description == "16GB DDR4 RDIMM"
    assert any(x.get("mpn") == "M393A2K40EB3-CWE" for x in (card.cross_references or []))
    assert card.enrichment_provenance["cross_ref"]["resolved_mpn"] == "M393A2K40EB3-CWE"
    # The resolved MPN's distributor manufacturer routes through the F1 ladder at the
    # confirming connector's registered source (mouser_api, tier 90) — same contract
    # as apply_authoritative.
    assert card.manufacturer == "Samsung"
    assert card.manufacturer_source == "mouser_api"
    assert card.manufacturer_tier == 90
    assert meter.claude_ok is True and meter.web_calls >= 2


@pytest.mark.asyncio
async def test_crossref_unconfirmed_mpn_falls_through(db_session):
    card = _oem_card()
    xr = CrossRefResult(
        status="resolved", resolved_mpn="BOGUS-NOPART", confidence=0.95, linkage_source_domain="support.lenovo.com"
    )
    with (
        patch.object(aes, "classify_oem_vendor", return_value="lenovo"),
        patch.object(aes, "extract_part_from_web", new=AsyncMock(return_value=type("W", (), {"status": "failed"})())),
        patch.object(aes, "cross_reference_mpn", new=AsyncMock(return_value=xr)),
        patch.object(aes, "fetch_authoritative", new=AsyncMock(return_value={})),
        patch.object(aes, "extract_oem_description", new=AsyncMock(return_value=OemExtractResult(status="failed"))),
        patch(
            "app.services.ai_inference_fallback.infer_part",
            new=AsyncMock(return_value=type("I", (), {"status": "not_found"})()),
        ),
    ):
        status = await aes.enrich_card(card, db_session, connectors=[], web_meter=WebMeter())
    # Unconfirmed cross-ref discarded; OEM desc failed; AI declined → not_catalogued (OEM pattern matched).
    assert status == MaterialEnrichmentStatus.NOT_CATALOGUED
    assert card.cross_references in (None, [])


@pytest.mark.asyncio
async def test_oem_description_path(db_session):
    card = _oem_card()
    oem = OemExtractResult(
        status="oem_sourced",
        description="ThinkSystem 16GB RDIMM",
        manufacturer="Lenovo",
        category="Memory Module",  # known distributor/OEM taxonomy alias → dram
        confidence=0.95,
        source_urls=["https://support.lenovo.com/x"],
        source_domains=["support.lenovo.com"],
    )
    with (
        patch.object(aes, "classify_oem_vendor", return_value="lenovo"),
        patch.object(aes, "extract_part_from_web", new=AsyncMock(return_value=type("W", (), {"status": "failed"})())),
        patch.object(aes, "cross_reference_mpn", new=AsyncMock(return_value=CrossRefResult(status="failed"))),
        patch.object(aes, "extract_oem_description", new=AsyncMock(return_value=oem)),
    ):
        status = await aes.enrich_card(card, db_session, connectors=[], web_meter=WebMeter())
    assert status == MaterialEnrichmentStatus.OEM_SOURCED
    assert card.description == "ThinkSystem 16GB RDIMM"
    assert card.enrichment_provenance["oem_sourced"] is True
    # The category WRITE still lands through the integration path: the alias map
    # canonicalizes the OEM-page taxonomy string and the ladder stamps oem_official/80.
    assert card.category == "dram"
    assert card.category_source == "oem_official"
    assert card.enrichment_provenance["category"]["source"] == "oem_official"


# ── apply_oem_sourced ladder matrix (same contract as the web/authoritative suites —
# apply_oem_sourced duplicates the routing block, so those tests do NOT protect it) ──


def test_apply_oem_sourced_lands_with_oem_official_provenance(db_session):
    """Happy path on an empty card: category + manufacturer land through the F1 ladder
    at ``oem_official``/80 with per-field provenance; the other fields raw-write."""
    card = _card(db_session, "01HW917")
    oem = OemExtractResult(
        status="oem_sourced",
        description="ThinkSystem 16GB RDIMM",
        manufacturer="Lenovo",
        category="dram",
        datasheet_url="https://support.lenovo.com/x.pdf",
        confidence=0.95,
        source_urls=["https://support.lenovo.com/x"],
        source_domains=["support.lenovo.com"],
    )
    aes.apply_oem_sourced(card, oem)

    assert card.category == "dram"
    assert card.category_source == "oem_official"
    assert card.category_tier == 80
    assert card.category_confidence == 0.95
    assert card.manufacturer == "Lenovo"
    assert card.manufacturer_source == "oem_official"
    assert card.manufacturer_tier == 80
    assert card.description == "ThinkSystem 16GB RDIMM"
    assert card.datasheet_url == "https://support.lenovo.com/x.pdf"
    assert card.enrichment_status == "oem_sourced"
    assert card.enrichment_source == "oem_official"
    prov = card.enrichment_provenance
    assert prov["oem_sourced"] is True
    assert prov["category"]["source"] == "oem_official"
    assert prov["manufacturer"]["source"] == "oem_official"
    assert prov["description"]["source"] == "oem_official"


def test_apply_oem_sourced_off_vocab_category_rejected(db_session):
    """An off-vocab OEM-page category is rejected by the ladder's normalizer — never
    persisted verbatim (the pre-ladder behavior), and dropped from the per-field
    provenance — while the description still lands and the status stays oem_sourced."""
    card = _card(db_session, "01HW918")
    oem = OemExtractResult(
        status="oem_sourced",
        description="Adjustable voltage regulator module",
        category="Voltage Regulator",  # free text — off-vocab
        confidence=0.95,
        source_urls=["https://support.lenovo.com/y"],
        source_domains=["support.lenovo.com"],
    )
    aes.apply_oem_sourced(card, oem)

    assert card.category is None
    assert "category" not in card.enrichment_provenance
    assert card.description == "Adjustable voltage regulator module"
    assert card.enrichment_status == "oem_sourced"


def test_apply_oem_sourced_category_loses_to_decode_85(db_session):
    """oem_official (80) can never overwrite a decode-85 category — the ladder keeps the
    prior and the rejected write gets NO per-field provenance entry (the persisted
    provenance never claims a write that didn't land)."""
    card = _card(db_session, "01HW919")
    card.category = "hdd"
    card.category_source = "mpn_decode"
    card.category_confidence = 0.95
    card.category_tier = 85
    card.category_updated_at = datetime.now(timezone.utc)
    db_session.flush()

    oem = OemExtractResult(
        status="oem_sourced",
        description="some OEM prose",
        category="dram",
        confidence=0.95,
        source_urls=["https://support.lenovo.com/z"],
        source_domains=["support.lenovo.com"],
    )
    aes.apply_oem_sourced(card, oem)

    assert card.category == "hdd"  # decode kept
    assert card.category_source == "mpn_decode"
    assert "category" not in card.enrichment_provenance
    assert card.enrichment_provenance["description"]["source"] == "oem_official"


@pytest.mark.asyncio
async def test_non_oem_failure_stays_not_found(db_session):
    card = _oem_card("LM2596S")
    with (
        patch.object(aes, "classify_oem_vendor", return_value=None),
        patch.object(aes, "extract_part_from_web", new=AsyncMock(return_value=type("W", (), {"status": "failed"})())),
        patch.object(aes, "fetch_authoritative", new=AsyncMock(return_value={})),
        patch(
            "app.services.ai_inference_fallback.infer_part",
            new=AsyncMock(return_value=type("I", (), {"status": "not_found"})()),
        ),
    ):
        status = await aes.enrich_card(card, db_session, connectors=[], web_meter=WebMeter())
    assert status == MaterialEnrichmentStatus.NOT_FOUND


@pytest.mark.asyncio
async def test_oem_tiers_skipped_when_web_disabled(db_session):
    card = _oem_card()
    xref = AsyncMock()
    with (
        patch.object(aes, "classify_oem_vendor", return_value="lenovo"),
        patch.object(aes, "fetch_authoritative", new=AsyncMock(return_value={})),
        patch.object(aes, "cross_reference_mpn", new=xref),
        patch(
            "app.services.ai_inference_fallback.infer_part",
            new=AsyncMock(return_value=type("I", (), {"status": "not_found"})()),
        ),
    ):
        status = await aes.enrich_card(card, db_session, connectors=[], disabled={"web_search"}, web_meter=WebMeter())
    xref.assert_not_called()  # OEM tiers gated by web budget
    assert status == MaterialEnrichmentStatus.NOT_FOUND  # not_catalogued requires an actual attempt


@pytest.mark.asyncio
# Pin the per-tier web-call billing contract with the §1.3 OEM-web-skip OFF so the
# distributor web tier runs for these OEM MPNs (the skip itself is covered by
# test_worker_lane_split.py). `new=False` means no mock is injected as an argument.
@patch("app.config.settings.enrichment_skip_web_for_oem_mpns", False)
async def test_web_meter_exact_counts_per_tier(db_session):
    """Reserve-before-dispatch billing: each web-search tier attempt is counted exactly once."""

    # 1 call: distributor web tier resolves immediately.
    card = _oem_card()
    with patch.object(
        aes, "extract_part_from_web", new=AsyncMock(return_value=type("W", (), {"status": "web_sourced"})())
    ):
        # apply_web_sourced needs the result fields; stub apply to a no-op write.
        with patch.object(aes, "apply_web_sourced", new=lambda c, w: None):
            meter = WebMeter()
            status = await aes.enrich_card(card, db_session, connectors=[], web_meter=meter)
    assert status == MaterialEnrichmentStatus.WEB_SOURCED
    assert meter.web_calls == 1

    # 2 calls: web fails (1) + cross-ref resolved & distributor-confirmed (1).
    card = _oem_card()
    xr = CrossRefResult(
        status="resolved",
        resolved_mpn="M393A2K40EB3-CWE",
        manufacturer="Samsung",
        linkage_source_url="https://support.lenovo.com/x",
        linkage_source_domain="support.lenovo.com",
        confidence=0.95,
    )

    async def fake_fetch(display, norm, conns, disabled, cooldown):
        if norm == "m393a2k40eb3cwe":
            return {"mouser": [{"mpn_matched": "M393A2K40EB3-CWE", "description": "16GB DDR4 RDIMM"}]}
        return {}

    with (
        patch.object(aes, "classify_oem_vendor", return_value="lenovo"),
        patch.object(aes, "extract_part_from_web", new=AsyncMock(return_value=type("W", (), {"status": "failed"})())),
        patch.object(aes, "cross_reference_mpn", new=AsyncMock(return_value=xr)),
        patch.object(aes, "fetch_authoritative", new=AsyncMock(side_effect=fake_fetch)),
    ):
        meter = WebMeter()
        status = await aes.enrich_card(card, db_session, connectors=[], web_meter=meter)
    assert status == MaterialEnrichmentStatus.VERIFIED
    assert meter.web_calls == 2

    # 3 calls: web fails (1) + cross-ref fails (1) + OEM description sourced (1).
    card = _oem_card()
    oem = OemExtractResult(
        status="oem_sourced",
        description="ThinkSystem 16GB RDIMM",
        manufacturer="Lenovo",
        confidence=0.95,
        source_urls=["https://support.lenovo.com/x"],
        source_domains=["support.lenovo.com"],
    )
    with (
        patch.object(aes, "classify_oem_vendor", return_value="lenovo"),
        patch.object(aes, "extract_part_from_web", new=AsyncMock(return_value=type("W", (), {"status": "failed"})())),
        patch.object(aes, "cross_reference_mpn", new=AsyncMock(return_value=CrossRefResult(status="failed"))),
        patch.object(aes, "extract_oem_description", new=AsyncMock(return_value=oem)),
    ):
        meter = WebMeter()
        status = await aes.enrich_card(card, db_session, connectors=[], web_meter=meter)
    assert status == MaterialEnrichmentStatus.OEM_SOURCED
    assert meter.web_calls == 3

    # 3 calls: web fails (1) + cross-ref fails (1) + OEM description fails (1) + infer declines.
    card = _oem_card()
    with (
        patch.object(aes, "classify_oem_vendor", return_value="lenovo"),
        patch.object(aes, "extract_part_from_web", new=AsyncMock(return_value=type("W", (), {"status": "failed"})())),
        patch.object(aes, "cross_reference_mpn", new=AsyncMock(return_value=CrossRefResult(status="failed"))),
        patch.object(aes, "extract_oem_description", new=AsyncMock(return_value=OemExtractResult(status="failed"))),
        patch(
            "app.services.ai_inference_fallback.infer_part",
            new=AsyncMock(return_value=type("I", (), {"status": "not_found"})()),
        ),
    ):
        meter = WebMeter()
        status = await aes.enrich_card(card, db_session, connectors=[], web_meter=meter)
    assert status == MaterialEnrichmentStatus.NOT_CATALOGUED
    assert meter.web_calls == 3


@pytest.mark.asyncio
async def test_web_meter_counts_billed_call_on_claude_error(db_session):
    """A cross-ref ClaudeError after the distributor-web reserve still counts both
    billed calls."""
    card = _oem_card()
    with (
        # OEM-web-skip OFF so the distributor web tier runs and bills alongside the
        # cross-ref reserve (the skip is covered by test_worker_lane_split.py).
        patch("app.config.settings.enrichment_skip_web_for_oem_mpns", False),
        patch.object(aes, "classify_oem_vendor", return_value="lenovo"),
        patch.object(aes, "extract_part_from_web", new=AsyncMock(return_value=type("W", (), {"status": "failed"})())),
        patch.object(aes, "cross_reference_mpn", new=AsyncMock(side_effect=ClaudeError("backend down"))),
    ):
        meter = WebMeter()
        with pytest.raises(ClaudeError):
            await aes.enrich_card(card, db_session, connectors=[], web_meter=meter)
    # reserve-before-dispatch: distributor web (1) + cross-ref (1, billed then raised) are counted.
    assert meter.web_calls >= 2


@pytest.mark.asyncio
async def test_ai_inferred_sets_claude_ok_without_web_call(db_session):
    """Web disabled + no OEM vendor: infer path latches claude_ok and bills no web
    call."""
    card = _oem_card("LM2596S")
    with (
        patch.object(aes, "classify_oem_vendor", return_value=None),
        patch.object(aes, "fetch_authoritative", new=AsyncMock(return_value={})),
        patch(
            "app.services.ai_inference_fallback.infer_part",
            new=AsyncMock(
                return_value=type(
                    "I",
                    (),
                    {"status": "ai_inferred", "description": "Buck converter", "category": "IC", "confidence": 0.96},
                )()
            ),
        ),
    ):
        meter = WebMeter()
        status = await aes.enrich_card(card, db_session, connectors=[], disabled={"web_search"}, web_meter=meter)
    assert status == MaterialEnrichmentStatus.AI_INFERRED
    assert meter.web_calls == 0 and meter.claude_ok is True


@pytest.mark.asyncio
async def test_dell_miss_is_not_found_not_catalogued(db_session):
    """A Dell (broad 5-char pattern) OEM-tier miss terminates not_found, not
    not_catalogued."""
    card = _oem_card("XYZ12")
    with (
        patch.object(aes, "classify_oem_vendor", return_value="dell"),
        patch.object(aes, "extract_part_from_web", new=AsyncMock(return_value=type("W", (), {"status": "failed"})())),
        patch.object(aes, "cross_reference_mpn", new=AsyncMock(return_value=CrossRefResult(status="failed"))),
        patch.object(aes, "fetch_authoritative", new=AsyncMock(return_value={})),
        patch.object(aes, "extract_oem_description", new=AsyncMock(return_value=OemExtractResult(status="failed"))),
        patch(
            "app.services.ai_inference_fallback.infer_part",
            new=AsyncMock(return_value=type("I", (), {"status": "not_found"})()),
        ),
    ):
        status = await aes.enrich_card(card, db_session, connectors=[], web_meter=WebMeter())
    assert status == MaterialEnrichmentStatus.NOT_FOUND  # dell is excluded from HIGH_PRECISION_VENDORS
