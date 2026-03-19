"""Tests for vendor spec enrichment service.

What: Tests parsing of structured specs from vendor API raw_data.
Called by: pytest
Depends on: vendor_spec_enrichment, spec_write_service, conftest fixtures
"""

from unittest.mock import MagicMock, patch

from app.services.vendor_spec_enrichment import (
    _extract_numeric,
    enrich_card_from_sightings,
    parse_digikey_specs,
    parse_mouser_specs,
    parse_nexar_specs,
)

# ── _extract_numeric ──


class TestExtractNumeric:
    def test_simple_integer(self):
        assert _extract_numeric("100") == (100.0, None)

    def test_with_unit(self):
        num, unit = _extract_numeric("100µF")
        assert num == 100.0
        assert unit == "µF"

    def test_decimal(self):
        num, unit = _extract_numeric("3.3V")
        assert num == 3.3
        assert unit == "V"

    def test_no_value(self):
        assert _extract_numeric("") == (None, None)
        assert _extract_numeric(None) == (None, None)

    def test_non_numeric(self):
        assert _extract_numeric("DDR5") == (None, None)

    def test_with_spaces(self):
        num, unit = _extract_numeric("  25 V  ")
        assert num == 25.0

    def test_percentage(self):
        num, unit = _extract_numeric("10%")
        assert num == 10.0
        assert unit == "%"

    def test_scientific_notation(self):
        num, unit = _extract_numeric("1e6")
        assert num == 1e6


# ── parse_digikey_specs ──


class TestParseDigikeySpecs:
    def test_capacitor_specs(self):
        raw = {
            "parameters": [
                {"parameter": "Capacitance", "value": "100µF"},
                {"parameter": "Voltage - Rated", "value": "25V"},
                {"parameter": "Temperature Coefficient", "value": "X7R"},
                {"parameter": "Package / Case", "value": "0805"},
            ]
        }
        result = parse_digikey_specs(raw, "capacitors")
        assert "capacitance" in result
        assert result["capacitance"]["value"] == 100.0
        assert result["capacitance"]["confidence"] == 0.95
        assert "voltage_rating" in result
        assert result["voltage_rating"]["value"] == 25.0
        assert result["dielectric"]["value"] == "X7R"
        # "0805" gets parsed as numeric 805.0; record_spec handles enum validation
        assert result["package"]["value"] == 805.0

    def test_dram_specs(self):
        raw = {
            "parameters": [
                {"parameter": "Memory Type", "value": "DDR5"},
                {"parameter": "Memory Size", "value": "16GB"},
                {"parameter": "Speed", "value": "4800MT/s"},
                {"parameter": "Module Type", "value": "DIMM"},
            ]
        }
        result = parse_digikey_specs(raw, "dram")
        assert result["ddr_type"]["value"] == "DDR5"
        assert result["capacity_gb"]["value"] == 16.0
        assert result["form_factor"]["value"] == "DIMM"

    def test_empty_parameters(self):
        assert parse_digikey_specs({"parameters": []}, "capacitors") == {}

    def test_no_parameters_key(self):
        assert parse_digikey_specs({}, "capacitors") == {}

    def test_unknown_category(self):
        raw = {"parameters": [{"parameter": "Capacitance", "value": "100µF"}]}
        assert parse_digikey_specs(raw, "unknown_category") == {}

    def test_none_raw_data(self):
        assert parse_digikey_specs(None, "capacitors") == {}


# ── parse_nexar_specs ──


class TestParseNexarSpecs:
    def test_capacitor_specs(self):
        raw = {
            "specs": [
                {"attribute": {"name": "Capacitance"}, "displayValue": "100pF"},
                {"attribute": {"name": "Voltage Rating"}, "displayValue": "50V"},
            ]
        }
        result = parse_nexar_specs(raw, "capacitors")
        assert "capacitance" in result
        assert "voltage_rating" in result

    def test_empty_specs(self):
        assert parse_nexar_specs({"specs": []}, "capacitors") == {}

    def test_no_specs_key(self):
        assert parse_nexar_specs({}, "capacitors") == {}


# ── parse_mouser_specs ──


class TestParseMouserSpecs:
    def test_capacitor_specs(self):
        raw = {
            "ProductAttributes": [
                {"AttributeName": "Capacitance", "AttributeValue": "10nF"},
                {"AttributeName": "Voltage Rated", "AttributeValue": "16V"},
            ]
        }
        result = parse_mouser_specs(raw, "capacitors")
        assert "capacitance" in result
        assert result["capacitance"]["value"] == 10.0
        assert "voltage_rating" in result

    def test_empty_attributes(self):
        assert parse_mouser_specs({"ProductAttributes": []}, "capacitors") == {}

    def test_no_attributes_key(self):
        assert parse_mouser_specs({}, "capacitors") == {}


# ── enrich_card_from_sightings ──


class TestEnrichCardFromSightings:
    def test_enriches_with_digikey_sighting(self, db_session):
        from app.models.intelligence import MaterialCard

        card = MaterialCard(
            normalized_mpn="test-cap-001",
            display_mpn="TEST-CAP-001",
            manufacturer="TDK",
            category="capacitors",
        )
        db_session.add(card)
        db_session.commit()

        # Seed the commodity schemas so record_spec has something to match
        from app.services.commodity_registry import seed_commodity_schemas

        seed_commodity_schemas(db_session)

        mock_sighting = MagicMock()
        mock_sighting.source_type = "digikey"
        mock_sighting.raw_data = {
            "parameters": [
                {"parameter": "Capacitance", "value": "100nF"},
                {"parameter": "Voltage - Rated", "value": "50V"},
            ]
        }

        # Patch the Sighting query inside enrich_card_from_sightings
        with patch(
            "app.services.vendor_spec_enrichment.Sighting",
        ) as MockSighting:
            mock_q = MagicMock()
            mock_q.filter.return_value = mock_q
            mock_q.all.return_value = [mock_sighting]
            # Make db.query(Sighting) return our mock chain
            original_query = db_session.query

            def patched_query(model, *args, **kwargs):
                if model is MockSighting:
                    return mock_q
                return original_query(model, *args, **kwargs)

            with patch.object(db_session, "query", side_effect=patched_query):
                count = enrich_card_from_sightings(db_session, card.id)

        assert count >= 1

    def test_returns_zero_for_missing_card(self, db_session):
        count = enrich_card_from_sightings(db_session, 999999)
        assert count == 0

    def test_returns_zero_for_card_without_category(self, db_session):
        from app.models.intelligence import MaterialCard

        card = MaterialCard(
            normalized_mpn="test-no-cat",
            display_mpn="TEST-NO-CAT",
            category=None,
        )
        db_session.add(card)
        db_session.commit()

        count = enrich_card_from_sightings(db_session, card.id)
        assert count == 0
